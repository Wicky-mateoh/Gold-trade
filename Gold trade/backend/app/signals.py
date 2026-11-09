"""Signal quality gate.

BUY, SELL, and NO TRADE are all complete decisions. A directional candidate
is published only when the independent checks agree. Missing data, event
blackouts, timeframe conflict, and weak confirmation stay NO TRADE.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.indicators import fibonacci


@dataclass
class MarketContext:
    instrument: str
    timestamp: datetime
    price: float | None
    spread: float | None
    atr: float | None
    biases: dict[str, str]
    conflict: bool
    structure_event: str | None
    regime: str
    premium_discount: str
    sweep_side: str | None
    displacement: str | None
    indicator_agreement: float
    adx: float | None
    volatility: str
    macro_blackout: bool
    macro_notes: list[str]
    sentiment_label: str
    sentiment_confidence: float
    sentiment_usable: bool
    fib_levels: dict[str, float]
    support: list[float]
    resistance: list[float]
    session: str
    data_problems: list[str] = field(default_factory=list)
    indicator_snapshot: dict = field(default_factory=dict)
    fundamental_notes: list[str] = field(default_factory=list)
    # Timeframe roles from the strategy profile (defaults = intraday).
    primary_tf: str = "D1"
    secondary_tf: str = "H4"
    confirm_tf: str = "H1"

    def tf_name(self, timeframe: str) -> str:
        return {"D1": "Daily", "H4": "H4", "H1": "H1", "M15": "M15"}.get(timeframe, timeframe)


def decide(context: MarketContext, min_confidence: float, min_reward_risk: float, max_spread: float) -> dict:
    problems = list(context.data_problems)
    if context.price is None:
        problems.append("Current price is unavailable.")
    if context.atr is None or context.atr <= 0:
        problems.append("ATR is unavailable, so a volatility-adjusted stop cannot be placed.")
    if context.spread is None:
        problems.append("Spread is unavailable.")
    if problems:
        return _no_trade(context, problems, confidence=None)

    assert context.price is not None and context.atr is not None
    reasons: list[str] = []
    if context.macro_blackout:
        reasons.append("A high-impact event window is active. New risk is paused.")
    if context.spread is not None and context.spread > max_spread:
        reasons.append(f"Spread {context.spread:.2f} is wider than the {max_spread:.2f} limit.")
    if context.volatility == "extreme":
        reasons.append("Volatility is in the extreme bucket. The setup is skipped.")
    if context.biases.get(context.primary_tf) not in {"bullish", "bearish"}:
        reasons.append(f"{context.tf_name(context.primary_tf)} bias is not directional.")
    if context.conflict:
        reasons.append("Timeframes disagree. Confidence is reduced and the setup must clear a higher bar.")

    direction = _candidate_direction(context)
    if direction is None:
        reasons.append("Higher-timeframe bias and location do not form a directional candidate.")
        return _no_trade(context, reasons, confidence=_score(context, None))

    location_ok = _location_ok(context, direction)
    if not location_ok:
        reasons.append("Price is on the wrong side of the dealing range for this direction, and there is no confirming sweep.")
    confirmation_ok = _confirmation_ok(context, direction)
    if not confirmation_ok:
        reasons.append("Price action and indicator agreement do not confirm the candidate.")
    if context.adx is not None and context.adx < 18 and context.sweep_side is None:
        reasons.append("ADX is below 18 and there is no liquidity sweep. Range conditions fail the quality bar.")

    levels = _levels(context, direction, min_reward_risk)
    if levels is None:
        reasons.append("A stop and target with the minimum reward-to-risk could not be placed.")
    score = _score(context, direction)
    if context.conflict:
        score *= 0.55
    if context.sentiment_usable and _sentiment_opposes(context, direction):
        score *= 0.75
        reasons.append("Usable sentiment opposes the candidate, so confidence was reduced.")
    confidence = _calibrate(score)
    if confidence < min_confidence:
        reasons.append(f"Calibrated confidence {confidence:.1f} is below {min_confidence:.1f}.")

    blocking = [
        reason
        for reason in reasons
        if not reason.startswith("Timeframes disagree") and not reason.startswith("Usable sentiment")
    ]
    if context.conflict and confidence < min_confidence:
        blocking.append("Timeframe conflict left the setup below the confidence floor.")
    if blocking or levels is None:
        return _no_trade(context, reasons, confidence=confidence, candidate=direction)

    evidence = _evidence(context, direction)
    return {
        "instrument": context.instrument,
        "direction": direction,
        "decision": direction,
        "price": context.price,
        "entry_low": levels["entry_low"],
        "entry_high": levels["entry_high"],
        "stop_loss": levels["stop"],
        "take_profits": levels["targets"],
        "reward_risk": levels["reward_risk"],
        "evidence": evidence,
        "fundamental_context": context.macro_notes + context.fundamental_notes,
        "confidence": confidence,
        "invalidation": [
            f"The setup is invalid if price trades through {levels['stop']:.2f}.",
            "The setup is invalid if the higher-timeframe bias flips before entry.",
            "The setup is invalid if spread or an event blackout appears before execution.",
        ],
        "timestamp": context.timestamp.astimezone(timezone.utc).isoformat(),
        "component_scores": _components(context, direction),
        "reasons": ["Quality checks passed. Risk limits are evaluated separately."],
        "candidate_direction": direction,
        "actionable": True,
    }


def _candidate_direction(context: MarketContext) -> str | None:
    primary = context.biases.get(context.primary_tf)
    confirm = context.biases.get(context.confirm_tf)
    if primary == "bullish" and confirm != "bearish":
        return "BUY"
    if primary == "bearish" and confirm != "bullish":
        return "SELL"
    return None


def _location_ok(context: MarketContext, direction: str) -> bool:
    if direction == "BUY":
        return context.premium_discount in {"discount", "equilibrium"} or context.sweep_side == "sell_side"
    return context.premium_discount in {"premium", "equilibrium"} or context.sweep_side == "buy_side"


def _confirmation_ok(context: MarketContext, direction: str) -> bool:
    agreement = context.indicator_agreement
    if direction == "BUY":
        if context.structure_event == "choch_bearish":
            return False
        if context.displacement == "bearish":
            return False
        return agreement >= 0.15
    if context.structure_event == "choch_bullish":
        return False
    if context.displacement == "bullish":
        return False
    return agreement <= -0.15


def _levels(context: MarketContext, direction: str, min_reward_risk: float) -> dict | None:
    assert context.price is not None and context.atr is not None
    atr_v = context.atr
    price = context.price
    if direction == "BUY":
        swing = context.support[-1] if context.support else price - 1.4 * atr_v
        stop = min(swing, price) - 0.2 * atr_v
        if stop >= price:
            stop = price - 1.2 * atr_v
        risk = price - stop
        target = price + max(min_reward_risk * risk * 1.002, 1.6 * atr_v)
        if context.resistance:
            # Aim at the nearest resistance that still clears the minimum, not past every level.
            beyond = [level for level in context.resistance if level > price + risk]
            if beyond:
                target = max(target, min(beyond))
        entry_low, entry_high = price - 0.2 * atr_v, price + 0.05 * atr_v
    else:
        swing = context.resistance[-1] if context.resistance else price + 1.4 * atr_v
        stop = max(swing, price) + 0.2 * atr_v
        if stop <= price:
            stop = price + 1.2 * atr_v
        risk = stop - price
        target = price - max(min_reward_risk * risk * 1.002, 1.6 * atr_v)
        if context.support:
            beyond = [level for level in context.support if level < price - risk]
            if beyond:
                target = min(target, max(beyond))
        entry_low, entry_high = price - 0.05 * atr_v, price + 0.2 * atr_v
    if risk <= 0:
        return None
    stop, target = round(stop, 3), round(target, 3)
    risk = abs(price - stop)
    reward = abs(target - price)
    ratio = reward / risk if risk else 0.0
    # Checked on the rounded levels, exactly as the risk gate will see them.
    if ratio < min_reward_risk:
        return None
    # TP2 is always beyond TP1: at least 2.6R, and at least one more R past TP1.
    extension = max(2.6 * risk, reward + risk)
    second = price + (extension if direction == "BUY" else -extension)
    return {
        "entry_low": round(entry_low, 3),
        "entry_high": round(entry_high, 3),
        "stop": round(stop, 3),
        "targets": [round(target, 3), round(second, 3)],
        "reward_risk": round(ratio, 2),
    }


def _score(context: MarketContext, direction: str | None) -> float:
    if direction is None:
        return 0.2
    components = _components(context, direction)
    weights = {
        "higher_timeframe": 0.22,
        "structure": 0.16,
        "liquidity": 0.12,
        "price_action": 0.12,
        "indicators": 0.14,
        "volatility": 0.08,
        "macro": 0.08,
        "sentiment": 0.05,
        "execution": 0.03,
    }
    return sum(components[name] * weight for name, weight in weights.items())


def _components(context: MarketContext, direction: str) -> dict[str, float]:
    primary = context.biases.get(context.primary_tf)
    secondary = context.biases.get(context.secondary_tf)
    wanted = "bullish" if direction == "BUY" else "bearish"
    higher = 1.0 if primary == wanted and secondary in {wanted, "ranging", "transition"} else 0.35
    if direction == "BUY" and context.structure_event == "bos_bullish":
        structure = 1.0
    elif direction == "SELL" and context.structure_event == "bos_bearish":
        structure = 1.0
    elif context.structure_event is None:
        structure = 0.6
    else:
        structure = 0.2
    sweep_match = (direction == "BUY" and context.sweep_side == "sell_side") or (
        direction == "SELL" and context.sweep_side == "buy_side"
    )
    liquidity = 1.0 if sweep_match else 0.55 if _location_ok(context, direction) else 0.15
    displacement_match = (direction == "BUY" and context.displacement == "bullish") or (
        direction == "SELL" and context.displacement == "bearish"
    )
    price_action = 1.0 if displacement_match else 0.45
    indicators = (context.indicator_agreement + 1) / 2 if direction == "BUY" else (-context.indicator_agreement + 1) / 2
    volatility = {"normal": 1.0, "low": 0.7, "high": 0.45, "extreme": 0.0, "unknown": 0.3}.get(context.volatility, 0.3)
    macro = 0.0 if context.macro_blackout else 0.85 if context.macro_notes else 0.7
    if context.sentiment_usable:
        sentiment = 1.0 if not _sentiment_opposes(context, direction) else 0.2
    else:
        sentiment = 0.5
    execution = 1.0 if context.spread is not None else 0.0
    return {
        "higher_timeframe": higher,
        "structure": structure,
        "liquidity": liquidity,
        "price_action": price_action,
        "indicators": float(indicators),
        "volatility": volatility,
        "macro": macro,
        "sentiment": sentiment,
        "execution": execution,
    }


def _sentiment_opposes(context: MarketContext, direction: str) -> bool:
    if direction == "BUY":
        return context.sentiment_label == "bearish" and context.sentiment_confidence >= 0.45
    return context.sentiment_label == "bullish" and context.sentiment_confidence >= 0.45


def _calibrate(score: float) -> float:
    # A logistic map keeps 90 as a ceiling. The number is a quality score,
    # not a probability of profit, until the learning module has a calibration sample.
    transformed = 1.0 / (1.0 + math.exp(-8.0 * (score - 0.72)))
    return round(min(90.0, 100.0 * transformed), 1)


def _evidence(context: MarketContext, direction: str) -> list[str]:
    wanted = "bullish" if direction == "BUY" else "bearish"
    rows = [
        f"{context.tf_name(context.primary_tf)} bias is {context.biases.get(context.primary_tf)}.",
        f"{context.secondary_tf} bias is {context.biases.get(context.secondary_tf)}; "
        f"{context.confirm_tf} bias is {context.biases.get(context.confirm_tf)}.",
        f"Price is in the {context.premium_discount} portion of the latest dealing range.",
        f"Indicator agreement is {context.indicator_agreement:+.2f} on a -1 to +1 scale.",
        f"Volatility regime is {context.volatility}.",
    ]
    if context.structure_event:
        rows.append(f"Latest confirmed structure event: {context.structure_event}.")
    if context.sweep_side:
        rows.append(f"A {context.sweep_side.replace('_', ' ')} sweep is present.")
    if context.fib_levels:
        rows.append("Fibonacci levels are attached from the latest dealing range and do not trigger a trade alone.")
    rows.append(f"The candidate direction is {direction}, aligned with the {wanted} {context.primary_tf} bias.")
    return rows


def _no_trade(context: MarketContext, reasons: list[str], confidence: float | None, candidate: str | None = None) -> dict:
    stamp = context.timestamp
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return {
        "instrument": context.instrument,
        "direction": "NO_TRADE",
        "decision": "NO_TRADE",
        "price": context.price,
        "entry_low": None,
        "entry_high": None,
        "stop_loss": None,
        "take_profits": [],
        "reward_risk": None,
        "evidence": reasons,
        "fundamental_context": context.macro_notes,
        "confidence": confidence,
        "invalidation": ["No order is proposed, so there is no price invalidation."],
        "timestamp": stamp.astimezone(timezone.utc).isoformat(),
        "component_scores": {},
        "reasons": reasons,
        "candidate_direction": candidate,
        "actionable": False,
    }


def fibonacci_for_context(high: float | None, low: float | None) -> dict[str, float]:
    if high is None or low is None or high <= low:
        return {}
    return fibonacci(high, low)
