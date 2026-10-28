"""The web app (FastAPI) that Vercel runs next to the static site.

    POST /api/ingest   called every ~10 minutes by Supabase pg_cron; fetches whatever is due. Needs the
                       secret in the Authorization header: "Bearer <INGEST_SECRET>".
    GET  /api/data     the JSON the pages draw, computed from the database, cached by the CDN for 5 minutes.
    GET  /api/health   200 when fresh data keeps arriving, 503 (with the reasons) when not. For an uptime monitor.

Run it locally with:  uvicorn src.web_app:app --reload   (needs DATABASE_URL, USER_AGENT, INGEST_SECRET)
"""
import hmac
import logging
import os

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from src import ingest, monitor
from src.db import connect
from src.service import build_payload

log = logging.getLogger("web_app")

# Shared CDN cache: the first visitor after it expires recomputes, everyone else gets the cached answer.
DATA_CACHE = "public, s-maxage=300, stale-while-revalidate=600"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

try:  # the radar routes come from a separate module, which may not be there yet
    from src.api_radar import router as radar_router
    app.include_router(radar_router)
except ModuleNotFoundError as exc:
    if exc.name != "src.api_radar":
        raise


def _check_secret(authorization: str | None) -> None:
    secret = os.environ.get("INGEST_SECRET")
    if not secret:
        raise HTTPException(503, "INGEST_SECRET is not configured")
    if not authorization or not hmac.compare_digest(authorization.encode(), f"Bearer {secret}".encode()):
        raise HTTPException(401, "bad or missing secret")


@app.post("/api/ingest")
def run_ingest(authorization: str | None = Header(default=None)):
    _check_secret(authorization)
    results = {}
    with connect() as conn:
        results["forecasts"] = ingest.run(conn, note="vercel")
        try:  # measured weather (Plan B). A failure there must not hide the forecast result.
            from src import observe
            results["observations"] = observe.run(conn)
        except ModuleNotFoundError as exc:
            if exc.name != "src.observe":
                raise
        except Exception as exc:  # noqa: BLE001
            log.exception("observations failed")
            results["observations"] = {"error": str(exc)}
    return JSONResponse(results, headers={"Cache-Control": "no-store"})


@app.get("/api/data")
def data():
    with connect() as conn:
        payload = build_payload(conn)
    return JSONResponse(payload, headers={"Cache-Control": DATA_CACHE})


@app.get("/api/health")
def health():
    with connect() as conn:
        result = monitor.check(conn)
    return JSONResponse(result, status_code=200 if result["ok"] else 503, headers={"Cache-Control": "no-store"})
