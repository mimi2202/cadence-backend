from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import unsubscribe
from database import execute, pool, q
from ingest import parse_csv
from personalize import validate_templates

app = FastAPI(title="Cadence")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "https://cadence-sand-chi.vercel.app",
    ],
    allow_methods=["*"], allow_headers=["*"],
)


# ------------------------------------------------------------------ mailboxes

class MailboxIn(BaseModel):
    address: str
    display_name: str
    transport: str = "smtp"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_pass: str | None = None
    resend_key: str | None = None
    daily_cap: int = Field(200, ge=20, le=2000)
    # Where bounces and replies land. Without this the reputation loop is open
    # and the worker will refuse to send after six hours.
    imap_host: str | None = None
    imap_port: int = 993
    imap_user: str | None = None
    imap_pass: str | None = None


@app.post("/mailboxes")
def add_mailbox(m: MailboxIn):
    if m.transport == "smtp" and not (m.smtp_host and m.smtp_user and m.smtp_pass):
        raise HTTPException(400, "smtp transport needs host, user and password")
    if m.transport == "resend" and not m.resend_key:
        raise HTTPException(400, "resend transport needs an api key")
    row = q(
        """INSERT INTO mailbox(address,display_name,transport,smtp_host,smtp_port,
                               smtp_user,smtp_pass,resend_key,daily_cap,
                               imap_host,imap_port,imap_user,imap_pass)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (m.address, m.display_name, m.transport, m.smtp_host, m.smtp_port,
         m.smtp_user, m.smtp_pass, m.resend_key, m.daily_cap,
         m.imap_host, m.imap_port, m.imap_user or m.address, m.imap_pass or m.smtp_pass),
        one=True,
    )
    return {"id": row["id"], "feedback_configured": bool(m.imap_host)}


@app.get("/mailboxes")
def list_mailboxes():
    return q(
        """SELECT m.id, m.address, m.display_name, m.transport, m.daily_cap,
                  m.activated_on, m.sent_today, m.paused_until, m.pause_reason, m.enabled,
                  m.last_polled_at, (m.imap_host IS NOT NULL) AS feedback_on,
                  mailbox_capacity(m.*) AS capacity_today
           FROM mailbox m ORDER BY m.address"""
    )


@app.post("/mailboxes/{mid}/resume")
def resume_mailbox(mid: str):
    execute("UPDATE mailbox SET paused_until=NULL, pause_reason=NULL WHERE id=%s", (mid,))
    return {"ok": True}


# ------------------------------------------------------------------ campaigns

class CampaignIn(BaseModel):
    name: str
    subject_tpl: str
    body_tpl: str
    brief: str
    tone: str = "direct"
    postal_address: str
    window_start: int = 9
    window_end: int = 17
    weekdays_only: bool = True
    domain_cooldown_s: int = 3600
    mailbox_ids: list[str] = []


@app.post("/campaigns")
def create_campaign(c: CampaignIn):
    problems = validate_templates(c.subject_tpl, c.body_tpl)
    if problems:
        raise HTTPException(400, {"template_problems": problems})
    if not c.postal_address.strip():
        raise HTTPException(400, "a physical postal address is required to send commercial email")
    if not c.mailbox_ids:
        raise HTTPException(400, "assign at least one mailbox")

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO campaign(name,subject_tpl,body_tpl,brief,tone,postal_address,
                                    window_start,window_end,weekdays_only,domain_cooldown_s)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (c.name, c.subject_tpl, c.body_tpl, c.brief, c.tone, c.postal_address,
             c.window_start, c.window_end, c.weekdays_only, c.domain_cooldown_s),
        )
        cid = cur.fetchone()["id"]
        for mb in c.mailbox_ids:
            cur.execute(
                "INSERT INTO campaign_mailbox(campaign_id,mailbox_id) VALUES(%s,%s)", (cid, mb)
            )
    return {"id": cid}


@app.get("/campaigns")
def list_campaigns():
    return q(
        """SELECT c.*,
                  count(l.id) FILTER (WHERE l.state='sent')                        AS sent,
                  count(l.id) FILTER (WHERE l.state='replied')                     AS replied,
                  count(l.id) FILTER (WHERE l.state IN ('new','ready'))            AS pending,
                  count(l.id) FILTER (WHERE l.state IN ('bounced','failed'))       AS problems,
                  count(l.id)                                                      AS total
           FROM campaign c LEFT JOIN lead l ON l.campaign_id = c.id
           GROUP BY c.id ORDER BY c.created_at DESC"""
    )


@app.post("/campaigns/{cid}/leads")
async def upload_leads(cid: str, file: UploadFile = File(...), check_mx: bool = True):
    if not q("SELECT 1 FROM campaign WHERE id=%s", (cid,), one=True):
        raise HTTPException(404, "no such campaign")
    try:
        report = parse_csv(await file.read(), check_mx=check_mx)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    inserted = 0
    with pool.connection() as conn, conn.cursor() as cur:
        for r in report.rows:
            cur.execute(
                """INSERT INTO lead(campaign_id,email,domain,business,website,city,
                                    employee_count,tz_offset,fields)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (campaign_id,email) DO NOTHING""",
                (cid, r["email"], r["domain"], r["business"], r["website"], r["city"],
                 r["employee_count"], r["tz_offset"], json.dumps(r["fields"])),
            )
            inserted += cur.rowcount
    return {**report.as_dict(), "inserted": inserted,
            "already_present": report.accepted - inserted}


@app.post("/campaigns/{cid}/status")
def set_status(cid: str, body: dict[str, Any]):
    status = body.get("status")
    if status not in ("draft", "running", "paused", "done"):
        raise HTTPException(400, "bad status")
    if status == "running":
        # Refuse to start a campaign that would send nothing, rather than
        # sitting in 'running' with an empty queue and no explanation.
        n = q("SELECT count(*) AS n FROM lead WHERE campaign_id=%s AND state IN ('new','ready')",
              (cid,), one=True)["n"]
        if n == 0:
            raise HTTPException(400, "no pending leads — upload a CSV first")
    execute("UPDATE campaign SET status=%s WHERE id=%s", (status, cid))
    return {"ok": True, "status": status}


@app.get("/campaigns/{cid}/leads")
def campaign_leads(cid: str, state: str | None = None, limit: int = 100):
    if state:
        return q(
            "SELECT id,email,business,employee_count,state,subject,opener,sent_at,error FROM lead "
            "WHERE campaign_id=%s AND state=%s ORDER BY sent_at DESC NULLS LAST LIMIT %s",
            (cid, state, limit),
        )
    return q(
        "SELECT id,email,business,employee_count,state,subject,opener,sent_at,error FROM lead "
        "WHERE campaign_id=%s ORDER BY sent_at DESC NULLS LAST LIMIT %s",
        (cid, limit),
    )


@app.get("/campaigns/{cid}/preview")
def preview(cid: str, n: int = 3):
    """Rendered samples, so you see the real email before anything leaves."""
    return q(
        "SELECT email,business,subject,body FROM lead "
        "WHERE campaign_id=%s AND state='ready' ORDER BY not_before LIMIT %s",
        (cid, n),
    )


# ------------------------------------------------------------------ unsubscribe

_PAGE = """<!doctype html><meta charset=utf-8>
<title>Unsubscribed</title>
<style>body{{font:16px/1.6 system-ui;margin:15vh auto;max-width:32rem;padding:0 1.5rem}}</style>
<h1>Unsubscribed</h1><p>{msg}</p>"""


@app.post("/u/{tok}")
def one_click(tok: str):
    """RFC 8058 target. Mail clients POST here with no body."""
    email = unsubscribe.verify(tok)
    if not email:
        raise HTTPException(400, "invalid token")
    _suppress(email)
    return {"ok": True}


@app.get("/u/{tok}", response_class=HTMLResponse)
def unsub_page(tok: str):
    email = unsubscribe.verify(tok)
    if not email:
        return HTMLResponse(_PAGE.format(msg="That link is not valid."), status_code=400)
    _suppress(email)
    return HTMLResponse(_PAGE.format(msg=f"{email} will not be contacted again."))


def _suppress(email: str) -> None:
    execute(
        "INSERT INTO suppression(scope,value,reason) VALUES('email',%s,'unsubscribe') "
        "ON CONFLICT DO NOTHING",
        (email,),
    )
    execute(
        "UPDATE lead SET state='suppressed' WHERE email=%s AND state IN ('new','ready')", (email,)
    )
    execute(
        "INSERT INTO send_event(lead_id,mailbox_id,kind) "
        "SELECT id, mailbox_id, 'unsubscribe' FROM lead WHERE email=%s AND state='sent' LIMIT 1",
        (email,),
    )


@app.post("/suppress")
def suppress(body: dict[str, str]):
    scope, value = body.get("scope", "email"), body.get("value", "").strip().lower()
    if scope not in ("email", "domain") or not value:
        raise HTTPException(400, "scope must be email|domain and value non-empty")
    execute(
        "INSERT INTO suppression(scope,value,reason) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
        (scope, value, body.get("reason", "manual")),
    )
    col = "email" if scope == "email" else "domain"
    n = execute(
        f"UPDATE lead SET state='suppressed' WHERE {col}=%s AND state IN ('new','ready')", (value,)
    )
    return {"ok": True, "leads_stopped": n}


@app.get("/campaigns/{cid}/health")
def campaign_health(cid: str):
    """Reply rate and bounce rate. The only two numbers that mean anything."""
    row = q("SELECT * FROM campaign_health WHERE id=%s", (cid,), one=True)
    if not row:
        raise HTTPException(404, "no such campaign")
    return row


@app.get("/inbound/unmatched")
def unmatched(limit: int = 50):
    """Feedback we could not attribute. Non-empty here means the numbers lie."""
    return q(
        "SELECT id,kind,subject,left(raw,400) AS raw,created_at FROM inbound_unmatched "
        "ORDER BY created_at DESC LIMIT %s", (limit,)
    )


@app.get("/health")
def health():
    return {"ok": True, "pending": q(
        "SELECT count(*) AS n FROM lead WHERE state IN ('new','ready')", one=True)["n"]}