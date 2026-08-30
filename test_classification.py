"""
Verifies the bounces.py classification fix: a no-reply newsletter should NOT
be treated as a bounce daemon message anymore.
"""
from email.message import EmailMessage
from bounces import parse_inbound

msg = EmailMessage()
msg["From"] = "no-reply@somenewsletter.com"
msg["Subject"] = "Get 80% off with Super Family!"
msg["To"] = "ulokajidaniel@gmail.com"
msg.set_content("Check out our latest offers!")

result = parse_inbound(msg)
print(f"Result: {result}")

if result is None:
    print("PASS: correctly ignored as non-feedback mail.")
else:
    print(f"FAIL: still classified as {result.kind}")