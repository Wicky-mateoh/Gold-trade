"""Economic calendar with NFP and CPI protection.

Sources, merged in this order:
  1. data/economic_calendar.csv (operator file, confirmed dates)
  2. an optional JSON feed (CALENDAR_FEED_URL, ForexFactory-style rows), cached
     on disk so a failed fetch keeps the last good copy
  3. a built-in tier-1 schedule that needs no network:
       NFP - first Friday of the month, 08:30 New York time. If that Friday is
             the 1st-3rd, the second Friday is also covered, because BLS
             sometimes publishes a week later.
       CPI - BLS publishes no fixed rule, so every weekday from the 10th to the
             15th at 08:30 New York time is treated as a candidate release.
     Estimates for a month are dropped once a confirmed date of the same kind
     exists for that month.

Tier-1 events (NFP, CPI) get the wider TIER1 blackout windows, and open paper
positions are flattened FLATTEN_BEFORE_TIER1_MIN minutes before release. The
calendar never predicts a direction.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.macro import TIER1_KINDS, EconomicEvent, classify_kind, event_dict, in_blackout

log = logging.getLogger("gits.calendar")
NEW_YORK = ZoneInfo("America/New_York")
RELEASE_TIME = (8, 30)  # BLS releases NFP and CPI at 08:30 Eastern


def _ny_release(day: date) -> datetime:
    local = datetime(day.year, day.month, day.day, *RELEASE_TIME, tzinfo=NEW_YORK)
    return local.astimezone(timezone.utc)


def estimate_tier1(year: int, month: int) -> list[EconomicEvent]:
    """Offline NFP and CPI estimates for one month. Always marked unconfirmed."""
    events: list[EconomicEvent] = []
    first = date(year, month, 1)
    first_friday = first + timedelta(days=(4 - first.weekday()) % 7)
    fridays = [first_friday] + ([first_friday + timedelta(days=7)] if first_friday.day <= 3 else [])
    for day in fridays:
        events.append(
            EconomicEvent(
                timestamp=_ny_release(day),
                name="US Non-Farm Payrolls (estimated date)",
                impact="high",
                category="employment",
                source="built_in_schedule",
                kind="nfp",
                confirmed=False,
            )
        )
    last = monthrange(year, month)[1]
    for dom in range(10, min(15, last) + 1):
        day = date(year, month, dom)
        if day.weekday() < 5:
            events.append(
                EconomicEvent(
                    timestamp=_ny_release(day),
                    name="US CPI (candidate date, unconfirmed)",
                    impact="high",
                    category="inflation",
                    source="built_in_schedule",
                    kind="cpi",
                    confirmed=False,
                )
            )
    return events


def parse_feed(rows: list[dict], source: str = "calendar_feed") -> list[EconomicEvent]:
    """Parse ForexFactory-style rows: title, country, date (ISO with offset), impact, forecast, previous, actual."""
    events: list[EconomicEvent] = []
    for row in rows:
        if str(row.get("country", "")).upper() != "USD":
            continue
        impact = str(row.get("impact", "")).strip().lower()
        if impact not in {"high", "medium", "holiday"}:
            continue
        title = str(row.get("title", "")).strip()
        try:
            stamp = datetime.fromisoformat(str(row["date"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=NEW_YORK)
        kind = classify_kind(title)
        events.append(
            EconomicEvent(
                timestamp=stamp.astimezone(timezone.utc),
                name=title,
                impact=impact,
                category=kind,
                source=source,
                kind=kind,
                confirmed=True,
                actual=_text(row.get("actual")),
                forecast=_text(row.get("forecast")),
                previous=_text(row.get("previous")),
            )
        )
    return events


def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class EventCalendar:
    def __init__(self, settings, file_events: list[EconomicEvent], cache_path: Path | None = None) -> None:
        self.settings = settings
        self.file_events = list(file_events)
        self.feed_events: list[EconomicEvent] = []
        self.cache_path = cache_path
        self.feed_status = "not configured" if not settings.calendar_feed_url else "not fetched yet"
        self.last_fetch: datetime | None = None
        if cache_path is not None and cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                self.feed_events = parse_feed(cached.get("rows", []), source="calendar_feed_cache")
                self.feed_status = f"using cache from {cached.get('fetched_at')}"
            except (OSError, ValueError):
                log.warning("Calendar cache is unreadable; ignoring it.")

    # ------------------------------------------------------------- feed
    def refresh(self, force: bool = False) -> dict:
        url = self.settings.calendar_feed_url
        if not url:
            return {"fetched": False, "status": self.feed_status}
        now = datetime.now(timezone.utc)
        due = self.last_fetch is None or now - self.last_fetch >= timedelta(minutes=self.settings.calendar_refresh_minutes)
        if not (force or due):
            return {"fetched": False, "status": self.feed_status}
        self.last_fetch = now
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "gits-calendar/0.1"})
            with urllib.request.urlopen(request, timeout=10) as response:
                rows = json.loads(response.read().decode("utf-8"))
            if not isinstance(rows, list):
                raise ValueError("feed did not return a JSON list")
        except Exception as exc:
            self.feed_status = f"fetch failed at {now.isoformat()} ({exc}); keeping {len(self.feed_events)} cached events"
            log.warning(self.feed_status)
            return {"fetched": False, "status": self.feed_status}
        self.feed_events = parse_feed(rows)
        if self.cache_path is not None:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps({"fetched_at": now.isoformat(), "rows": rows}), encoding="utf-8")
        self.feed_status = f"fetched {len(self.feed_events)} USD events at {now.isoformat()}"
        return {"fetched": True, "status": self.feed_status}

    # ------------------------------------------------------------ merge
    def confirmed(self) -> list[EconomicEvent]:
        seen: set[tuple[str, str]] = set()
        out: list[EconomicEvent] = []
        for event in self.file_events + self.feed_events:
            key = (event.name.lower(), event.timestamp.astimezone(timezone.utc).isoformat()[:16])
            if key in seen:
                continue
            seen.add(key)
            out.append(event)
        return out

    def events_between(self, start: datetime, end: datetime) -> list[EconomicEvent]:
        start = _aware(start) - timedelta(days=1)
        end = _aware(end) + timedelta(days=1)
        confirmed = [e for e in self.confirmed() if start <= e.timestamp <= end]
        have = {(e.kind, e.timestamp.year, e.timestamp.month) for e in confirmed if e.kind in TIER1_KINDS}
        estimates: list[EconomicEvent] = []
        for year, month in _months(start, end):
            for event in estimate_tier1(year, month):
                if (event.kind, year, month) in have or not (start <= event.timestamp <= end):
                    continue
                estimates.append(event)
        merged = confirmed + estimates
        for event in merged:
            if event.tier1:
                event.before_min = self.settings.tier1_blackout_before_min
                event.after_min = self.settings.tier1_blackout_after_min
                event.impact = "high"
        return sorted(merged, key=lambda e: e.timestamp)

    def around(self, moment: datetime, days_back: int = 3, days_ahead: int = 45) -> list[EconomicEvent]:
        moment = _aware(moment)
        return self.events_between(moment - timedelta(days=days_back), moment + timedelta(days=days_ahead))

    # ----------------------------------------------------------- status
    def tier1_status(self, moment: datetime, events: list[EconomicEvent] | None = None) -> dict:
        moment = _aware(moment)
        events = events if events is not None else self.around(moment)
        s = self.settings
        flatten_window = timedelta(minutes=s.flatten_before_tier1_min)
        out: dict = {
            "policy": {
                "blackout_before_min": s.tier1_blackout_before_min,
                "blackout_after_min": s.tier1_blackout_after_min,
                "flatten_before_min": s.flatten_before_tier1_min,
                "rule": "No new simulated trades inside the window; open positions close before release. No directional bet on the number.",
            },
            "feed_status": self.feed_status,
        }
        blackout = False
        flatten_due = False
        for kind, label in (("nfp", "Non-Farm Payrolls"), ("cpi", "Consumer Price Index")):
            rows = [e for e in events if e.kind == kind]
            active = [e for e in rows if in_blackout(e, moment, 0, 0)]
            upcoming = [e for e in rows if e.timestamp >= moment]
            recent = [e for e in rows if e.timestamp < moment and e.confirmed]
            nxt = upcoming[0] if upcoming else None
            due = bool(nxt and s.flatten_before_tier1_min > 0 and nxt.timestamp - moment <= flatten_window)
            blackout = blackout or bool(active)
            flatten_due = flatten_due or due
            candidates = [e for e in upcoming if not e.confirmed and nxt and (e.timestamp.year, e.timestamp.month) == (nxt.timestamp.year, nxt.timestamp.month)]
            out[kind] = {
                "label": label,
                "next": None if nxt is None else event_dict(nxt),
                "minutes_until": None if nxt is None else round((nxt.timestamp - moment).total_seconds() / 60),
                "confirmed": None if nxt is None else nxt.confirmed,
                "candidate_dates": [e.timestamp.isoformat() for e in candidates][:6] if nxt is not None and not nxt.confirmed else [],
                "in_blackout": bool(active),
                "flatten_due": due,
                "last_release": None if not recent else event_dict(recent[-1]),
            }
        out["blackout"] = blackout
        out["flatten_due"] = flatten_due
        return out


def _months(start: datetime, end: datetime):
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        month += 1
        if month == 13:
            year, month = year + 1, 1


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)
