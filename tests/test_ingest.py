"""Ingest polling rules and the web app, none of it needs a database (the stored-rows tests are in test_db.py)."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from helpers import NOW
from src import ingest, service, web_app
from src.build_site import render_site
from src.common import WEB_DIR

LOC = {"name": "České Budějovice", "lat": 48.9744, "lon": 14.4743}


# ---- polling rules

def test_is_due_only_after_the_expires_time():
    entry = {"expires": (NOW + timedelta(minutes=5)).isoformat()}
    assert not ingest.is_due(entry, NOW)
    assert ingest.is_due(entry, NOW + timedelta(minutes=5))
    assert ingest.is_due({}, NOW)                                  # never fetched


def test_open_meteo_is_fetched_once_per_interval_and_errors_are_retried(monkeypatch):
    calls = []

    def fake(loc):
        calls.append(loc["name"])
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"ok": True}
    monkeypatch.setattr("src.ingest.fetch_open_meteo", fake)
    state, interval = {}, timedelta(minutes=55)

    with pytest.raises(RuntimeError):
        ingest.fetch_open_meteo_if_due(LOC, state, NOW, interval)
    assert state == {}                                              # a failed call does not start the waiting time

    assert ingest.fetch_open_meteo_if_due(LOC, state, NOW, interval) == {"ok": True}
    assert "ceske-budejovice" in state
    assert ingest.fetch_open_meteo_if_due(LOC, state, NOW + timedelta(minutes=30), interval) is None
    assert ingest.fetch_open_meteo_if_due(LOC, state, NOW + timedelta(minutes=30), interval, force=True) == {"ok": True}
    assert len(calls) == 3


def test_met_no_is_not_asked_before_expires_and_sends_if_modified_since(monkeypatch):
    monkeypatch.setenv("USER_AGENT", "test/0.1 https://example.test")
    key = ingest.met_key(LOC)
    assert key == "48.9744,14.4743"
    state = {key: {"expires": (NOW + timedelta(minutes=10)).isoformat()}}
    monkeypatch.setattr("src.ingest.requests.get", lambda *a, **k: pytest.fail("must not call the API before Expires"))
    assert ingest.fetch_met_no(LOC, state, NOW) is None

    seen = {}

    class Reply:
        status_code = 200
        headers = {"Expires": "Wed, 01 Jul 2026 12:00:00 GMT", "Last-Modified": "Wed, 01 Jul 2026 10:00:00 GMT"}
        def raise_for_status(self): pass
        def json(self): return {"properties": {"timeseries": []}}

    def get(url, params, headers, timeout):
        seen.update(headers=headers)
        return Reply()
    monkeypatch.setattr("src.ingest.requests.get", get)
    state[key]["last_modified"] = "Wed, 01 Jul 2026 09:00:00 GMT"
    ingest.fetch_met_no(LOC, state, NOW + timedelta(minutes=11))
    assert seen["headers"]["If-Modified-Since"] == "Wed, 01 Jul 2026 09:00:00 GMT"
    assert state[key]["last_modified"] == "Wed, 01 Jul 2026 10:00:00 GMT"
    assert state[key]["expires"].startswith("2026-07-01T12:00:00")


def test_met_no_refuses_to_run_without_a_user_agent(monkeypatch):
    monkeypatch.delenv("USER_AGENT", raising=False)
    with pytest.raises(RuntimeError):
        ingest.fetch_met_no(LOC, {}, NOW)


# ---- the web app

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("src.web_app.connect", lambda: nullcontext(object()))
    return TestClient(web_app.app)


def test_ingest_endpoint_needs_the_secret(client, monkeypatch):
    monkeypatch.setattr("src.web_app.ingest.run", lambda conn, note=None: {"open-meteo": {"stored": 1}})
    monkeypatch.setenv("INGEST_SECRET", "s3cret")
    assert client.post("/api/ingest").status_code == 401
    assert client.post("/api/ingest", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/api/ingest", headers={"Authorization": "s3cret"}).status_code == 401
    ok = client.post("/api/ingest", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200 and ok.json()["forecasts"] == {"open-meteo": {"stored": 1}}
    assert ok.headers["cache-control"] == "no-store"


def test_ingest_endpoint_is_closed_when_no_secret_is_configured(client, monkeypatch):
    monkeypatch.delenv("INGEST_SECRET", raising=False)
    assert client.post("/api/ingest", headers={"Authorization": "Bearer "}).status_code == 503


def test_data_endpoint_is_cached_by_the_cdn(client, monkeypatch):
    monkeypatch.setattr("src.web_app.build_payload", lambda conn: {"generated_at": "x", "locations": []})
    r = client.get("/api/data")
    assert r.status_code == 200 and r.json()["generated_at"] == "x"
    assert "s-maxage=300" in r.headers["cache-control"]


def test_health_endpoint_turns_503_when_data_is_stale(client, monkeypatch):
    monkeypatch.setattr("src.web_app.monitor.check", lambda conn: {"ok": True, "problems": [], "newest": {}})
    assert client.get("/api/health").status_code == 200
    monkeypatch.setattr("src.web_app.monitor.check", lambda conn: {"ok": False, "problems": ["met-no: old"], "newest": {}})
    r = client.get("/api/health")
    assert r.status_code == 503 and r.json()["problems"] == ["met-no: old"]


# ---- the payload helpers and the static shell

def test_newest_fetch_picks_the_latest_source_time():
    forecast = {"locations": [
        {"sources": {"open-meteo": "2026-07-01T10:00:00Z", "met-no": None}},
        {"sources": {"open-meteo": "2026-07-01T10:20:00Z", "met-no": "2026-07-01T09:00:00Z"}}]}
    assert service.newest_fetch(forecast) == datetime(2026, 7, 1, 10, 20, tzinfo=timezone.utc)
    assert service.newest_fetch({"locations": [{"sources": {"open-meteo": None, "met-no": None}}]}) is None


def test_the_site_shell_has_a_page_per_place_and_no_data_file(tmp_path):
    shell = {"locations": [{"name": "Praha", "slug": "praha"}, {"name": "Brno", "slug": "brno"}]}
    render_site(shell, "https://example.test/repo", web_dir=WEB_DIR, site_dir=tmp_path / "site")
    site = tmp_path / "site"
    assert (site / "index.html").exists() and (site / "praha" / "index.html").exists()
    assert "Brno" in (site / "brno" / "index.html").read_text(encoding="utf-8")
    assert not (site / "data.json").exists()
