"""Multi-timeframe analysis orchestrator.

Collects structure, SMC, indicators, macro, and sentiment for every frame and
assembles the `MarketContext` that the signal gate scores. Higher frames set
direction (MN1, W1, D1, H4); lower frames (H1, M15, M5) describe the entry.
This module does not size or route orders.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from app.frames import PriceFrame
from app.indicators import atr, indicator_agreement, indicator_snapshot, volatility_regime
from app.macro import EconomicEvent, macro_context
from app.market import TIMEFRAMES, MarketDataEngine
from app.sentiment import NewsItem, classify_news
from app.sessions import session_at
from app.signals import MarketContext, fibonacci_for_context
from app.smc import smc_snapshot
from app.structure import market_structure, timeframe_conflict

HIGHER_TIMEFRAMES = ("MN1", "W1", "D1", "H4")
LOWER_TIMEFRAMES = ("H1", "M15", "M5")
ENTRY_TIMEFRAME = "M15"
# Frames used for the conflict test. MN1 and W1 move too slowly to veto an
# intraday setup, so they are reported but not counted as a conflict.
CONFLICT_TIMEFRAMES = ("D1", "H4", "H1")
# Bars per frame passed to the heavier engines. Older history does not change
# the latest swing structure and would only slow the API.
ANALYSIS_WINDOW = {"MN1": 240, "W1": 400, "D1": 600, "H4": 800, "H1": 1000, "M15": 1000, "M5": 1200}
STALE_AFTER = timedelta(hours=72)


def _tail(frame: PriceFrame, timeframe: str) -> PriceFrame:
    limit = ANALYSIS_WINDOW.get(timeframe, 1000)
    if len(frame) <= limit:
        return frame
    start = len(frame) - limit
    return PriceFrame(
        frame.timeframe,
        frame.time[start:],
        frame.open[start:],
        frame.high[start:],
        frame.low[start:],
        frame.close[start:],
        frame.volume[start:],
        frame.spread[start:],
    )


def timeframe_report(engine: MarketDataEngine) -> dict[str, dict]:
    report: dict[str, dict] = {}
    for timeframe in TIMEFRAMES:
        frame = engine.frame(timeframe)
        if frame is None or len(frame) == 0:
            report[timeframe] = {"available": False, "bias": "unknown"}
            continue
        window = _tail(frame, timeframe)
        structure = market_structure(window)
        snapshot = indicator_snapshot(window)
        report[timeframe] = {
            "available": True,
            "bars": len(frame),
            "last_time": pd.Timestamp(frame.time[-1]).isoformat(),
            "bias": structure.bias,
            "event": structure.event,
            "regime": structure.regime,
            "support": structure.support,
            "resistance": structure.resistance,
            "demand_zones": structure.demand_zones,
            "supply_zones": structure.supply_zones,
            "last_swing_high": structure.last_swing_high,
            "last_swing_low": structure.last_swing_low,
            "indicators": snapshot,
            "indicator_agreement": round(indicator_agreement(snapshot), 3),
            "role": "direction" if timeframe in HIGHER_TIMEFRAMES else "entry",
        }
    return report


def overall_direction(report: dict[str, dict]) -> dict:
    weights = {"MN1": 0.5, "W1": 0.8, "D1": 1.2, "H4": 1.0, "H1": 0.6, "M15": 0.3, "M5": 0.1}
    score = 0.0
    total = 0.0
    for timeframe, row in report.items():
        weight = weights.get(timeframe, 0.0)
        bias = row.get("bias")
        if bias == "bullish":
            score += weight
        elif bias == "bearish":
            score -= weight
        if row.get("available"):
            total += weight
    normalized = score / total if total else 0.0
    if normalized >= 0.35:
        label = "bullish"
    elif normalized <= -0.35:
        label = "bearish"
    else:
        label = "mixed"
    biases = {tf: report[tf]["bias"] for tf in CONFLICT_TIMEFRAMES if tf in report}
    return {
        "label": label,
        "score": round(normalized, 3),
        "conflict": timeframe_conflict(biases),
        "conflict_frames": list(CONFLICT_TIMEFRAMES),
    }


def sentiment_summary(items: list[NewsItem], now: datetime, lookback_hours: int = 48) -> dict:
    moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    classified = [classify_news(item) for item in items]
    recent = []
    for row in classified:
        published = datetime.fromisoformat(row["published_at"])
        if moment - timedelta(hours=lookback_hours) <= published <= moment:
            recent.append(row)
    usable = [row for row in recent if row["usable_as_evidence"]]
    weight = {"high": 1.5, "medium": 1.0, "low": 0.5}
    score = 0.0
    mass = 0.0
    for row in usable:
        w = weight.get(row["importance"], 1.0) * row["confidence"]
        if row["label"] == "bullish":
            score += w
        elif row["label"] == "bearish":
            score -= w
        mass += w
    if not usable or mass == 0:
        label = "neutral"
        confidence = 0.0
    else:
        net = score / mass
        label = "bullish" if net >= 0.25 else "bearish" if net <= -0.25 else "neutral"
        confidence = round(min(0.9, abs(net) * min(1.0, mass / 2.0)), 3)
    return {
        "label": label,
        "confidence": confidence,
        "usable": bool(usable) and label != "neutral",
        "items": classified[-25:],
        "recent_count": len(recent),
        "usable_count": len(usable),
        "lookback_hours": lookback_hours,
        "note": "Sentiment is weighted by source reliability and importance. It adjusts confidence and never opens a trade alone.",
    }


def build_context(
    engine: MarketDataEngine,
    events: list[EconomicEvent],
    news: list[NewsItem],
    settings,
    now: datetime | None = None,
) -> tuple[MarketContext, dict]:
    """Return the signal context plus a full dashboard payload."""
    report = timeframe_report(engine)
    direction = overall_direction(report)
    entry = engine.frame(ENTRY_TIMEFRAME)
    base = engine.frame("M5")
    problems: list[str] = []
    if entry is None or len(entry) < 60:
        problems.append(f"{ENTRY_TIMEFRAME} history is too short for an entry read.")
    for timeframe in ("D1", "H4", "H1"):
        if not report.get(timeframe, {}).get("available"):
            problems.append(f"{timeframe} frame is unavailable.")
    sync = engine.synchronization_report()
    if not sync["aligned"]:
        problems.append(f"Timeframes are not synchronized: {sync['detail']}.")

    last_bar = None if base is None or len(base) == 0 else pd.Timestamp(base.time[-1]).tz_localize("UTC")
    if now is None:
        # Analysis time is the last confirmed bar, so history replays stay causal.
        moment = last_bar.to_pydatetime() if last_bar is not None else datetime.now(timezone.utc)
    else:
        moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        if last_bar is not None and engine.source != "synthetic" and moment - last_bar.to_pydatetime() > STALE_AFTER:
            problems.append("The latest candle is stale relative to the analysis clock.")

    entry_window = _tail(entry, ENTRY_TIMEFRAME) if entry is not None else None
    smc = smc_snapshot(entry_window) if entry_window is not None else smc_snapshot(None)
    entry_report = report.get(ENTRY_TIMEFRAME, {})
    snapshot = entry_report.get("indicators", {}) if entry_report.get("available") else {}
    atr_series = atr(entry_window.high, entry_window.low, entry_window.close) if entry_window is not None else np.array([])
    volatility = volatility_regime(atr_series) if len(atr_series) else "unknown"

    macro = macro_context(
        moment,
        events,
        settings.macro_blackout_before_min,
        settings.macro_blackout_after_min,
        settings.dxy_session_change,
        settings.us10y_session_change,
    )
    sentiment = sentiment_summary(news, moment)

    price = None
    spread = None
    if base is not None and len(base):
        price = float(base.close[-1])
        raw_spread = base.spread[-1]
        spread = None if not np.isfinite(raw_spread) else float(raw_spread)

    sweeps = smc.get("sweeps") or []
    recent_sweep = None
    if sweeps and entry_window is not None:
        last = sweeps[-1]
        if last["index"] >= len(entry_window) - 6:
            recent_sweep = last["side"]

    fundamental_notes = []
    if sentiment["usable"]:
        fundamental_notes.append(
            f"News sentiment over the last {sentiment['lookback_hours']}h reads {sentiment['label']} "
            f"(confidence {sentiment['confidence']:.2f}, {sentiment['usable_count']} usable items)."
        )

    d1 = report.get("D1", {})
    context = MarketContext(
        instrument=settings.instrument,
        timestamp=moment,
        price=price,
        spread=spread,
        atr=snapshot.get("atr"),
        biases={tf: report[tf]["bias"] for tf in TIMEFRAMES if tf in report},
        conflict=direction["conflict"],
        structure_event=entry_report.get("event"),
        regime=entry_report.get("regime", "unknown"),
        premium_discount=smc["premium_discount"],
        sweep_side=recent_sweep,
        displacement=smc["displacement"],
        indicator_agreement=entry_report.get("indicator_agreement", 0.0),
        adx=snapshot.get("adx"),
        volatility=volatility,
        macro_blackout=macro["blackout"],
        macro_notes=macro["notes"],
        sentiment_label=sentiment["label"],
        sentiment_confidence=sentiment["confidence"],
        sentiment_usable=sentiment["usable"],
        fib_levels=fibonacci_for_context(d1.get("last_swing_high"), d1.get("last_swing_low")),
        support=entry_report.get("support", []),
        resistance=entry_report.get("resistance", []),
        session=session_at(moment),
        data_problems=problems,
        indicator_snapshot=snapshot,
        fundamental_notes=fundamental_notes,
    )
    payload = {
        "timestamp": moment.astimezone(timezone.utc).isoformat(),
        "price": price,
        "spread": spread,
        "session": context.session,
        "direction": direction,
        "timeframes": report,
        "smc": smc,
        "volatility": volatility,
        "macro": macro,
        "sentiment": sentiment,
        "fibonacci": context.fib_levels,
        "data": {
            "source": engine.source,
            "note": engine.source_note,
            "warnings": engine.warnings[-20:],
            "synchronization": sync,
            "problems": problems,
        },
    }
    return context, payload
