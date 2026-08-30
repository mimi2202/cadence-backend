"""
Hybrid personalization.

The LLM writes exactly one sentence: the opener. Everything else is a Jinja
template you control.

That split is deliberate and it's the part most people get wrong. If you let a
model generate the whole email, three things happen: every message is
structurally different so you can never A/B anything, the model occasionally
invents a claim about the recipient's business that you get to explain later,
and the cost is 40x. Constraining generation to one sentence keeps the surface
area of a hallucination to one sentence.

Openers are generated in batches ahead of send time, not inline. A worker that
blocks on an API call while holding a lead lock is a worker that stalls the
whole pool when the API is slow.
"""
from __future__ import annotations

import json
import re

import anthropic
from jinja2 import Environment, StrictUndefined, TemplateError

from config import ANTHROPIC_API_KEY, OPENER_MODEL

_env = Environment(undefined=StrictUndefined, autoescape=False)
_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

SYSTEM = """You write the opening line of a cold B2B email. One sentence, plain, specific.

Rules:
- Reference something concrete about THIS business from the data given. If the data is thin, reference their industry or city instead of inventing a detail.
- Never invent facts: no revenue figures, no headcount, no "I saw your recent post", no claims about their website unless the data says so.
- No flattery ("love what you're doing", "impressive work").
- No questions. No greeting. No sign-off. No em dashes.
- Under 25 words.

Return JSON only: {"opener": "..."} with no markdown fence."""


def _fallback(lead: dict) -> str:
    city = lead.get("city")
    where = f" in {city}" if city else ""
    return f"I work with businesses like {lead['business']}{where} and had a specific reason to reach out."


def generate_openers(leads: list[dict], brief: str, tone: str) -> dict[str, str]:
    """
    Batch openers. Returns {lead_id: opener}.

    Batching matters for more than cost: one request for 20 leads gives the model
    the whole cohort at once, so the openers vary from each other instead of
    converging on the same phrasing 20 times. Twenty identical-shaped emails
    from one domain is a pattern filters are specifically looking for.
    """
    if not _client:
        return {str(l["id"]): _fallback(l) for l in leads}

    out: dict[str, str] = {}
    for i in range(0, len(leads), 20):
        chunk = leads[i:i + 20]
        payload = [
            {
                "id": str(l["id"]),
                "business": l["business"],
                "city": l.get("city"),
                "website": l.get("website"),
                "details": l.get("fields") or {},
            }
            for l in chunk
        ]
        prompt = (
            f"What I'm offering:\n{brief}\n\nTone: {tone}\n\n"
            f"Businesses:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
            'Return JSON only: {"openers":[{"id":"...","opener":"..."}]}'
        )
        try:
            resp = _client.messages.create(
                model=OPENER_MODEL,
                max_tokens=2000,
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
            for item in json.loads(text).get("openers", []):
                opener = (item.get("opener") or "").strip()
                # Trust nothing. A model that ignored the length rule produces an
                # email that reads wrong, and you find out from your reply rate.
                if 0 < len(opener.split()) <= 35:
                    out[str(item["id"])] = opener
        except Exception:
            pass  # per-chunk failure falls through to the template default

    for l in leads:
        out.setdefault(str(l["id"]), _fallback(l))
    return out


def render(campaign: dict, lead: dict, opener: str, unsubscribe_url: str) -> tuple[str, str]:
    ctx = {
        "business": lead["business"],
        "city": lead.get("city") or "",
        "website": lead.get("website") or "",
        "email": lead["email"],
        "domain": lead["domain"],
        "opener": opener,
        "unsubscribe_url": unsubscribe_url,
        **(lead.get("fields") or {}),
    }
    try:
        subject = _env.from_string(campaign["subject_tpl"]).render(**ctx).strip()
        body = _env.from_string(campaign["body_tpl"]).render(**ctx).strip()
    except TemplateError as e:
        raise ValueError(f"template error: {e}") from e

    # The footer is appended here, not left to the template, because a template
    # is user-editable and this is the part that must be present on every single
    # message regardless of what anyone typed into a textarea.
    body = (
        f"{body}\n\n---\n{campaign['postal_address']}\n"
        f"Not relevant? Unsubscribe: {unsubscribe_url}"
    )
    return subject, body


def validate_templates(subject_tpl: str, body_tpl: str) -> list[str]:
    """Called at campaign creation. Fail loudly at edit time, not at send time."""
    problems = []
    if "{{ opener }}" not in body_tpl and "{{opener}}" not in body_tpl:
        problems.append("body must contain {{ opener }} or the personalization does nothing")
    for name, tpl in (("subject", subject_tpl), ("body", body_tpl)):
        try:
            _env.from_string(tpl)
        except TemplateError as e:
            problems.append(f"{name}: {e}")
    if len(subject_tpl) > 90:
        problems.append("subject over 90 chars will truncate on mobile")
    return problems