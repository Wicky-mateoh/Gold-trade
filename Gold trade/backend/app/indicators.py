"""Causal technical indicators.

Every series at index i depends only on rows at or before i. Callers that
need a confirmed reading should pass a prefix, not a future-filled frame.
"""

from __future__ import annotations

import numpy as np


def ema(values: np.ndarray, period: int) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    out = np.full(values.shape, np.nan)
    if period < 1 or len(values) < period:
        return out
    alpha = 2.0 / (period + 1.0)
    out[period - 1] = float(np.mean(values[:period]))
    for i in range(period, len(values)):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def rsi(values: np.ndarray, period: int = 14) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    out = np.full(values.shape, np.nan)
    if period < 1 or len(values) <= period:
        return out
    delta = np.diff(values)
    gain = np.clip(delta, 0, None)
    loss = np.clip(-delta, 0, None)
    avg_gain = float(np.mean(gain[:period]))
    avg_loss = float(np.mean(loss[:period]))

    def _value(avg_g: float, avg_l: float) -> float:
        if avg_l == 0:
            return 100.0
        rs = avg_g / avg_l
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = _value(avg_gain, avg_loss)
    for i in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        out[i + 1] = _value(avg_gain, avg_loss)
    return out


def macd(values: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    line = ema(values, fast) - ema(values, slow)
    signal_line = _ema_skip_nan(line, signal)
    hist = line - signal_line
    return line, signal_line, hist


def _ema_skip_nan(values: np.ndarray, period: int) -> np.ndarray:
    out = np.full(values.shape, np.nan)
    finite_idx = np.flatnonzero(np.isfinite(values))
    if len(finite_idx) < period:
        return out
    start = int(finite_idx[period - 1])
    window = values[finite_idx[:period]]
    out[start] = float(np.mean(window))
    alpha = 2.0 / (period + 1.0)
    previous = out[start]
    for i in range(start + 1, len(values)):
        if not np.isfinite(values[i]):
            continue
        previous = alpha * values[i] + (1.0 - alpha) * previous
        out[i] = previous
    return out


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    prev = np.empty_like(close)
    prev[0] = close[0]
    prev[1:] = close[:-1]
    ranges = np.vstack([high - low, np.abs(high - prev), np.abs(low - prev)])
    return np.max(ranges, axis=0)


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    tr = true_range(high, low, close)
    out = np.full(tr.shape, np.nan)
    if len(tr) < period:
        return out
    out[period - 1] = float(np.mean(tr[:period]))
    for i in range(period, len(tr)):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def bollinger(values: np.ndarray, period: int = 20, stds: float = 2.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    values = np.asarray(values, dtype=float)
    mid = np.full(values.shape, np.nan)
    upper = np.full(values.shape, np.nan)
    lower = np.full(values.shape, np.nan)
    if len(values) < period:
        return mid, upper, lower
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        center = float(np.mean(window))
        sigma = float(np.std(window))
        mid[i] = center
        upper[i] = center + stds * sigma
        lower[i] = center - stds * sigma
    return mid, upper, lower


def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    n = len(close)
    adx_out = np.full(n, np.nan)
    plus_out = np.full(n, np.nan)
    minus_out = np.full(n, np.nan)
    if n <= period * 2:
        return adx_out, plus_out, minus_out
    up_move = high[1:] - high[:-1]
    down_move = low[:-1] - low[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = true_range(high, low, close)[1:]
    atr_s = np.zeros(len(tr))
    plus_s = np.zeros(len(tr))
    minus_s = np.zeros(len(tr))
    atr_s[period - 1] = np.sum(tr[:period])
    plus_s[period - 1] = np.sum(plus_dm[:period])
    minus_s[period - 1] = np.sum(minus_dm[:period])
    for i in range(period, len(tr)):
        atr_s[i] = atr_s[i - 1] - (atr_s[i - 1] / period) + tr[i]
        plus_s[i] = plus_s[i - 1] - (plus_s[i - 1] / period) + plus_dm[i]
        minus_s[i] = minus_s[i - 1] - (minus_s[i - 1] / period) + minus_dm[i]
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * plus_s / atr_s
        minus_di = 100.0 * minus_s / atr_s
        denom = plus_di + minus_di
        dx = 100.0 * np.abs(plus_di - minus_di) / denom
    start = (period - 1) + period
    if start >= len(dx):
        return adx_out, plus_out, minus_out
    adx_vals = np.full(len(dx), np.nan)
    adx_vals[start] = float(np.nanmean(dx[period - 1 : start + 1]))
    for i in range(start + 1, len(dx)):
        if not np.isfinite(dx[i]) or not np.isfinite(adx_vals[i - 1]):
            continue
        adx_vals[i] = (adx_vals[i - 1] * (period - 1) + dx[i]) / period
    adx_out[1:] = adx_vals
    plus_out[1:] = plus_di
    minus_out[1:] = minus_di
    return adx_out, plus_out, minus_out


def fibonacci(swing_high: float, swing_low: float) -> dict[str, float]:
    span = float(swing_high) - float(swing_low)
    ratios = {
        "0.0": 0.0,
        "0.236": 0.236,
        "0.382": 0.382,
        "0.5": 0.5,
        "0.618": 0.618,
        "0.786": 0.786,
        "1.0": 1.0,
    }
    return {name: float(swing_high) - ratio * span for name, ratio in ratios.items()}


def volume_ratio(volume: np.ndarray, index: int, lookback: int = 20) -> float | None:
    if index < lookback:
        return None
    window = np.asarray(volume[index - lookback : index], dtype=float)
    base = float(np.mean(window))
    if base <= 0 or not np.isfinite(volume[index]):
        return None
    return float(volume[index] / base)


def indicator_snapshot(frame) -> dict:
    close = frame.close
    high = frame.high
    low = frame.low
    ema20 = ema(close, 20)
    ema50 = ema(close, 50)
    ema100 = ema(close, 100)
    ema200 = ema(close, 200)
    rsi_v = rsi(close)
    macd_line, macd_signal, macd_hist = macd(close)
    atr_v = atr(high, low, close)
    mid, upper, lower = bollinger(close)
    adx_v, plus_di, minus_di = adx(high, low, close)
    i = len(frame) - 1

    def last(series: np.ndarray) -> float | None:
        if i < 0 or not np.isfinite(series[i]):
            return None
        return float(series[i])

    return {
        "ema20": last(ema20),
        "ema50": last(ema50),
        "ema100": last(ema100),
        "ema200": last(ema200),
        "rsi": last(rsi_v),
        "macd": last(macd_line),
        "macd_signal": last(macd_signal),
        "macd_hist": last(macd_hist),
        "atr": last(atr_v),
        "bollinger_mid": last(mid),
        "bollinger_upper": last(upper),
        "bollinger_lower": last(lower),
        "adx": last(adx_v),
        "plus_di": last(plus_di),
        "minus_di": last(minus_di),
        "volume_ratio": volume_ratio(frame.volume, i),
        "close": None if i < 0 else float(close[i]),
    }


def indicator_agreement(snapshot: dict) -> float:
    """Return a vote in [-1, 1]. Positive leans bullish. No single input decides."""
    votes: list[float] = []
    close = snapshot.get("close")
    for key, weight in (("ema20", 1.0), ("ema50", 1.0), ("ema200", 1.2)):
        level = snapshot.get(key)
        if close is None or level is None:
            continue
        votes.append(weight if close > level else -weight)
    rsi_v = snapshot.get("rsi")
    if rsi_v is not None:
        if rsi_v >= 75 or rsi_v <= 25:
            votes.append(0.0)
        elif rsi_v >= 55:
            votes.append(0.8)
        elif rsi_v <= 45:
            votes.append(-0.8)
    hist = snapshot.get("macd_hist")
    if hist is not None:
        votes.append(1.0 if hist > 0 else -1.0 if hist < 0 else 0.0)
    plus_di = snapshot.get("plus_di")
    minus_di = snapshot.get("minus_di")
    adx_v = snapshot.get("adx")
    if plus_di is not None and minus_di is not None and adx_v is not None and adx_v >= 18:
        votes.append(1.0 if plus_di > minus_di else -1.0)
    mid = snapshot.get("bollinger_mid")
    if close is not None and mid is not None:
        votes.append(0.5 if close > mid else -0.5)
    if not votes:
        return 0.0
    raw = float(np.mean(votes))
    return float(np.clip(raw / 1.2, -1.0, 1.0))


def volatility_regime(atr_values: np.ndarray) -> str:
    finite = atr_values[np.isfinite(atr_values)]
    if len(finite) < 20:
        return "unknown"
    latest = float(finite[-1])
    rank = float(np.mean(finite <= latest))
    if rank >= 0.97:
        return "extreme"
    if rank >= 0.8:
        return "high"
    if rank <= 0.2:
        return "low"
    return "normal"
