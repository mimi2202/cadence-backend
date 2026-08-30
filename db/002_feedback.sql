-- 002: bounce + reply ingestion.
--
-- Apply to an existing database:  psql cadence -f backend/app/migrations/002_feedback.sql

-- IMAP credentials. Separate columns from SMTP because they genuinely differ on
-- some providers, and because a mailbox can be send-only (Resend) while still
-- having a real inbox somewhere that collects its bounces.
ALTER TABLE mailbox
  ADD COLUMN imap_host text,
  ADD COLUMN imap_port int DEFAULT 993,
  ADD COLUMN imap_user text,
  ADD COLUMN imap_pass text,
  -- IMAP UIDs are only meaningful within a UIDVALIDITY epoch. Store both, or a
  -- mailbox rebuild on the provider's side silently makes you reprocess or skip
  -- everything depending on which way the numbers moved.
  ADD COLUMN imap_uidvalidity bigint,
  ADD COLUMN imap_last_uid bigint NOT NULL DEFAULT 0,
  ADD COLUMN last_polled_at timestamptz;

-- A reply is a terminal state. It outranks everything: no follow-up, ever.
ALTER TABLE lead
  ADD COLUMN replied_at timestamptz,
  ADD COLUMN reply_snippet text;

ALTER TABLE lead DROP CONSTRAINT lead_state_check;
ALTER TABLE lead ADD CONSTRAINT lead_state_check CHECK (
  state IN ('new','ready','sending','sent','failed','suppressed','bounced','replied')
);

-- Message-ID is how a DSN or a reply gets matched back to the lead that caused
-- it. Without this index every inbound mail is a seq scan over the whole table.
CREATE INDEX lead_message_id_idx ON lead (message_id) WHERE message_id IS NOT NULL;

-- Raw inbound we could not classify. Do not silently drop these — an unparsed
-- bounce is a bounce you are not counting, and the whole reputation model
-- depends on the count being roughly right.
CREATE TABLE inbound_unmatched (
  id         bigserial PRIMARY KEY,
  mailbox_id uuid REFERENCES mailbox(id) ON DELETE CASCADE,
  kind       text NOT NULL,          -- bounce_unmatched | arf_unmatched | parse_error
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