"""
The worker. Run as many as you like; claim_lead() makes them safe to parallelise.

Three loops in one process:

  prep  — turns 'new' leads into 'ready' ones (openers + rendered bodies).
          Runs ahead of send time so the send path never blocks on an LLM call.
  send  — claims one ready lead at a time and delivers it.
  poll  — reads bounces, complaints, replies and opt-outs back off IMAP.

Their failure modes are unrelated, which is why they're separate. The LLM being
down should slow how fast new leads become sendable; it should not stop the
thousand already-rendered leads from going out. IMAP being down should not stop
sending either — but it *should* stop it eventually, which is what the staleness
check in main() does: if we haven't heard feedback in six hours we're flying
blind on reputation, and flying blind is how the domain dies.
"""
from __future__ import annotations

import random
import signal
import time
from datetime import datetime, timedelta, timezone

import unsubscribe
from bounces import poll_all
from config import (IMAP_POLL_S, MAX_BOUNCES_24H, MAX_COMPLAINTS_24H, POLL_IDLE_S, WORKER_ID)
from database import execute, pool, q
from personalize import generate_openers, render
from transports import Outbound, TransportError, build_message, new_message_id, transport_for

_running = True


def _stop(*_):
    global _running
    _running = False
    print("draining...")


signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)


# ------------------------------------------------------------------ prep

def prep_batch(limit: int = 40) -> int:
    campaigns = q("SELECT * FROM campaign WHERE status IN ('running','paused')")
    total = 0
    for c in campaigns:
        leads = q(
            "SELECT * FROM lead WHERE campaign_id=%s AND state='new' ORDER BY id LIMIT %s",
            (c["id"], limit),
        )
        if not leads:
            continue

        openers = generate_openers(leads, c["brief"], c["tone"])

        for l in leads:
            try:
                uurl = unsubscribe.url(l["email"])
                subject, body = render(c, l, openers[str(l["id"])], uurl)
            except ValueError as e:
                execute("UPDATE lead SET state='failed', error=%s WHERE id=%s", (str(e), l["id"]))
                continue

            # Jitter the earliest send time. Perfectly even spacing is a
            # machine signature; a human sending from this mailbox would not
            # emit one message every 216 seconds on the dot.
            jitter = timedelta(seconds=random.randint(0, 900))
            execute(
                """UPDATE lead SET opener=%s, subject=%s, body=%s, state='ready',
                                   not_before = now() + %s
                   WHERE id=%s""",
                (openers[str(l["id"])], subject, body, jitter, l["id"]),
            )
            total += 1
    return total


# ------------------------------------------------------------------ send

def claim():
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM claim_lead(%s)", (WORKER_ID,))
        return cur.fetchone()


def _breaker(mailbox_id) -> None:
    """
    Trip the mailbox if the last 24h look bad.

    Counted from send_event rather than a running column, so a restart or a
    manual DB fix can't leave a mailbox stuck open with a bad reputation.
    """
    row = q(
        """SELECT
             count(*) FILTER (WHERE kind='hard_bounce') AS b,
             count(*) FILTER (WHERE kind='complaint')   AS c
           FROM send_event
           WHERE mailbox_id=%s AND created_at > now() - interval '24 hours'""",
        (mailbox_id,), one=True,
    )
    if row["b"] >= MAX_BOUNCES_24H or row["c"] >= MAX_COMPLAINTS_24H:
        execute(
            """UPDATE mailbox SET paused_until = now() + interval '24 hours',
                                  pause_reason = %s
               WHERE id=%s AND paused_until IS NULL""",
            (f"auto: {row['b']} bounces / {row['c']} complaints in 24h", mailbox_id),
        )


