"""Weather radar frames from ČHMÚ, as a small FastAPI router (the site draws them on the map).

    app.include_router(api_radar.router)          # in app.py
    python -m src.api_radar --serve               # local preview: this router + the built site/ folder

    GET /api/radar/frames                 newest frames (about the last hour) and where to draw them
    GET /api/radar/frame/<YYYYMMDDHHMM>.png   one frame, fetched from ČHMÚ and cached

Facts about the data (checked against radar_description_en.pdf and the live server, 2026-10-26):

  * Product: MAX_Z composite (maximum reflectivity over the Czech radars Brdy-Praha and Skalky), PNG with
    the "masked" display (areas where rain reaches the ground in full colour, the rest paler):
        https://opendata.chmi.cz/meteorology/weather/radar/composite/maxz/png_masked/
        pacz2gmaps3.z_max3d.<YYYYMMDD>.<HHMM>.0.png          (UTC, the END of the 5-minute interval)
    A new frame appears every 5 minutes, at about the minute in its name (HEAD of the 22:05 frame
    already answered at 22:06). Never read the directory listing: it is huge. We build the file names
    from the clock and probe back in 5-minute steps instead.
  * Image: 680 x 460 px, 8-bit palette PNG with transparency, about 4 kB. Projection EPSG:3857 (Web
    Mercator), 1 x 1 km. The whole image covers E 11.267 to 20.770 and N 48.047 to 52.167 (checked: square
    Mercator pixels give exactly 460 px of height for 680 px of width).
  * The picture is NOT only the map. It is ČHMÚ's classic "MAX" product: the map sits in the lower left
    (E 11.267 to 19.624, N 48.047 to 51.458, pixels x 1..597, y 82..458, which matches the PDF's "boundary of
    data"); a side-view strip of the same data runs along the top and another down the right, with black frame
    lines, and a black caption "CZRAD - Z: MAX - dd.mm.yyyy hh:mm UT" is printed on the top left of the map. We
    crop the map out and make the caption transparent (crop_frame), and give the browser the matching corners.
  * CORS: opendata.chmi.cz sends no Access-Control-Allow-Origin header, so a browser cannot use the
    frames as map textures directly. This router fetches them on the server and serves them from our own
    origin. Frames never change, so they are cached for a long time.
  * Licence: ČHMÚ open data. Credit "Czech Hydrometeorological Institute (ČHMÚ)", see DATA_LICENSES.md.
    The radar must not be used as a basis for official warnings (ČHMÚ's terms).
  * The 60-minute extrapolation forecast (fct_maxz, pacz2gmaps3.fct_z_max.<..>.<ff>.png, inside a TAR per
    run) is not used yet.
"""
import io
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests
from fastapi import APIRouter, HTTPException, Response

BASE = "https://opendata.chmi.cz/meteorology/weather/radar/composite/maxz/png_masked"
STEP = timedelta(minutes=5)
FRAME_COUNT = 13            # newest frame plus the hour before it
LOOK_BACK_STEPS = 6         # how far back to look for the newest frame (30 minutes)
MAX_AGE = timedelta(hours=3)  # the frame proxy refuses older frames: nothing needs them
# Corners of the whole ČHMÚ image, west / east / north / south in degrees (EPSG:3857, 680 x 460 px).
WEST, EAST, NORTH, SOUTH = 11.267, 20.770, 52.167, 48.047
IMAGE_SIZE = (680, 460)
# The map inside it (left, top, right, bottom in pixels; right and bottom are exclusive), without the frame lines.
MAP_BOX = (1, 82, 598, 459)


def _mercator(lat: float) -> float:
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def _lat_at(y: float) -> float:
    """Latitude of pixel row y of the whole image (Mercator: rows are linear in the projected y)."""
    top, bottom = _mercator(NORTH), _mercator(SOUTH)
    merc = top - y / IMAGE_SIZE[1] * (top - bottom)
    return math.degrees(2 * math.atan(math.exp(merc)) - math.pi / 2)


def _lon_at(x: float) -> float:
    return WEST + x / IMAGE_SIZE[0] * (EAST - WEST)


# Corners of the cropped map, in MapLibre's `image` source order: top-left, top-right, bottom-right,
# bottom-left, as [lon, lat].
_w, _n, _e, _s = _lon_at(MAP_BOX[0]), _lat_at(MAP_BOX[1]), _lon_at(MAP_BOX[2]), _lat_at(MAP_BOX[3])
COORDINATES = [[round(_w, 4), round(_n, 4)], [round(_e, 4), round(_n, 4)],
               [round(_e, 4), round(_s, 4)], [round(_w, 4), round(_s, 4)]]
