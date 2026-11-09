"""Durable runtime state.

The journal records history; this file records the *current* state that the
bot needs after a restart: account equity and day/week anchors, the loss
pause, the emergency stop, open simulated positions, the mode, and the
metadata of live broker positions.

Writes are atomic (temp file + replace), so a crash mid-write leaves the
previous good copy. Live mode is never restored: after a restart it must be
re-armed through the checklist.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("gits.state")
VERSION = 1


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("State file %s is unreadable (%s); starting fresh and keeping a copy.", self.path, exc)
            try:
                self.path.replace(self.path.with_suffix(".corrupt.json"))
            except OSError:
                pass
            return None
        if data.get("version") != VERSION:
            log.warning("State file version %s is not %s; ignoring it.", data.get("version"), VERSION)
            return None
        return data

    def save(self, data: dict) -> None:
        payload = {"version": VERSION, "saved_at": datetime.now(timezone.utc).isoformat(), **data}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