def send_one() -> bool:
    job = claim()
    if not job:
        return False

    lead_id = job["lead_id"]
    mailbox_id = job["out_mailbox_id"]
    sender_domain = job["from_address"].split("@", 1)[1]
    mid = new_message_id(sender_domain)

    msg = build_message(Outbound(
        to=job["email"],
        subject=job["subject"],
        body=job["body"],
        from_address=job["from_address"],
        from_name=job["from_name"],
        unsubscribe_url=unsubscribe.url(job["email"]),
        unsubscribe_mailto=unsubscribe.mailto(job["email"]),
        message_id=mid,
    ))

    try:
        transport_for(job).send(msg)
    except TransportError as e:
        if e.permanent:
            execute("UPDATE lead SET state='bounced', error=%s WHERE id=%s", (str(e), lead_id))
            execute(
                "INSERT INTO suppression(scope,value,reason) VALUES('email',%s,%s) "
                "ON CONFLICT DO NOTHING",
                (job["email"], f"hard bounce: {e}"),
            )
            execute(
                "INSERT INTO send_event(lead_id,mailbox_id,kind,detail) VALUES(%s,%s,'hard_bounce',%s)",
                (lead_id, mailbox_id, str(e)),
            )
            _breaker(mailbox_id)
        else:
            # Exponential backoff with a cap. Attempt 3 is the last one; the
            # claim_lead reaper won't resurrect it beyond that.
            row = q("SELECT attempts FROM lead WHERE id=%s", (lead_id,), one=True)
            delay = timedelta(minutes=min(60, 5 * 2 ** row["attempts"]))
            state = "failed" if row["attempts"] >= 3 else "ready"
            execute(
                "UPDATE lead SET state=%s, error=%s, not_before=now()+%s, locked_by=NULL WHERE id=%s",
                (state, str(e), delay, lead_id),
            )
            execute(
                "INSERT INTO send_event(lead_id,mailbox_id,kind,detail) VALUES(%s,%s,'soft_bounce',%s)",
                (lead_id, mailbox_id, str(e)),
            )
        return True

    execute(
        "UPDATE lead SET state='sent', message_id=%s, sent_at=now(), locked_by=NULL, error=NULL WHERE id=%s",
        (mid, lead_id),
    )
    execute("INSERT INTO send_event(lead_id,mailbox_id,kind) VALUES(%s,%s,'sent')", (lead_id, mailbox_id))
    print(f"sent {job['email']} via {job['from_address']}")
    return True


# ------------------------------------------------------------------ loop

def feedback_is_stale() -> bool:
    """
    True if any enabled mailbox with IMAP configured hasn't been polled recently.

    Sending without feedback is the dangerous state, not sending too fast. If
    the poller has been broken since yesterday you have no idea whether your
    bounce rate is 1% or 30%, and the difference between those is whether you
    still have a domain next week. So we stop.
    """
    row = q(
        """SELECT count(*) AS n FROM mailbox
           WHERE enabled AND imap_host IS NOT NULL
             AND (last_polled_at IS NULL OR last_polled_at < now() - interval '6 hours')""",
        one=True,
    )
    return row["n"] > 0


def main():
    print(f"worker {WORKER_ID} up")
    last_prep = last_reap = last_poll = 0.0
    blind = False

    while _running:
        now = time.time()

        if now - last_reap > 120:
            n = q("SELECT reap_stalled() AS n", one=True)["n"]
            if n:
                print(f"reaped {n} stalled")
            last_reap = now

        if now - last_poll > IMAP_POLL_S:
            try:
                s = poll_all()
                if s["seen"]:
                    print(f"inbound: {s['seen']} seen, {s['applied']} applied, "
                          f"{s['unmatched']} unmatched")
            except Exception as e:
                print(f"poll error: {e}")
            last_poll = now

            was_blind, blind = blind, feedback_is_stale()
            if blind and not was_blind:
                print("HOLDING SENDS: no IMAP feedback in 6h — reputation is unobservable")
            elif was_blind and not blind:
                print("feedback restored, resuming sends")

        if now - last_prep > 30:
            made = prep_batch()
            if made:
                print(f"prepped {made}")
            last_prep = now

        if blind:
            time.sleep(POLL_IDLE_S)
            continue

        if not send_one():
            time.sleep(POLL_IDLE_S)
        else:
            # Small human-ish gap even when there's a backlog. The token bucket
            # is the real limiter; this just avoids a tight loop against SMTP.
            time.sleep(random.uniform(0.8, 2.5))

    # Release anything this worker was holding so another picks it up now
    # instead of waiting for the reaper.
    execute(
        "UPDATE lead SET state='ready', locked_by=NULL, locked_at=NULL WHERE locked_by=%s AND state='sending'",
        (WORKER_ID,),
    )
    print("stopped")


if __name__ == "__main__":
    main()