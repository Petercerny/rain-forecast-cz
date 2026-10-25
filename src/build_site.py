"""Step 4: assemble the static site in site/.

    web/index.html, style.css, app.js   hand-written page (copied, {{REPO_URL}} filled in)
    build/forecast.json + summaries.json  -> site/data.json   (what the page draws)

Usage:  python -m src.build_site
"""
import json
import os
import shutil
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from src.common import BUILD_DIR, SITE_DIR, WEB_DIR

PRAGUE = ZoneInfo("Europe/Prague")
DEFAULT_REPO_URL = "https://github.com/YOUR-USER/YOUR-REPO"


def build_data(forecast: dict, summaries: dict) -> dict:
    """Merge forecast + summaries into the JSON file the page loads."""
    generated = datetime.fromisoformat(forecast["generated_at"].replace("Z", "+00:00"))
    locations = []
    for loc in forecast["locations"]:
        s = summaries.get(loc["name"], {})
        locations.append(
            {
                "name": loc["name"],
                "summary": {
                    "text": s.get("text", ""),
                    # The page shows an "AI-generated" label only when this is true.
                    "ai_generated": bool(s.get("ai_generated")),
                    "generated_by": s.get("generated_by", "template"),
                },
                "sources": loc["sources"],
                "totals": loc["totals"],
                "daily": loc["daily"],
                "peaks": loc["peaks"],
                "hourly": [
                    {"t": h["t"], "om": h["om_mm"], "met": h["met_mm"], "prob": h["prob_pct"],
                     "om_temp": h["om_temp_c"], "met_temp": h["met_temp_c"]}
                    for h in loc["hourly"]
                ],
            }
        )
    return {
        "generated_at": forecast["generated_at"],
        "generated_at_local": generated.astimezone(PRAGUE).strftime("%d %b %Y, %H:%M %Z"),
        "window_hours": forecast["window_hours"],
        "locations": locations,
    }


def main() -> int:
    forecast = json.loads((BUILD_DIR / "forecast.json").read_text(encoding="utf-8"))
    summaries = json.loads((BUILD_DIR / "summaries.json").read_text(encoding="utf-8"))
    repo_url = os.environ.get("REPO_URL", DEFAULT_REPO_URL).rstrip("/")

    if SITE_DIR.exists():
        shutil.rmtree(SITE_DIR)  # site/ is fully rebuilt every run
    SITE_DIR.mkdir()
    for f in WEB_DIR.iterdir():
        text = f.read_text(encoding="utf-8").replace("{{REPO_URL}}", repo_url)
        (SITE_DIR / f.name).write_text(text, encoding="utf-8")

    data = build_data(forecast, summaries)
    (SITE_DIR / "data.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {SITE_DIR} ({len(data['locations'])} locations, updated {data['generated_at_local']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
