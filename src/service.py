"""What the web app serves: the JSON the pages draw, built from the database on request.

The old pipeline wrote this to site/data.json once an hour (transform -> summarize -> build_site). The app
now does the same steps per request and lets the CDN cache the answer for a few minutes, so a page never
waits on a rebuild and the database is hit at most once per cache period.
"""
import os
from datetime import datetime, timezone

from src.build_site import PRAGUE, build_data
from src.common import load_settings
from src.summarize import build_facts, summarize
from src.transform import build_forecast, run_sql


def summaries_for(forecast: dict) -> dict:
    """The template summary of every place (the local LLM is never used on the server)."""
    generated = datetime.fromisoformat(forecast["generated_at"].replace("Z", "+00:00"))
    today = generated.astimezone(PRAGUE).date()  # "today" is the Prague date, not the UTC one
    settings = load_settings()
    out = {}
    for loc in forecast["locations"]:
        if not loc["has_data"]:
            out[loc["name"]] = {"text": f"No forecast data is available for {loc['name']} right now.",
                                "generated_by": "template", "ai_generated": False}
            continue
        out[loc["name"]] = summarize(build_facts(loc, today), "template", settings)
    return out


def newest_fetch(forecast: dict) -> datetime | None:
    """The time of the newest forecast fetch behind the numbers (None if there is none)."""
    times = [t for loc in forecast["locations"] for t in loc["sources"].values() if t]
    return max(datetime.fromisoformat(t.replace("Z", "+00:00")) for t in times) if times else None


def build_payload(conn, now: datetime | None = None) -> dict:
    """The whole data.json for the pages, computed from the database right now.

    `generated_at` (what the pages show as "last updated" and compare with their stale limit) is the time of
    the newest fetch, NOT the time this answer was computed: a computed-on-request answer would otherwise
    always look fresh, even when the pipeline had stopped. `computed_at` is the moment of computing.
    """
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    run_sql(conn, now)
    forecast = build_forecast(conn, now)
    data = build_data(forecast, summaries_for(forecast), os.environ.get("CARTO_KEY", ""))
    newest = newest_fetch(forecast)
    data["computed_at"] = forecast["generated_at"]
    if newest is not None:
        data["generated_at"] = newest.strftime("%Y-%m-%dT%H:%M:%SZ")
        data["generated_at_local"] = newest.astimezone(PRAGUE).strftime("%d %b %Y, %H:%M %Z")
    return data
