"""XAU/USD session clock in UTC.

Overlap is reported on its own because London/New York overlap is the
liquidity window the risk module can allow independently.
"""

from __future__ import annotations

import pandas as pd

SESSIONS = (
    "asia",
    "london",
    "new_york",
    "london_new_york_overlap",
    "off",
    "closed",
)


def session_at(moment) -> str:
    ts = pd.Timestamp(moment)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    hour = ts.hour + ts.minute / 60.0
    # Spot gold closes Friday 22:00 UTC and reopens Sunday 22:00 UTC (17:00 New York, approx.).
    day = ts.dayofweek
    if day == 5 or (day == 6 and hour < 22) or (day == 4 and hour >= 22):
        return "closed"
    if 13 <= hour < 16:
        return "london_new_york_overlap"
    if 7 <= hour < 16:
        return "london"
    if 16 <= hour < 22:
        return "new_york"
    if 0 <= hour < 7:
        return "asia"
    return "off"
