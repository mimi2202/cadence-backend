-- Cadence schema — consolidated.
--
-- This combines the original schema, the 002_feedback migration (IMAP/bounce
-- tracking, replied state, campaign_health view), and the employee_count
-- column added during lead-sourcing work. Safe to run once, top to bottom,
-- against a fresh empty database.
--
-- The scheduler lives in Postgres, not in a broker. A single send decision needs
-- the suppression list, the mailbox warmup budget, the per-recipient-domain
-- throttle, and the queue row to be consistent at the exact instant of dispatch.
-- Here that is one transaction. Split across Redis + Postgres it is a race, and
-- the way you lose it is double-sends, which is the fastest route to a
-- complaint-flagged sending domain.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================== mailboxes

CREATE TABLE mailbox (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  address       text NOT NULL UNIQUE,
  display_name  text NOT NULL,

  transport     text NOT NULL DEFAULT 'smtp' CHECK (transport IN ('smtp','resend')),
  smtp_host     text,
  smtp_port     int DEFAULT 587,
  smtp_user     text,
  smtp_pass     text,
  resend_key    text,

  -- IMAP credentials. Separate columns from SMTP because they genuinely differ
  -- on some providers, and because a mailbox can be send-only (Resend) while
  -- still having a real inbox somewhere that collects its bounces.
  imap_host        text,
  imap_port        int DEFAULT 993,
  imap_user        text,
  imap_pass        text,
  -- IMAP UIDs are only meaningful within a UIDVALIDITY epoch. Store both, or a
  -- mailbox rebuild on the provider's side silently makes you reprocess or skip
  -- everything depending on which way the numbers moved.
  imap_uidvalidity bigint,
  imap_last_uid    bigint NOT NULL DEFAULT 0,
  last_polled_at   timestamptz,

  -- Warmup. A mailbox earns throughput, it is not granted it.
  activated_on  date NOT NULL DEFAULT CURRENT_DATE,
  daily_cap     int  NOT NULL DEFAULT 200,   -- ceiling once fully warm

  -- Token bucket, refilled lazily on read. No cron, no drift.
  tokens        real        NOT NULL DEFAULT 0,
  tokens_at     timestamptz NOT NULL DEFAULT now(),
  sent_today    int         NOT NULL DEFAULT 0,
  today         date        NOT NULL DEFAULT CURRENT_DATE,

  -- Reputation circuit breaker. The worker trips it, a human clears it.
  paused_until   timestamptz,
  pause_reason   text,
  bounces_24h    int NOT NULL DEFAULT 0,
  complaints_24h int NOT NULL DEFAULT 0,

  enabled       boolean NOT NULL DEFAULT true
);

-- Today's ceiling for a mailbox: linear ramp 20 -> daily_cap over 21 days.
-- Deliberately a function of activated_on rather than a column a cron updates,
-- because a column can be wrong and a function cannot.
CREATE FUNCTION mailbox_capacity(m mailbox) RETURNS int
LANGUAGE sql IMMUTABLE AS $$
  SELECT LEAST(
    m.daily_cap,
    GREATEST(20, (20 + (CURRENT_DATE - m.activated_on) * ((m.daily_cap - 20)::real / 21.0))::int)
  );
$$;

-- ============================================================== campaigns

