-- Local test seed.  psql cadence -f backend/db/seed_test.sql
--
-- Four things in the production design will silently send nothing on a fresh
-- database. All four are correct in production and all four have to be defused
-- to test. This is the file that does it — never run it against real sending.

-- 1. A fake SMTP sink so nothing leaves your machine.
--    Start it first:  docker run -d -p 1025:1025 -p 8025:8025 axllent/mailpit
--    Then watch mail arrive at http://localhost:8025
--
-- 2. activated_on is backdated 30 days. On a fresh mailbox mailbox_capacity()
--    returns 20, because the warmup ramp starts the day you add it.
INSERT INTO mailbox (address, display_name, transport, smtp_host, smtp_port,
                     smtp_user, smtp_pass, daily_cap, activated_on)
VALUES ('outreach@testing.local', 'Test Sender', 'smtp', 'localhost', 1025,
        'test', 'test', 500, CURRENT_DATE - 30)
ON CONFLICT (address) DO UPDATE SET activated_on = CURRENT_DATE - 30;

-- 3. Prime the token bucket. This is the one that catches everyone.
--    Tokens refill at cap/(12*3600) per second. At cap=500 that's ~0.0116/s,
--    so from a cold start the FIRST message waits 86 seconds and you conclude
--    the worker is broken. It isn't; it's pacing exactly as designed.
UPDATE mailbox SET tokens = 3.0, tokens_at = now()
WHERE address = 'outreach@testing.local';

-- 4. Wide-open send window. The default is 9-17 recipient-local, weekdays only,
--    so running this on a Saturday evening sends nothing and looks like a bug.
INSERT INTO campaign (name, subject_tpl, body_tpl, brief, postal_address,
                      window_start, window_end, weekdays_only, domain_cooldown_s, status)
VALUES (
  'Smoke test',
  'Quick question about {{ business }}',
  E'{{ opener }}\n\nThis is a local smoke test. If you are reading it in Mailpit, the pipeline works end to end.\n\nBlessing',
  'Testing the Cadence pipeline locally.',
  'Test Co, 1 Example Street, Rotterdam',
  0, 24, false, 0, 'draft'
)
ON CONFLICT DO NOTHING;

INSERT INTO campaign_mailbox (campaign_id, mailbox_id)
SELECT c.id, m.id FROM campaign c, mailbox m
WHERE c.name = 'Smoke test' AND m.address = 'outreach@testing.local'
ON CONFLICT DO NOTHING;

-- Leads inserted directly, bypassing CSV ingest. Ingest does a live MX lookup
-- and drops role accounts and free providers, so a hand-made test CSV of
-- fake addresses gets legitimately rejected down to zero rows.
INSERT INTO lead (campaign_id, email, domain, business, city)
SELECT c.id, v.email, split_part(v.email, '@', 2), v.business, v.city
FROM campaign c,
     (VALUES
        ('anna@northgate-dental.test',  'Northgate Dental',  'Rotterdam'),
        ('bram@kade-architects.test',   'Kade Architects',   'Rotterdam'),
        ('cleo@vinkveld-logistics.test','Vinkveld Logistics','Utrecht'),
        ('dirk@maasoever-cafe.test',    'Maasoever Cafe',    'Rotterdam'),
        ('eva@stroomlijn-hvac.test',    'Stroomlijn HVAC',   'Delft')
     ) AS v(email, business, city)
WHERE c.name = 'Smoke test'
ON CONFLICT (campaign_id, email) DO NOTHING;

-- Start it.
UPDATE campaign SET status = 'running' WHERE name = 'Smoke test';

SELECT 'mailbox capacity today' AS check, mailbox_capacity(m.*)::text AS value
FROM mailbox m WHERE m.address = 'outreach@testing.local'
UNION ALL
SELECT 'leads queued', count(*)::text FROM lead l
JOIN campaign c ON c.id = l.campaign_id WHERE c.name = 'Smoke test';