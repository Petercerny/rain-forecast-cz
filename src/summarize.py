"""Step 3: write a short plain-language summary for every location.

Two interchangeable summarizers sit behind one function, `summarize(facts, mode)`:

  template  deterministic text built from the facts (default, used in CI)
  ollama    a small local LLM, called through the Ollama HTTP API

Both only ever see `facts`: a small dict computed from the data in step 2. The LLM never
sees raw API responses and is told to describe, not to explain or guess. Whatever it
writes goes through `check_faithfulness`; if a number is not in the facts, the template
text is used instead.

Usage:  python -m src.summarize                  # uses config/settings.yaml (or $SUMMARIZER)
        SUMMARIZER=ollama python -m src.summarize
"""
import json
import logging
import os
import re
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

from src.common import BUILD_DIR, load_settings

log = logging.getLogger("summarize")

DRY_THRESHOLD_MM = 0.1  # below this we call a period "dry"
SRC_NAMES = {"open_meteo": "Open-Meteo", "met_no": "MET Norway"}
PRAGUE = ZoneInfo("Europe/Prague")


# ============================================================ 1. facts


def day_label(d: date, today: date) -> str:
    """'today', 'tomorrow', or the weekday name. Words only, so no stray numbers."""
    delta = (d - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    return d.strftime("%A")  # Weekday name, e.g. "Wednesday"


def build_facts(loc: dict, today: date) -> dict:
    """Reduce one location's forecast to the handful of facts a summary may mention.

    `today` is the Prague-local date. Every number in this dict is allowed to appear in a
    summary; anything else is flagged by the faithfulness check.
    """
    totals = loc["totals"] or {}
    om, met = totals.get("open_meteo_mm"), totals.get("met_no_mm")
    available = [k for k, v in (("open_meteo", om), ("met_no", met)) if v is not None]

    facts = {
        "location": loc["name"],
        "window_hours": totals.get("hours"),
        "dry_threshold_mm": DRY_THRESHOLD_MM,
        "sources_available": [SRC_NAMES[k] for k in available],
        "total_mm": {SRC_NAMES[k]: v for k, v in (("open_meteo", om), ("met_no", met)) if v is not None},
        "days": [
            {"day": day_label(date.fromisoformat(d["date"]), today),
             **{SRC_NAMES[k]: d[f"{k}_mm"] for k in available}}
            for d in loc["daily"]
        ],
        # Temperature range over the window (average of the two sources), in deg C.
        "temperature_c": (
            {"min": totals["temp_min_c"], "max": totals["temp_max_c"]}
            if totals.get("temp_min_c") is not None else None
        ),
        "peak": None,
        "max_rain_probability_pct": totals.get("max_prob_pct"),
    }
    if len(available) == 2:
        diff = abs(round(om - met, 1))
        facts["difference_mm"] = diff
        facts["wetter_source"] = (
            "equal" if diff < DRY_THRESHOLD_MM else SRC_NAMES["open_meteo" if om > met else "met_no"]
        )
    if loc["peaks"]:
        p = loc["peaks"][0]
        facts["peak"] = {
            "day": day_label(date.fromisoformat(p["local_date"]), today),
            "time": p["local_time"],
            **{SRC_NAMES[k]: p[f"{k}_mm"] for k in available if p[f"{k}_mm"] is not None},
        }
    if loc.get("observed"):
        facts["measured"] = measured_facts(loc["observed"])
    return facts


def measured_facts(obs: dict) -> dict:
    """What the weather station measured, as facts a summary may mention (the time is Prague-local)."""
    when = datetime.fromisoformat(obs["t"].replace("Z", "+00:00")).astimezone(PRAGUE)
    facts = {"station": obs["station"], "time": when.strftime("%H:%M")}
    for key, name in (("temp_c", "temperature_c"), ("wind_ms", "wind_ms"), ("humidity_pct", "humidity_pct"),
                      ("precip_1h_mm", "rain_last_hour_mm")):
        if obs.get(key) is not None:
            facts[name] = obs[key]
    return facts


# ============================================================ 2. template summarizer


def template_summary(facts: dict) -> str:
    """Deterministic summary: same facts in, same text out."""
    name, hours = facts["location"], facts["window_hours"]
    totals = facts["total_mm"]
    if not totals:
        return f"No forecast data is available for {name} right now."

    # Sentence 1: how much rain in total.
    if all(v < DRY_THRESHOLD_MM for v in totals.values()):
        who = " and ".join(totals)
        first = (f"No rain is expected in {name} over the next {hours} hours "
                 f"({who} forecast less than {DRY_THRESHOLD_MM} mm).")
    else:
        (src1, mm1), *rest = totals.items()
        first = f"Over the next {hours} hours {src1} expects {mm1} mm of rain in {name}"
        if rest:
            first += f" and {rest[0][0]} {rest[0][1]} mm"
            # Do the two sources agree?
            if facts["wetter_source"] == "equal":
                first += f" (they agree to within {DRY_THRESHOLD_MM} mm)."
            else:
                first += f" (a difference of {facts['difference_mm']} mm, {facts['wetter_source']} being wetter)."
        else:
            first += f" (only {src1} data is available right now)."

    sentences = [first]

    # A measured sentence goes first: it is what is happening now.
    m = facts.get("measured")
    if m and "temperature_c" in m:
        now = f"Measured at {m['station']} at {m['time']}: {m['temperature_c']} °C"
        if "wind_ms" in m:
            now += f", wind {m['wind_ms']} m/s"
        if m.get("rain_last_hour_mm"):
            now += f", {m['rain_last_hour_mm']} mm of rain in the last hour"
        sentences.insert(0, now + ".")

    # Sentence 2: the wettest hour.
    peak = facts["peak"]
    if peak:
        amounts = ", ".join(f"{k} {v} mm" for k, v in peak.items() if k not in ("day", "time"))
        sentences.append(f"The wettest hour is {peak['day']} at {peak['time']} ({amounts}).")

    # Sentence 3: temperature range.
    temp = facts.get("temperature_c")
    if temp:
        sentences.append(f"Temperatures range from {temp['min']} to {temp['max']} °C.")
    return " ".join(sentences)


# ============================================================ 3. faithfulness check

# A token is either a clock time like 14:00 or a plain number like 3 / 3.2.
_TOKEN = re.compile(r"\d{1,2}:\d{2}|\d+(?:\.\d+)?")


def _tokens(text: str) -> set:
    """Numbers (as floats) and clock times (as strings) found in a piece of text."""
    out = set()
    for tok in _TOKEN.findall(text):
        out.add(tok if ":" in tok else float(tok))
    return out


def check_faithfulness(text: str, facts: dict) -> list:
    """Return the numbers in `text` that do NOT appear in `facts` (empty list = faithful).

    The facts are dumped to JSON text and scanned with the same rule, so a number counts as
    "in the facts" wherever it appears there, including inside strings such as "14:00".
    """
    allowed = _tokens(json.dumps(facts, ensure_ascii=False))
    return sorted((t for t in _tokens(text) if t not in allowed), key=str)


# ============================================================ 4. Ollama summarizer

OLLAMA_PROMPT = """You write a short weather note for a website, from the JSON facts below.

Rules:
- Write 2 or 3 plain English sentences.
- Describe only what the facts show: rain, then temperature (in degrees Celsius). Do not explain causes, do not speculate, do not give advice.
- Use only numbers that appear in the facts. Do not calculate new numbers. Amounts are in mm.
- Name the sources (Open-Meteo, MET Norway) when you compare them.
- If the facts contain `measured`, you may start with one short sentence about it (station, time, temperature).
- Output only the note, with no heading and no quotation marks.

Facts:
{facts}
"""


def ollama_summary(facts: dict, cfg: dict) -> str:
    """Ask a local Ollama model for a summary. Raises on any HTTP problem."""
    resp = requests.post(
        f"{cfg['url']}/api/generate",
        json={
            "model": cfg["model"],
            "prompt": OLLAMA_PROMPT.format(facts=json.dumps(facts, ensure_ascii=False, indent=1)),
            "stream": False,
            "options": {"temperature": 0},  # as repeatable as a local model gets
        },
        timeout=cfg.get("timeout_seconds", 120),
    )
    resp.raise_for_status()
    return resp.json()["response"].strip()


def _looks_like_summary(text: str) -> bool:
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    return 1 <= len(sentences) <= 4 and len(text) <= 600


# ============================================================ 5. the one public function


def summarize(facts: dict, mode: str = "template", settings: dict | None = None) -> dict:
    """Return {"text", "generated_by", "ai_generated"} for one location.

    mode "ollama" falls back to the template if Ollama is unreachable, the answer looks
    wrong, or the faithfulness check finds a number that is not in the facts.
    """
    fallback = template_summary(facts)
    if mode == "ollama":
        cfg = (settings or load_settings())["ollama"]
        try:
            text = ollama_summary(facts, cfg)
            unknown = check_faithfulness(text, facts)
            if unknown:
                log.warning("%s: LLM used numbers not in the facts %s, using template", facts["location"], unknown)
            elif not _looks_like_summary(text):
                log.warning("%s: LLM output has an unexpected shape, using template", facts["location"])
            else:
                return {"text": text, "generated_by": f"ollama:{cfg['model']}", "ai_generated": True}
        except Exception as exc:  # noqa: BLE001 - any failure means "use the template"
            log.warning("%s: Ollama failed (%s), using template", facts["location"], exc)
    elif mode != "template":
        raise ValueError(f"Unknown summarizer {mode!r} (use 'template' or 'ollama')")
    return {"text": fallback, "generated_by": "template", "ai_generated": False}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    mode = os.environ.get("SUMMARIZER") or settings["summarizer"]
    forecast = json.loads((BUILD_DIR / "forecast.json").read_text(encoding="utf-8"))

    # "today" must be the Prague-local date, not the UTC one.
    from zoneinfo import ZoneInfo
    generated = datetime.fromisoformat(forecast["generated_at"].replace("Z", "+00:00"))
    today = generated.astimezone(ZoneInfo("Europe/Prague")).date()

    summaries = {}
    for loc in forecast["locations"]:
        facts = build_facts(loc, today) if loc["has_data"] else None
        if facts is None:
            summaries[loc["name"]] = {"text": f"No forecast data is available for {loc['name']} right now.",
                                      "generated_by": "template", "ai_generated": False, "facts": None}
            continue
        result = summarize(facts, mode, settings)
        summaries[loc["name"]] = {**result, "facts": facts}
        print(f"[{result['generated_by']}] {result['text']}")

    out = BUILD_DIR / "summaries.json"
    out.write_text(json.dumps(summaries, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
