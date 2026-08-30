"""
Inbound feedback: bounces, complaints, replies, and human opt-outs.

This closes the loop that makes the warmup model mean anything. Without it the
circuit breaker in worker.py never trips, and a mailbox can be dying for three
weeks while Cadence keeps cheerfully pouring messages through it.

Two classification decisions in here are the ones that matter, and both are
places where the obvious implementation is wrong:

1. NOT EVERY 5.x.x IS THE RECIPIENT'S FAULT.
   The naive rule is "5xx is permanent, suppress the address." But 5.7.x is a
   policy rejection — you were blocked, the mailbox exists fine. Suppressing
   there throws away a good lead AND hides the actual emergency, which is that
   this sending mailbox is now on someone's blocklist. So 5.7.x requeues the
   lead onto a different mailbox and trips the breaker hard, while 5.1.x/5.2.x
   suppresses the address and counts as a normal bounce.

2. A HUMAN SAYING "REMOVE ME" IS A LEGAL OPT-OUT.
   It doesn't matter that they didn't click the header link. CAN-SPAM and GDPR
   both count a plain-English request. If you only honour the button you will
   eventually mail someone who told you to stop, twice, in writing.

Anything that can't be classified goes to inbound_unmatched rather than being
dropped. An uncounted bounce is worse than a logged mystery.
"""
from __future__ import annotations

import email
import imaplib
import re
from dataclasses import dataclass
from email.message import Message
from email.utils import parseaddr

from database import execute, pool, q

# ------------------------------------------------------------------ parsing

MSGID_RE = re.compile(r"<[^<>@\s]+@[^<>\s]+>")
STATUS_RE = re.compile(r"\b([45])\.(\d{1,3})\.(\d{1,3})\b")

# Plain-English opt-out. Deliberately narrow: it must look like an instruction,
# not a passing mention. "unsubscribe" alone matches the footer we ourselves
# put in the quoted original, so the phrase has to carry an imperative.
OPTOUT_RE = re.compile(
    r"\b(?:please\s+)?(?:un-?subscribe(?:\s+me)?|remove\s+(?:me|us)|take\s+(?:me|us)\s+off"
    r"|opt\s*out|stop\s+(?:emailing|contacting|sending)|do\s+not\s+(?:contact|email))\b",
    re.I,
)

# Auto-replies must not be treated as engagement. Someone's OOO is not a reply,
# and counting it as one both corrupts your reply rate and stops a follow-up
# that should have gone out.
AUTOREPLY_SUBJECT_RE = re.compile(
    r"\b(out of (the )?office|automatic reply|auto[- ]?reply|autoresponder|vacation|"
    r"away from my desk|on leave|maternity|paternity|abwesenheit|afwezig)\b", re.I
)


@dataclass
class Feedback:
    kind: str                    # hard_bounce | soft_bounce | blocked | complaint | reply | optout
    message_id: str | None       # of the ORIGINAL outbound message
    recipient: str | None
    status: str | None           # enhanced status, e.g. "5.1.1"
    detail: str
    snippet: str | None = None


def _walk_status_blocks(part: Message) -> list[dict]:
    """message/delivery-status payload is a list of header blocks (RFC 3464)."""
    blocks = []
    payload = part.get_payload()
    if isinstance(payload, list):
        for b in payload:
            if isinstance(b, Message):
                blocks.append({k.lower(): v for k, v in b.items()})
    return blocks


def _original_message_id(msg: Message) -> str | None:
    """Dig the failed message's own Message-ID out of the returned copy."""
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "message/rfc822":
            inner = part.get_payload()
            if isinstance(inner, list) and inner and isinstance(inner[0], Message):
                mid = inner[0].get("Message-ID")
                if mid:
                    return mid.strip()
        elif ctype == "text/rfc822-headers":
            raw = part.get_payload(decode=True)
            if raw:
                mid = email.message_from_string(raw.decode("utf-8", "replace")).get("Message-ID")
                if mid:
                    return mid.strip()
    return None


def _flat_text(msg: Message, limit: int = 8000) -> str:
    out = []
    for part in msg.walk():
        if part.get_content_maintype() == "text":
            try:
                raw = part.get_payload(decode=True) or b""
                out.append(raw.decode(part.get_content_charset() or "utf-8", "replace"))
            except Exception:
                continue
    return "\n".join(out)[:limit]


