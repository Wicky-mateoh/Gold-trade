"""Module 4: indicators are correct and causal."""

import numpy as np

from app.indicators import adx, atr, bollinger, ema, fibonacci, indicator_agreement, indicator_snapshot, macd, rsi


def test_ema_seed_and_recursion():
    values = np.arange(1, 11, dtype=float)
    out = ema(values, 3)
    assert np.isnan(out[1]) and out[2] == 2.0
    assert np.isclose(out[3], 0.5 * 4 + 0.5 * 2.0)


def test_rsi_bounds_and_extremes():
    up = rsi(np.arange(50, dtype=float))
    assert up[-1] == 100.0
    noisy = rsi(np.random.default_rng(1).normal(0, 1, 300).cumsum() + 100)
    finite = noisy[np.isfinite(noisy)]
    assert finite.min() >= 0 and finite.max() <= 100


def test_atr_constant_range():
    high = np.full(50, 11.0)
    low = np.full(50, 10.0)
    close = np.full(50, 10.5)
    assert np.isclose(atr(high, low, close)[-1], 1.0)


def test_bollinger_contains_mean():
    v = np.random.default_rng(2).normal(100, 1, 100)
    mid, up, lo = bollinger(v)
    assert np.all((lo[19:] <= mid[19:]) & (mid[19:] <= up[19:]))


def test_adx_detects_trend():
    n = 200
    close = np.linspace(100, 200, n)
    a, plus, minus = adx(close + 0.5, close - 0.5, close)
    assert a[-1] > 25 and plus[-1] > minus[-1]


def test_indicators_are_causal():
    """Appending future bars must not change past values."""
    rng = np.random.default_rng(5)
    close = rng.normal(0, 1, 400).cumsum() + 2000
    high, low = close + 0.5, close - 0.5
    cut = 300
    for fn in (lambda c, h, l: ema(c, 20), lambda c, h, l: rsi(c), lambda c, h, l: macd(c)[2],
               lambda c, h, l: atr(h, l, c), lambda c, h, l: adx(h, l, c)[0], lambda c, h, l: bollinger(c)[1]):
        full = fn(close, high, low)[:cut]
        prefix = fn(close[:cut], high[:cut], low[:cut])
        assert np.allclose(full, prefix, equal_nan=True)


def test_fibonacci_levels():
    levels = fibonacci(200, 100)
    assert levels["0.0"] == 200 and levels["1.0"] == 100 and levels["0.5"] == 150


def test_agreement_requires_multiple_votes(uptrend_engine):
    snap = indicator_snapshot(uptrend_engine.frame("H4"))
    score = indicator_agreement(snap)
    assert -1 <= score <= 1
    assert indicator_agreement({"close": 10.0, "ema20": 9.0}) < 1.0  # one vote cannot max the score
