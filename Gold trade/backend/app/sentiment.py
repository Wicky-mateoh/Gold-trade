"""News classification for gold and the dollar.

Headlines from unverified social sources are labeled and then excluded from
signal evidence. The classifier is a transparent lexicon, so a reading can be
audited word by word. It is not a forecast.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SOURCE_RELIABILITY = {
    "reuters": 0.9,
    "bloomberg": 0.9,
    "financial_times": 0.85,
    "wall_street_journal": 0.85,
    "federal_reserve": 0.95,
    "bureau_of_labor_statistics": 0.95,
    "bureau_of_economic_analysis": 0.95,
    "world_gold_council": 0.8,
    "ice": 0.8,
    "cme": 0.8,
    "social_media": 0.0,
    "unverified": 0.0,
}

BULLISH_PHRASES = (
    "rate cut",
    "cuts rates",
    "dollar weakness",
    "weaker dollar",
    "falling yields",
    "yields fall",
    "central bank buying",
    "central banks bought",
    "safe-haven",
    "safe haven",
    "geopolitical tension",
    "geopolitical risk",
    "inflation hedge",
    "easing cycle",
)
BEARISH_PHRASES = (
    "rate hike",
    "hikes rates",
    "dollar strength",
    "stronger dollar",
    "rising yields",
    "yields rise",
    "risk-on",
    "etf outflow",
    "etf outflows",
    "higher real rates",
    "hawkish",
)
UNCERTAIN_PHRASES = (
    "mixed",
    "unclear",
    "uncertain",
    "unexpected",
    "volatile reaction",
    "no consensus",
)


@dataclass
class NewsItem:
    published_at: datetime
    source: str
    headline: str
    body: str = ""
    importance: str = "medium"


def classify_news(item: NewsItem) -> dict:
    source_key = item.source.strip().lower().replace(" ", "_")
    reliability = SOURCE_RELIABILITY.get(source_key, 0.4)
    text = f"{item.headline} {item.body}".lower()
    bullish_hits = [phrase for phrase in BULLISH_PHRASES if phrase in text]
    bearish_hits = [phrase for phrase in BEARISH_PHRASES if phrase in text]
    uncertain_hits = [phrase for phrase in UNCERTAIN_PHRASES if phrase in text]
    if uncertain_hits or (bullish_hits and bearish_hits):
        label = "uncertain"
    elif bullish_hits and not bearish_hits:
        label = "bullish"
    elif bearish_hits and not bullish_hits:
        label = "bearish"
    else:
        label = "neutral"
    coverage = len(bullish_hits) + len(bearish_hits) + len(uncertain_hits)
    confidence = min(0.9, 0.35 + 0.15 * coverage) * reliability
    usable = reliability >= 0.6 and label in {"bullish", "bearish", "neutral"} and confidence >= 0.25
    if source_key in {"social_media", "unverified"}:
        usable = False
    return {
        "published_at": item.published_at.astimezone(timezone.utc).isoformat(),
        "source": item.source,
        "source_reliability": reliability,
        "headline": item.headline,
        "importance": item.importance,
        "label": label,
        "confidence": round(float(confidence), 3),
        "usable_as_evidence": usable,
        "bullish_hits": bullish_hits,
        "bearish_hits": bearish_hits,
        "uncertain_hits": uncertain_hits,
        "excluded_reason": None
        if usable
        else "Source reliability or wording is not strong enough to use as trading evidence.",
    }


def load_news_directory(path: Path) -> list[NewsItem]:
    if not path.exists():
        return []
    items: list[NewsItem] = []
    for file in sorted(path.glob("*.json")):
        if file.name.endswith(".example.json"):
            continue
        payload = json.loads(file.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else [payload]
        for row in rows:
            stamp = datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
            items.append(
                NewsItem(
                    published_at=stamp,
                    source=row["source"],
                    headline=row["headline"],
                    body=row.get("body", ""),
                    importance=row.get("importance", "medium"),
                )
            )
    return sorted(items, key=lambda item: item.published_at)
