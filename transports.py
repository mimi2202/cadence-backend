"""
Transport adapters.

The contract is deliberately tiny: hand me a built MIME message, I return a
Message-ID or raise. Everything a transport could disagree about (headers,
threading, unsubscribe) is settled before we get here, in build_message().

One rule that isn't obvious until it bites you: the Message-ID must be generated
by us, not by the provider. If you let the provider assign it, you cannot
correlate an inbound bounce to the lead that caused it, because the bounce quotes
the ID from the original headers and you never stored it.
"""
from __future__ import annotations

import smtplib
import ssl
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Protocol

import httpx


class TransportError(Exception):
    """Raised on a send failure. `permanent` decides retry vs suppress."""

    def __init__(self, msg: str, permanent: bool = False):
        super().__init__(msg)
        self.permanent = permanent


@dataclass
class Outbound:
    to: str
    subject: str
    body: str
    from_address: str
    from_name: str
    unsubscribe_url: str
    unsubscribe_mailto: str
    message_id: str


def build_message(o: Outbound) -> EmailMessage:
    m = EmailMessage()
    m["Message-ID"] = o.message_id
    m["Date"] = formatdate(localtime=True)
    m["From"] = formataddr((o.from_name, o.from_address))
    m["To"] = o.to
    m["Subject"] = o.subject
    m["Reply-To"] = o.from_address

    # RFC 8058 one-click. The POST variant is what makes Gmail and Outlook render
    # a native unsubscribe button, which diverts people who would otherwise hit
    # "report spam". That button is the single highest-leverage deliverability
    # control available to a cold sender, and it costs two headers.
    m["List-Unsubscribe"] = f"<{o.unsubscribe_url}>, <mailto:{o.unsubscribe_mailto}>"
    m["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    # Plain text only, no HTML part, no tracking pixel. A cold email that looks
    # like a newsletter gets filtered like a newsletter. This is a real
    # deliverability decision, not minimalism.
    m.set_content(o.body)
    return m


class Transport(Protocol):
    def send(self, msg: EmailMessage) -> str: ...


class SMTPTransport:
    """For mailboxes you own: Google Workspace, Microsoft 365, self-hosted."""

    def __init__(self, host: str, port: int, user: str, password: str):
        self.host, self.port, self.user, self.password = host, port, user, password

    def send(self, msg: EmailMessage) -> str:
        ctx = ssl.create_default_context()
        try:
            if self.port == 465:
                srv = smtplib.SMTP_SSL(self.host, self.port, context=ctx, timeout=30)
            else:
                srv = smtplib.SMTP(self.host, self.port, timeout=30)
                srv.ehlo()
                # Only upgrade to TLS if the server actually offers it. Real
                # providers advertise STARTTLS and still get encrypted; a plain
                # local sink like Mailpit does not, and must not be forced.
                if srv.has_extn("starttls"):
                    srv.starttls(context=ctx)
                    srv.ehlo()
            with srv:
                # Same for auth: skip login when the server has no AUTH and no
                # credentials were given, so a local sink doesn't reject us.
                if self.user and self.password and srv.has_extn("auth"):
                    srv.login(self.user, self.password)
                srv.send_message(msg)
        except smtplib.SMTPRecipientsRefused as e:
            raise TransportError(f"recipient refused: {e}", permanent=True) from e
        except smtplib.SMTPAuthenticationError as e:
            # Operator error, not the recipient's fault. Never suppress the lead.
            raise TransportError(f"auth failed for {self.user}: {e}", permanent=False) from e
        except smtplib.SMTPResponseException as e:
            # 5xx is permanent, 4xx is worth retrying. Getting this wrong in
            # either direction is expensive: retry a 5xx and you look like a
            # spammer, suppress a 4xx and you throw away a real lead.
            raise TransportError(f"{e.smtp_code} {e.smtp_error}", permanent=500 <= e.smtp_code < 600) from e
        except (OSError, smtplib.SMTPException) as e:
            raise TransportError(str(e), permanent=False) from e
        return msg["Message-ID"]


class ResendTransport:
    """API provider path. Same contract, different failure taxonomy."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    def send(self, msg: EmailMessage) -> str:
        payload = {
            "from": msg["From"],
            "to": [msg["To"]],
            "subject": msg["Subject"],
            "text": msg.get_content(),
            "reply_to": msg["Reply-To"],
            "headers": {
                "Message-ID": msg["Message-ID"],
                "List-Unsubscribe": msg["List-Unsubscribe"],
                "List-Unsubscribe-Post": msg["List-Unsubscribe-Post"],
            },
        }
        try:
            r = httpx.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=30,
            )
        except httpx.HTTPError as e:
            raise TransportError(str(e), permanent=False) from e

        if r.status_code == 429:
            raise TransportError("rate limited", permanent=False)
        if 400 <= r.status_code < 500:
            raise TransportError(f"{r.status_code} {r.text}", permanent=True)
        if r.status_code >= 500:
            raise TransportError(f"{r.status_code} {r.text}", permanent=False)
        return msg["Message-ID"]


def transport_for(row: dict) -> Transport:
    kind = row["transport"]
    if kind == "smtp":
        return SMTPTransport(row["smtp_host"], row["smtp_port"] or 587,
                             row["smtp_user"], row["smtp_pass"])
    if kind == "resend":
        return ResendTransport(row["resend_key"])
    raise TransportError(f"unknown transport {kind!r}", permanent=True)


def new_message_id(domain: str) -> str:
    return make_msgid(idstring=uuid.uuid4().hex[:12], domain=domain)