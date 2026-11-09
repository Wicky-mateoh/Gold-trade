"""MT5 order adapter and live flow, against a simulated broker.

Covers the demo lock, broker-side SL/TP, duplicate protection, slippage,
unprotected-fill handling, reconciliation with broker-closed positions,
emergency and pre-NFP closes, and the live-arming checklist.
"""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from app.broker_mt5 import MT5Adapter, client_id
from app.execution import LiveChecklist
from app.frames import PriceFrame
from app.market import MarketDataEngine
from app.service import TradingService
from tests.conftest import ROOT
from tests.fake_broker import FakeBroker

ALL_TRUE = LiveChecklist(True, True, True, True)


def live_settings(settings, **extra):
    base = {"mt5_login": "777", "mt5_server": "Broker-Demo", "mt5_password": "x", "broker_adapter": "mt5",
            "live_trading_authorized": True, "live_max_lots": 0.05}
    base.update(extra)
    return settings.model_copy(update=base)


def adapter(settings, broker=None, **extra):
    a = MT5Adapter(live_settings(settings, **extra), module=broker or FakeBroker())
    a.check()
    return a


def order(**over):
    row = {"client_id": "abc123", "direction": "BUY", "lots": 0.5, "stop_loss": 2645.0, "take_profit": 2660.0,
           "entry_low": 2649.5, "entry_high": 2650.2}
    row.update(over)
    return row


# ---------------------------------------------------------------- adapter
def test_demo_account_is_ready(settings):
    a = adapter(settings)
    assert a.ready and a.status["account_type"] == "demo"


def test_real_account_is_locked_unless_allowed(settings):
    real = adapter(settings, FakeBroker(account_mode=2))
    assert not real.ready and any("REAL-money" in r for r in real.status["reasons"])
    assert real.submit(order())["status"] == "refused"
    assert adapter(settings, FakeBroker(account_mode=2), allow_real_account=True).ready


def test_algo_trading_off_and_investor_password_block(settings):
    assert any("Algo Trading" in r for r in adapter(settings, FakeBroker(algo_trading=False)).status["reasons"])
    assert any("investor password" in r for r in adapter(settings, FakeBroker(trade_allowed=False)).status["reasons"])


def test_order_has_broker_side_stops_magic_and_lot_cap(settings):
    broker = FakeBroker()
    result = adapter(settings, broker).submit(order(lots=0.5))
    assert result["status"] == "filled"
    sent = broker.sent[-1]
    assert sent["sl"] == 2645.0 and sent["tp"] == 2660.0
    assert sent["volume"] == 0.05  # capped by LIVE_MAX_LOTS
    assert sent["magic"] == 7319001 and sent["comment"] == "gits:abc123"
    assert sent["deviation"] == 30  # MAX_SLIPPAGE 0.30 / point 0.01
    assert sent["type_filling"] == FakeBroker.ORDER_FILLING_IOC
    assert broker.positions[result["ticket"]].sl == 2645.0


def test_duplicate_order_is_not_sent_twice(settings):
    broker = FakeBroker()
    a = adapter(settings, broker)
    assert a.submit(order())["status"] == "filled"
    assert a.submit(order())["status"] == "duplicate"
    assert len(broker.positions) == 1
    broker.hit_stop(next(iter(broker.positions)))
    assert a.submit(order())["status"] == "duplicate"  # found in deal history after the close


def test_slippage_and_wrong_side_refused(settings):
    broker = FakeBroker(ask=2651.0)
    assert "Slippage" in adapter(settings, broker).submit(order())["reason"]
    assert not broker.positions
    assert "wrong side" in adapter(settings).submit(order(stop_loss=2655.0))["reason"]


def test_stop_reattached_or_position_closed(settings):
    broker = FakeBroker()
    broker.drop_sl_on_fill = True
    result = adapter(settings, broker).submit(order())
    assert result["status"] == "filled" and result["protection"]["protected"]
    assert broker.positions[result["ticket"]].sl == 2645.0

    unsafe = FakeBroker()
    unsafe.drop_sl_on_fill = True
    unsafe.refuse_sltp = True
    result = adapter(settings, unsafe).submit(order(client_id="x2"))
    assert result["status"] == "closed_unprotected" and not unsafe.positions


def test_broker_rejection_is_reported(settings):
    broker = FakeBroker()
    broker.reject_next = True
    result = adapter(settings, broker).submit(order())
    assert result["status"] == "rejected" and result["retcode"] == 10006


def test_modify_stop_tighten_only(settings):
    broker = FakeBroker()
    a = adapter(settings, broker)
    ticket = a.submit(order())["ticket"]
    assert not a.modify_stop(ticket, "BUY", 2640.0)["accepted"]
    assert a.modify_stop(ticket, "BUY", 2647.0)["accepted"]
    assert broker.positions[ticket].sl == 2647.0


