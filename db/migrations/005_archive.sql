-- =====================================================================================
-- 005_archive.sql : bookkeeping and permissions for the long-term archive (Cloudflare R2).
--
-- Postgres keeps only the newest ~15 days of forecasts and observations. Every night
-- `python -m src.archive` copies each finished UTC day to a Parquet file in R2, reads the file
-- back and compares its row count with Postgres. Only a day recorded here as verified, and older
-- than 15 days, is then deleted from the big tables. Applied with `python -m src.db_migrate`.
--
-- Additive. The `pipeline` role is NOT given DELETE, so the hourly job can never remove data. A
-- second role, `archiver`, may read and delete but not insert into the data tables, and it is only
-- used by the daily archive job (`python -m src.db_migrate --create-archiver-role` gives it a login).
-- =====================================================================================

-- ----------------------------------------------------------------- archive_log
-- One row per (kind, day) that was copied to R2 and read back successfully.
--   kind : 'forecasts:open-meteo', 'forecasts:met-no' or 'observations'
--   day  : the UTC day: fetched_at for forecasts, observed_at for observations
--   rows : rows in the file = rows in Postgres at the time (for forecasts: rows of rain.forecast_hours)
--   uri  : s3://<bucket>/<key>; NULL when the day had no rows (nothing was uploaded)
CREATE TABLE rain.archive_log (
    kind         text        NOT NULL,
    day          date        NOT NULL,
    rows         integer     NOT NULL,
    uri          text,
    archived_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, day)
);


-- ----------------------------------------------------------------- the archiver role
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'archiver') THEN
        CREATE ROLE archiver NOLOGIN;
    END IF;
END $$;


-- ----------------------------------------------------------------- security (same pattern as 001_init.sql)
ALTER TABLE rain.archive_log ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON rain.archive_log FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON rain.archive_log FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON rain.archive_log FROM authenticated;
    END IF;
END $$;

-- The pipeline role may read the log (monitoring), nothing else.
GRANT SELECT ON rain.archive_log TO pipeline;
CREATE POLICY pipeline_read ON rain.archive_log FOR SELECT TO pipeline USING (true);

-- The archiver: read the data, delete forecast fetches, keep its own log.
GRANT USAGE ON SCHEMA rain TO archiver;
GRANT SELECT         ON rain.locations, rain.forecast_hours TO archiver;
GRANT SELECT, DELETE ON rain.fetches TO archiver;
GRANT SELECT, INSERT, UPDATE ON rain.archive_log TO archiver;

CREATE POLICY archiver_read   ON rain.locations   FOR SELECT TO archiver USING (true);
CREATE POLICY archiver_read   ON rain.fetches     FOR SELECT TO archiver USING (true);
CREATE POLICY archiver_delete ON rain.fetches     FOR DELETE TO archiver USING (true);
CREATE POLICY archiver_all    ON rain.archive_log FOR ALL    TO archiver USING (true) WITH CHECK (true);

-- Observations come from migration 004. Archiving them is optional, so this migration also works
-- on a database that does not have the table (the archive job skips observations then).
DO $$
BEGIN
    IF to_regclass('rain.observations') IS NOT NULL THEN
        GRANT SELECT, DELETE ON rain.observations TO archiver;
        CREATE POLICY archiver_read   ON rain.observations FOR SELECT TO archiver USING (true);
        CREATE POLICY archiver_delete ON rain.observations FOR DELETE TO archiver USING (true);
    END IF;
END $$;
