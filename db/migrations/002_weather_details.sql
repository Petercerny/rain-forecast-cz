-- =====================================================================================
-- 002_weather_details.sql : clouds, light, wind and weather type (Open-Meteo only).
--
-- Adds more hourly arrays to rain.fetches, in the same layout as precip_mm and temp_c:
-- element 1 is first_hour, element 2 one hour later, and so on. All of them are NULLable:
--   * every MET Norway fetch has them NULL (we only use MET for rain and temperature),
--   * every fetch stored before this migration has them NULL (there is no history to fill in).
-- See src/parsers.py for what each value means. Applied with `python -m src.db_migrate`.
-- =====================================================================================

ALTER TABLE rain.fetches
    ADD COLUMN cloud_pct       smallint[],   -- total cloud cover, %
    ADD COLUMN cloud_low_pct   smallint[],
    ADD COLUMN cloud_mid_pct   smallint[],
    ADD COLUMN cloud_high_pct  smallint[],
    ADD COLUMN is_day          smallint[],   -- 1 sun is up, 0 night
    ADD COLUMN sunshine_s      smallint[],   -- seconds of sunshine in the hour, 0..3600
    ADD COLUMN weather_code    smallint[],   -- WMO code (0 clear ... 95-99 thunderstorm)
    ADD COLUMN visibility_m    integer[],
    ADD COLUMN humidity_pct    smallint[],
    ADD COLUMN wind_ms         real[],       -- m/s
    ADD COLUMN gust_ms         real[],       -- m/s
    ADD COLUMN wind_dir_deg    smallint[],   -- direction the wind blows FROM
    -- One element per UTC day (about 3), not per hour.
    ADD COLUMN sunrise         timestamptz[],
    ADD COLUMN sunset          timestamptz[];

-- Every hourly array that is present must line up with precip_mm, hour by hour.
-- (A NULL array is fine: coalesce(cardinality(NULL), n) = n.)
ALTER TABLE rain.fetches ADD CONSTRAINT fetches_extra_arrays_same_length CHECK (
    coalesce(cardinality(cloud_pct),      cardinality(precip_mm)) = cardinality(precip_mm)
    AND coalesce(cardinality(cloud_low_pct),  cardinality(precip_mm)) = cardinality(precip_mm)
    AND coalesce(cardinality(cloud_mid_pct),  cardinality(precip_mm)) = cardinality(precip_mm)
    AND coalesce(cardinality(cloud_high_pct), cardinality(precip_mm)) = cardinality(precip_mm)
    AND coalesce(cardinality(is_day),         cardinality(precip_mm)) = cardinality(precip_mm)
    AND coalesce(cardinality(sunshine_s),     cardinality(precip_mm)) = cardinality(precip_mm)
    AND coalesce(cardinality(weather_code),   cardinality(precip_mm)) = cardinality(precip_mm)
    AND coalesce(cardinality(visibility_m),   cardinality(precip_mm)) = cardinality(precip_mm)
    AND coalesce(cardinality(humidity_pct),   cardinality(precip_mm)) = cardinality(precip_mm)
    AND coalesce(cardinality(wind_ms),        cardinality(precip_mm)) = cardinality(precip_mm)
    AND coalesce(cardinality(gust_ms),        cardinality(precip_mm)) = cardinality(precip_mm)
    AND coalesce(cardinality(wind_dir_deg),   cardinality(precip_mm)) = cardinality(precip_mm)
    AND coalesce(cardinality(sunrise), cardinality(sunset)) = cardinality(sunset)
);

-- The one-row-per-forecast-hour view now also unpacks the new arrays. CREATE OR REPLACE VIEW may
-- only ADD columns at the end, so the existing columns keep their order. A NULL array is treated as
-- empty by unnest(), and unnest() pads shorter arrays with NULLs, so old rows still appear (with NULLs).
CREATE OR REPLACE VIEW rain.forecast_hours AS
SELECT
    f.fetch_id,
    f.source,
    f.location_slug,
    f.fetched_at,
    f.source_updated_at,
    f.first_hour + (t.i - 1) * interval '1 hour' AS hour_start,
    t.precip_mm,
    t.precip_prob,
    t.temp_c,
    t.cloud_pct,
    t.cloud_low_pct,
    t.cloud_mid_pct,
    t.cloud_high_pct,
    t.is_day,
    t.sunshine_s,
    t.weather_code,
    t.visibility_m,
    t.humidity_pct,
    t.wind_ms,
    t.gust_ms,
    t.wind_dir_deg
FROM rain.fetches AS f
CROSS JOIN LATERAL unnest(
        f.precip_mm, f.precip_prob, f.temp_c,
        f.cloud_pct, f.cloud_low_pct, f.cloud_mid_pct, f.cloud_high_pct,
        f.is_day, f.sunshine_s, f.weather_code, f.visibility_m, f.humidity_pct,
        f.wind_ms, f.gust_ms, f.wind_dir_deg)
     WITH ORDINALITY AS t (precip_mm, precip_prob, temp_c,
        cloud_pct, cloud_low_pct, cloud_mid_pct, cloud_high_pct,
        is_day, sunshine_s, weather_code, visibility_m, humidity_pct,
        wind_ms, gust_ms, wind_dir_deg, i)
WHERE t.precip_mm IS NOT NULL;   -- NULL = a gap in the forecast, not a dry hour
