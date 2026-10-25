-- =====================================================================================
-- forecast_summary.sql : turn raw forecast snapshots into the "next 48 hours" tables.
--
-- Run by src/transform.py. Before running, Python sets these DuckDB variables
-- (read them with getvariable('name')):
--
--   snapshot_glob     where the Parquet snapshots are, e.g. 'data/snapshots/*/*/*.parquet'
--   now_utc           "now" as a timestamp (a variable, so tests can pretend it is another time)
--   window_hours      length of the forecast window, normally 48
--   stale_after_hours ignore a source whose newest snapshot is older than this
--
-- The script builds four TEMP tables, in this order. Each step reads the one before:
--
--   latest_snapshot   newest forecast per (source, location)          <- start reading here
--   hourly            one row per location and hour, both sources side by side
--   daily             rain totals per location and (Prague) calendar day
--   peak_hours        the wettest hours per location
--   location_totals   one row per location: window totals + data freshness
--
-- Vocabulary:  om  = Open-Meteo,  met = MET Norway,  mm = millimetres of precipitation
-- =====================================================================================

-- All timestamps are stored in UTC. Make DuckDB *display* and truncate them in UTC too,
-- otherwise date_trunc('hour', ...) would depend on the computer's time zone.
SET TimeZone = 'UTC';


-- -------------------------------------------------------------------------------------
-- 1. latest_snapshot
--    Every ingest run adds a new snapshot, so the same hour is forecast many times.
--    For the website we want only the NEWEST forecast of each source for each location.
--    (The older ones stay on disk: they are the history for scoring forecasts later.)
-- -------------------------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE latest_snapshot AS
WITH all_snapshots AS (
    -- hive_partitioning turns the folder names source=.../date=... into columns.
    SELECT *
    FROM read_parquet(getvariable('snapshot_glob'), hive_partitioning = true)
),
newest AS (
    SELECT source, location, max(fetched_at) AS fetched_at
    FROM all_snapshots
    GROUP BY source, location
)
SELECT s.*
FROM all_snapshots AS s
JOIN newest AS n USING (source, location, fetched_at)   -- keeps only the newest fetch
-- A source that has been failing for a while must not show old numbers as if fresh.
WHERE n.fetched_at >= getvariable('now_utc') - to_hours(getvariable('stale_after_hours')::INTEGER);


-- -------------------------------------------------------------------------------------
-- 2. hourly
--    One row per location and hour of the window, with both sources side by side.
--    FULL OUTER JOIN = keep an hour even if only ONE source has it (the other side is
--    NULL). That is how one broken API does not break the page.
--
--    Join key is hour_start (the hour the rain covers), NOT valid_time: the two APIs
--    label their hours differently, see src/parsers.py.
-- -------------------------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE hourly AS
WITH om AS (
    SELECT location, hour_start, precip_mm, precip_prob, temp_c
    FROM latest_snapshot WHERE source = 'open-meteo'
),
met AS (
    SELECT location, hour_start, precip_mm, temp_c
    FROM latest_snapshot WHERE source = 'met-no'
),
joined AS (
    SELECT
        coalesce(om.location,   met.location)   AS location,
        coalesce(om.hour_start, met.hour_start) AS hour_start,
        om.precip_mm   AS om_mm,
        met.precip_mm  AS met_mm,
        om.precip_prob AS om_prob_pct,          -- chance of rain, Open-Meteo only
        om.temp_c      AS om_temp_c,
        met.temp_c     AS met_temp_c
    FROM om
    FULL OUTER JOIN met
        ON om.location = met.location AND om.hour_start = met.hour_start
)
SELECT
    *,
    -- Difference between the sources. Positive = Open-Meteo expects MORE rain.
    -- NULL when one of the sources is missing for that hour.
    round(om_mm - met_mm, 2) AS diff_mm,
    -- One temperature for the map: the average of both sources. If only one source has
    -- the hour, use that one (a + b is NULL when either is NULL, so coalesce falls through).
    coalesce(round((om_temp_c + met_temp_c) / 2, 1), om_temp_c, met_temp_c) AS avg_temp_c,
    -- The Prague-local calendar day and clock hour, for "today / tomorrow" style output.
    CAST(hour_start AT TIME ZONE 'Europe/Prague' AS DATE)          AS local_date,
    strftime(hour_start AT TIME ZONE 'Europe/Prague', '%H:%M')     AS local_time
