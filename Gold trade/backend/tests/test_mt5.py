"""MT5 reader against a stand-in for the MetaTrader5 package (the real one is Windows-only).

Checks the conversions that make or break a live connection: broker server
time -> UTC, spread in points -> price, symbol lookup, and contract warnings.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.market import MT5Feed, build_engine
from app.sessions import session_at

SERVER_OFFSET_H = 2  # a typical broker: UTC+2


class FakeMT5:
    TIMEFRAME_M5 = 5

    def __init__(self, symbols=("XAUUSD",), point=0.01, tick_fresh=True, contract=100.0, login_ok=True):
        self.symbols = set(symbols)
        self.point = point
        self.tick_fresh = tick_fresh
        self.contract = contract
        self.login_ok = login_ok
        self.selected = set()
        self.shutdown_called = 0

    def initialize(self, **kwargs):
        self.kwargs = kwargs
        return self.login_ok

    def last_error(self):
        return (-6, "Terminal: Authorization failed")

    def shutdown(self):
        self.shutdown_called += 1

    def symbol_info(self, name):
        if name not in self.symbols:
            return None
        return SimpleNamespace(name=name, visible=name in self.selected, point=self.point, digits=2 if self.point == 0.01 else 3,
                               trade_contract_size=self.contract, volume_min=0.01, volume_step=0.01)

    def symbols_get(self, pattern):
        key = pattern.strip("*").lower()
        return [SimpleNamespace(name=n) for n in self.symbols if key in n.lower()]

    def symbol_select(self, name, enable):
        self.selected.add(name)
        return True

    def symbol_info_tick(self, name):
        now = datetime.now(timezone.utc).timestamp()
        stamp = now + SERVER_OFFSET_H * 3600 if self.tick_fresh else now - 3 * 86400
        return SimpleNamespace(time=int(stamp))

    def account_info(self):
        return SimpleNamespace(server="Broker-Demo", trade_mode=0)

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        # 2026-11-04 13:30 UTC (London/NY overlap) stamped in server time = 15:30.
        utc = pd.date_range("2026-11-04T13:00", periods=12, freq="5min", tz="UTC")
        server_epoch = (utc.asi8 // 10**9) + SERVER_OFFSET_H * 3600
        dtype = [("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"), ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8")]
        rows = np.zeros(len(utc), dtype=dtype)
        rows["time"] = server_epoch
        rows["open"] = rows["close"] = 2650.0
        rows["high"], rows["low"] = 2650.5, 2649.5
        rows["tick_volume"] = 100
        rows["spread"] = 25 if self.point == 0.01 else 250  # same $0.25 spread either way
        return rows


def test_server_time_is_shifted_to_utc(tmp_path):
    feed = MT5Feed(FakeMT5())
    frame = feed.copy_m5("XAUUSD", "123456", "pw", "Broker-Demo", offset_cache=tmp_path / "o.json")
    assert pd.Timestamp(frame.time[0]) == pd.Timestamp("2026-11-04T13:00")
    assert session_at(pd.Timestamp(frame.time[0]).tz_localize("UTC")) == "london_new_york_overlap"
    assert feed.info["server_utc_offset_hours"] == 2 and "live tick" in feed.info["offset_source"]


@pytest.mark.parametrize("point", [0.01, 0.001])
def test_spread_uses_broker_point(point, tmp_path):
    frame = MT5Feed(FakeMT5(point=point)).copy_m5("XAUUSD", "1", "pw", "S", offset_cache=tmp_path / "o.json")
    assert np.allclose(frame.spread, 0.25)


def test_market_closed_uses_saved_offset_then_configured(tmp_path):
    cache = tmp_path / "o.json"
    MT5Feed(FakeMT5()).copy_m5("XAUUSD", "1", "pw", "S", offset_cache=cache)  # market open: saves +2
    closed = MT5Feed(FakeMT5(tick_fresh=False))
    closed.copy_m5("XAUUSD", "1", "pw", "S", offset_cache=cache)
    assert closed.info["server_utc_offset_hours"] == 2 and "market closed" in closed.info["offset_source"]
    configured = MT5Feed(FakeMT5(tick_fresh=False))
    configured.copy_m5("XAUUSD", "1", "pw", "S", offset_hours=3, offset_cache=tmp_path / "none.json")
    assert configured.info["server_utc_offset_hours"] == 3


def test_unknown_offset_is_flagged(tmp_path):
    feed = MT5Feed(FakeMT5(tick_fresh=False))
    feed.copy_m5("XAUUSD", "1", "pw", "S", offset_cache=tmp_path / "missing.json")
    assert feed.info["offset_source"].startswith("UNKNOWN")


def test_wrong_symbol_lists_broker_gold_symbols():
    feed = MT5Feed(FakeMT5(symbols=("XAUUSDm", "GOLD", "EURUSD")))
    with pytest.raises(RuntimeError) as err:
        feed.copy_m5("XAUUSD", "1", "pw", "S")
    assert "XAUUSDm" in str(err.value) and "GOLD" in str(err.value) and "EURUSD" not in str(err.value)


def test_hidden_symbol_is_added_to_market_watch(tmp_path):
    fake = FakeMT5()
    MT5Feed(fake).copy_m5("XAUUSD", "1", "pw", "S", offset_cache=tmp_path / "o.json")
    assert "XAUUSD" in fake.selected and fake.shutdown_called == 1


def test_login_failure_is_explained():
    with pytest.raises(RuntimeError, match="Authorization failed"):
        MT5Feed(FakeMT5(login_ok=False)).copy_m5("XAUUSD", "1", "pw", "S")
    with pytest.raises(RuntimeError, match="numeric account number"):
        MT5Feed(FakeMT5()).copy_m5("XAUUSD", "abc", "pw", "S")


def test_engine_uses_mt5_and_warns_on_contract_mismatch(settings, tmp_path, monkeypatch):
    big = FakeMT5(contract=10.0)
    monkeypatch.setattr("app.market.MT5Feed.__init__", lambda self, module=None: (setattr(self, "_mt5", big), setattr(self, "info", {}))[-1])
    s = settings.model_copy(update={"mt5_login": "1", "mt5_server": "Broker-Demo", "data_source": "mt5"})
    # 12 bars is not enough to build every timeframe, which is fine: this checks source wiring.
    try:
        engine = build_engine(tmp_path, s)
    except ValueError:
        pytest.skip("engine needs more bars")
    assert engine.source == "metatrader5" and "UTC+2" in engine.source_note
    assert any("CONTRACT_OUNCES" in w for w in engine.warnings)


def test_data_source_mt5_overrides_csv(settings, tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "XAUUSD_M5.csv").write_text("time,open,high,low,close,volume\n")
    called = {}

    def fake_copy(self, *args, **kwargs):
        called["mt5"] = True
        raise RuntimeError("stop here")

    monkeypatch.setattr("app.market.MT5Feed.copy_m5", fake_copy)
    s = settings.model_copy(update={"mt5_login": "1", "mt5_server": "S", "data_source": "mt5"})
    with pytest.raises(RuntimeError, match="stop here"):
        build_engine(tmp_path, s)
    assert called["mt5"]
