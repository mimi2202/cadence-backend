"""
Standalone SMTP connectivity test. Sends ONE real test email to yourself,
using the same mailbox row, to confirm auth and delivery work before
plugging into transports.py's send path.
"""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from database import q

mb = q("SELECT * FROM mailbox WHERE address = %s", ("daniel.ulokaji@eng.uniben.edu",), one=True)

if not mb["smtp_pass"] or mb["smtp_pass"] == "<app password>":
    print("smtp_pass is missing or still the placeholder — set the real app password first.")
    raise SystemExit(1)

msg = EmailMessage()
msg["Message-ID"] = make_msgid(domain="gmail.com")
msg["Date"] = formatdate(localtime=True)
msg["From"] = mb["address"]
msg["To"] = mb["address"]  # sending to yourself — safe, no real recipient touched
msg["Subject"] = "Cadence SMTP test"
msg.set_content("This is a manual SMTP connectivity test. If you're reading this, SMTP auth and delivery both work.")

print(f"Connecting to {mb['smtp_host']}:{mb['smtp_port']} as {mb['smtp_user']}...")

try:
    ctx = ssl.create_default_context()
    if mb["smtp_port"] == 465:
        srv = smtplib.SMTP_SSL(mb["smtp_host"], mb["smtp_port"], context=ctx, timeout=30)
    else:
        srv = smtplib.SMTP(mb["smtp_host"], mb["smtp_port"], timeout=30)
        srv.ehlo()
        if srv.has_extn("starttls"):
            srv.starttls(context=ctx)
            srv.ehlo()

    with srv:
        srv.login(mb["smtp_user"], mb["smtp_pass"])
        srv.send_message(msg)

    print("Login OK. Message sent successfully.")
    print(f"Message-ID: {msg['Message-ID']}")
    print("Check the inbox for daniel.ulokaji@eng.uniben.edu to confirm it arrived.")

except smtplib.SMTPAuthenticationError as e:
    print(f"Auth failed: {e}")
except smtplib.SMTPException as e:
    print(f"SMTP error: {e}")
except OSError as e:
    print(f"Network/connection error: {e}")