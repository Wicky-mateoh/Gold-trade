"""Strategy profiles.

A profile names which timeframes set direction, which one confirms, and
which one times the entry. Both profiles use the operator's chosen set:
M15 + H1 + H4 + D1. MN1 and W1 are still built and shown on the dashboard
as context, but they do not vote.

Risk limits are not part of a profile. They stay operator-controlled.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    name: str
    primary: str        # must be directional for a candidate
    secondary: str      # supports the primary
    confirm: str        # must not oppose the candidate
    entry: str          # entry timing, SMC, stops and targets
    max_hold_minutes: int
    description: str

    @property
    def decision_frames(self) -> tuple[str, ...]:
        frames = (self.primary, self.secondary, self.confirm, self.entry)
        return tuple(dict.fromkeys(frames))

    @property
    def conflict_frames(self) -> tuple[str, str, str]:
        return (self.primary, self.secondary, self.confirm)


PROFILES: dict[str, Profile] = {
    "intraday": Profile(
        name="intraday",
        primary="D1",
        secondary="H4",
        confirm="H1",
        entry="M15",
        max_hold_minutes=24 * 60,
        description="D1 and H4 set direction, H1 confirms, M15 times the entry. Positions close within 24h.",
    ),
    "scalping": Profile(
        name="scalping",
        primary="H4",
        secondary="H1",
        confirm="M15",
        entry="M15",
        max_hold_minutes=4 * 60,
        description="H4 and H1 set direction, M15 confirms and times the entry. Positions close within 4h.",
    ),
}

DISPLAY_ONLY = ("MN1", "W1", "M5")


def get_profile(name: str) -> Profile:
    return PROFILES.get((name or "intraday").lower(), PROFILES["intraday"])
