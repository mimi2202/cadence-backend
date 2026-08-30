"""
CSV ingest.

This is where most of the damage gets prevented. A cold list is mostly garbage:
scraped role addresses, catch-alls, spam traps, duplicates across five variants
of the same domain. Every bad row you accept here becomes a bounce later, and
bounces are what get a sending domain flagged.

So ingest is aggressive about rejection, and it tells you exactly what it
dropped and why. A list that shrinks 40% at ingest is normal and good.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from collections import Counter
from dataclasses import dataclass, field

import dns.resolver
from email_validator import EmailNotValidError, validate_email

logger = logging.getLogger("cadence.ingest")

# Role accounts route to a shared inbox nobody owns. They bounce, they get
# marked as spam by whoever is on rota, and they never reply. Dropping these
# costs you nothing and is the single biggest bounce-rate win available.
ROLE_LOCALPARTS = {
    "abuse", "admin", "administrator", "all", "billing", "compliance", "contact",
    "everyone", "help", "hostmaster", "info", "jobs", "legal", "mail", "marketing",
    "noc", "no-reply", "noreply", "office", "postmaster", "privacy", "root",
    "sales", "security", "spam", "staff", "support", "team", "webmaster", "www",
}

# Free providers mean you scraped a person, not a business. Different legal
# basis under GDPR, different deliverability profile. Out.
FREE_PROVIDERS = {
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "aol.com", "icloud.com", "me.com", "proton.me", "protonmail.com",
    "gmx.com", "mail.com", "yandex.com", "zoho.com",
}

EMAIL_COLS      = ("email", "e-mail", "mail", "email_address", "contact_email")
BUSINESS_COLS   = ("business", "company", "company_name", "name", "organization", "business_name")
WEBSITE_COLS    = ("website", "url", "site", "domain", "web")
CITY_COLS       = ("city", "town", "location", "locality")
EMPLOYEE_COLS   = ("# employees", "employees", "employee count", "headcount", "company size")
FIRST_NAME_COLS = ("first name", "firstname", "first_name", "fname", "given name")
COUNTRY_COLS    = ("country", "company country", "location country", "company_country")

# Representative UTC offset in minutes, one per country. Countries spanning
# multiple timezones (US, Canada, Brazil, Australia, Mexico) use the offset
# for their most common business population, not a precise per-city lookup.
# Daylight saving is not accounted for. Good enough to keep the send window
# from firing at 3am local; not precise to the minute.
COUNTRY_TZ_OFFSET_MIN = {
    "argentina": -180, "australia": 600, "belize": -360, "bolivia": -240,
    "brazil": -180, "canada": -300, "chile": -240, "colombia": -300,
    "costa rica": -360, "ecuador": -300, "el salvador": -360, "guatemala": -360,
    "honduras": -360, "japan": 540, "mexico": -360, "new zealand": 720,
    "nicaragua": -360, "panama": -300, "paraguay": -240, "peru": -300,
    "puerto rico": -240, "qatar": 180, "saudi arabia": 180, "singapore": 480,
    "south africa": 120, "south korea": 540, "taiwan": 480, "ukraine": 120,
    "united arab emirates": 240, "uae": 240, "uruguay": -180, "venezuela": -240,
}

# Anything at or above this heads count gets flagged in the ingest report so
# it's visible before a campaign starts, not discovered after a Fortune 500
# VP gets a "$8/hour to film your shop" pitch.
LARGE_COMPANY_THRESHOLD = 100


@dataclass
class IngestReport:
    accepted: int = 0
    rejected: Counter = field(default_factory=Counter)
    rows: list[dict] = field(default_factory=list)
    mx_checked: int = 0
    large_companies: int = 0
    tz_unresolved: int = 0

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "rejected": dict(self.rejected),
            "rejected_total": sum(self.rejected.values()),
            "mx_checked": self.mx_checked,
            "large_companies": self.large_companies,
            "tz_unresolved": self.tz_unresolved,
        }


def _pick(header: list[str], candidates: tuple[str, ...]) -> str | None:
    norm = {re.sub(r"[^a-z]", "", h.lower()): h for h in header}
    for c in candidates:
        key = re.sub(r"[^a-z]", "", c)
        if key in norm:
            return norm[key]
    return None


_mx_cache: dict[str, bool] = {}


def has_mx(domain: str) -> bool:
    """One DNS lookup per *domain*, not per row. A 5k list is usually ~2k domains."""
    if domain in _mx_cache:
        return _mx_cache[domain]
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5.0)
        ok = len(answers) > 0
    except Exception as e:
        logger.warning(f"MX lookup failed for {domain}: {type(e).__name__}: {e}")
        ok = False
    _mx_cache[domain] = ok
    return ok


def parse_csv(raw: bytes, *, check_mx: bool = True) -> IngestReport:
    text = raw.decode("utf-8-sig", errors="replace")
    # Sniff the dialect rather than assuming commas. Exports from European
    # tools are semicolon-delimited more often than not.
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)

    header = reader.fieldnames or []
    c_email = _pick(header, EMAIL_COLS)
    if not c_email:
        raise ValueError(f"no email column found. headers seen: {header}")
    c_biz  = _pick(header, BUSINESS_COLS)
    c_web  = _pick(header, WEBSITE_COLS)
    c_city = _pick(header, CITY_COLS)
    c_emp  = _pick(header, EMPLOYEE_COLS)
    c_first = _pick(header, FIRST_NAME_COLS)
    c_country = _pick(header, COUNTRY_COLS)

    rep = IngestReport()
    seen: set[str] = set()

    for row in reader:
        raw_email = (row.get(c_email) or "").strip()
        if not raw_email:
            rep.rejected["blank"] += 1
            continue

        try:
            v = validate_email(raw_email, check_deliverability=False)
            email = v.normalized.lower()
        except EmailNotValidError:
            rep.rejected["invalid_syntax"] += 1
            continue

        local, _, domain = email.partition("@")

        if local in ROLE_LOCALPARTS or local.startswith(("noreply", "no-reply", "donotreply")):
            rep.rejected["role_account"] += 1
            continue
        if domain in FREE_PROVIDERS:
            rep.rejected["free_provider"] += 1
            continue
        if email in seen:
            rep.rejected["duplicate"] += 1
            continue

        if check_mx:
            rep.mx_checked += 1
            if not has_mx(domain):
                rep.rejected["no_mx"] += 1
                continue

        seen.add(email)
        business = (row.get(c_biz) or "").strip() if c_biz else ""
        if not business:
            # Fall back to a readable form of the domain rather than dropping
            # the row. "acme-dental.co.uk" -> "Acme Dental".
            business = domain.rsplit(".", 2)[0].replace("-", " ").replace("_", " ").title()

        employee_count = None
        if c_emp:
            raw_emp = (row.get(c_emp) or "").strip().replace(",", "")
            if raw_emp.isdigit():
                employee_count = int(raw_emp)
                if employee_count >= LARGE_COMPANY_THRESHOLD:
                    rep.large_companies += 1

        extra = {k: v for k, v in row.items()
                 if k and k not in {c_email, c_biz, c_web, c_city, c_emp, c_first, c_country}
                 and (v or "").strip()}

        # Folded in under a clean, Jinja-safe key so {{ first_name }} works
        # directly in a template. The raw CSV header ("First Name", with a
        # space) can't be referenced as a Jinja variable, so it's normalized
        # here rather than left for the template author to work around.
        if c_first:
            first_name = (row.get(c_first) or "").strip()
            if first_name:
                extra["first_name"] = first_name

        # Recipient-local send window depends on this being right. Unresolved
        # countries fall back to UTC rather than rejecting the row outright —
        # a lead with the wrong timezone is still worth having, it just won't
        # be scheduled precisely until someone notices and fixes it.
        tz_offset = 0
        if c_country:
            country_raw = (row.get(c_country) or "").strip().lower()
            if country_raw in COUNTRY_TZ_OFFSET_MIN:
                tz_offset = COUNTRY_TZ_OFFSET_MIN[country_raw]
            elif country_raw:
                rep.tz_unresolved += 1

        rep.rows.append({
            "email": email,
            "domain": domain,
            "business": business,
            "website": (row.get(c_web) or "").strip() if c_web else None,
            "city": (row.get(c_city) or "").strip() if c_city else None,
            "employee_count": employee_count,
            "tz_offset": tz_offset,
            "fields": extra,
        })
        rep.accepted += 1

    return rep