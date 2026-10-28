"""The web app (FastAPI) that Vercel runs next to the static site.

    POST /api/ingest   called every ~10 minutes by Supabase pg_cron; fetches whatever is due. Needs the
                       secret in the Authorization header: "Bearer <INGEST_SECRET>".
    GET  /api/data     the JSON the pages draw, computed from the database, cached by the CDN for 5 minutes.
    GET  /api/health   200 when fresh data keeps arriving, 503 (with the reasons) when not. For an uptime monitor.
    everything else    the static site in site/ (python -m src.build_site). Vercel treats this project as a
                       FastAPI app and sends EVERY request here, so the app serves the pages itself.

Run it locally with:  uvicorn src.web_app:app --reload   (needs DATABASE_URL, USER_AGENT, INGEST_SECRET)
"""
import hmac
import logging
import os

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from src import ingest, monitor
from src.common import SITE_DIR
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


# ---------------------------------------------------------------- the static site (must come last)


@app.middleware("http")
async def cache_pages(request, call_next):
    """Let the CDN keep the pages: they only change with a deployment, and the CDN cache is per deployment."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/") and response.status_code == 200 and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "public, max-age=0, s-maxage=86400, stale-while-revalidate=604800"
    return response


def mount_site(application: FastAPI, directory=SITE_DIR) -> None:
    """Serve the built site (index.html, <city>/index.html, js, css) at "/". A missing folder is skipped, so
    the API still starts on a machine where the site has not been built."""
    if directory.is_dir():
        application.mount("/", StaticFiles(directory=directory, html=True), name="site")


mount_site(app)
