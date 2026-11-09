"""Market-data engine.

Loads an M5 base series, validates it, and builds the higher timeframes from
that single source so the frames stay aligned. MetaTrader 5 is optional. When
it is absent, CSV history is used, and otherwise a labeled synthetic feed
keeps the rest of the system testable.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from app.frames import PriceFrame, resample_frame, validate_frame

TIMEFRAMES = ("MN1", "W1", "D1", "H4", "H1", "M15", "M5")


class MarketDataEngine:
    def __init__(self) -> None:
        self.frames: dict[str, PriceFrame] = {}
        self.source = "unloaded"
        self.source_note = "No market data loaded."
        self.warnings: list[str] = []

    def load_base(self, frame: PriceFrame, source: str, note: str) -> None:
        report = validate_frame(frame)
        self.warnings = list(report.warnings)
        if report.rejected or report.frame is None:
            raise ValueError("; ".join(report.errors) or "Base frame was rejected")
        self.frames = {"M5": report.frame}
        for timeframe in ("M15", "H1", "H4", "D1", "W1", "MN1"):
            resampled = resample_frame(report.frame, timeframe)
            checked = validate_frame(resampled)
            if checked.frame is not None:
                self.frames[timeframe] = checked.frame
                self.warnings.extend(checked.warnings)
        self.source = source
        self.source_note = note

    def frame(self, timeframe: str) -> PriceFrame | None:
        return self.frames.get(timeframe)

    def synchronization_report(self) -> dict[str, bool | str]:
        base = self.frames.get("M5")
        hour = self.frames.get("H1")
        if base is None or hour is None or len(hour) == 0:
            return {"aligned": False, "detail": "M5 and H1 frames are not both available"}
        last_hour = pd.Timestamp(hour.time[-1])
        mask = (pd.DatetimeIndex(base.time) >= last_hour) & (
            pd.DatetimeIndex(base.time) < last_hour + pd.Timedelta(hours=1)
        )
        if not np.any(mask):
            return {"aligned": False, "detail": "No M5 bars found inside the latest H1 bucket"}
        child_close = float(base.close[np.flatnonzero(mask)[-1]])
        aligned = np.isclose(child_close, float(hour.close[-1]))
        return {
            "aligned": bool(aligned),
            "detail": "Latest H1 close matches the last M5 close in that hour"
            if aligned
            else "Latest H1 close does not match the M5 child close",
        }


def load_csv(path: Path) -> PriceFrame:
    df = pd.read_csv(path)
    required = {"time", "open", "high", "low", "close", "volume"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"CSV is missing columns: {sorted(missing)}")
    spread = df["spread"] if "spread" in df.columns else np.full(len(df), np.nan)
    return PriceFrame("M5", df["time"], df["open"], df["high"], df["low"], df["close"], df["volume"], spread)


def synthetic_m5(days: int = 420, seed: int = 42, start: str = "2025-01-06T00:00:00Z") -> PriceFrame:
    """Build a weekend-aware random path for plumbing tests.

    The path is not a claim about gold's future distribution.
    """
    rng = np.random.default_rng(seed)
    cursor = pd.Timestamp(start)
    times: list[pd.Timestamp] = []
    while len(times) < days * 288:
        if cursor.dayofweek < 5:
            times.append(cursor)
        cursor += pd.Timedelta(minutes=5)
    n = len(times)
    shocks = rng.normal(0.0, 0.35, n)
    regime = np.sin(np.linspace(0, 18, n)) * 0.15
    close = 2620 + np.cumsum(regime + shocks)
    open_ = np.empty(n)
    open_[0] = close[0] - shocks[0]
    open_[1:] = close[:-1]
    wick = np.abs(rng.normal(0.18, 0.08, n))
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - wick
    volume = rng.integers(80, 400, n).astype(float)
    spread = np.full(n, 0.24)
    return PriceFrame(
        "M5",
        pd.DatetimeIndex(times),
        open_,
        high,
        low,
        close,
        volume,
        spread,
    )


class MT5Feed:
    """Optional MetaTrader 5 reader. This class does not send orders."""

    def __init__(self) -> None:
        try:
            import MetaTrader5 as mt5  # type: ignore
        except ImportError:
            mt5 = None
        self._mt5 = mt5

    @property
    def installed(self) -> bool:
        return self._mt5 is not None

    def copy_m5(self, symbol: str, login: str, password: str, server: str, count: int = 20000) -> PriceFrame:
        if self._mt5 is None:
            raise RuntimeError("The MetaTrader5 package is not installed. No terminal connection was opened.")
        if not login or not server:
            raise RuntimeError("MT5 login and server are required before a connection is attempted.")
        mt5 = self._mt5
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        authorized = mt5.login(int(login), password=password, server=server)
        if not authorized:
            mt5.shutdown()
            raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, count)
        mt5.shutdown()
        if rates is None or len(rates) == 0:
            raise RuntimeError("MT5 returned no M5 rates.")
        return PriceFrame(
            "M5",
            pd.to_datetime(rates["time"], unit="s", utc=True),
            rates["open"],
            rates["high"],
            rates["low"],
            rates["close"],
            rates["tick_volume"],
            rates["spread"] * 0.01,
        )


def build_engine(root: Path, symbol: str, mt5_login: str, mt5_password: str, mt5_server: str) -> MarketDataEngine:
    engine = MarketDataEngine()
    csv_path = root / "data" / f"{symbol}_M5.csv"
    if csv_path.exists():
        engine.load_base(
            load_csv(csv_path),
            source="csv",
            note=f"Historical file {csv_path.name}. Confirm the broker contract before comparing it with a live chart.",
        )
        return engine
    feed = MT5Feed()
    if feed.installed and mt5_login and mt5_server:
        frame = feed.copy_m5(symbol, mt5_login, mt5_password, mt5_server)
        engine.load_base(frame, source="metatrader5", note="M5 rates requested from the configured MT5 terminal.")
        return engine
    engine.load_base(
        synthetic_m5(),
        source="synthetic",
        note="Synthetic XAU/USD path for development. It is not a broker feed and is not evidence of an edge.",
    )
    return engine
