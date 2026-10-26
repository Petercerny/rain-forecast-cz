-- =====================================================================================
-- forecast_summary.sql : turn the stored forecasts into the "next 48 hours" tables.
--
-- PostgreSQL. Run by src/transform.py against the `rain` schema (see
-- db/migrations/001_init.sql). Before running, Python sets these settings (read them in
-- SQL with current_setting('rain.name')):
--
--   rain.now_utc            "now" as a timestamp (a setting, so tests can pretend it is another time)
--   rain.window_hours       length of the forecast window, normally 48
--   rain.stale_after_hours  ignore a source whose newest fetch is older than this
--
-- The script builds five TEMP tables, in this order. Each step reads the one before:
--
--   latest_snapshot   newest forecast per (source, location)          <- start reading here
--   hourly            one row per location and hour, both sources side by side
--   daily             totals per location and (Prague) calendar day
--   peak_hours        the wettest hours per location
--   location_totals   one row per location: window totals + data freshness
--
-- Vocabulary:  om  = Open-Meteo,  met = MET Norway,  mm = millimetres of precipitation
--
-- If you know the old DuckDB version of this file, the differences are small:
--   getvariable('x')            ->  current_setting('rain.x')
--   to_hours(n)                 ->  make_interval(hours => n)
--   strftime(ts, '%H:%M')       ->  to_char(ts, 'HH24:MI')
--   QUALIFY rank <= 3           ->  the window function moves into a sub-select, WHERE rank <= 3 outside
--   round(real, 1)              ->  needs numeric, so values are cast with ::numeric
-- =====================================================================================

-- All timestamps are stored in UTC. Make date_trunc() and friends work in UTC too,
-- otherwise "the start of the hour" would depend on the database server's time zone.
SET TIME ZONE 'UTC';

-- Re-running the script in the same session must start clean.
DROP TABLE IF EXISTS latest_snapshot, hourly, daily, peak_hours, location_totals;


-- -------------------------------------------------------------------------------------
-- 1. latest_snapshot
--    Every ingest run adds a new fetch, so the same hour is forecast many times.
--    For the website we want only the NEWEST forecast of each source for each location.
--    (The older ones stay in the database: they are the history for scoring forecasts later.)
-- -------------------------------------------------------------------------------------
CREATE TEMP TABLE latest_snapshot AS
WITH newest AS (
    -- DISTINCT ON keeps the first row of each (source, location) group, so with
    -- ORDER BY ... fetched_at DESC that is the newest fetch.
    SELECT DISTINCT ON (source, location_slug)
           fetch_id, source, location_slug, fetched_at
    FROM rain.fetches
    -- A source that has been failing for a while must not show old numbers as if fresh.
    WHERE fetched_at >= current_setting('rain.now_utc')::timestamptz
                        - make_interval(hours => current_setting('rain.stale_after_hours')::int)
    ORDER BY source, location_slug, fetched_at DESC
)
-- rain.forecast_hours unpacks the arrays of a fetch into one row per forecast hour.
-- Values are cast to numeric so sums and rounding behave like decimal numbers.
SELECT
    n.source,
    l.name                 AS location,
    n.fetched_at,
    h.hour_start,
    h.precip_mm::numeric   AS precip_mm,
    h.precip_prob,
    h.temp_c::numeric      AS temp_c
FROM newest AS n
JOIN rain.locations      AS l ON l.slug = n.location_slug
JOIN rain.forecast_hours AS h USING (fetch_id);


-- -------------------------------------------------------------------------------------
-- 2. hourly
--    One row per location and hour of the window, with both sources side by side.
--    FULL OUTER JOIN = keep an hour even if only ONE source has it (the other side is
--    NULL). That is how one broken API does not break the page.
--
--    Join key is hour_start (the hour the rain covers), NOT the source's own timestamp:
--    the two APIs label their hours differently, see src/parsers.py.
-- -------------------------------------------------------------------------------------
CREATE TEMP TABLE hourly AS
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
    -- (timestamptz AT TIME ZONE 'x' gives the local wall-clock time in x.)
    (hour_start AT TIME ZONE 'Europe/Prague')::date                  AS local_date,
    to_char(hour_start AT TIME ZONE 'Europe/Prague', 'HH24:MI')      AS local_time
FROM joined
-- Window: from the start of the current hour, window_hours long.
WHERE hour_start >= date_trunc('hour', current_setting('rain.now_utc')::timestamptz)
  AND hour_start <  date_trunc('hour', current_setting('rain.now_utc')::timestamptz)
                    + make_interval(hours => current_setting('rain.window_hours')::int);


-- -------------------------------------------------------------------------------------
-- 3. daily
--    Total expected rain per location and Prague calendar day.
--    The window is 48 hours from "now", so it usually touches 3 calendar days and the
--    first and last are partial: hours_in_window says how many hours each total covers.
--    sum() skips NULLs, so a day with no data from a source shows NULL, not 0.
--    Also per day: lowest / highest temperature of each source, the highest rain chance,
--    and the highest / lowest of the two-source average (used by the map markers).
-- -------------------------------------------------------------------------------------
CREATE TEMP TABLE daily AS
SELECT
    location,
    local_date,
    count(*)                           AS hours_in_window,
    round(sum(om_mm), 1)               AS om_mm,
    round(sum(met_mm), 1)              AS met_mm,
    round(sum(om_mm) - sum(met_mm), 1) AS diff_mm,   -- positive = Open-Meteo wetter
    min(om_temp_c)                     AS om_tmin_c,
    max(om_temp_c)                     AS om_tmax_c,
    min(met_temp_c)                    AS met_tmin_c,
    max(met_temp_c)                    AS met_tmax_c,
    min(avg_temp_c)                    AS avg_tmin_c,
    max(avg_temp_c)                    AS avg_tmax_c,
    max(om_prob_pct)                   AS max_prob_pct
FROM hourly
GROUP BY location, local_date
ORDER BY location, local_date;


-- -------------------------------------------------------------------------------------
-- 4. peak_hours
--    The 3 wettest hours per location. "Wettest" = the larger of the two sources, so an
--    hour counts even if only one source expects heavy rain there.
--    greatest() ignores NULLs in PostgreSQL, so a missing source does not hide the other.
--
--    Postgres has no QUALIFY: rank the hours in a sub-select, then filter outside it.
-- -------------------------------------------------------------------------------------
CREATE TEMP TABLE peak_hours AS
-- Ranking uses the exact values; the amounts are rounded to 1 decimal only for display.
SELECT location, rank, hour_start, local_date, local_time,
       round(om_mm, 1) AS om_mm, round(met_mm, 1) AS met_mm, round(peak_mm, 1) AS peak_mm
FROM (
    SELECT
        location,
        row_number() OVER (PARTITION BY location
                           ORDER BY greatest(om_mm, met_mm) DESC, hour_start) AS rank,
        hour_start,
        local_date,
        local_time,
        om_mm,
        met_mm,
        greatest(om_mm, met_mm) AS peak_mm
    FROM hourly
    WHERE greatest(om_mm, met_mm) > 0            -- a dry hour is never a "peak"
) AS ranked
WHERE rank <= 3
ORDER BY location, rank;


-- -------------------------------------------------------------------------------------
-- 5. location_totals
--    One row per location: totals over the whole window, the highest rain probability,
--    the temperature range (of the two-source average), and when each source was last
--    fetched (NULL = no usable data from that source).
-- -------------------------------------------------------------------------------------
CREATE TEMP TABLE location_totals AS
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
