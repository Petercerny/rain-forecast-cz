"""The radar router: finding the newest frame from the clock, and serving frames from our own origin.

ČHMÚ is replaced by a fake, so no network is used.
"""
from datetime import datetime, timedelta, timezone

import io

import pytest
from fastapi import FastAPI
from PIL import Image
from fastapi.testclient import TestClient

from src import api_radar

NOW = datetime(2026, 10, 26, 22, 7, 30, tzinfo=timezone.utc)


def picture(size=(680, 460)) -> bytes:
    """A stand-in for ČHMÚ's picture: transparent, a green echo on the map, a blue echo in the top side-view strip,
    black frame lines and a black caption on the map's top-left corner."""
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    px = im.load()
    for x in range(size[0]):
        px[x, 81] = (0, 0, 0, 255)                     # frame line under the top strip
    for y in range(size[1]):
        px[598, y] = (0, 0, 0, 255)                    # frame line left of the right strip
    for x in range(300, 320):
        for y in range(200, 220):
            px[x, y] = (0, 160, 0, 255)                # echo on the map
    for x in range(100, 120):
        for y in range(40, 60):
            px[x, y] = (0, 0, 252, 255)                # echo in the top strip (must be cropped away)
    for x in range(5, 60):
        px[x, 88] = (0, 0, 0, 255)                     # caption
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


PNG = picture()


@pytest.fixture(autouse=True)
def fresh_caches():
    for cache in (api_radar._exists_cache, api_radar._png_cache):
        cache.clear()
    api_radar._list_cache.update(at=0.0, body=None)


@pytest.fixture
def upstream(monkeypatch):
    """Frames ČHMÚ 'has' (a set of stamps); every probe and download is recorded."""
    class Upstream:
        have = set()
        probes = []
        downloads = []
    u = Upstream()
    monkeypatch.setattr(api_radar, "upstream_exists", lambda s: (u.probes.append(s), s in u.have)[1])
    monkeypatch.setattr(api_radar, "upstream_download", lambda s: (u.downloads.append(s), PNG if s in u.have else None)[1])
    return u


def stamps(first, count, step_minutes=5):
    t = datetime.strptime(first, "%Y%m%d%H%M")
    return {(t + timedelta(minutes=step_minutes * i)).strftime("%Y%m%d%H%M") for i in range(count)}


def test_frame_file_name_follows_the_chmi_pattern():
    assert api_radar.frame_url("202610262205").endswith(
        "/maxz/png_masked/pacz2gmaps3.z_max3d.20261026.2205.0.png")


def test_the_clock_is_floored_to_five_minutes():
    assert api_radar.stamp_of(api_radar.floor5(NOW)) == "202610262205"


def test_newest_frame_is_found_by_probing_back_and_the_hour_before_it_is_listed(upstream):
    upstream.have = stamps("202610262100", 14)                 # 21:00 .. 22:05, the 22:05 frame is the newest
    found = api_radar.find_frames(NOW)
    assert found[-1] == "202610262205" and len(found) == 13 and found[0] == "202610262105"


def test_when_the_newest_frame_is_late_the_search_goes_back(upstream):
    upstream.have = stamps("202610262100", 12)                 # up to 21:55
    assert api_radar.find_frames(NOW)[-1] == "202610262155"
    assert upstream.probes[:3] == ["202610262205", "202610262200", "202610262155"]


def test_gaps_are_left_out_and_no_frames_at_all_gives_an_empty_list(upstream):
    upstream.have = stamps("202610262100", 14) - {"202610262130"}
    found = api_radar.find_frames(NOW)
    assert "202610262130" not in found and len(found) == 12
    upstream.have = set()
    api_radar._exists_cache.clear()
    assert api_radar.find_frames(NOW) == []


def test_a_broken_probe_counts_as_missing(monkeypatch):
    def boom(stamp):
        raise RuntimeError("timeout")
    monkeypatch.setattr(api_radar, "upstream_exists", boom)
    assert api_radar.find_frames(NOW) == []


# ---- HTTP

