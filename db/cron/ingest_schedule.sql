-- =====================================================================================
-- ingest_schedule.sql : make Supabase call the ingest endpoint every 10 minutes.
--
-- Why not GitHub Actions or Vercel Cron? GitHub's scheduler skips runs (we saw 4 in 20 hours instead
-- of 20), and Vercel Cron on the free plan runs once a day. pg_cron inside Supabase keeps proper time.
--
-- Apply BY HAND in the Supabase SQL editor (not with db_migrate): it contains your deploy URL and a
-- secret. Do the three steps in order.
--
-- 1. In Vercel set the environment variable INGEST_SECRET (any long random string) and redeploy.
-- 2. Enable the extensions (Dashboard -> Database -> Extensions, or run these two lines):
CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE EXTENSION IF NOT EXISTS pg_net;

-- 3. Store the same secret in the Vault (replace the two <...> values), then schedule the job.
SELECT vault.create_secret('<the same value as INGEST_SECRET>', 'ingest_secret');

SELECT cron.schedule(
    'ingest-every-10-min',
    '*/10 * * * *',
    $$
    SELECT net.http_post(
        url     := 'https://<your-project>.vercel.app/api/ingest',
        headers := jsonb_build_object(
            'Authorization', 'Bearer ' || (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = 'ingest_secret')
        ),
        body    := '{}'::jsonb,
        timeout_milliseconds := 60000
    );
    $$
);

-- ---------------------------------------------------------------- checking on it
-- Did the job fire?                SELECT * FROM cron.job_run_details ORDER BY start_time DESC LIMIT 10;
-- What did the endpoint answer?    SELECT created, status_code, content FROM net._http_response ORDER BY created DESC LIMIT 10;
-- Pause / remove it:               SELECT cron.unschedule('ingest-every-10-min');
-- (An ingest call in which nothing is due is cheap: it makes no API requests and stores nothing.)
