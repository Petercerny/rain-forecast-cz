"""Turn raw API responses into simple hourly rows.

Both APIs describe the weather differently, so each gets its own parser. Both return
the same row shape (a plain dict), which is what ends up in the Parquet snapshots:

    valid_time   when the source says the value is valid (UTC)
    hour_start   start of the hour the precipitation amount covers (UTC)
    precip_mm    precipitation in that hour (mm)
    precip_prob  chance of precipitation in % (Open-Meteo only, else None)
    temp_c       air temperature (deg C)

Open-Meteo rows also carry the "sky and wind" values below (None for MET Norway, which we only
use for rain and temperature):

    cloud_pct, cloud_low_pct, cloud_mid_pct, cloud_high_pct   cloud cover in %, total and per layer
    is_day           1 = sun is up, 0 = night
    sunshine_s       seconds of sunshine in the hour (0 to 3600)
    weather_code     WMO code: 0 clear, 1-3 clouds, 45/48 fog, 5x drizzle, 6x rain, 7x snow, 8x showers, 9x thunder
    visibility_m     metres
    humidity_pct     relative humidity in %
    wind_ms, gust_ms wind speed and gusts in m/s
    wind_dir_deg     direction the wind blows FROM, degrees
    sunrise, sunset  that UTC day's sunrise and sunset (repeated on every hour of the day)

These values are "instant" values at the row's `valid_time`, like temperature, so for Open-Meteo they
describe the END of the `hour_start` hour (at most one hour off, which does not matter for a forecast
of clouds and wind). `sunshine_s`, like precipitation, is the sum of the preceding hour.

Why two time columns? The sources disagree about what a timestamp means:

  * Open-Meteo: `precipitation` at 14:00 is the sum of the PRECEDING hour (13:00-14:00).
  * MET Norway: `next_1_hours.precipitation_amount` at 14:00 is the FOLLOWING hour
    (14:00-15:00).

`hour_start` puts both on the same footing so they can be joined hour by hour.
Getting this wrong would shift one source by an hour against the other.
"""
from datetime import datetime, timedelta, timezone


# Open-Meteo variable name -> (row key, converter). They are optional in the response: a payload
# requested with fewer variables (or an older saved one) just gives None for the missing ones.
OPEN_METEO_EXTRAS = {
    "cloud_cover": ("cloud_pct", int),
    "cloud_cover_low": ("cloud_low_pct", int),
    "cloud_cover_mid": ("cloud_mid_pct", int),
    "cloud_cover_high": ("cloud_high_pct", int),
    "is_day": ("is_day", int),
    "sunshine_duration": ("sunshine_s", lambda v: int(round(v))),
    "weather_code": ("weather_code", int),
    "visibility": ("visibility_m", lambda v: int(round(v))),
    "relative_humidity_2m": ("humidity_pct", int),
    "wind_speed_10m": ("wind_ms", float),
    "wind_gusts_10m": ("gust_ms", float),
    "wind_direction_10m": ("wind_dir_deg", int),
}


def _utc(text: str) -> datetime:
    """'2026-10-25T14:00' (no zone, because we asked for UTC) -> aware datetime."""
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def parse_open_meteo(payload: dict) -> list[dict]:
    """Parse an Open-Meteo /v1/forecast response requested with timezone=UTC."""
    hourly = payload["hourly"]
    daily = payload.get("daily") or {}
    # {"2026-10-25": (sunrise, sunset)} so every hour can carry its own day's sun times.
    sun = {d: (_utc(rise) if rise else None, _utc(sset) if sset else None)
           for d, rise, sset in zip(daily.get("time", []), daily.get("sunrise", []), daily.get("sunset", []))}
    rows = []
    for i, t in enumerate(hourly["time"]):
        # Open-Meteo times look like "2026-10-25T14:00" (no zone, because we asked for UTC).
        valid_time = _utc(t)
        precip = hourly["precipitation"][i]
        if precip is None:  # the API uses null when a value is missing
            continue
        row = {
            "valid_time": valid_time,
            "hour_start": valid_time - timedelta(hours=1),  # value covers the preceding hour
            "precip_mm": float(precip),
            "precip_prob": _opt_int(hourly["precipitation_probability"][i]),
            "temp_c": _opt_float(hourly["temperature_2m"][i]),
        }
        for api_name, (key, convert) in OPEN_METEO_EXTRAS.items():
            values = hourly.get(api_name)
            row[key] = None if values is None or values[i] is None else convert(values[i])
        row["sunrise"], row["sunset"] = sun.get(valid_time.date().isoformat(), (None, None))
        rows.append(row)
    return rows


def parse_met_no(payload: dict) -> list[dict]:
    """Parse a MET Norway Locationforecast 2.0 /compact response."""
    rows = []
    for step in payload["properties"]["timeseries"]:
        # MET times look like "2026-10-25T14:00:00Z".
        valid_time = datetime.fromisoformat(step["time"].replace("Z", "+00:00"))
        data = step["data"]
        next_1h = data.get("next_1_hours")
        # Further out than ~2.5 days MET only gives next_6_hours blocks. We only want
        # hourly values, so those steps are skipped.
        if next_1h is None:
            continue
        rows.append(
            {
                "valid_time": valid_time,
                "hour_start": valid_time,  # next_1_hours covers the following hour
                "precip_mm": float(next_1h["details"]["precipitation_amount"]),
                "precip_prob": None,  # compact format has no probability
                "temp_c": _opt_float(data["instant"]["details"].get("air_temperature")),
            }
        )
    return rows


def met_updated_at(payload: dict) -> datetime | None:
    """When MET says the forecast model run was updated (properties.meta.updated_at)."""
    value = payload.get("properties", {}).get("meta", {}).get("updated_at")
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _opt_float(value):
    return None if value is None else float(value)


def _opt_int(value):
    return None if value is None else int(value)
