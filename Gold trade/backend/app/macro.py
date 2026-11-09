"""Macro context for gold.

The calendar can block new risk around high-impact releases. It does not
assign a direction to the release. Dollar and yield inputs are context when a
feed supplies them, and they stay empty when it does not.

Tier-1 releases (NFP, CPI) carry their own, wider windows. See calendar.py.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

HIGH_IMPACT = {"high", "holiday"}
TIER1_KINDS = {"nfp", "cpi"}


@dataclass
class EconomicEvent:
    timestamp: datetime
    name: str
    impact: str
    category: str
    source: str = "operator_calendar"
    kind: str = "other"                # nfp, cpi, fomc, ppi, gdp, employment, other
    confirmed: bool = True             # False = estimated by the built-in schedule
    before_min: int | None = None      # per-event window; None = global setting
    after_min: int | None = None
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None

    @property
    def tier1(self) -> bool:
        return self.kind in TIER1_KINDS

    def window(self, before_default: int, after_default: int) -> tuple[datetime, datetime]:
        before = self.before_min if self.before_min is not None else before_default
        after = self.after_min if self.after_min is not None else after_default
        return self.timestamp - timedelta(minutes=before), self.timestamp + timedelta(minutes=after)


def classify_kind(name: str, category: str = "") -> str:
    text = f"{name} {category}".lower()
    if "non-farm" in text or "nonfarm" in text or "non farm" in text or "payroll" in text or text.strip() == "nfp":
        return "nfp"
    if "cpi" in text or "consumer price" in text:
        return "cpi"
    if "fomc" in text or "federal funds" in text or "fed rate" in text or "rate decision" in text:
        return "fomc"
    if "ppi" in text or "producer price" in text:
        return "ppi"
    if "gdp" in text:
        return "gdp"
    if "unemployment" in text or "jobless" in text or "employment" in text:
        return "employment"
    return "other"


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
            stamp = pd.Timestamp(stamp_text)
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize("UTC")
            category = (row.get("category") or "other").strip().lower()
            events.append(
                EconomicEvent(
                    timestamp=stamp.to_pydatetime(),
                    name=name,
                    impact=(row.get("impact") or "low").strip().lower(),
                    category=category,
                    source=(row.get("source") or "operator_calendar").strip(),
                    kind=classify_kind(name, category),
                )
            )
    return sorted(events, key=lambda item: item.timestamp)


def in_blackout(event: EconomicEvent, moment: datetime, before_min: int, after_min: int) -> bool:
    if event.impact not in HIGH_IMPACT:
        return False
    start, end = event.window(before_min, after_min)
    return start <= moment <= end


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
        if in_blackout(event, moment, before_minutes, after_minutes):
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
        "active_events": [event_dict(event) for event in active],
        "upcoming": [event_dict(event) for event in upcoming if event.confirmed or event.tier1][:10],
        "notes": notes,
        "categories_monitored": categories,
        "directional_assumption": None,
    }


def event_dict(event: EconomicEvent) -> dict:
    return {
        "timestamp": event.timestamp.astimezone(timezone.utc).isoformat(),
        "name": event.name,
        "impact": event.impact,
        "category": event.category,
        "source": event.source,
        "kind": event.kind,
        "tier1": event.tier1,
        "confirmed": event.confirmed,
        "window_before_min": event.before_min,
        "window_after_min": event.after_min,
        "actual": event.actual,
        "forecast": event.forecast,
        "previous": event.previous,
    }


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)
