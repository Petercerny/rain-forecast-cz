-- =====================================================================================
-- 004_observations.sql : what the weather stations actually measured.
--
-- Forecasts say what WILL happen; ČHMÚ's automatic stations say what IS happening. Every 10
-- minutes each station reports temperature, humidity, pressure, wind, rain and sunshine, and
-- src/observe.py copies those readings here. They feed the "Now" view of the site and (later) the
-- scoring of forecasts against reality. Applied with `python -m src.db_migrate`.
--
-- Additive: no existing table changes. The `pipeline` role may read, add and correct rows, but
-- not delete them (same rule as the forecast tables).
-- =====================================================================================

-- ----------------------------------------------------------------- stations
-- One row per station. `wsi` is the WIGOS Station Identifier ČHMÚ uses in its file names,
-- for example 0-20000-0-11518 (Praha, Ruzyně).
CREATE TABLE rain.stations (
    wsi        text             PRIMARY KEY,
    name       text             NOT NULL,
    lat        double precision NOT NULL,
    lon        double precision NOT NULL,
    elevation  real                       -- metres above sea level
);


-- ----------------------------------------------------------------- observations
-- One row per station and 10-minute reading. `observed_at` is the timestamp ČHMÚ puts on the
-- reading (UTC; the interval ends at that time, the docs do not say more). Any value may be NULL: a sensor can be missing or a value flagged as
-- unusable, and not every station measures everything (sunshine and rain especially).
CREATE TABLE rain.observations (
    station_wsi    text             NOT NULL REFERENCES rain.stations (wsi),
    observed_at    timestamptz      NOT NULL,
    temp_c         real,                  -- air temperature, deg C                (ČHMÚ element T)
    humidity_pct   real,                  -- relative humidity, %                  (H)
    pressure_hpa   real,                  -- air pressure at the station, hPa      (P)
    wind_ms        real,                  -- wind speed, m/s                       (F)
    gust_ms        real,                  -- strongest gust in the 10 minutes, m/s (Fmax)
    wind_dir_deg   smallint,              -- direction the wind blows FROM, degrees (D)
    precip_10m_mm  real,                  -- rain in the 10 minutes, mm            (SRA10M)
    sunshine_s     real,                  -- seconds of sunshine in the 10 minutes, 0..600 (SSV10M)
    PRIMARY KEY (station_wsi, observed_at)
);


-- ----------------------------------------------------------------- observation_sources
-- Conditional download state, one row per file URL: the server's Last-Modified / ETag from the last
-- time we got it, so the next poll can ask "anything new?" and get a cheap 304.
CREATE TABLE rain.observation_sources (
    url            text        PRIMARY KEY,
    last_modified  text,
    etag           text,
    checked_at     timestamptz NOT NULL DEFAULT now()
);


-- ----------------------------------------------------------------- security (same pattern as 001_init.sql)
ALTER TABLE rain.stations             ENABLE ROW LEVEL SECURITY;
ALTER TABLE rain.observations         ENABLE ROW LEVEL SECURITY;
ALTER TABLE rain.observation_sources  ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON rain.stations, rain.observations, rain.observation_sources FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON rain.stations, rain.observations, rain.observation_sources FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON rain.stations, rain.observations, rain.observation_sources FROM authenticated;
    END IF;
END $$;

-- The pipeline role: read, add, and correct (a station file is rewritten every hour and a value can change
-- quality). No DELETE, no DDL.
GRANT SELECT, INSERT, UPDATE ON rain.stations, rain.observations, rain.observation_sources TO pipeline;

CREATE POLICY pipeline_all ON rain.stations            FOR ALL TO pipeline USING (true) WITH CHECK (true);
CREATE POLICY pipeline_all ON rain.observations        FOR ALL TO pipeline USING (true) WITH CHECK (true);
CREATE POLICY pipeline_all ON rain.observation_sources FOR ALL TO pipeline USING (true) WITH CHECK (true);
