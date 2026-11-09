"""Shared fixtures. All data is generated in-process; no network or broker."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.config import Settings
from app.frames import PriceFrame
from app.market import MarketDataEngine, synthetic_m5

ROOT = Path(__file__).resolve().parents[2]


def make_m5(days: int = 60, drift: float = 0.0, seed: int = 3, spread: float = 0.2, start: str = "2025-01-06") -> PriceFrame:
    """Weekday M5 bars. A positive drift with a slow cycle gives trends with pullbacks."""
    rng = np.random.default_rng(seed)
    times = pd.date_range(start, periods=days * 288 * 7 // 5 + 288 * 3, freq="5min")
    times = times[times.dayofweek < 5][: days * 288]
    n = len(times)
    cycle = np.sin(np.arange(n) / (288 * 1.5)) * 0.25
    close = 2600 + np.cumsum(drift + cycle + rng.normal(0, 0.3, n))
    open_ = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0.15, 0.05, n))
    return PriceFrame(
        "M5",
        times,
        open_,
        np.maximum(open_, close) + wick,
        np.minimum(open_, close) - wick,
        close,
        rng.integers(80, 400, n).astype(float),
        np.full(n, spread),
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture(scope="session")
def synthetic_engine() -> MarketDataEngine:
    engine = MarketDataEngine()
    engine.load_base(synthetic_m5(days=160), "synthetic", "test")
    return engine


@pytest.fixture(scope="session")
def uptrend_engine() -> MarketDataEngine:
    engine = MarketDataEngine()
    engine.load_base(make_m5(days=200, drift=0.02), "test", "uptrend")
    return engine


@pytest.fixture
def service(settings, synthetic_engine, tmp_path):
    from app.service import TradingService

    return TradingService(settings, ROOT, engine=synthetic_engine, state_dir=tmp_path)
