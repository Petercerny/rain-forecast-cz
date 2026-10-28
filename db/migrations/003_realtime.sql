-- =====================================================================================
-- 003_realtime.sql : state that used to live in files, now in the database.
--
-- The ingest step runs as a serverless function (Vercel) that has no disk between calls, so the
-- bookkeeping that used to be in data/cache/met_state.json lives here instead. One row per thing
-- we poll (key = "<source>:<id>"):
--   * MET Norway:  Expires + Last-Modified of the last response (their terms: do not ask again
--                  before Expires, and send Last-Modified back as If-Modified-Since).
--   * Open-Meteo:  `expires` is "do not ask again before", our own minimum poll interval.
-- Applied with `python -m src.db_migrate`. Purely additive.
-- =====================================================================================

CREATE TABLE rain.http_state (
    key           text        PRIMARY KEY,
    expires       timestamptz,
    last_modified text,
    etag          text,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE rain.http_state ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON rain.http_state FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON rain.http_state FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON rain.http_state FROM authenticated;
    END IF;
END $$;

GRANT SELECT, INSERT, UPDATE ON rain.http_state TO pipeline;
CREATE POLICY pipeline_all ON rain.http_state FOR ALL TO pipeline USING (true) WITH CHECK (true);
