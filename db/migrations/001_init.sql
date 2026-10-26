-- =====================================================================================
-- 001_init.sql : the forecast history database.
--
-- Applied once by `python -m src.db_migrate` (run by you locally with the admin
-- connection, never by CI). Everything lives in the schema `rain`, which is NOT exposed
-- through Supabase's REST API, and every table has row level security switched on.
--
-- The idea: every time we call a forecast API we store ONE row (a "fetch") holding the
-- whole 48-72 hour forecast as arrays. That is ~25x fewer rows than one row per forecast
-- hour, which is what keeps the history inside the 500 MB free tier for ~2 years.
-- The view rain.forecast_hours turns it back into one row per forecast hour, so you can
-- query it like a normal table.
-- =====================================================================================

CREATE SCHEMA IF NOT EXISTS rain;

-- The role the GitHub Actions pipeline logs in as. NOLOGIN here: `db_migrate
-- --create-pipeline-role` gives it a login password. It may read and add data, but it can
-- not change the table structure.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pipeline') THEN
        CREATE ROLE pipeline NOLOGIN;
    END IF;
END $$;


-- ----------------------------------------------------------------- locations
-- One row per place. `slug` is the stable ASCII id (also the page address on the site).
CREATE TABLE rain.locations (
    slug  text             PRIMARY KEY,
    name  text             NOT NULL,
    lat   double precision NOT NULL,
    lon   double precision NOT NULL
);


-- ----------------------------------------------------------------- fetches
-- One row per (source, location, moment we downloaded it).
--
-- The three arrays are hourly values, element 1 is `first_hour`, element 2 is one hour
-- later, and so on. A missing hour is stored as NULL so the position always equals the
-- hour offset. `first_hour` is the START of the hour the precipitation covers (see
-- src/parsers.py for why that matters).
CREATE TABLE rain.fetches (
    fetch_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source            text        NOT NULL CHECK (source IN ('open-meteo', 'met-no')),
    location_slug     text        NOT NULL REFERENCES rain.locations (slug),
    fetched_at        timestamptz NOT NULL,   -- when WE downloaded it
    source_updated_at timestamptz,            -- model run time, if the API tells us
    first_hour        timestamptz NOT NULL,
    precip_mm         real[]      NOT NULL,
    precip_prob       smallint[]  NOT NULL,   -- % ; all NULL for MET Norway
    temp_c            real[]      NOT NULL,

    -- Loading the same fetch twice must be impossible: this is what makes load_db safe to repeat.
    UNIQUE (source, location_slug, fetched_at),
    -- The three arrays must line up hour by hour.
    CONSTRAINT fetches_arrays_same_length CHECK (
        cardinality(precip_mm) = cardinality(precip_prob)
        AND cardinality(precip_mm) = cardinality(temp_c)
    )
);


-- ----------------------------------------------------------------- ingest_runs
-- One row per source per pipeline run: how much was loaded, how many places failed.
CREATE TABLE rain.ingest_runs (
    run_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recorded_at   timestamptz NOT NULL DEFAULT now(),
    fetched_at    timestamptz,
    source        text        NOT NULL,
    rows_loaded   integer     NOT NULL,   -- hourly rows in the Parquet file for this run
    failures      integer     NOT NULL,   -- locations that failed to download
    github_run_url text,
    note          text
);


-- ----------------------------------------------------------------- forecast_hours (view)
-- One row per forecast hour, like the old Parquet snapshots. unnest() with several arrays
-- walks them in parallel, WITH ORDINALITY adds the position i = 1, 2, 3...
CREATE VIEW rain.forecast_hours AS
SELECT
    f.fetch_id,
    f.source,
    f.location_slug,
    f.fetched_at,
    f.source_updated_at,
    f.first_hour + (t.i - 1) * interval '1 hour' AS hour_start,
    t.precip_mm,
    t.precip_prob,
    t.temp_c
FROM rain.fetches AS f
CROSS JOIN LATERAL unnest(f.precip_mm, f.precip_prob, f.temp_c)
     WITH ORDINALITY AS t (precip_mm, precip_prob, temp_c, i)
WHERE t.precip_mm IS NOT NULL;   -- NULL = a gap in the forecast, not a dry hour


-- ----------------------------------------------------------------- security
-- 1. Row level security on, so that a role without an explicit policy sees nothing.
ALTER TABLE rain.locations   ENABLE ROW LEVEL SECURITY;
ALTER TABLE rain.fetches     ENABLE ROW LEVEL SECURITY;
ALTER TABLE rain.ingest_runs ENABLE ROW LEVEL SECURITY;

-- 2. Nobody but the owner gets anything by default.
REVOKE ALL ON SCHEMA rain FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA rain FROM PUBLIC;

-- Supabase's API roles must never touch this schema (these roles only exist on Supabase).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON SCHEMA rain FROM anon;
        REVOKE ALL ON ALL TABLES IN SCHEMA rain FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON SCHEMA rain FROM authenticated;
        REVOKE ALL ON ALL TABLES IN SCHEMA rain FROM authenticated;
    END IF;
END $$;

-- 3. The pipeline role: read everything, add rows, update locations. No DELETE, no DDL.
GRANT USAGE ON SCHEMA rain TO pipeline;
GRANT SELECT, INSERT, UPDATE ON rain.locations TO pipeline;
GRANT SELECT, INSERT         ON rain.fetches, rain.ingest_runs TO pipeline;
GRANT SELECT                 ON rain.forecast_hours TO pipeline;

-- Row level security is on, so the pipeline needs explicit policies ("this role may see/add any row").
CREATE POLICY pipeline_all ON rain.locations   FOR ALL    TO pipeline USING (true) WITH CHECK (true);
CREATE POLICY pipeline_all ON rain.fetches     FOR ALL    TO pipeline USING (true) WITH CHECK (true);
CREATE POLICY pipeline_all ON rain.ingest_runs FOR ALL    TO pipeline USING (true) WITH CHECK (true);
