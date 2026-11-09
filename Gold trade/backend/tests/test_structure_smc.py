"""Modules 2 and 3: structure and SMC descriptions."""

import numpy as np

from app.frames import PriceFrame
from app.smc import smc_snapshot
from app.structure import find_swings, market_structure, timeframe_conflict


def _frame(close, spread=0.2):
    n = len(close)
    close = np.asarray(close, float)
    # Open halfway from the prior close so a peak bar's high is strictly above its neighbours.
    open_ = np.r_[close[0], (close[:-1] + close[1:]) / 2]
    t = np.arange(n).astype("timedelta64[h]") + np.datetime64("2025-01-06T00:00")
    return PriceFrame("H1", t, open_, np.maximum(open_, close) + 0.2, np.minimum(open_, close) - 0.2, close, np.ones(n), np.full(n, spread))


def zigzag(up=True, legs=10):
    pts = []
    level = 100.0
    for leg in range(legs):
        step = (3 if leg % 2 == 0 else -1.5) * (1 if up else -1)
        pts.extend(np.linspace(level, level + step, 8)[1:])
        level += step
    return np.array(pts)


def test_swings_need_right_side_confirmation():
    f = _frame(zigzag())
    highs, _ = find_swings(f.high, f.low, 3, 3)
    assert all(i + 3 < len(f) for i in highs)


def test_bullish_and_bearish_bias():
    assert market_structure(_frame(zigzag(True))).bias == "bullish"
    assert market_structure(_frame(zigzag(False))).bias == "bearish"


def test_bos_and_choch_events():
    up = zigzag(True)
    breakout = np.r_[up, np.linspace(up[-1], up.max() + 5, 5)]
    assert market_structure(_frame(breakout)).event == "bos_bullish"
    collapse = np.r_[up, np.linspace(up[-1], up.min() - 5, 8)]
    assert market_structure(_frame(collapse)).event in {"choch_bearish", "bos_bearish"}


def test_timeframe_conflict():
    assert timeframe_conflict({"D1": "bullish", "H1": "bearish"})
    assert not timeframe_conflict({"D1": "bullish", "H1": "ranging"})


def test_smc_snapshot_is_context_only(uptrend_engine):
    snap = smc_snapshot(uptrend_engine.frame("M15"))
    assert snap["confirmation_required"] is True
    assert snap["premium_discount"] in {"premium", "discount", "equilibrium", "unknown"}
    for gap in snap["fair_value_gaps"]:
        assert gap["high"] > gap["low"]


def test_sweep_detected():
    base = [100, 101, 102, 105, 102, 101, 100, 101, 102, 105, 102, 101, 100, 99, 100, 101, 102, 103]
    close = np.array(base * 2 + [104, 104.5, 104.8], float)
    f = _frame(close)
    f.high[-1] = 106.5  # wick through the equal highs, close back below
    f.close[-1] = 104.0
    f.open[-1] = 104.8
    f.low[-1] = 103.8
    snap = smc_snapshot(f)
    assert snap["equal_highs"], "equal highs should be detected"
    assert any(s["side"] == "buy_side" for s in snap["sweeps"])