def test_closed_result_from_deal_history(settings):
    broker = FakeBroker()
    a = adapter(settings, broker)
    ticket = a.submit(order())["ticket"]
    broker.hit_stop(ticket)
    result = a.closed_result(ticket)
    assert result["exit_reason"] == "stop (broker)" and result["pnl"] < 0


def test_client_id_is_deterministic():
    assert client_id("XAUUSD", "2026-11-05T14:00", "BUY") == client_id("XAUUSD", "2026-11-05T14:00", "BUY")
    assert client_id("XAUUSD", "2026-11-05T14:00", "BUY") != client_id("XAUUSD", "2026-11-05T14:05", "BUY")


# ---------------------------------------------------------------- service
def _flat_engine(end: str):
    times = pd.date_range("2026-11-02", end, freq="5min", inclusive="left")
    n = len(times)
    p = np.full(n, 2650.0)
    engine = MarketDataEngine()
    engine.load_base(PriceFrame("M5", times, p, p + 0.3, p - 0.3, p, np.full(n, 100.0), np.full(n, 0.2)), "test", "test")
    return engine


def _force_buy(monkeypatch):
    def fake(context, *args):
        return {"instrument": "XAUUSD", "direction": "BUY", "decision": "BUY", "price": 2650.0, "entry_low": 2649.5,
                "entry_high": 2650.2, "stop_loss": 2645.0, "take_profits": [2660.0, 2665.0], "reward_risk": 2.0,
                "evidence": [], "fundamental_context": [], "confidence": 70.0, "invalidation": [],
                "timestamp": context.timestamp.isoformat(), "component_scores": {}, "reasons": [],
                "candidate_direction": "BUY", "actionable": True}

    monkeypatch.setattr("app.service.decide", fake)


@pytest.fixture
def broker():
    return FakeBroker()


@pytest.fixture
def live(settings, tmp_path, monkeypatch, broker):
    monkeypatch.setattr("app.service.MT5Adapter", lambda s: MT5Adapter(s, module=broker))
    svc = TradingService(live_settings(settings), ROOT, engine=_flat_engine("2026-11-05T14:00"), state_dir=tmp_path)
    return svc


def test_live_needs_authorization_in_env(settings, tmp_path, monkeypatch, broker):
    monkeypatch.setattr("app.service.MT5Adapter", lambda s: MT5Adapter(s, module=broker))
    svc = TradingService(live_settings(settings, live_trading_authorized=False), ROOT, engine=_flat_engine("2026-11-05T14:00"), state_dir=tmp_path)
    result = svc.request_live(ALL_TRUE)
    assert not result["accepted"] and "explicit authorization" in result["missing"]
    assert any("LIVE_TRADING_AUTHORIZED" in h for h in result["hints"])


def test_live_refused_on_real_account(settings, tmp_path, monkeypatch):
    real = FakeBroker(account_mode=2)
    monkeypatch.setattr("app.service.MT5Adapter", lambda s: MT5Adapter(s, module=real))
    svc = TradingService(live_settings(settings), ROOT, engine=_flat_engine("2026-11-05T14:00"), state_dir=tmp_path)
    result = svc.request_live(ALL_TRUE)
    assert not result["accepted"] and any("REAL-money" in h for h in result["hints"])


def test_live_order_flow_and_no_duplicate_on_repoll(live, broker, monkeypatch):
    _force_buy(monkeypatch)
    assert live.request_live(ALL_TRUE)["accepted"] and live.book.mode == "live"
    r = live.evaluate()
    assert r["orders_sent_to_broker"] == 1 and r["live_order"]["status"] == "filled"
    assert len(broker.positions) == 1 and len(live.live_positions) == 1
    assert live.evaluate()["orders_sent_to_broker"] == 0  # same bar
    assert len(broker.positions) == 1


def test_one_position_limit_counts_live_positions(live, broker, monkeypatch):
    _force_buy(monkeypatch)
    live.request_live(ALL_TRUE)
    live.evaluate()
    live.engine = _flat_engine("2026-11-05T14:05")
    r = live.evaluate()
    assert r["orders_sent_to_broker"] == 0 and "Maximum open positions has been reached." in r["risk"]["reasons"]


def test_broker_stop_is_reconciled(live, broker, monkeypatch):
    _force_buy(monkeypatch)
    live.request_live(ALL_TRUE)
    ticket = live.evaluate()["live_order"]["ticket"]
    broker.hit_stop(ticket)
    closed = live.reconcile_live()
    assert len(closed) == 1 and closed[0]["exit_reason"] == "stop (broker)" and closed[0]["pnl"] < 0
    assert live.account.consecutive_losses == 1
    assert live.account.equity == pytest.approx(broker.account_info().equity)
    assert not live.live_positions


def test_emergency_closes_broker_positions(live, broker, monkeypatch):
    _force_buy(monkeypatch)
    live.request_live(ALL_TRUE)
    live.evaluate()
    result = live.emergency_shutdown("test")
    assert not broker.positions and not live.live_positions
    assert result["mode"] == "analysis" and not live.book.live_enabled
    assert not live.request_live(ALL_TRUE)["accepted"]  # must clear the shutdown first


