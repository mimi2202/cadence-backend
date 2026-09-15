"""
Single-pass worker run, for GitHub Actions (or any scheduler that runs a
script to completion rather than keeping a process alive).

worker.py's main() loop assumes a long-lived process: it sleeps between
cycles and relies on wall-clock time passing while it's alive. GitHub Actions
doesn't work that way — a job runs, finishes, and exits, then gets triggered
again later on a cron schedule. So this does exactly one pass of the same
work main() does per iteration, then exits cleanly.

The token bucket lives in Postgres, not in this process, so nothing is lost
between runs — a mailbox that earned 3 tokens an hour ago still has them
whether this script has been running the whole time or not.
"""
from __future__ import annotations

from bounces import poll_all
from config import WORKER_ID
from database import q
from worker import feedback_is_stale, prep_batch, send_one


def run_once() -> None:
    print(f"worker {WORKER_ID} — single pass starting")

    n = q("SELECT reap_stalled() AS n", one=True)["n"]
    if n:
        print(f"reaped {n} stalled")

    try:
        s = poll_all()
        if s["seen"]:
            print(f"inbound: {s['seen']} seen, {s['applied']} applied, {s['unmatched']} unmatched")
        else:
            print("inbound: nothing new")
    except Exception as e:
        print(f"poll error: {e}")

    if feedback_is_stale():
        print("HOLDING SENDS: no IMAP feedback in 6h — reputation is unobservable")
        return

    made = prep_batch()
    if made:
        print(f"prepped {made}")

    # One run may have earned more than one token since the last run (the
    # scheduler interval is shorter than the refill rate early in warmup,
    # but won't always be). Keep sending until claim_lead() finds nothing,
    # capped so a bug elsewhere can't turn one scheduled run into a flood.
    sent = 0
    while sent < 10:
        if not send_one():
            break
        sent += 1

    print(f"sent {sent} this run" if sent else "nothing eligible to send this run")
    print("single pass complete")


if __name__ == "__main__":
    run_once()