"""Market-structure engine.

Swings are confirmed only after `right` bars have printed. Break of structure
and change of character are descriptions of price, not orders.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.indicators import atr


@dataclass
class StructureState:
    bias: str
    swing_highs: list[dict]
    swing_lows: list[dict]
    event: str | None
    support: list[float]
    resistance: list[float]
    demand_zones: list[dict]
    supply_zones: list[dict]
    regime: str
    last_swing_high: float | None
    last_swing_low: float | None


def find_swings(high: np.ndarray, low: np.ndarray, left: int = 3, right: int = 3) -> tuple[list[int], list[int]]:
    highs: list[int] = []
    lows: list[int] = []
    n = len(high)
    for i in range(left, n - right):
        if high[i] > np.max(high[i - left : i]) and high[i] > np.max(high[i + 1 : i + right + 1]):
            highs.append(i)
        if low[i] < np.min(low[i - left : i]) and low[i] < np.min(low[i + 1 : i + right + 1]):
            lows.append(i)
    return highs, lows


def _visible(indices: list[int], right: int, end: int) -> list[int]:
    return [i for i in indices if i + right <= end - 1]


def market_structure(frame, left: int = 3, right: int = 3) -> StructureState:
    if frame is None or len(frame) < left + right + 5:
        return StructureState("unknown", [], [], None, [], [], [], [], "unknown", None, None)
    end = len(frame)
    highs, lows = find_swings(frame.high, frame.low, left, right)
    high_idx = _visible(highs, right, end)
    low_idx = _visible(lows, right, end)
    high_points = [_point(frame, i, "high") for i in high_idx[-8:]]
    low_points = [_point(frame, i, "low") for i in low_idx[-8:]]
    bias = "unknown"
    if len(high_idx) >= 2 and len(low_idx) >= 2:
        hh = frame.high[high_idx[-1]] > frame.high[high_idx[-2]]
        hl = frame.low[low_idx[-1]] > frame.low[low_idx[-2]]
        lh = frame.high[high_idx[-1]] < frame.high[high_idx[-2]]
        ll = frame.low[low_idx[-1]] < frame.low[low_idx[-2]]
        if hh and hl:
            bias = "bullish"
        elif lh and ll:
            bias = "bearish"
        else:
            bias = "ranging"
    event = _structure_event(frame, bias, high_idx, low_idx)
    atr_v = atr(frame.high, frame.low, frame.close)
    atr_last = float(atr_v[-1]) if np.isfinite(atr_v[-1]) else None
    support, resistance = _clusters(low_points, high_points, atr_last)
    demand, supply = _zones(frame, atr_v)
    regime = _regime(bias, atr_last, frame)
    return StructureState(
        bias=bias,
        swing_highs=high_points,
        swing_lows=low_points,
        event=event,
        support=support,
        resistance=resistance,
        demand_zones=demand,
        supply_zones=supply,
        regime=regime,
        last_swing_high=None if not high_idx else float(frame.high[high_idx[-1]]),
        last_swing_low=None if not low_idx else float(frame.low[low_idx[-1]]),
    )


def _point(frame, index: int, kind: str) -> dict:
    price = float(frame.high[index] if kind == "high" else frame.low[index])
    return {"index": int(index), "time": str(frame.time[index]), "price": price, "kind": kind}


def _structure_event(frame, bias: str, high_idx: list[int], low_idx: list[int]) -> str | None:
    close = float(frame.close[-1])
    if bias == "bullish" and low_idx and close < float(frame.low[low_idx[-1]]):
        return "choch_bearish"
    if bias == "bearish" and high_idx and close > float(frame.high[high_idx[-1]]):
        return "choch_bullish"
    if bias == "bullish" and high_idx and close > float(frame.high[high_idx[-1]]):
        return "bos_bullish"
    if bias == "bearish" and low_idx and close < float(frame.low[low_idx[-1]]):
        return "bos_bearish"
    return None


def _clusters(lows: list[dict], highs: list[dict], atr_last: float | None) -> tuple[list[float], list[float]]:
    tolerance = (atr_last or 1.0) * 0.25

    def cluster(points: list[dict]) -> list[float]:
        prices = [p["price"] for p in points]
        grouped: list[list[float]] = []
        for price in prices:
            if grouped and abs(price - np.mean(grouped[-1])) <= tolerance:
                grouped[-1].append(price)
            else:
                grouped.append([price])
        return [round(float(np.mean(bucket)), 3) for bucket in grouped[-4:]]

    return cluster(lows), cluster(highs)


def _zones(frame, atr_values: np.ndarray) -> tuple[list[dict], list[dict]]:
    demand: list[dict] = []
    supply: list[dict] = []
    start = max(2, len(frame) - 180)
    for i in range(start, len(frame)):
        if not np.isfinite(atr_values[i]):
            continue
        body = abs(float(frame.close[i] - frame.open[i]))
        span = float(frame.high[i] - frame.low[i])
        if span < 1.4 * float(atr_values[i]) or span == 0 or body / span < 0.6:
            continue
        origin = i - 1
        zone = {
            "time": str(frame.time[origin]),
            "low": float(frame.low[origin]),
            "high": float(frame.high[origin]),
        }
        if frame.close[i] > frame.open[i]:
            demand.append(zone)
        else:
            supply.append(zone)
    return demand[-3:], supply[-3:]


def _regime(bias: str, atr_last: float | None, frame) -> str:
    if bias == "unknown" or atr_last is None:
        return "unknown"
    window = frame.close[-40:] if len(frame) >= 40 else frame.close
    width = float(np.max(window) - np.min(window))
    if bias == "ranging" and width < atr_last * 6:
        return "consolidation"
    if bias in {"bullish", "bearish"}:
        return "trend"
    return "transition"


def timeframe_conflict(biases: dict[str, str]) -> bool:
    directional = [bias for bias in biases.values() if bias in {"bullish", "bearish"}]
    return "bullish" in directional and "bearish" in directional
