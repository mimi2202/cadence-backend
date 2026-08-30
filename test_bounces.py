"""Parser fixtures. Run: python -m tests.test_bounces (no DB needed)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sys, types, email
# stub db so bounces.py imports without postgres
m = types.ModuleType("app.db")
m.execute = lambda *a, **k: 0
m.q = lambda *a, **k: None
m.pool = None
sys.modules["app.db"] = m
from app.bounces import parse_inbound

def P(raw): return parse_inbound(email.message_from_string(raw))

HARD = """From: Mail Delivery Subsystem <MAILER-DAEMON@mx.google.com>
Subject: Delivery Status Notification (Failure)
Content-Type: multipart/report; report-type=delivery-status; boundary="b1"

--b1
Content-Type: text/plain

Address not found.

--b1
Content-Type: message/delivery-status

Reporting-MTA: dns; mx.google.com

Final-Recipient: rfc822; bob@acme.com
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 No such user

--b1
Content-Type: message/rfc822

Message-ID: <abc123@sender.com>
Subject: Quick question about Acme

body
--b1--
"""

BLOCKED = HARD.replace("Status: 5.1.1", "Status: 5.7.1").replace(
    "550 5.1.1 No such user", "550 5.7.1 Message blocked due to sender reputation")

DELAY = HARD.replace("Action: failed", "Action: delayed").replace("Status: 5.1.1","Status: 4.2.2")

ARF = """From: complaints@yahoo.com
Subject: complaint about message
Content-Type: multipart/report; report-type=feedback-report; boundary="c1"

--c1
Content-Type: message/feedback-report

Feedback-Type: abuse
User-Agent: Yahoo!-Mail-Feedback/2.0

--c1
Content-Type: message/rfc822

Message-ID: <xyz789@sender.com>

body
--c1--
"""

REPLY = """From: Jane <jane@acme.com>
Subject: Re: Quick question about Acme
In-Reply-To: <abc123@sender.com>
Content-Type: text/plain

Sure, send over the details. Tuesday works.

On Mon, you wrote:
> Not relevant? Unsubscribe: https://x.com/u/tok
"""

OPTOUT = """From: Jane <jane@acme.com>
Subject: Re: Quick question about Acme
In-Reply-To: <abc123@sender.com>
Content-Type: text/plain

Please remove me from your list.

On Mon, you wrote:
> Not relevant? Unsubscribe: https://x.com/u/tok
"""

OOO = """From: Jane <jane@acme.com>
Subject: Automatic reply: Quick question about Acme
In-Reply-To: <abc123@sender.com>
Auto-Submitted: auto-replied
Content-Type: text/plain

I am out of the office until Monday.
"""

# The trap: a human forwards you an error code. Must NOT be a bounce.
HUMAN = """From: Jane <jane@acme.com>
Subject: Re: Quick question
In-Reply-To: <abc123@sender.com>
Content-Type: text/plain

Our IT said you might hit a 5.1.1 error, use my other address.
"""

for name, raw in [("hard",HARD),("blocked",BLOCKED),("delayed",DELAY),("arf",ARF),
                  ("reply",REPLY),("optout",OPTOUT),("ooo",OOO),("human",HUMAN)]:
    fb = P(raw)
    print(f"{name:9} -> kind={fb.kind if fb else None:<12} status={fb.status if fb else None} mid={fb.message_id if fb else None}")

EXPECTED = {
    "hard": "hard_bounce", "blocked": "blocked", "delayed": "soft_bounce",
    "arf": "complaint", "reply": "reply", "optout": "optout",
    "ooo": "soft_bounce", "human": "reply",
}
fails = []
for name, raw in [("hard",HARD),("blocked",BLOCKED),("delayed",DELAY),("arf",ARF),
                  ("reply",REPLY),("optout",OPTOUT),("ooo",OOO),("human",HUMAN)]:
    fb = P(raw)
    got = fb.kind if fb else None
    if got != EXPECTED[name] or not (fb and fb.message_id):
        fails.append((name, got, EXPECTED[name], fb.message_id if fb else None))
print("FAILURES:", fails or "none")
raise SystemExit(1 if fails else 0)