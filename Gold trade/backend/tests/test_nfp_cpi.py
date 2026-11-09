"""NFP / CPI monitoring: schedule, confirmed-date override, blackout, pre-release flatten."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.econ_calendar import EventCalendar, estimate_tier1, parse_feed
from app.macro import EconomicEvent, macro_context

UTC = timezone.utc


def test_nfp_first_friday_0830_new_york_with_dst():
    nov = [e for e in estimate_tier1(2026, 11) if e.kind == "nfp"]
    assert [e.timestamp for e in nov] == [datetime(2026, 11, 6, 13, 30, tzinfo=UTC)]  # EST: UTC-5
    jul = [e for e in estimate_tier1(2026, 7) if e.kind == "nfp"]
    assert jul[0].timestamp == datetime(2026, 7, 3, 12, 30, tzinfo=UTC)  # EDT: UTC-4
    assert all(not e.confirmed for e in nov + jul)


def test_nfp_early_first_friday_also_covers_second_friday():
    jan = [e.timestamp.day for e in estimate_tier1(2026, 1) if e.kind == "nfp"]
    assert jan == [2, 9]


def test_cpi_candidates_cover_weekdays_10_to_15():
    days = [e.timestamp.day for e in estimate_tier1(2026, 11) if e.kind == "cpi"]
    assert days == [10, 11, 12, 13]  # 14th/15th are a weekend
    assert all(e.timestamp.hour == 13 and e.timestamp.minute == 30 for e in estimate_tier1(2026, 11) if e.kind == "cpi")


FEED = [
    {"title": "CPI m/m", "country": "USD", "date": "2026-11-12T08:30:00-05:00", "impact": "High", "forecast": "0.3%", "previous": "0.2%"},
    {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-11-06T08:30:00-05:00", "impact": "High", "forecast": "150K", "previous": "120K", "actual": "180K"},
    {"title": "ECB Press Conference", "country": "EUR", "date": "2026-11-05T08:45:00-05:00", "impact": "High"},
    {"title": "Retail Sales m/m", "country": "USD", "date": "2026-11-17T08:30:00-05:00", "impact": "Medium"},
]


def test_feed_parsing_keeps_usd_and_classifies():
    events = parse_feed(FEED)
    kinds = {e.name: e.kind for e in events}
    assert kinds == {"CPI m/m": "cpi", "Non-Farm Employment Change": "nfp", "Retail Sales m/m": "other"}
    nfp = next(e for e in events if e.kind == "nfp")
    assert nfp.timestamp == datetime(2026, 11, 6, 13, 30, tzinfo=UTC) and nfp.actual == "180K"


def test_confirmed_dates_replace_estimates_for_that_month(settings):
    cal = EventCalendar(settings, [])
    cal.feed_events = parse_feed(FEED)
    events = cal.events_between(datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 30, tzinfo=UTC))
    cpi = [e for e in events if e.kind == "cpi"]
    assert len(cpi) == 1 and cpi[0].confirmed and cpi[0].timestamp.day == 12
    assert all(e.before_min == 60 and e.after_min == 60 for e in events if e.tier1)
    # December has no confirmed rows, so estimates still protect it.
    dec = cal.events_between(datetime(2026, 12, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC))
    assert any(e.kind == "cpi" and not e.confirmed for e in dec)


def test_feed_failure_keeps_cache(settings, tmp_path):
    cache = tmp_path / "calendar_cache.json"
    cache.write_text(json.dumps({"fetched_at": "2026-11-01T00:00:00+00:00", "rows": FEED}))
    s = settings.model_copy(update={"calendar_feed_url": "http://127.0.0.1:9/unreachable.json"})
    cal = EventCalendar(s, [], cache)
    result = cal.refresh(force=True)
    assert result["fetched"] is False and "fetch failed" in result["status"]
    assert any(e.kind == "nfp" and e.confirmed for e in cal.confirmed())


@pytest.mark.parametrize(
    "offset_min,blocked",
    [(-61, False), (-60, True), (-5, True), (0, True), (45, True), (60, True), (61, False)],
)
def test_tier1_window_is_wider_than_ordinary_events(settings, offset_min, blocked):
    cal = EventCalendar(settings, [])
    release = datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
    events = cal.events_between(release - timedelta(days=2), release + timedelta(days=2))
    ctx = macro_context(release + timedelta(minutes=offset_min), events, 30, 15)
    assert ctx["blackout"] is blocked


def test_ordinary_high_impact_event_keeps_global_window(settings):
    cal = EventCalendar(settings, [EconomicEvent(datetime(2026, 11, 18, 19, 0, tzinfo=UTC), "FOMC Minutes", "high", "fed", kind="fomc")])
    events = cal.events_between(datetime(2026, 11, 18, tzinfo=UTC), datetime(2026, 11, 19, tzinfo=UTC))
    assert macro_context(datetime(2026, 11, 18, 18, 35, tzinfo=UTC), events, 30, 15)["blackout"]
    assert not macro_context(datetime(2026, 11, 18, 18, 25, tzinfo=UTC), events, 30, 15)["blackout"]


def test_tier1_status_countdown_and_flatten_flag(settings):
    cal = EventCalendar(settings, [])
    release = datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
    status = cal.tier1_status(release - timedelta(minutes=10))
    assert status["nfp"]["minutes_until"] == 10
    assert status["nfp"]["flatten_due"] and status["flatten_due"] and status["blackout"]
    assert status["nfp"]["confirmed"] is False
    early = cal.tier1_status(release - timedelta(hours=3))
    assert not early["flatten_due"] and not early["blackout"]
    cpi = cal.tier1_status(datetime(2026, 11, 9, tzinfo=UTC))["cpi"]
    assert cpi["candidate_dates"] and cpi["next"]["kind"] == "cpi"
