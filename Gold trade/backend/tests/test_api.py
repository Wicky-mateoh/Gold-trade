"""Integration: API, service pipeline, and audit journal."""

from fastapi.testclient import TestClient

from app.journal import Journal
from app.main import create_app


def client(service):
    return TestClient(create_app(service))


def test_status_and_analysis(service):
    with client(service) as c:
        s = c.get("/api/status").json()
        assert s["mode"] == "analysis" and s["live_enabled"] is False
        a = c.get("/api/analysis").json()
        assert set(a["timeframes"]) == {"MN1", "W1", "D1", "H4", "H1", "M15", "M5"}
        assert a["direction"]["label"] in {"bullish", "bearish", "mixed"}
        assert "smc" in a and "macro" in a and "sentiment" in a


def test_signal_is_journaled(service):
    with client(service) as c:
        r = c.get("/api/signal").json()
        assert r["orders_sent_to_broker"] == 0
        kind = "signal" if r["signal"]["actionable"] else "rejected_setup"
        assert c.get(f"/api/journal?kind={kind}").json()


def test_candles(service):
    with client(service) as c:
        rows = c.get("/api/candles?timeframe=H4&limit=50").json()["candles"]
        assert len(rows) == 50
        assert c.get("/api/candles?timeframe=X").status_code == 400


def test_live_cannot_be_enabled_by_api(service):
    with client(service) as c:
        assert c.post("/api/mode", json={"mode": "live"}).status_code == 422
        r = c.post("/api/live/request", json={k: True for k in ("explicit_authorization", "broker_connected", "account_risk_limits_set", "emergency_shutdown_armed")}).json()
        assert r["accepted"] is False and "a ready broker adapter" in r["missing"]


def test_emergency_shutdown_cycle(service):
    with client(service) as c:
        c.post("/api/mode", json={"mode": "paper"})
        assert c.post("/api/emergency/shutdown", json={"reason": "manual test"}).json()["emergency_shutdown"]
        assert c.get("/api/risk").json()["emergency_shutdown"] is True
        assert c.post("/api/emergency/reset", json={"operator": "ops", "acknowledgement": "ok"}).json()["reset"] is False
        assert c.post("/api/emergency/reset", json={"operator": "ops", "acknowledgement": "I have reviewed the shutdown cause"}).json()["reset"]
        assert c.get("/api/journal?kind=emergency").json()


def test_backtest_endpoint_records_model_version(service):
    with client(service) as c:
        r = c.post("/api/backtest", json={"timeframe": "H4"}).json()
        assert "metrics" in r and "out_of_sample" in r and r["model_version"]
        learn = c.get("/api/learning").json()
        assert learn["registry"]
        assert c.post("/api/learning/promote", json={"version": r["model_version"]}).json()["promoted"] is r["out_of_sample"]["passed"]


def test_recommendations_drop_risk_keys(service):
    with client(service) as c:
        r = c.post("/api/learning/recommend", json={"max_daily_loss_pct": 10, "swing_left": 4}).json()
        assert r["removed_risk_keys"] == ["max_daily_loss_pct"]


def test_news_ingest(service):
    with client(service) as c:
        r = c.post("/api/news", json={"published_at": "2026-08-14T10:00:00Z", "source": "reuters", "headline": "Dollar strength weighs"}).json()
        assert r["label"] == "bearish"


def test_journal_persists(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.record("operator_note", {"x": 1})
    assert Journal(tmp_path / "j.jsonl").rows[0]["payload"] == {"x": 1}
