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
)


def session_at(moment) -> str:
    ts = pd.Timestamp(moment)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    hour = ts.hour + ts.minute / 60.0
    if 13 <= hour < 16:
        return "london_new_york_overlap"
    if 7 <= hour < 16:
        return "london"
    if 16 <= hour < 22:
        return "new_york"
    if 0 <= hour < 7:
        return "asia"
    return "off"
