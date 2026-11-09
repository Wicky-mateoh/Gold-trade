"""Audit journal.

Every signal, rejected setup, risk decision, mode change, and execution result
is appended here. The default store is an append-only JSON-lines file so the
record survives restarts without a database. When DATABASE_URL is set and
psycopg is installed, rows are mirrored to PostgreSQL (see db/schema.sql).
Journal writes never raise into the trading path: a failed mirror is logged
as a warning on the in-memory row instead.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("gits.journal")

KINDS = {
    "signal",
    "rejected_setup",
    "risk_decision",
    "execution",
    "mode_change",
    "emergency",
    "model_version",
    "operator_note",
}


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def to_jsonable(payload: Any) -> Any:
    """Round-trip through json so numpy scalars and datetimes become plain types."""
    return json.loads(json.dumps(payload, default=_default))


class Journal:
    def __init__(self, path: Path | None, database_url: str = "") -> None:
        self.path = path
        self._lock = threading.Lock()
        self.rows: list[dict] = []
        self._pg = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        try:
                            self.rows.append(json.loads(line))
                        except json.JSONDecodeError:
                            log.warning("Skipped a corrupt journal line")
        if database_url:
            self._pg = _PostgresMirror.connect(database_url)

    @property
    def backend(self) -> str:
        if self._pg is not None:
            return "jsonl+postgres"
        return "jsonl" if self.path is not None else "memory"

    def record(self, kind: str, payload: dict, note: str = "") -> dict:
        if kind not in KINDS:
            raise ValueError(f"Unknown journal kind {kind}")
        row = {
            "id": str(uuid.uuid4()),
            "kind": kind,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "note": note,
            "payload": to_jsonable(payload),
        }
        with self._lock:
            self.rows.append(row)
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row) + "\n")
        if self._pg is not None:
            try:
                self._pg.insert(row)
            except Exception as exc:  # pragma: no cover - depends on a live database
                row["mirror_warning"] = f"PostgreSQL mirror failed: {exc}"
                log.warning("PostgreSQL mirror failed: %s", exc)
        return row

    def query(self, kind: str | None = None, limit: int = 100) -> list[dict]:
        rows = self.rows if kind is None else [row for row in self.rows if row["kind"] == kind]
        return list(reversed(rows[-limit:]))

    def signal_accuracy(self) -> dict:
        """Score closed paper/backtest trades against the confidence attached at entry."""
        closes = [row["payload"] for row in self.rows if row["kind"] == "execution" and row["payload"].get("event") in {"close", "emergency_flatten"}]
        if not closes:
            return {"closed": 0, "wins": 0, "win_rate": None, "net": 0.0}
        wins = sum(1 for row in closes if float(row.get("pnl", 0)) > 0)
        net = sum(float(row.get("pnl", 0)) for row in closes)
        return {
            "closed": len(closes),
            "wins": wins,
            "win_rate": round(wins / len(closes), 4),
            "net": round(net, 2),
        }


class _PostgresMirror:
    def __init__(self, conn) -> None:
        self.conn = conn

    @classmethod
    def connect(cls, url: str) -> "_PostgresMirror | None":
        try:
            import psycopg  # type: ignore
        except ImportError:
            log.info("psycopg is not installed; journal stays on JSON lines.")
            return None
        dsn = url.replace("postgresql+psycopg://", "postgresql://")
        try:
            conn = psycopg.connect(dsn, autocommit=True, connect_timeout=3)
        except Exception as exc:  # pragma: no cover - depends on a live database
            log.warning("PostgreSQL is unreachable (%s); journal stays on JSON lines.", exc)
            return None
        return cls(conn)

    def insert(self, row: dict) -> None:  # pragma: no cover - depends on a live database
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO journal (id, kind, recorded_at, note, payload) VALUES (%s, %s, %s, %s, %s::jsonb)",
                (row["id"], row["kind"], row["recorded_at"], row["note"], json.dumps(row["payload"])),
            )


class Cache:
    """Thin Redis wrapper for the latest analysis snapshot. Falls back to memory."""

    def __init__(self, url: str = "") -> None:
        self._memory: dict[str, str] = {}
        self._redis = None
        if url:
            try:
                import redis  # type: ignore

                client = redis.Redis.from_url(url, socket_connect_timeout=2)
                client.ping()
                self._redis = client
            except Exception as exc:  # pragma: no cover - depends on a live server
                log.info("Redis unavailable (%s); using in-process cache.", exc)

    @property
    def backend(self) -> str:
        return "redis" if self._redis is not None else "memory"

    def set_json(self, key: str, value: Any, ttl: int = 60) -> None:
        text = json.dumps(to_jsonable(value))
        if self._redis is not None:  # pragma: no cover
            try:
                self._redis.setex(f"gits:{key}", ttl, text)
                return
            except Exception:
                pass
        self._memory[key] = text

    def get_json(self, key: str) -> Any:
        if self._redis is not None:  # pragma: no cover
            try:
                raw = self._redis.get(f"gits:{key}")
                return None if raw is None else json.loads(raw)
            except Exception:
                pass
        raw = self._memory.get(key)
        return None if raw is None else json.loads(raw)
