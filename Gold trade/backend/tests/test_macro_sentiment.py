"""Modules 5 and 6: macro blackout and news classification."""

from datetime import datetime, timedelta, timezone

from app.analysis import sentiment_summary
from app.macro import EconomicEvent, macro_context
from app.sentiment import NewsItem, classify_news

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def test_blackout_window():
    events = [EconomicEvent(NOW + timedelta(minutes=20), "US CPI", "high", "inflation")]
    ctx = macro_context(NOW, events, 30, 15)
    assert ctx["blackout"] and ctx["directional_assumption"] is None
    assert not macro_context(NOW - timedelta(hours=2), events, 30, 15)["blackout"]
    assert macro_context(NOW + timedelta(minutes=30), events, 30, 15)["blackout"]  # after window
    assert not macro_context(NOW + timedelta(minutes=40), events, 30, 15)["blackout"]


def test_medium_impact_does_not_block():
    events = [EconomicEvent(NOW, "Claims", "medium", "employment")]
    assert not macro_context(NOW, events, 30, 15)["blackout"]


def test_news_labels():
    assert classify_news(NewsItem(NOW, "reuters", "Yields fall as weaker dollar lifts metals"))["label"] == "bullish"
    assert classify_news(NewsItem(NOW, "bloomberg", "Hawkish Fed signals rate hike"))["label"] == "bearish"
    assert classify_news(NewsItem(NOW, "reuters", "Reaction mixed after data"))["label"] == "uncertain"


def test_social_media_never_evidence():
    row = classify_news(NewsItem(NOW, "social_media", "rate cut tomorrow, weaker dollar, safe haven bid"))
    assert row["usable_as_evidence"] is False


def test_sentiment_summary_weights_and_window():
    items = [
        NewsItem(NOW - timedelta(hours=1), "reuters", "Weaker dollar and falling yields", importance="high"),
        NewsItem(NOW - timedelta(hours=100), "reuters", "Hawkish rate hike", importance="high"),  # outside window
        NewsItem(NOW - timedelta(hours=1), "social_media", "rate hike rumour"),
    ]
    summary = sentiment_summary(items, NOW)
    assert summary["label"] == "bullish"
    assert summary["usable_count"] == 1