FROM joined
-- Window: from the start of the current hour, window_hours long.
WHERE hour_start >= date_trunc('hour', getvariable('now_utc'))
  AND hour_start <  date_trunc('hour', getvariable('now_utc')) + to_hours(getvariable('window_hours')::INTEGER);


-- -------------------------------------------------------------------------------------
-- 3. daily
--    Total expected rain per location and Prague calendar day.
--    The window is 48 hours from "now", so it usually touches 3 calendar days and the
--    first and last are partial: hours_in_window says how many hours each total covers.
--    sum() skips NULLs, so a day with no data from a source shows NULL, not 0.
--    Also per day: lowest / highest temperature of each source, the highest rain chance,
--    and the highest / lowest of the two-source average (used by the map markers).
-- -------------------------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE daily AS
SELECT
    location,
    local_date,
    count(*)                        AS hours_in_window,
    round(sum(om_mm), 1)            AS om_mm,
    round(sum(met_mm), 1)           AS met_mm,
    round(sum(om_mm) - sum(met_mm), 1) AS diff_mm,   -- positive = Open-Meteo wetter
    min(om_temp_c)                  AS om_tmin_c,
    max(om_temp_c)                  AS om_tmax_c,
    min(met_temp_c)                 AS met_tmin_c,
    max(met_temp_c)                 AS met_tmax_c,
    min(avg_temp_c)                 AS avg_tmin_c,
    max(avg_temp_c)                 AS avg_tmax_c,
    max(om_prob_pct)                AS max_prob_pct
FROM hourly
GROUP BY location, local_date
ORDER BY location, local_date;


-- -------------------------------------------------------------------------------------
-- 4. peak_hours
--    The 3 wettest hours per location. "Wettest" = the larger of the two sources, so an
--    hour counts even if only one source expects heavy rain there.
--    greatest() ignores NULLs in DuckDB, so a missing source does not hide the other.
-- -------------------------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE peak_hours AS
SELECT
    location,
    row_number() OVER (PARTITION BY location ORDER BY greatest(om_mm, met_mm) DESC, hour_start) AS rank,
    hour_start,
    local_date,
    local_time,
    om_mm,
    met_mm,
    greatest(om_mm, met_mm) AS peak_mm
FROM hourly
WHERE greatest(om_mm, met_mm) > 0                -- a dry hour is never a "peak"
QUALIFY rank <= 3                                -- QUALIFY = WHERE for window functions
ORDER BY location, rank;


-- -------------------------------------------------------------------------------------
-- 5. location_totals
--    One row per location: totals over the whole window, the highest rain probability,
--    the temperature range (of the two-source average), and when each source was last
--    fetched (NULL = no usable data from that source).
-- -------------------------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE location_totals AS
SELECT
    h.location,
    count(*)                              AS hours,
    round(sum(h.om_mm), 1)                AS om_total_mm,
    round(sum(h.met_mm), 1)               AS met_total_mm,
    round(sum(h.om_mm) - sum(h.met_mm), 1) AS diff_total_mm,
    max(h.om_prob_pct)                    AS max_prob_pct,
    min(h.avg_temp_c)                     AS temp_min_c,
    max(h.avg_temp_c)                     AS temp_max_c,
    (SELECT max(fetched_at) FROM latest_snapshot s
      WHERE s.location = h.location AND s.source = 'open-meteo') AS om_fetched_at,
    (SELECT max(fetched_at) FROM latest_snapshot s
      WHERE s.location = h.location AND s.source = 'met-no')     AS met_fetched_at
FROM hourly AS h
GROUP BY h.location
ORDER BY h.location;
