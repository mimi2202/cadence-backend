"""
Direct call to poll_mailbox() against the real mailbox row.
Should report 0 seen (proving the watermark worked) and last_polled_at
should update afterward.
"""
from database import q
from bounces import poll_mailbox

mb = q("SELECT * FROM mailbox WHERE address = %s", ("daniel.ulokaji@eng.uniben.edu",), one=True)

print(f"Polling {mb['address']}...")
stats = poll_mailbox(mb)
print(f"Result: {stats}")

mb_after = q("SELECT last_polled_at, imap_last_uid FROM mailbox WHERE address = %s",
             ("daniel.ulokaji@eng.uniben.edu",), one=True)
print(f"last_polled_at is now: {mb_after['last_polled_at']}")
print(f"imap_last_uid is now: {mb_after['imap_last_uid']}")