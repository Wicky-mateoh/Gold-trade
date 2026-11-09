"""Module 1: data engine, validation, synchronization."""

import numpy as np
import pandas as pd

from app.frames import PriceFrame, resample_frame, validate_frame
from app.market import TIMEFRAMES, MarketDataEngine
from app.sessions import session_at
from tests.conftest import make_m5


def test_all_timeframes_built_from_m5(synthetic_engine):
    for tf in TIMEFRAMES:
        frame = synthetic_engine.frame(tf)
        assert frame is not None and len(frame) > 0, tf


def test_frames_are_synchronized(synthetic_engine):
    assert synthetic_engine.synchronization_report()["aligned"] is True


def test_duplicates_dropped_and_reported():
    f = make_m5(days=2)
    dup = PriceFrame("M5", np.r_[f.time, f.time[:5]], np.r_[f.open, f.open[:5]], np.r_[f.high, f.high[:5]],
                     np.r_[f.low, f.low[:5]], np.r_[f.close, f.close[:5]], np.r_[f.volume, f.volume[:5]], np.r_[f.spread, f.spread[:5]])
    report = validate_frame(dup)
    assert report.duplicates == 5
    assert len(report.frame) == len(f)


def test_corrupt_candles_removed():
    f = make_m5(days=5)
    f.high[10] = f.low[10] - 1  # impossible bar
    report = validate_frame(f)
    assert len(report.frame) == len(f) - 1
    assert any("corrupt" in w for w in report.warnings)


def test_heavy_corruption_rejects_frame():
    f = make_m5(days=1)
    f.close[: len(f) // 10] = np.nan
    report = validate_frame(f)
    assert report.rejected and report.frame is None


def test_missing_candles_detected_but_weekend_ignored():
    f = make_m5(days=10)
    assert validate_frame(f).missing_bars == 0  # weekends are not gaps
    keep = np.ones(len(f), bool)
    keep[100:106] = False
    gap = PriceFrame("M5", f.time[keep], f.open[keep], f.high[keep], f.low[keep], f.close[keep], f.volume[keep], f.spread[keep])
    assert validate_frame(gap).missing_bars == 6


def test_resample_ohlc_matches_children():
    f = make_m5(days=3)
    h1 = resample_frame(f, "H1")
    idx = pd.DatetimeIndex(f.time)
    first = (idx >= pd.Timestamp(h1.time[0])) & (idx < pd.Timestamp(h1.time[0]) + pd.Timedelta(hours=1))
    assert h1.open[0] == f.open[first][0]
    assert h1.close[0] == f.close[first][-1]
    assert h1.high[0] == f.high[first].max()
    assert h1.low[0] == f.low[first].min()


def test_incomplete_bucket_dropped():
    f = make_m5(days=1)
    cut = PriceFrame("M5", f.time[:-3], f.open[:-3], f.high[:-3], f.low[:-3], f.close[:-3], f.volume[:-3], f.spread[:-3])
    h1 = resample_frame(cut, "H1")
    assert len(h1) == 23  # the last hour has only 9 of 12 children


def test_sessions():
    assert session_at(pd.Timestamp("2025-01-06T03:00Z")) == "asia"
    assert session_at(pd.Timestamp("2025-01-06T08:00Z")) == "london"
    assert session_at(pd.Timestamp("2025-01-06T14:00Z")) == "london_new_york_overlap"
    assert session_at(pd.Timestamp("2025-01-06T18:00Z")) == "new_york"
    assert session_at(pd.Timestamp("2025-01-06T23:00Z")) == "off"


def test_engine_rejects_bad_base():
    engine = MarketDataEngine()
    f = make_m5(days=1)
    f.close[:] = -1
    try:
        engine.load_base(f, "x", "x")
    except ValueError:
        return
    raise AssertionError("expected rejection")
