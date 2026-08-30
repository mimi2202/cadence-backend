"""
Standalone IMAP connectivity test. Doesn't touch bounces.py or worker.py —
just confirms login and basic access work before wiring anything real in.
"""
import imaplib
from database import q

mb = q("SELECT * FROM mailbox WHERE address = %s", ('daniel.ulokaji@eng.uniben.edu',), one=True)

if not mb:
    print("No mailbox row found for that address.")
    raise SystemExit(1)

if not mb["imap_pass"] or mb["imap_pass"] == "<app password>":
    print("imap_pass is missing or still the placeholder — set the real app password first.")
    raise SystemExit(1)

print(f"Connecting to {mb['imap_host']}:{mb['imap_port']} as {mb['imap_user']}...")

try:
    conn = imaplib.IMAP4_SSL(mb["imap_host"], mb["imap_port"])
    conn.login(mb["imap_user"], mb["imap_pass"])
    typ, data = conn.select("INBOX", readonly=True)
    print(f"Login OK. INBOX select status: {typ}, message count: {data[0].decode()}")

    typ, data = conn.status("INBOX", "(UIDVALIDITY)")
    print(f"UIDVALIDITY: {data[0].decode()}")

    conn.logout()
    print("Connection closed cleanly. IMAP credentials are working.")
except imaplib.IMAP4.error as e:
    print(f"IMAP login/command failed: {e}")
except OSError as e:
    print(f"Network/connection error: {e}")