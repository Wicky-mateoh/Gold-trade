"""Market-data engine.

Loads an M5 base series, validates it, and builds the higher timeframes from
that single source so the frames stay aligned. MetaTrader 5 is optional. When
it is absent, CSV history is used, and otherwise a labeled synthetic feed
keeps the rest of the system testable.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from app.frames import PriceFrame, resample_frame, validate_frame

TIMEFRAMES = ("MN1", "W1", "D1", "H4", "H1", "M15", "M5")
# One lock for every MetaTrader 5 call (price feed and orders share the terminal connection).
MT5_LOCK = threading.RLock()


class MarketDataEngine:
    def __init__(self) -> None:
        self.frames: dict[str, PriceFrame] = {}
        self.source = "unloaded"
        self.source_note = "No market data loaded."
        self.warnings: list[str] = []
        self.mt5_info: dict = {}

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
    """Optional MetaTrader 5 reader. This class never sends orders.

    Requires Windows, a running MT5 terminal, and `pip install MetaTrader5`.
    MT5 stamps bars in the broker's SERVER time (often UTC+2/UTC+3), so bar
    times are shifted to UTC before analysis; otherwise sessions and the
    NFP/CPI windows would be off by hours.
    """

    def __init__(self, module=None) -> None:
        if module is None:
            try:
                import MetaTrader5 as module  # type: ignore
            except ImportError:
                module = None
        self._mt5 = module
        self.info: dict = {}

    @property
    def installed(self) -> bool:
        return self._mt5 is not None

    def connect(self, login: str, password: str, server: str, terminal_path: str = ""):
        if self._mt5 is None:
            raise RuntimeError(
                "The MetaTrader5 Python package is not installed (Windows only: pip install MetaTrader5)."
            )
        if not login or not server:
            raise RuntimeError("MT5_LOGIN and MT5_SERVER are required before a connection is attempted.")
        mt5 = self._mt5
        kwargs = {"path": terminal_path} if terminal_path else {}
        try:
            login_id = int(login)
        except ValueError as exc:
            raise RuntimeError("MT5_LOGIN must be the numeric account number.") from exc
        if not mt5.initialize(login=login_id, password=password, server=server, **kwargs):
            error = mt5.last_error()
            mt5.shutdown()
            raise RuntimeError(
                f"MT5 initialize/login failed: {error}. Check that the MT5 terminal is installed and can log in "
                "with these details by hand, and that MT5_SERVER matches the server name shown in the terminal."
            )
        return mt5

    def resolve_symbol(self, mt5, symbol: str):
        info = mt5.symbol_info(symbol)
        if info is None:
            candidates = sorted(
                {row.name for pattern in ("*XAU*", "*GOLD*", "*Gold*") for row in (mt5.symbols_get(pattern) or [])}
            )
            hint = f" Gold symbols on this server: {', '.join(candidates)}." if candidates else ""
            raise RuntimeError(f"Symbol {symbol} is not offered by this broker.{hint} Set MT5_SYMBOL to the exact name.")
        if not info.visible and not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"Symbol {symbol} could not be added to Market Watch: {mt5.last_error()}")
        return mt5.symbol_info(symbol) or info

    def server_offset_hours(self, mt5, symbol: str, configured: float | None, cache: Path | None) -> tuple[float, str]:
        """Broker server time minus UTC, in hours."""
        if configured is not None:
            return float(configured), "configured"
        tick = mt5.symbol_info_tick(symbol)
        now = datetime.now(timezone.utc).timestamp()
        if tick is not None and getattr(tick, "time", 0):
            raw = float(tick.time) - now
            hours = round(raw / 1800) / 2  # half-hour resolution
            # Only trust a fresh tick (market open): the remainder must be small.
            if abs(raw - hours * 3600) < 600 and abs(hours) <= 14:
                if cache is not None:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(json.dumps({"hours": hours, "detected_at": datetime.now(timezone.utc).isoformat()}))
                return hours, "detected from live tick"
        if cache is not None and cache.exists():
            try:
                return float(json.loads(cache.read_text())["hours"]), "last detected value (market closed)"
            except (ValueError, KeyError, OSError):
                pass
        return 0.0, "UNKNOWN - market closed and no saved value; set MT5_SERVER_UTC_OFFSET_HOURS"

    def copy_m5(
        self,
        symbol: str,
        login: str,
        password: str,
        server: str,
        count: int = 100_000,
        terminal_path: str = "",
        offset_hours: float | None = None,
        offset_cache: Path | None = None,
    ) -> PriceFrame:
        with MT5_LOCK:
            return self._copy_m5_locked(symbol, login, password, server, count, terminal_path, offset_hours, offset_cache)

    def _copy_m5_locked(self, symbol, login, password, server, count, terminal_path, offset_hours, offset_cache) -> PriceFrame:
        mt5 = self.connect(login, password, server, terminal_path)
        try:
            info = self.resolve_symbol(mt5, symbol)
            offset, offset_source = self.server_offset_hours(mt5, symbol, offset_hours, offset_cache)
            # Position 1 skips the bar that is still forming; analysis uses closed bars only.
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 1, count)
            account = mt5.account_info()
        finally:
            mt5.shutdown()
        if rates is None or len(rates) == 0:
            raise RuntimeError(
                "MT5 returned no M5 rates. Open an XAUUSD M5 chart once so the terminal downloads history, "
                "and check Tools > Options > Charts > Max bars in chart."
            )
        point = float(getattr(info, "point", 0.01) or 0.01)
        self.info = {
            "symbol": symbol,
            "digits": getattr(info, "digits", None),
            "point": point,
            "contract_size": getattr(info, "trade_contract_size", None),
            "volume_min": getattr(info, "volume_min", None),
            "volume_step": getattr(info, "volume_step", None),
            "server_utc_offset_hours": offset,
            "offset_source": offset_source,
            "bars": int(len(rates)),
            "account_server": getattr(account, "server", server),
            "account_trade_mode": getattr(account, "trade_mode", None),
        }
        utc_seconds = rates["time"].astype("int64") - int(round(offset * 3600))
        return PriceFrame(
            "M5",
            pd.to_datetime(utc_seconds, unit="s", utc=True),
            rates["open"],
            rates["high"],
            rates["low"],
            rates["close"],
            rates["tick_volume"],
            rates["spread"] * point,  # MT5 spread is in points; point is 0.01 or 0.001 depending on the broker
        )


def build_engine(root: Path, settings) -> MarketDataEngine:
    """Pick the data source: DATA_SOURCE=auto uses a CSV if present, else MT5, else synthetic."""
    s = settings
    engine = MarketDataEngine()
    csv_path = root / "data" / f"{s.mt5_symbol}_M5.csv"
    mode = getattr(s, "data_source", "auto")
    if mode == "csv" or (mode == "auto" and csv_path.exists()):
        if not csv_path.exists():
            raise RuntimeError(f"DATA_SOURCE=csv but {csv_path} does not exist.")
        engine.load_base(
            load_csv(csv_path),
            source="csv",
            note=f"Historical file {csv_path.name}. Confirm the broker contract before comparing it with a live chart.",
        )
        return engine
    feed = MT5Feed()
    if mode == "mt5" or (feed.installed and s.mt5_login and s.mt5_server):
        frame = feed.copy_m5(
            s.mt5_symbol,
            s.mt5_login,
            s.mt5_password,
            s.mt5_server,
            count=s.mt5_history_bars,
            terminal_path=s.mt5_terminal_path,
            offset_hours=s.mt5_server_utc_offset_hours,
            offset_cache=root / "var" / "mt5_server_offset.json",
        )
        info = feed.info
        engine.load_base(
            frame,
            source="metatrader5",
            note=(
                f"MT5 {info['account_server']} {info['symbol']}: {info['bars']} closed M5 bars, "
                f"server time UTC{info['server_utc_offset_hours']:+g} ({info['offset_source']}), shifted to UTC."
            ),
        )
        engine.mt5_info = info
        if info["offset_source"].startswith("UNKNOWN"):
            engine.warnings.append("MT5 server time offset is unknown; sessions and NFP/CPI windows may be shifted.")
        _check_contract(engine, info, s)
        return engine
    engine.load_base(
        synthetic_m5(),
        source="synthetic",
        note="Synthetic XAU/USD path for development. It is not a broker feed and is not evidence of an edge.",
    )
    return engine


def _check_contract(engine: MarketDataEngine, info: dict, settings) -> None:
    """Position sizing uses CONTRACT_OUNCES/MIN_LOT/LOT_STEP; warn when the broker differs."""
    for key, setting in (("contract_size", "contract_ounces"), ("volume_min", "min_lot"), ("volume_step", "lot_step")):
        broker = info.get(key)
        mine = getattr(settings, setting)
        if broker is not None and abs(float(broker) - float(mine)) > 1e-9:
            engine.warnings.append(
                f"Broker {key} is {broker} but {setting.upper()} is {mine}. Update .env so position sizes are correct."
            )