@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(api_radar.router)
    return TestClient(app)


def test_frames_endpoint_gives_corners_and_our_own_urls(client, upstream, monkeypatch):
    monkeypatch.setattr(api_radar, "find_frames", lambda *a, **k: ["202610262200", "202610262205"])
    body = client.get("/api/radar/frames").json()
    assert body["coordinates"] == api_radar.COORDINATES and len(body["coordinates"]) == 4
    assert body["frames"] == [{"t": "2026-10-26T22:00:00Z", "url": f"/api/radar/frame/202610262200.png?v={api_radar.FRAME_VERSION}"},
                              {"t": "2026-10-26T22:05:00Z", "url": f"/api/radar/frame/202610262205.png?v={api_radar.FRAME_VERSION}"}]


def test_frame_endpoint_proxies_the_png_once_and_lets_it_be_cached(client, upstream):
    t = datetime.now(timezone.utc) - timedelta(minutes=20)
    stamp = api_radar.stamp_of(api_radar.floor5(t))
    upstream.have = {stamp}
    first = client.get(f"/api/radar/frame/{stamp}.png")
    assert first.status_code == 200 and first.content.startswith(api_radar.PNG_MAGIC)
    assert Image.open(io.BytesIO(first.content)).size == (597, 377)      # the cropped map, not ČHMÚ's whole picture
    assert first.headers["content-type"] == "image/png" and "immutable" in first.headers["cache-control"]
    client.get(f"/api/radar/frame/{stamp}.png")
    assert upstream.downloads == [stamp]                       # the second request came from memory


@pytest.mark.parametrize("bad", ["abc", "20261026220", "../../etc/passwd", "202613992200", "202610262203"])
def test_frame_endpoint_only_accepts_real_five_minute_times(client, upstream, bad):
    assert client.get(f"/api/radar/frame/{bad}.png").status_code == 404
    assert upstream.downloads == []


def test_old_and_missing_frames_are_404_and_a_non_png_is_refused(client, upstream, monkeypatch):
    assert client.get("/api/radar/frame/202001010000.png").status_code == 404   # far too old
    stamp = api_radar.stamp_of(api_radar.floor5(datetime.now(timezone.utc) - timedelta(minutes=30)))
    assert client.get(f"/api/radar/frame/{stamp}.png").status_code == 404       # ČHMÚ has no such frame
    upstream.have = {stamp}
    monkeypatch.setattr(api_radar, "upstream_download", lambda s: b"<html>not a png</html>")
    assert client.get(f"/api/radar/frame/{stamp}.png").status_code == 502


# ---- cropping the map out of ČHMÚ's picture

def test_crop_keeps_only_the_map_and_removes_the_black_caption():
    out = Image.open(io.BytesIO(api_radar.crop_frame(PNG))).convert("RGBA")
    assert out.size == (api_radar.MAP_BOX[2] - api_radar.MAP_BOX[0], api_radar.MAP_BOX[3] - api_radar.MAP_BOX[1])
    left, top = api_radar.MAP_BOX[:2]
    assert out.getpixel((310 - left, 210 - top)) == (0, 160, 0, 255)          # the echo on the map is where it was
    assert out.getpixel((30 - left, 88 - top))[3] == 0                        # the caption is gone
    seen = {out.getpixel((x, y)) for x in range(out.width) for y in range(out.height)}
    assert not any(a and (r, g, b) == (0, 0, 0) for r, g, b, a in seen)        # nothing black is left
    assert (0, 0, 252, 255) not in seen                                         # the top strip is not in it


def test_a_picture_with_another_layout_is_refused():
    with pytest.raises(ValueError):
        api_radar.crop_frame(picture((600, 400)))


def test_cropped_corners_match_the_published_data_boundary():
    (w, n), (e, _), (_, s), _ = api_radar.COORDINATES
    assert (e, n) == (19.624, 51.4585)               # PDF: data to E 19.624, N 51.458
    assert 11.26 < w < 11.30 and 48.04 < s < 48.07   # PDF: from E 11.267, N 48.047 (frame lines trimmed off)
