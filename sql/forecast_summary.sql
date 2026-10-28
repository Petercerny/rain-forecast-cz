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
--   rain.observation_stale_minutes  ignore a station reading older than this
--
-- It also reads the TEMP table location_stations(location, station_wsi, rain_station_wsi), which
-- src/transform.py fills from config/locations.yaml before running this file.
--
-- The script builds seven TEMP tables, in this order. Each step reads the one before:
--
--   latest_snapshot   newest forecast per (source, location)          <- start reading here
--   hourly            one row per location and hour, both sources side by side
--   daily             totals per location and (Prague) calendar day
--   peak_hours        the wettest hours per location
--   location_totals   one row per location: window totals + data freshness
--   sun_times         sunrise and sunset per location and day (Open-Meteo)
--   latest_obs        the newest MEASURED reading per location (ČHMÚ stations, see 004_observations.sql)
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
DROP TABLE IF EXISTS latest_snapshot, hourly, daily, peak_hours, location_totals, sun_times, latest_obs;


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
    h.temp_c::numeric      AS temp_c,
    -- Sky, light and wind: Open-Meteo only (NULL for MET Norway and for fetches from before
    -- these were collected). Wind is cast to numeric so it rounds like the other decimals.
    h.cloud_pct, h.cloud_low_pct, h.cloud_mid_pct, h.cloud_high_pct,
    h.is_day, h.sunshine_s, h.weather_code, h.visibility_m, h.humidity_pct,
    h.wind_ms::numeric     AS wind_ms,
    h.gust_ms::numeric     AS gust_ms,
    h.wind_dir_deg
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
    SELECT location, hour_start, precip_mm, precip_prob, temp_c,
           cloud_pct, cloud_low_pct, cloud_mid_pct, cloud_high_pct,
           is_day, sunshine_s, weather_code, visibility_m, humidity_pct,
           wind_ms, gust_ms, wind_dir_deg
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
        met.temp_c     AS met_temp_c,
        -- Open-Meteo only: cloud cover (total and per layer, %), 1 = daylight, seconds of sun in
        -- the hour, WMO weather code, visibility (m), humidity (%), wind and gusts (m/s), wind direction.
        om.cloud_pct      AS om_cloud_pct,
        om.cloud_low_pct  AS om_cloud_low_pct,
        om.cloud_mid_pct  AS om_cloud_mid_pct,
        om.cloud_high_pct AS om_cloud_high_pct,
        om.is_day         AS om_is_day,
        om.sunshine_s     AS om_sunshine_s,
        om.weather_code   AS om_weather_code,
        om.visibility_m   AS om_visibility_m,
        om.humidity_pct   AS om_humidity_pct,
        om.wind_ms        AS om_wind_ms,
        om.gust_ms        AS om_gust_ms,
        om.wind_dir_deg   AS om_wind_dir_deg
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
--    From Open-Meteo: average cloud cover, hours of sunshine, strongest wind and gust,
--    average humidity, poorest visibility and the worst weather of the day.
--
--    "Worst weather" = the highest WMO weather code of the day. That is a simple rule that works
--    because the codes grow roughly with how unpleasant they are: 0-3 clear to overcast, 45/48 fog,
--    51-57 drizzle, 61-67 rain, 71-77 snow, 80-86 showers, 95-99 thunderstorm.
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
    max(om_prob_pct)                   AS max_prob_pct,
    round(avg(om_cloud_pct))           AS cloud_avg_pct,
    round(sum(om_sunshine_s) / 3600.0, 1) AS sunshine_h,
    max(om_wind_ms)                    AS wind_max_ms,
    max(om_gust_ms)                    AS gust_max_ms,
    round(avg(om_humidity_pct))        AS humidity_avg_pct,
    min(om_visibility_m)               AS visibility_min_m,
    max(om_weather_code)               AS worst_weather_code
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


-- -------------------------------------------------------------------------------------
-- 6. sun_times
--    Sunrise and sunset per location and Prague calendar day, from the newest Open-Meteo fetch.
--    A fetch stores them as two small arrays (one element per UTC day); unnest() with several
--    arrays walks them side by side, so each output row is one day's sunrise AND sunset.
--    Fetches from before this was collected have NULL arrays and simply give no rows.
-- -------------------------------------------------------------------------------------
CREATE TEMP TABLE sun_times AS
SELECT
    l.name                                                           AS location,
    (t.sunrise AT TIME ZONE 'Europe/Prague')::date                   AS local_date,
    t.sunrise,
    t.sunset,
    to_char(t.sunrise AT TIME ZONE 'Europe/Prague', 'HH24:MI')       AS sunrise_local,
    to_char(t.sunset  AT TIME ZONE 'Europe/Prague', 'HH24:MI')       AS sunset_local