def classify_status(status: str) -> str:
    """
    Map an enhanced status code to an action.

    5.7.x is the important carve-out — see the module docstring. 5.5.x is a
    protocol error on our side, which is our bug, not a dead address.
    """
    if status.startswith("4"):
        return "soft_bounce"
    if status.startswith(("5.7", "5.3")):   # policy / blocked, system full
        return "blocked"
    if status.startswith("5.5"):            # protocol error, our fault
        return "soft_bounce"
    if status.startswith("5"):              # 5.1.x bad mailbox, 5.2.x mailbox problem
        return "hard_bounce"
    return "soft_bounce"


def parse_inbound(msg: Message) -> Feedback | None:
    """Classify one inbound message. Returns None if it's not feedback at all."""
    subject = (msg.get("Subject") or "").strip()
    ctype = (msg.get_content_type() or "").lower()
    report_type = (msg.get_param("report-type", header="content-type") or "").lower()
    body = _flat_text(msg)

    # --- ARF complaint (feedback loop). The most expensive signal there is.
    if report_type == "feedback-report" or ctype == "message/feedback-report":
        return Feedback(
            kind="complaint",
            message_id=_original_message_id(msg) or _first(MSGID_RE.findall(body)),
            recipient=None, status=None,
            detail=f"ARF complaint: {subject}",
        )

    # --- Structured DSN
    if report_type == "delivery-status" or ctype == "multipart/report":
        status = action = recipient = diag = None
        for part in msg.walk():
            if part.get_content_type() != "message/delivery-status":
                continue
            for block in _walk_status_blocks(part):
                status = block.get("status", status)
                action = (block.get("action") or action or "").lower() or None
                if block.get("final-recipient"):
                    recipient = block["final-recipient"].split(";", 1)[-1].strip()
                diag = block.get("diagnostic-code", diag)

        if action == "delivered":
            return None
        if status:
            kind = "soft_bounce" if action == "delayed" else classify_status(status)
            return Feedback(
                kind=kind,
                message_id=_original_message_id(msg),
                recipient=recipient,
                status=status,
                detail=(diag or subject or "")[:500],
            )

    # --- Unstructured bounce. Gmail and Outlook frequently send prose.
    #     Only treat it as a bounce if the sender looks like a mailer daemon;
    #     otherwise a human quoting an error code becomes a false bounce.
    from_addr = parseaddr(msg.get("From") or "")[1].lower()
    looks_daemon = any(t in from_addr for t in
                   ("mailer-daemon", "postmaster")) \
        or msg.get("Auto-Submitted", "").lower().startswith("auto")

    if looks_daemon:
        m = STATUS_RE.search(body)
        status = m.group(0) if m else None
        # Order matters. A returned copy is the most reliable source, but
        # auto-responders (OOO, vacation) send no copy and instead thread
        # properly via In-Reply-To — without this fallback every out-of-office
        # lands in inbound_unmatched and buries the bounces you care about.
        mid = (_original_message_id(msg)
               or (msg.get("In-Reply-To") or "").strip() or None
               or _first(MSGID_RE.findall(msg.get("References") or ""))
               or _first(MSGID_RE.findall(body)))
        return Feedback(
            kind=classify_status(status) if status else "soft_bounce",
            message_id=mid,
            recipient=_first(re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", body)),
            status=status,
            detail=(subject or "unstructured bounce")[:500],
        )

    # --- Human mail: reply, or an opt-out request written in words.
    in_reply_to = (msg.get("In-Reply-To") or "").strip()
    refs = MSGID_RE.findall(msg.get("References") or "")
    mid = in_reply_to or _first(refs)
    if not mid:
        return None

    if AUTOREPLY_SUBJECT_RE.search(subject) or \
       (msg.get("Auto-Submitted", "").lower() not in ("", "no")):
        return Feedback(kind="soft_bounce", message_id=mid, recipient=from_addr,
                        status=None, detail=f"auto-reply: {subject}"[:500])

    # Strip the quoted original before scanning for an opt-out, so our own
    # footer ("Not relevant? Unsubscribe: ...") can't match itself.
    reply_only = re.split(r"\n\s*(?:>|On .{0,80}wrote:|-{2,}\s*Original)", body, maxsplit=1)[0]

    if OPTOUT_RE.search(reply_only):
        return Feedback(kind="optout", message_id=mid, recipient=from_addr, status=None,
                        detail="opt-out requested in reply",
                        snippet=reply_only.strip()[:400])

    return Feedback(kind="reply", message_id=mid, recipient=from_addr, status=None,
                    detail=subject[:500], snippet=reply_only.strip()[:400])


def _first(xs):
    return xs[0] if xs else None


# ------------------------------------------------------------------ applying

def apply_feedback(fb: Feedback, mailbox_id) -> bool:
    """Write the consequence of one feedback item. Returns False if unmatched."""
    lead = None
    if fb.message_id:
        lead = q("SELECT * FROM lead WHERE message_id=%s", (fb.message_id.strip(),), one=True)
    if not lead and fb.recipient:
        # Fall back to the address. Less precise across campaigns, but a bounce
        # you attribute to the wrong campaign still beats one you don't count.
        lead = q(
            "SELECT * FROM lead WHERE email=%s AND state='sent' ORDER BY sent_at DESC LIMIT 1",
            (fb.recipient.lower(),), one=True,
        )
    if not lead:
        return False

    lid, email_addr, domain = lead["id"], lead["email"], lead["domain"]

    if fb.kind == "hard_bounce":
        execute("UPDATE lead SET state='bounced', error=%s WHERE id=%s", (fb.detail, lid))
        execute(
            "INSERT INTO suppression(scope,value,reason) VALUES('email',%s,%s) ON CONFLICT DO NOTHING",
            (email_addr, f"hard bounce {fb.status or ''}".strip()),
        )
        _maybe_suppress_domain(domain)

    elif fb.kind == "blocked":
        # The address is fine; this mailbox is not welcome. Put the lead back in
        # the pool so another mailbox can carry it, and pause this one for a day.
        execute(
            "UPDATE lead SET state='ready', error=%s, mailbox_id=NULL, "
            "not_before = now() + interval '6 hours' WHERE id=%s",
            (f"blocked: {fb.detail}", lid),
        )
        execute(
            """UPDATE mailbox SET paused_until = now() + interval '24 hours',
                                  pause_reason = %s
               WHERE id=%s AND (paused_until IS NULL OR paused_until < now())""",
            (f"policy block {fb.status or ''}: {fb.detail[:120]}", mailbox_id),
        )

    elif fb.kind == "soft_bounce":
        execute(
            "UPDATE lead SET error=%s WHERE id=%s AND state NOT IN ('replied','bounced')",
            (fb.detail, lid),
        )

    elif fb.kind == "complaint":
        execute("UPDATE lead SET state='suppressed' WHERE id=%s", (lid,))
        execute(
            "INSERT INTO suppression(scope,value,reason) VALUES('email',%s,'complaint') "
            "ON CONFLICT DO NOTHING", (email_addr,),
        )

    elif fb.kind == "optout":
        execute(
            "UPDATE lead SET state='suppressed', reply_snippet=%s WHERE id=%s", (fb.snippet, lid)
        )
        # Domain scope: someone at the company said stop. Honour it for the
        # company, not just the individual who bothered to answer.
        execute(
            "INSERT INTO suppression(scope,value,reason) VALUES('domain',%s,'opt-out in reply') "
            "ON CONFLICT DO NOTHING", (domain,),
        )
        execute(
            "UPDATE lead SET state='suppressed' WHERE domain=%s AND state IN ('new','ready')",
            (domain,),
        )

    elif fb.kind == "reply":
        execute(
            "UPDATE lead SET state='replied', replied_at=now(), reply_snippet=%s WHERE id=%s",
            (fb.snippet, lid),
        )
        # Terminal for the whole company. If someone at acme.com is talking to
        # you, another cold email to their colleague makes you look like a bot.
        execute(
            "UPDATE lead SET state='suppressed' WHERE campaign_id=%s AND domain=%s "
            "AND state IN ('new','ready')",
            (lead["campaign_id"], domain),
        )

    execute(
        "INSERT INTO send_event(lead_id,mailbox_id,kind,detail) VALUES(%s,%s,%s,%s)",
        (lid, mailbox_id, fb.kind, fb.detail),
    )
    return True


def _maybe_suppress_domain(domain: str) -> None:
    """
    Three hard bounces at one domain means the list is stale for that company,
    not that you got unlucky three times. Stop before the fourth.
    """
    n = q(
        """SELECT count(*) AS n FROM lead
           WHERE domain=%s AND state='bounced' AND sent_at > now() - interval '30 days'""",
        (domain,), one=True,
    )["n"]
    if n >= 3:
        execute(
            "INSERT INTO suppression(scope,value,reason) VALUES('domain',%s,%s) ON CONFLICT DO NOTHING",
            (domain, f"{n} hard bounces in 30d"),
        )
        execute(
            "UPDATE lead SET state='suppressed' WHERE domain=%s AND state IN ('new','ready')",
            (domain,),
        )


# ------------------------------------------------------------------ polling

def poll_mailbox(mb: dict) -> dict:
    """
    Read new mail for one mailbox and apply it.

    UID-based, watermarked, and guarded by a Postgres advisory lock so that
    running twenty workers doesn't mean twenty simultaneous IMAP sessions
    against the same account — most providers will start refusing connections
    long before you notice why.
    """
    stats = {"seen": 0, "applied": 0, "unmatched": 0}
    if not (mb.get("imap_host") and mb.get("imap_user") and mb.get("imap_pass")):
        return stats

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS got", (f"imap:{mb['id']}",))
        if not cur.fetchone()["got"]:
            return stats

        try:
            M = imaplib.IMAP4_SSL(mb["imap_host"], mb.get("imap_port") or 993, timeout=30)
            try:
                M.login(mb["imap_user"], mb["imap_pass"])
                typ, data = M.select("INBOX", readonly=True)
                if typ != "OK":
                    return stats

                _, uv = M.status("INBOX", "(UIDVALIDITY)")
                uidvalidity = int(re.search(rb"UIDVALIDITY (\d+)", uv[0]).group(1))

                # If the epoch changed, old UIDs are meaningless. Restart from
                # the current tail rather than replaying the entire mailbox.
                last_uid = mb["imap_last_uid"] or 0
                if mb["imap_uidvalidity"] != uidvalidity:
                    last_uid = 0

                typ, data = M.uid("SEARCH", None, f"UID {last_uid + 1}:*")
                uids = [u for u in (data[0] or b"").split() if int(u) > last_uid]

                highest = last_uid
                for uid in uids[:200]:
                    typ, fetched = M.uid("FETCH", uid, "(RFC822)")
                    if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                        continue
                    stats["seen"] += 1
                    highest = max(highest, int(uid))
                    try:
                        msg = email.message_from_bytes(fetched[0][1])
                        fb = parse_inbound(msg)
                    except Exception as e:
                        _log_unmatched(mb["id"], "parse_error", None, str(e))
                        continue

                    if fb is None:
                        continue
                    if apply_feedback(fb, mb["id"]):
                        stats["applied"] += 1
                    else:
                        stats["unmatched"] += 1
                        _log_unmatched(
                            mb["id"], f"{fb.kind}_unmatched",
                            (msg.get("Subject") or "")[:200], fb.detail,
                        )

                cur.execute(
                    """UPDATE mailbox SET imap_last_uid=%s, imap_uidvalidity=%s,
                                          last_polled_at=now() WHERE id=%s""",
                    (highest, uidvalidity, mb["id"]),
                )
            finally:
                try:
                    M.logout()
                except Exception:
                    pass
        except (imaplib.IMAP4.error, OSError) as e:
            _log_unmatched(mb["id"], "parse_error", "imap connection", str(e))
        finally:
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"imap:{mb['id']}",))

    return stats


def _log_unmatched(mailbox_id, kind: str, subject: str | None, raw: str) -> None:
    execute(
        "INSERT INTO inbound_unmatched(mailbox_id,kind,subject,raw) VALUES(%s,%s,%s,%s)",
        (mailbox_id, kind, subject, raw[:4000]),
    )


def poll_all() -> dict:
    total = {"seen": 0, "applied": 0, "unmatched": 0}
    for mb in q("SELECT * FROM mailbox WHERE enabled AND imap_host IS NOT NULL"):
        s = poll_mailbox(mb)
        for k in total:
            total[k] += s[k]
    return total