def test_live_positions_closed_before_nfp(settings, tmp_path, monkeypatch, broker):
    monkeypatch.setattr("app.service.MT5Adapter", lambda s: MT5Adapter(s, module=broker))
    svc = TradingService(live_settings(settings), ROOT, engine=_flat_engine("2026-11-06T11:00"), state_dir=tmp_path)
    _force_buy(monkeypatch)
    svc.request_live(ALL_TRUE)
    assert svc.evaluate()["orders_sent_to_broker"] == 1
    svc.engine = _flat_engine("2026-11-06T13:20")  # 10 minutes before NFP
    r = svc.evaluate()
    assert not broker.positions and r["orders_sent_to_broker"] == 0
    closes = [x["payload"] for x in svc.journal.rows if x["payload"].get("event") == "close" and x["payload"].get("mode") == "live"]
    assert closes and "Non-Farm" in closes[0]["exit_reason"]


def test_unknown_position_with_our_magic_is_adopted(live, broker):
    a = live.broker
    ticket = a.submit(order(client_id="manual"))["ticket"]
    live.reconcile_live()
    assert str(ticket) in live.live_positions and live.live_positions[str(ticket)]["adopted"]


def test_account_switched_to_real_blocks_orders(live, broker, monkeypatch):
    _force_buy(monkeypatch)
    live.request_live(ALL_TRUE)
    broker.account_mode = 2
    r = live.evaluate()
    assert r["live_order"]["status"] == "refused" and not broker.positions


# ---------------------------------------------------------------- restart
def test_restart_restores_state(settings, tmp_path):
    from tests.test_live_path import _force_buy as force_paper, _paper_until  # noqa: F401

    svc = _paper_until(settings, tmp_path, "2026-11-05T14:00")
    svc.account.equity = 99_000
    svc.account.loss_pause = True
    svc.book.paper_positions.append({"id": "paper-9", "direction": "BUY", "entry": 2650.0, "stop_loss": 2645.0,
                                     "take_profit": 2660.0, "lots": 0.1, "risk_amount": 50, "opened_at": "2026-11-05T13:00:00+00:00"})
    svc.set_mode("analysis")
    svc.evaluate()
    again = TradingService(svc.settings, ROOT, engine=svc.engine, state_dir=tmp_path)
    assert again.account.equity == 99_000 and again.account.loss_pause
    assert again.book.paper_positions[0]["id"] == "paper-9"
    assert again.book.mode == "analysis"
    assert again.evaluate()["repeat_of_journaled_bar"] is True
    assert any("State restored" in r["note"] for r in again.journal.rows)


def test_live_mode_is_not_restored(live, broker, monkeypatch, tmp_path):
    _force_buy(monkeypatch)
    live.request_live(ALL_TRUE)
    live.evaluate()
    monkeypatch.setattr("app.service.MT5Adapter", lambda s: MT5Adapter(s, module=broker))
    again = TradingService(live.settings, ROOT, engine=live.engine, state_dir=tmp_path)
    assert again.book.mode == "paper" and not again.book.live_enabled
    assert len(again.live_positions) == 1  # still tracked, so broker closes are still recorded


def test_emergency_survives_restart(service, tmp_path):
    service.emergency_shutdown("test")
    again = TradingService(service.settings, ROOT, engine=service.engine, state_dir=service.state_dir)
    assert again.account.emergency_shutdown


def test_corrupt_state_file_starts_fresh(settings, tmp_path, synthetic_engine):
    (tmp_path / "state.json").write_text("{not json")
    svc = TradingService(settings, ROOT, engine=synthetic_engine, state_dir=tmp_path)
    assert svc.account.equity == settings.account_equity
    assert (tmp_path / "state.corrupt.json").exists()


def test_live_flow_through_the_http_api(live, broker, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    _force_buy(monkeypatch)
    with TestClient(create_app(live)) as c:
        status = c.get("/api/status").json()
        assert status["broker"]["ready"] and status["broker"]["account_type"] == "demo"
        armed = c.post("/api/live/request", json={k: True for k in ("explicit_authorization", "broker_connected", "account_risk_limits_set", "emergency_shutdown_armed")}).json()
        assert armed["accepted"] and c.get("/api/status").json()["mode"] == "live"
        sig = c.get("/api/signal").json()
        assert sig["orders_sent_to_broker"] == 1
        risk = c.get("/api/risk").json()
        assert risk["open_positions"][0]["mode"] == "live"
        stop = c.post("/api/emergency/shutdown", json={"reason": "api test"}).json()
        assert stop["live_still_open"] == [] and not broker.positions
        kinds = {r["payload"].get("event") for r in c.get("/api/journal?kind=execution").json()}
        assert {"live_order", "live_close_request", "close"} <= kinds
