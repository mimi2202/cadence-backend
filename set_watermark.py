"""
One-time watermark set. Points imap_last_uid at 'now' so poll_mailbox()
doesn't try to classify the entire existing inbox as fresh feedback.
"""
import imaplib
from database import q, execute

mb = q("SELECT * FROM mailbox WHERE address = %s", ("daniel.ulokaji@eng.uniben.edu",), one=True)

conn = imaplib.IMAP4_SSL(mb["imap_host"], mb["imap_port"])
conn.login(mb["imap_user"], mb["imap_pass"])
conn.select("INBOX", readonly=True)

typ, data = conn.status("INBOX", "(UIDVALIDITY)")
import re
uidvalidity = int(re.search(rb"UIDVALIDITY (\d+)", data[0]).group(1))

typ, data = conn.uid("SEARCH", None, "ALL")
uids = [int(u) for u in (data[0] or b"").split()]
highest = max(uids) if uids else 0

conn.logout()

execute(
    "UPDATE mailbox SET imap_last_uid=%s, imap_uidvalidity=%s WHERE address=%s",
    (highest, uidvalidity, "daniel.ulokaji@eng.uniben.edu"),
)

print(f"Watermark set: imap_last_uid={highest}, imap_uidvalidity={uidvalidity}")
print("Next poll_mailbox() call will only see mail with UID greater than this.")