FROM (
    SELECT DISTINCT ON (location_slug) location_slug, sunrise, sunset
    FROM rain.fetches
    WHERE source = 'open-meteo'
      AND sunrise IS NOT NULL
      AND fetched_at >= current_setting('rain.now_utc')::timestamptz
                        - make_interval(hours => current_setting('rain.stale_after_hours')::int)
    ORDER BY location_slug, fetched_at DESC
) AS f
JOIN rain.locations AS l ON l.slug = f.location_slug
CROSS JOIN LATERAL unnest(f.sunrise, f.sunset) AS t (sunrise, sunset)
ORDER BY l.name, t.sunrise;


-- -------------------------------------------------------------------------------------
-- 7. latest_obs
--    What the weather stations measured most recently, one row per location (none when the
--    location has no station, or the station reported nothing newer than the stale limit).
--    The main station (temperature, wind, humidity, sunshine) is the newest reading that has a
--    temperature. Rain comes from the rain station when the location has one (`rain_station`),
--    else from the main station: the newest rain reading, and the sum over the hour up to it
--    (six 10-minute readings). ČHMÚ rewrites its files hourly, so the newest reading is normally
--    10 to 70 minutes old; the stale limit (settings.yaml) must stay above that.
--    Values are cast to numeric so they round like decimals instead of showing float noise.
-- -------------------------------------------------------------------------------------
CREATE TEMP TABLE latest_obs AS
WITH main AS (
    SELECT DISTINCT ON (ls.location)
           ls.location, ls.station_wsi, ls.rain_station_wsi, o.observed_at,
           o.temp_c, o.humidity_pct, o.pressure_hpa, o.wind_ms, o.gust_ms, o.wind_dir_deg, o.sunshine_s
    FROM location_stations AS ls
    JOIN rain.observations AS o ON o.station_wsi = ls.station_wsi
    WHERE o.temp_c IS NOT NULL
      AND o.observed_at <= current_setting('rain.now_utc')::timestamptz
      AND o.observed_at >= current_setting('rain.now_utc')::timestamptz
                           - make_interval(mins => current_setting('rain.observation_stale_minutes')::int)
    ORDER BY ls.location, o.observed_at DESC
),
rain AS (
    SELECT DISTINCT ON (m.location)
           m.location, o.station_wsi AS rain_wsi, o.observed_at AS rain_at, o.precip_10m_mm
    FROM main AS m
    JOIN rain.observations AS o ON o.station_wsi = coalesce(m.rain_station_wsi, m.station_wsi)
    WHERE o.precip_10m_mm IS NOT NULL
      AND o.observed_at <= current_setting('rain.now_utc')::timestamptz
      AND o.observed_at >= current_setting('rain.now_utc')::timestamptz
                           - make_interval(mins => current_setting('rain.observation_stale_minutes')::int)
    ORDER BY m.location, o.observed_at DESC
)
SELECT
    m.location,
    m.station_wsi,
    s.name                         AS station_name,
    m.observed_at,
    m.temp_c::numeric              AS temp_c,
    m.humidity_pct::numeric        AS humidity_pct,
    m.pressure_hpa::numeric        AS pressure_hpa,
    m.wind_ms::numeric             AS wind_ms,
    m.gust_ms::numeric             AS gust_ms,
    m.wind_dir_deg,
    m.sunshine_s::numeric          AS sunshine_s,
    r.precip_10m_mm::numeric       AS precip_10m_mm,
    (SELECT round(sum(o.precip_10m_mm)::numeric, 1)
       FROM rain.observations AS o
      WHERE o.station_wsi = r.rain_wsi
        AND o.observed_at >  r.rain_at - interval '60 minutes'
        AND o.observed_at <= r.rain_at)                          AS precip_1h_mm,
    r.rain_at                      AS rain_observed_at,
    CASE WHEN r.rain_wsi <> m.station_wsi THEN rs.name END       AS rain_station_name
FROM main AS m
JOIN rain.stations AS s ON s.wsi = m.station_wsi
LEFT JOIN rain AS r ON r.location = m.location
LEFT JOIN rain.stations AS rs ON rs.wsi = r.rain_wsi
ORDER BY m.location;