STAMP = re.compile(r"^\d{12}$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
FRAME_VERSION = 2           # bump when crop_frame changes: frames are cached "forever" under their URL
LIST_TTL = 60               # seconds the frame list is reused inside one process
USER_AGENT_FALLBACK = "rain-forecast-cz (radar proxy)"

router = APIRouter(prefix="/api/radar")

_lock = threading.Lock()
_exists_cache: dict[str, bool] = {}     # stamp -> frame exists (only positives are kept forever)
_png_cache: dict[str, bytes] = {}
_list_cache: dict = {"at": 0.0, "body": None}


# ---------------------------------------------------------------- talking to ČHMÚ (replaced in the tests)


def _headers() -> dict:
    import os
    return {"User-Agent": os.environ.get("USER_AGENT") or USER_AGENT_FALLBACK}


def frame_url(stamp: str) -> str:
    return f"{BASE}/pacz2gmaps3.z_max3d.{stamp[:8]}.{stamp[8:]}.0.png"


def crop_frame(png: bytes) -> bytes:
    """ČHMÚ's picture -> just the map, as a transparent PNG (see the module docstring).

    Raises ValueError for anything that is not the expected 680 x 460 picture, so a changed layout shows up
    as an error instead of a misplaced overlay.
    """
    from PIL import Image, ImageChops  # imported here so a missing Pillow breaks the frames only, not the whole app

    im = Image.open(io.BytesIO(png))
    if im.size != IMAGE_SIZE:
        raise ValueError(f"unexpected radar image size {im.size}, expected {IMAGE_SIZE}")
    r, g, b, a = im.convert("RGBA").crop(MAP_BOX).split()
    # The caption is pure black; the radar colour scale has no black. Black (all three channels 0) -> transparent.
    not_black = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v else 0)
    out = Image.merge("RGBA", (r, g, b, ImageChops.multiply(a, not_black)))
    buf = io.BytesIO()
    # Back to a small palette (the picture has about 20 colours): a third of the size of a full-colour PNG.
    out.quantize(colors=32, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def upstream_exists(stamp: str) -> bool:
    """Is there a frame for this time? (a HEAD request, 4 kB are never downloaded)"""
    resp = requests.head(frame_url(stamp), headers=_headers(), timeout=10)
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    return True


def upstream_download(stamp: str) -> bytes | None:
    """The PNG bytes of a frame, or None when it does not exist."""
    resp = requests.get(frame_url(stamp), headers=_headers(), timeout=15)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.content


# ---------------------------------------------------------------- which frames exist


def stamp_of(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y%m%d%H%M")


def time_of(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)


def floor5(t: datetime) -> datetime:
    t = t.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return t - timedelta(minutes=t.minute % 5)


def exists(stamp: str) -> bool:
    if _exists_cache.get(stamp):
        return True
    try:
        found = upstream_exists(stamp)
    except Exception:  # noqa: BLE001 - ČHMÚ hiccup: treat as "not there" for now, retried on the next list
        return False
    if found:
        with _lock:
            _exists_cache[stamp] = True
    return found


def find_frames(now: datetime | None = None, count: int = FRAME_COUNT) -> list[str]:
    """Stamps of the newest `count` frames, oldest first. Empty when ČHMÚ has nothing recent.

    The newest frame is found by probing back from the clock in 5-minute steps; the older ones are probed
    side by side (a missing one leaves a gap in the loop, it does not stop it).
    """
    now = now or datetime.now(timezone.utc)
    newest = None
    for k in range(LOOK_BACK_STEPS):
        t = floor5(now) - k * STEP
        if exists(stamp_of(t)):
            newest = t
            break
    if newest is None:
        return []
    wanted = [stamp_of(newest - i * STEP) for i in range(count - 1, -1, -1)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        found = list(pool.map(exists, wanted))
    return [s for s, ok in zip(wanted, found) if ok]


def _trim_caches(keep: set) -> None:
    with _lock:
        for cache in (_exists_cache, _png_cache):
            for s in [s for s in cache if s not in keep and s < min(keep, default="")]:
                del cache[s]


# ---------------------------------------------------------------- routes


@router.get("/frames")
def frames(response: Response):
    """{"coordinates": [[lon, lat] x4], "frames": [{"t": ISO time, "url": "/api/radar/frame/<stamp>.png"}]}."""
    response.headers["Cache-Control"] = "public, max-age=60, s-maxage=120, stale-while-revalidate=300"
    with _lock:
        cached = _list_cache["body"] if time.time() - _list_cache["at"] < LIST_TTL else None
    if cached is None:
        stamps = find_frames()
        cached = {
            "coordinates": COORDINATES,
            "frames": [{"t": time_of(s).strftime("%Y-%m-%dT%H:%M:%SZ"), "url": f"/api/radar/frame/{s}.png?v={FRAME_VERSION}"} for s in stamps],
            "attribution": "Radar: ČHMÚ (Czech Hydrometeorological Institute)",
        }
        _trim_caches(set(stamps))
        with _lock:
            _list_cache.update(at=time.time(), body=cached)
    return cached


@router.get("/frame/{stamp}.png")
def frame(stamp: str):
    """One radar frame (PNG), proxied from ČHMÚ. Only recent frames with a valid time are served."""
    if not STAMP.match(stamp):
        raise HTTPException(404, "not a radar frame time")
    try:
        t = time_of(stamp)
    except ValueError:
        raise HTTPException(404, "not a radar frame time") from None
    if t.minute % 5 or t > datetime.now(timezone.utc) + STEP or datetime.now(timezone.utc) - t > MAX_AGE:
        raise HTTPException(404, "no such frame")
    png = _png_cache.get(stamp)
    if png is None:
        try:
            png = upstream_download(stamp)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, "radar source unavailable") from exc
        if png is None:
            raise HTTPException(404, "no such frame")
        if not png.startswith(PNG_MAGIC):
            raise HTTPException(502, "radar source returned something that is not a PNG")
        try:
            png = crop_frame(png)
        except Exception as exc:  # noqa: BLE001 - a damaged or re-laid-out picture
            raise HTTPException(502, f"radar picture could not be used: {exc}") from exc
        with _lock:
            if len(_png_cache) > 64:
                _png_cache.pop(min(_png_cache))
            _png_cache[stamp] = png
    # A frame never changes once it exists: let browsers and the CDN keep it.
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400, s-maxage=86400, immutable"})


# ---------------------------------------------------------------- local preview


def serve(port: int = 8000) -> None:
    """Serve this router plus the built site/ folder (python -m src.build_site first), for local testing."""
    import uvicorn
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles

    from src.common import SITE_DIR

    app = FastAPI()
    app.include_router(router)
    app.mount("/", StaticFiles(directory=SITE_DIR, html=True), name="site")
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    import sys
    if "--serve" in sys.argv:
        serve()
    else:
        print(__doc__)
