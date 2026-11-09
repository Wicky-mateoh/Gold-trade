"""Smart-money descriptions.

Liquidity, gaps, and blocks are context. None of these objects is an order.
A later signal still needs price-action confirmation and a passing risk check.
"""

from __future__ import annotations

import numpy as np

from app.indicators import atr
from app.structure import find_swings, market_structure


def smc_snapshot(frame, left: int = 3, right: int = 3) -> dict:
    empty = {
        "equal_highs": [],
        "equal_lows": [],
        "buy_side_liquidity": [],
        "sell_side_liquidity": [],
        "sweeps": [],
        "fair_value_gaps": [],
        "order_blocks": [],
        "breaker_blocks": [],
        "mitigation_blocks": [],
        "premium_discount": "unknown",
        "equilibrium": None,
        "dealing_high": None,
        "dealing_low": None,
        "displacement": None,
        "structure_shift": None,
        "confirmation_required": True,
    }
    if frame is None or len(frame) < 30:
        return empty
    atr_values = atr(frame.high, frame.low, frame.close)
    atr_last = float(atr_values[-1]) if np.isfinite(atr_values[-1]) else 1.0
    highs, lows = find_swings(frame.high, frame.low, left, right)
    visible_high = [i for i in highs if i + right <= len(frame) - 1]
    visible_low = [i for i in lows if i + right <= len(frame) - 1]
    tolerance = atr_last * 0.08
    equal_highs = _equals(frame, visible_high, "high", tolerance)
    equal_lows = _equals(frame, visible_low, "low", tolerance)
    sweeps = _sweeps(frame, equal_highs, equal_lows)
    gaps = _fair_value_gaps(frame, atr_values)
    blocks = _order_blocks(frame, atr_values)
    structure = market_structure(frame, left, right)
    premium = _premium_discount(frame, structure.last_swing_high, structure.last_swing_low)
    displacement = _latest_displacement(frame, atr_values)
    return {
        "equal_highs": equal_highs[-4:],
        "equal_lows": equal_lows[-4:],
        "buy_side_liquidity": [row["price"] for row in equal_highs[-4:]],
        "sell_side_liquidity": [row["price"] for row in equal_lows[-4:]],
        "sweeps": sweeps[-4:],
        "fair_value_gaps": gaps[-4:],
        "order_blocks": blocks["order_blocks"][-4:],
        "breaker_blocks": blocks["breaker_blocks"][-4:],
        "mitigation_blocks": blocks["mitigation_blocks"][-4:],
        "premium_discount": premium["zone"],
        "equilibrium": premium["equilibrium"],
        "dealing_high": premium["high"],
        "dealing_low": premium["low"],
        "displacement": displacement,
        "structure_shift": structure.event,
        "confirmation_required": True,
    }


def _equals(frame, indices: list[int], kind: str, tolerance: float) -> list[dict]:
    pairs: list[dict] = []
    prices = [float(frame.high[i] if kind == "high" else frame.low[i]) for i in indices]
    for pos in range(1, len(indices)):
        if abs(prices[pos] - prices[pos - 1]) <= tolerance:
            pairs.append(
                {
                    "price": round((prices[pos] + prices[pos - 1]) / 2, 3),
                    "left_index": int(indices[pos - 1]),
                    "right_index": int(indices[pos]),
                    "kind": kind,
                }
            )
    return pairs


def _sweeps(frame, equal_highs: list[dict], equal_lows: list[dict]) -> list[dict]:
    found: list[dict] = []
    start = max(2, len(frame) - 120)
    for i in range(start, len(frame)):
        for pool in equal_highs:
            if pool["right_index"] >= i:
                continue
            level = pool["price"]
            if frame.high[i] > level and frame.close[i] < level:
                found.append({"time": str(frame.time[i]), "side": "buy_side", "level": level, "index": i})
        for pool in equal_lows:
            if pool["right_index"] >= i:
                continue
            level = pool["price"]
            if frame.low[i] < level and frame.close[i] > level:
                found.append({"time": str(frame.time[i]), "side": "sell_side", "level": level, "index": i})
    return found


