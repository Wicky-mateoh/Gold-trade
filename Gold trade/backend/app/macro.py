"""Macro context for gold.

The calendar can block new risk around high-impact releases. It does not
assign a direction to the release. Dollar and yield inputs are context when a
feed supplies them, and they stay empty when it does not.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

HIGH_IMPACT = {"high", "holiday"}


@dataclass
class EconomicEvent:
    timestamp: datetime
    name: str
    impact: str
    category: str
    source: str = "operator_calendar"


def load_calendar(path: Path) -> list[EconomicEvent]:
    if not path.exists():
        return []
    events: list[EconomicEvent] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            name = (row.get("name") or "").strip()
            stamp_text = (row.get("timestamp_utc") or "").strip()
            if not name or name.startswith("#") or not stamp_text or stamp_text.startswith("#"):
                continue
            stamp = pd.Timestamp(row["timestamp_utc"])
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize("UTC")
            events.append(
                EconomicEvent(
                    timestamp=stamp.to_pydatetime(),
                    name=name,
                    impact=(row.get("impact") or "low").strip().lower(),
                    category=(row.get("category") or "other").strip().lower(),
                    source=(row.get("source") or "operator_calendar").strip(),
                )
            )
    return sorted(events, key=lambda item: item.timestamp)


def macro_context(
    now: datetime,
    events: list[EconomicEvent],
    before_minutes: int,
    after_minutes: int,
    dxy_change: float | None = None,
    yield_change: float | None = None,
) -> dict:
    moment = _aware(now)
    active: list[EconomicEvent] = []
    upcoming: list[EconomicEvent] = []
    for event in events:
        start = event.timestamp - timedelta(minutes=before_minutes)
        end = event.timestamp + timedelta(minutes=after_minutes)
        if event.impact in HIGH_IMPACT and start <= moment <= end:
            active.append(event)
        elif event.timestamp >= moment:
            upcoming.append(event)
    notes: list[str] = []
    if not events:
        notes.append("No economic calendar file is loaded. High-impact restrictions apply once events are supplied.")
    if active:
        names = ", ".join(event.name for event in active)
        notes.append(
            f"New risk is restricted around {names}. The event window is a volatility control, not a directional view."
        )
    if dxy_change is not None:
        direction = "higher" if dxy_change > 0 else "lower" if dxy_change < 0 else "unchanged"
        notes.append(
            f"The supplied DXY session change is {direction} ({dxy_change:+.2f}%). "
            "Gold has often moved opposite the dollar, and that history is not a forecast."
        )
    if yield_change is not None:
        direction = "higher" if yield_change > 0 else "lower" if yield_change < 0 else "unchanged"
        notes.append(
            f"The supplied US 10-year yield change is {direction} ({yield_change:+.2f} bp). "
            "Real-rate context is descriptive and is not a trade by itself."
        )
    categories = sorted({event.category for event in events})
    return {
        "blackout": bool(active),
        "active_events": [_event_dict(event) for event in active],
        "upcoming": [_event_dict(event) for event in upcoming[:8]],
        "notes": notes,
        "categories_monitored": categories,
        "directional_assumption": None,
    }


def _event_dict(event: EconomicEvent) -> dict:
    return {
        "timestamp": event.timestamp.astimezone(timezone.utc).isoformat(),
        "name": event.name,
        "impact": event.impact,
        "category": event.category,
        "source": event.source,
    }


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)
