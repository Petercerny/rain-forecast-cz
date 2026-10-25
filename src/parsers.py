"""Turn raw API responses into simple hourly rows.

Both APIs describe the weather differently, so each gets its own parser. Both return
the same row shape (a plain dict), which is what ends up in the Parquet snapshots:

    valid_time   when the source says the value is valid (UTC)
    hour_start   start of the hour the precipitation amount covers (UTC)
    precip_mm    precipitation in that hour (mm)
    precip_prob  chance of precipitation in % (Open-Meteo only, else None)
    temp_c       air temperature (deg C)

Why two time columns? The sources disagree about what a timestamp means:

  * Open-Meteo: `precipitation` at 14:00 is the sum of the PRECEDING hour (13:00-14:00).
  * MET Norway: `next_1_hours.precipitation_amount` at 14:00 is the FOLLOWING hour
    (14:00-15:00).

`hour_start` puts both on the same footing so they can be joined hour by hour.
Getting this wrong would shift one source by an hour against the other.
"""
from datetime import datetime, timedelta, timezone


def parse_open_meteo(payload: dict) -> list[dict]:
    """Parse an Open-Meteo /v1/forecast response requested with timezone=UTC."""
    hourly = payload["hourly"]
    rows = []
    for i, t in enumerate(hourly["time"]):
        # Open-Meteo times look like "2026-10-25T14:00" (no zone, because we asked for UTC).
        valid_time = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        precip = hourly["precipitation"][i]
        if precip is None:  # the API uses null when a value is missing
            continue
        rows.append(
            {
                "valid_time": valid_time,
                "hour_start": valid_time - timedelta(hours=1),  # value covers the preceding hour
                "precip_mm": float(precip),
                "precip_prob": _opt_int(hourly["precipitation_probability"][i]),
                "temp_c": _opt_float(hourly["temperature_2m"][i]),
            }
        )
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
