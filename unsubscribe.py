"""
Unsubscribe tokens.

Stateless and HMAC-signed, so the link keeps working after the lead row is
deleted and there's no token table to grow forever. The signature covers the
email, which means a token can only ever unsubscribe the address it was issued
for — someone iterating IDs can't unsubscribe your whole list.

No confirmation page. One click, done. A confirmation step exists to protect
list size and it costs you spam complaints, which is a bad trade: an
unsubscribe is free, a complaint is charged against your sending domain.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

from config import PUBLIC_BASE_URL, UNSUB_SECRET


def _sig(email: str) -> str:
    mac = hmac.new(UNSUB_SECRET, email.encode(), hashlib.sha256).digest()[:18]
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def token(email: str) -> str:
    payload = base64.urlsafe_b64encode(email.encode()).decode().rstrip("=")
    return f"{payload}.{_sig(email)}"


def verify(tok: str) -> str | None:
    try:
        payload, sig = tok.split(".", 1)
        pad = "=" * (-len(payload) % 4)
        email = base64.urlsafe_b64decode(payload + pad).decode()
    except Exception:
        return None
    return email if hmac.compare_digest(sig, _sig(email)) else None


def url(email: str) -> str:
    return f"{PUBLIC_BASE_URL}/u/{token(email)}"


def mailto(email: str) -> str:
    # The mailto arm of List-Unsubscribe. Some clients prefer it; point it at a
    # real inbox with a filter that POSTs to /u/ so both arms end up in the same
    # suppression table.
    return f"unsubscribe@{PUBLIC_BASE_URL.split('//')[-1].split(':')[0]}?subject={token(email)}"