def _fair_value_gaps(frame, atr_values: np.ndarray) -> list[dict]:
    gaps: list[dict] = []
    for i in range(2, len(frame)):
        if not np.isfinite(atr_values[i]):
            continue
        span = float(frame.high[i - 1] - frame.low[i - 1])
        if span < float(atr_values[i]) * 1.2:
            continue
        if frame.low[i] > frame.high[i - 2]:
            gaps.append(
                {
                    "direction": "bullish",
                    "low": float(frame.high[i - 2]),
                    "high": float(frame.low[i]),
                    "time": str(frame.time[i - 1]),
                    "index": i - 1,
                }
            )
        elif frame.high[i] < frame.low[i - 2]:
            gaps.append(
                {
                    "direction": "bearish",
                    "low": float(frame.high[i]),
                    "high": float(frame.low[i - 2]),
                    "time": str(frame.time[i - 1]),
                    "index": i - 1,
                }
            )
    return gaps


def _order_blocks(frame, atr_values: np.ndarray) -> dict[str, list[dict]]:
    order_blocks: list[dict] = []
    breaker_blocks: list[dict] = []
    mitigation_blocks: list[dict] = []
    for i in range(2, len(frame)):
        if not np.isfinite(atr_values[i]):
            continue
        body = abs(float(frame.close[i] - frame.open[i]))
        span = float(frame.high[i] - frame.low[i])
        if span < 1.5 * float(atr_values[i]) or span == 0 or body / span < 0.65:
            continue
        origin = i - 1
        bullish = frame.close[i] > frame.open[i]
        block = {
            "direction": "bullish" if bullish else "bearish",
            "low": float(min(frame.open[origin], frame.close[origin])),
            "high": float(max(frame.open[origin], frame.close[origin])),
            "time": str(frame.time[origin]),
            "index": origin,
        }
        later_close = frame.close[i + 1 :]
        later_low = frame.low[i + 1 :]
        later_high = frame.high[i + 1 :]
        if len(later_close) == 0:
            order_blocks.append(block)
            continue
        invalidated = bool(np.any(later_close < block["low"])) if bullish else bool(np.any(later_close > block["high"]))
        revisited = bool(np.any((later_high >= block["low"]) & (later_low <= block["high"])))
        if invalidated:
            breaker_blocks.append({**block, "role": "breaker"})
        elif revisited:
            mitigation_blocks.append({**block, "role": "mitigation"})
        else:
            order_blocks.append(block)
    return {
        "order_blocks": order_blocks,
        "breaker_blocks": breaker_blocks,
        "mitigation_blocks": mitigation_blocks,
    }


def _premium_discount(frame, high: float | None, low: float | None) -> dict:
    if high is None or low is None or high <= low:
        return {"zone": "unknown", "equilibrium": None, "high": high, "low": low}
    equilibrium = (high + low) / 2
    close = float(frame.close[-1])
    band = (high - low) * 0.05
    if abs(close - equilibrium) <= band:
        zone = "equilibrium"
    elif close > equilibrium:
        zone = "premium"
    else:
        zone = "discount"
    return {"zone": zone, "equilibrium": float(equilibrium), "high": float(high), "low": float(low)}


def _latest_displacement(frame, atr_values: np.ndarray) -> str | None:
    for i in range(len(frame) - 1, max(0, len(frame) - 8), -1):
        if not np.isfinite(atr_values[i]):
            continue
        span = float(frame.high[i] - frame.low[i])
        body = abs(float(frame.close[i] - frame.open[i]))
        if span >= 1.5 * float(atr_values[i]) and span > 0 and body / span >= 0.65:
            return "bullish" if frame.close[i] > frame.open[i] else "bearish"
    return None