CREATE TABLE campaign (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name           text NOT NULL,

  subject_tpl    text NOT NULL,   -- Jinja: "Quick question about {{ business }}"
  body_tpl       text NOT NULL,   -- Jinja, must contain {{ opener }} and {{ unsubscribe_url }}
  brief          text NOT NULL,   -- what you're offering; feeds the LLM opener
  tone           text NOT NULL DEFAULT 'direct',

  postal_address text NOT NULL,   -- CAN-SPAM 7704(a)(5). Campaign won't start without it.

  -- Recipient-local send window, 24h clock. 9..17 = 9am-5pm their time.
  window_start   int NOT NULL DEFAULT 9  CHECK (window_start BETWEEN 0 AND 23),
  window_end     int NOT NULL DEFAULT 17 CHECK (window_end   BETWEEN 1 AND 24),
  weekdays_only  boolean NOT NULL DEFAULT true,

  -- Don't hit one company with a burst. Seconds between sends to same domain.
  domain_cooldown_s int NOT NULL DEFAULT 3600,

  status     text NOT NULL DEFAULT 'draft'
             CHECK (status IN ('draft','running','paused','done')),
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Mailboxes assigned to a campaign. Throughput scales by adding rows here,
-- never by pushing a single mailbox harder.
CREATE TABLE campaign_mailbox (
  campaign_id uuid REFERENCES campaign(id) ON DELETE CASCADE,
  mailbox_id  uuid REFERENCES mailbox(id)  ON DELETE CASCADE,
  PRIMARY KEY (campaign_id, mailbox_id)
);

-- ============================================================== leads

CREATE TABLE lead (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id uuid NOT NULL REFERENCES campaign(id) ON DELETE CASCADE,

  email       text NOT NULL,
  domain      text NOT NULL,          -- lowercased, derived at ingest
  business    text NOT NULL,
  website     text,
  city        text,
  employee_count int,                 -- from CSV, used to flag oversized companies at ingest
  fields      jsonb NOT NULL DEFAULT '{}',  -- every other CSV column, verbatim (incl. first_name)

  tz_offset   int NOT NULL DEFAULT 0,  -- minutes from UTC, for the send window

  opener      text,                    -- LLM output, the only generated sentence
  subject     text,
  body        text,

  -- A reply is a terminal state. It outranks everything: no follow-up, ever.
  replied_at    timestamptz,
  reply_snippet text,

  state       text NOT NULL DEFAULT 'new'
              CHECK (state IN ('new','ready','sending','sent','failed','suppressed','bounced','replied')),

  attempts    int NOT NULL DEFAULT 0,
  not_before  timestamptz NOT NULL DEFAULT now(),
  locked_by   text,
  locked_at   timestamptz,

  mailbox_id  uuid REFERENCES mailbox(id),
  message_id  text,
  sent_at     timestamptz,
  error       text,

  UNIQUE (campaign_id, email)
);

-- Partial index: the dispatcher only ever scans work that is actually pending.
CREATE INDEX lead_pending_idx ON lead (not_before, id)
  WHERE state IN ('new','ready');

CREATE INDEX lead_domain_recent_idx ON lead (campaign_id, domain, sent_at DESC)
  WHERE state = 'sent';

-- Message-ID is how a DSN or a reply gets matched back to the lead that caused
-- it. Without this index every inbound mail is a seq scan over the whole table.
CREATE INDEX lead_message_id_idx ON lead (message_id) WHERE message_id IS NOT NULL;

-- ============================================================== suppression

-- Domain scope matters. A hard bounce at acme.com says the list is stale for
-- acme.com, not just for that one address.
CREATE TABLE suppression (
  scope      text NOT NULL CHECK (scope IN ('email','domain')),
  value      text NOT NULL,
  reason     text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (scope, value)
);

CREATE TABLE send_event (
  id         bigserial PRIMARY KEY,
  lead_id    uuid REFERENCES lead(id) ON DELETE CASCADE,
  mailbox_id uuid REFERENCES mailbox(id) ON DELETE SET NULL,
  kind       text NOT NULL,   -- sent | soft_bounce | hard_bounce | complaint | unsubscribe | reply
  detail     text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX send_event_recent_idx ON send_event (mailbox_id, kind, created_at DESC);

-- Raw inbound we could not classify. Do not silently drop these — an unparsed
-- bounce is a bounce you are not counting, and the whole reputation model
-- depends on the count being roughly right.
CREATE TABLE inbound_unmatched (
  id         bigserial PRIMARY KEY,
  mailbox_id uuid REFERENCES mailbox(id) ON DELETE CASCADE,
  kind       text NOT NULL,          -- bounce_unmatched | arf_unmatched | parse_error | reply_unmatched
  subject    text,
  raw        text,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Campaign-level truth. Reply rate is the only number that means anything;
-- open rate requires a tracking pixel, and a tracking pixel is a spam signal.
CREATE VIEW campaign_health AS
SELECT
  c.id, c.name, c.status,
  count(l.id) FILTER (WHERE l.state = 'sent' OR l.replied_at IS NOT NULL) AS delivered,
  count(l.id) FILTER (WHERE l.replied_at IS NOT NULL)                     AS replies,
  count(l.id) FILTER (WHERE l.state = 'bounced')                          AS bounces,
  round(100.0 * count(l.id) FILTER (WHERE l.replied_at IS NOT NULL)
        / NULLIF(count(l.id) FILTER (WHERE l.state='sent' OR l.replied_at IS NOT NULL), 0), 2)
    AS reply_rate_pct,
  round(100.0 * count(l.id) FILTER (WHERE l.state='bounced')
        / NULLIF(count(l.id) FILTER (WHERE l.state IN ('sent','bounced')), 0), 2)
    AS bounce_rate_pct
FROM campaign c LEFT JOIN lead l ON l.campaign_id = c.id
GROUP BY c.id;

-- ============================================================== dispatch
--
-- claim_lead() is the whole scheduler. One call, one transaction, returns at
-- most one lead that is safe to send right now, and reserves the capacity to
-- send it. N workers can call it concurrently and will never collide.
--
-- The order of checks is not cosmetic. Suppression is evaluated inside the same
-- statement that locks the row, so an unsubscribe landing mid-flight cannot be
-- read-then-ignored by a worker that already made its decision.

CREATE FUNCTION claim_lead(p_worker text)
RETURNS TABLE (
  lead_id uuid, out_mailbox_id uuid, email text, subject text, body text,
  from_address text, from_name text, transport text,
  smtp_host text, smtp_port int, smtp_user text, smtp_pass text, resend_key text
)
LANGUAGE plpgsql AS $$
DECLARE
  mb  mailbox%ROWTYPE;
  ld  lead%ROWTYPE;
  cap int;
  refill real;
BEGIN
  -- 1. Pick a mailbox with capacity. Least-recently-touched first, so the pool
  --    rotates evenly instead of hammering whichever row Postgres returns first.
  SELECT * INTO mb FROM mailbox m
  WHERE m.enabled
    AND (m.paused_until IS NULL OR m.paused_until < now())
    AND EXISTS (SELECT 1 FROM campaign_mailbox cm
                JOIN campaign c ON c.id = cm.campaign_id
                WHERE cm.mailbox_id = m.id AND c.status = 'running')
  ORDER BY m.tokens_at ASC
  FOR UPDATE SKIP LOCKED
  LIMIT 1;

  IF NOT FOUND THEN RETURN; END IF;

  -- Roll the day over, then refill the bucket for elapsed time.
  IF mb.today <> CURRENT_DATE THEN
    mb.today := CURRENT_DATE; mb.sent_today := 0; mb.tokens := 0;
    mb.tokens_at := now();
  END IF;

  cap := mailbox_capacity(mb);

  -- Spread `cap` sends across a 12h working day, and never bank more than 3.
  -- The burst ceiling is what stops a paused-then-resumed campaign from
  -- dumping 200 messages in one minute.
  refill := EXTRACT(EPOCH FROM (now() - mb.tokens_at)) * (cap / (12.0 * 3600.0));
  mb.tokens := LEAST(3.0, mb.tokens + refill);
  mb.tokens_at := now();

  IF mb.tokens < 1.0 OR mb.sent_today >= cap THEN
    UPDATE mailbox SET tokens = mb.tokens, tokens_at = mb.tokens_at,
                       today = mb.today, sent_today = mb.sent_today
    WHERE id = mb.id;
    RETURN;
  END IF;

  -- 2. Pick a lead. Every eligibility rule is in this one statement.
  SELECT l.* INTO ld
  FROM lead l
  JOIN campaign c          ON c.id = l.campaign_id
  JOIN campaign_mailbox cm ON cm.campaign_id = c.id AND cm.mailbox_id = mb.id
  WHERE l.state = 'ready'
    AND l.not_before <= now()
    AND c.status = 'running'
    -- recipient-local send window
    AND EXTRACT(HOUR FROM (now() + make_interval(mins => l.tz_offset)))
        BETWEEN c.window_start AND c.window_end - 1
    AND (NOT c.weekdays_only
         OR EXTRACT(ISODOW FROM (now() + make_interval(mins => l.tz_offset))) <= 5)
    -- suppression, checked under the same lock as the claim
    AND NOT EXISTS (SELECT 1 FROM suppression s
                    WHERE (s.scope='email'  AND s.value = l.email)
                       OR (s.scope='domain' AND s.value = l.domain))
    -- per-recipient-domain cooldown: don't burst one company's inbox
    AND NOT EXISTS (SELECT 1 FROM lead p
                    WHERE p.campaign_id = l.campaign_id
                      AND p.domain = l.domain
                      AND p.state = 'sent'
                      AND p.sent_at > now() - make_interval(secs => c.domain_cooldown_s))
  ORDER BY l.not_before, l.id
  FOR UPDATE OF l SKIP LOCKED
  LIMIT 1;

  IF NOT FOUND THEN
    UPDATE mailbox SET tokens = mb.tokens, tokens_at = mb.tokens_at,
                       today = mb.today, sent_today = mb.sent_today
    WHERE id = mb.id;
    RETURN;
  END IF;

  -- 3. Commit the reservation. Capacity is spent here, not after the SMTP call,
  --    so a worker that dies mid-send costs one message rather than the budget.
  UPDATE mailbox
     SET tokens = mb.tokens - 1.0, tokens_at = mb.tokens_at,
         today = mb.today, sent_today = mb.sent_today + 1
   WHERE id = mb.id;

  UPDATE lead
     SET state='sending', locked_by=p_worker, locked_at=now(),
         mailbox_id=mb.id, attempts = lead.attempts + 1
   WHERE lead.id = ld.id;

  RETURN QUERY SELECT
    ld.id, mb.id, ld.email, ld.subject, ld.body,
    mb.address, mb.display_name, mb.transport,
    mb.smtp_host, mb.smtp_port, mb.smtp_user, mb.smtp_pass, mb.resend_key;
END $$;

-- Reap leads whose worker died holding the lock.
CREATE FUNCTION reap_stalled(p_older_than interval DEFAULT '10 minutes')
RETURNS int LANGUAGE sql AS $$
  WITH r AS (
    UPDATE lead SET state='ready', locked_by=NULL, locked_at=NULL,
                    not_before = now() + interval '2 minutes'
    WHERE state='sending' AND locked_at < now() - p_older_than AND attempts < 3
    RETURNING 1
  ) SELECT count(*)::int FROM r;
$$;