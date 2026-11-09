"""Price-frame container and market-data validation.

Candles live in columnar arrays so checks for duplicates, corrupt OHLC, and
missing bars stay vectorized and free of look-ahead side effects.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

TIMEFRAME_MINUTES: dict[str, int | None] = {
    "M5": 5,
    "M15": 15,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
    "W1": None,
    "MN1": None,
}

CHILD_BARS: dict[str, int] = {
    "M15": 3,
    "H1": 12,
    "H4": 48,
    "D1": 288,
    "W1": 1440,
}


@dataclass
class PriceFrame:
    timeframe: str
    time: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    spread: np.ndarray

    def __post_init__(self) -> None:
        self.time = pd.to_datetime(self.time, utc=True).to_numpy(dtype="datetime64[ns]")
        self.open = np.asarray(self.open, dtype=float)
        self.high = np.asarray(self.high, dtype=float)
        self.low = np.asarray(self.low, dtype=float)
        self.close = np.asarray(self.close, dtype=float)
        self.volume = np.asarray(self.volume, dtype=float)
        self.spread = np.asarray(self.spread, dtype=float)
        lengths = {
            len(self.time),
            len(self.open),
            len(self.high),
            len(self.low),
            len(self.close),
            len(self.volume),
            len(self.spread),
        }
        if len(lengths) != 1:
            raise ValueError("Price frame columns must have the same length")

    def __len__(self) -> int:
        return int(len(self.time))

    def slice(self, end: int) -> "PriceFrame":
        end = int(end)
        return PriceFrame(
            self.timeframe,
            self.time[:end],
            self.open[:end],
            self.high[:end],
            self.low[:end],
            self.close[:end],
            self.volume[:end],
            self.spread[:end],
        )

    def to_records(self, limit: int = 120) -> list[dict[str, float | str | None]]:
        start = max(0, len(self) - limit)
        rows: list[dict[str, float | str | None]] = []
        for i in range(start, len(self)):
            spread = self.spread[i]
            rows.append(
                {
                    "time": pd.Timestamp(self.time[i]).isoformat(),
                    "open": float(self.open[i]),
                    "high": float(self.high[i]),
                    "low": float(self.low[i]),
                    "close": float(self.close[i]),
                    "volume": float(self.volume[i]),
                    "spread": None if np.isnan(spread) else float(spread),
                }
            )
        return rows


@dataclass
class ValidationReport:
    frame: PriceFrame | None
    errors: list[str]
    warnings: list[str]
    duplicates: int
    missing_bars: int
    rejected: bool


def _weekend_gap(left: np.datetime64, right: np.datetime64) -> bool:
    start = pd.Timestamp(left)
    end = pd.Timestamp(right)
    if end <= start:
        return False
    if start.dayofweek == 4 and end.dayofweek == 0 and (end - start) <= pd.Timedelta(days=3, hours=12):
        return True
    return False


def validate_frame(frame: PriceFrame) -> ValidationReport:
    errors: list[str] = []
    warnings: list[str] = []
    if frame.timeframe not in TIMEFRAME_MINUTES:
        return ValidationReport(None, [f"Unsupported timeframe {frame.timeframe}"], [], 0, 0, True)
    if len(frame) == 0:
        return ValidationReport(None, ["Price frame is empty"], [], 0, 0, True)

    order = np.argsort(frame.time, kind="mergesort")
    ordered = PriceFrame(
        frame.timeframe,
        frame.time[order],
        frame.open[order],
        frame.high[order],
        frame.low[order],
        frame.close[order],
        frame.volume[order],
        frame.spread[order],
    )
    times = ordered.time
    _, unique_idx, counts = np.unique(times, return_index=True, return_counts=True)
    duplicates = int(np.sum(counts - 1))
    if duplicates:
        warnings.append(f"Dropped {duplicates} duplicate candle timestamps")
    keep = np.sort(unique_idx)
    ordered = PriceFrame(
        frame.timeframe,
        times[keep],
        ordered.open[keep],
        ordered.high[keep],
        ordered.low[keep],
        ordered.close[keep],
        ordered.volume[keep],
        ordered.spread[keep],
    )

    finite = np.isfinite(ordered.open) & np.isfinite(ordered.high) & np.isfinite(ordered.low) & np.isfinite(ordered.close)
    positive = (ordered.open > 0) & (ordered.high > 0) & (ordered.low > 0) & (ordered.close > 0)
    ohlc = (
        (ordered.high >= np.maximum(ordered.open, ordered.close))
        & (ordered.low <= np.minimum(ordered.open, ordered.close))
        & (ordered.high >= ordered.low)
    )
    volume_ok = np.isfinite(ordered.volume) & (ordered.volume >= 0)
    spread_ok = np.isnan(ordered.spread) | ((np.isfinite(ordered.spread)) & (ordered.spread >= 0))
    good = finite & positive & ohlc & volume_ok & spread_ok
    corrupt = int(np.size(good) - np.sum(good))
    if corrupt:
        warnings.append(f"Removed {corrupt} corrupt candles")
    if corrupt / max(len(ordered), 1) > 0.02:
        errors.append("More than 2% of candles failed OHLC validation")
        return ValidationReport(None, errors, warnings, duplicates, 0, True)

    cleaned = PriceFrame(
        frame.timeframe,
        ordered.time[good],
        ordered.open[good],
        ordered.high[good],
        ordered.low[good],
        ordered.close[good],
        ordered.volume[good],
        ordered.spread[good],
    )
    missing = _count_missing(cleaned)
    if missing:
        warnings.append(f"Detected {missing} missing candles")
    if len(cleaned) == 0:
        errors.append("No valid candles remained after validation")
    rejected = bool(errors)
    return ValidationReport(None if rejected else cleaned, errors, warnings, duplicates, missing, rejected)


def _count_missing(frame: PriceFrame) -> int:
    minutes = TIMEFRAME_MINUTES[frame.timeframe]
    if len(frame) < 2:
        return 0
    if minutes is None:
        return 0
    step = np.timedelta64(minutes, "m")
    deltas = np.diff(frame.time.astype("datetime64[ns]"))
    missing = 0
    for left, right, delta in zip(frame.time[:-1], frame.time[1:], deltas):
        if delta == step:
            continue
        if delta > step and _weekend_gap(left, right):
            continue
        if delta > step:
            gaps = int(delta / step) - 1
            missing += max(gaps, 1)
        else:
            missing += 1
    return missing


def resample_frame(frame: PriceFrame, timeframe: str) -> PriceFrame:
    if timeframe not in CHILD_BARS and timeframe not in {"W1", "MN1"}:
        raise ValueError(f"Cannot resample into {timeframe}")
    if frame.timeframe != "M5":
        raise ValueError("Resample expects an M5 base frame")
    rule = {
        "M15": "15min",
        "H1": "1h",
        "H4": "4h",
        "D1": "1D",
        "W1": "W-MON",
        "MN1": "MS",
    }[timeframe]
    index = pd.DatetimeIndex(frame.time)
    df = pd.DataFrame(
        {
            "open": frame.open,
            "high": frame.high,
            "low": frame.low,
            "close": frame.close,
            "volume": frame.volume,
            "spread": frame.spread,
            "count": np.ones(len(frame)),
        },
        index=index,
    )
    grouped = df.resample(rule, label="left", closed="left").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "spread": "mean",
            "count": "sum",
        }
    )
    grouped = grouped.dropna(subset=["open", "high", "low", "close"])
    expected = CHILD_BARS.get(timeframe)
    if expected is not None:
        minimum = expected if timeframe != "D1" else int(expected * 0.9)
        if timeframe == "W1":
            minimum = int(expected * 0.9)
        grouped = grouped[grouped["count"] >= minimum]
    if timeframe == "MN1":
        grouped = grouped[grouped["count"] >= 20 * 288 * 0.9]
    return PriceFrame(
        timeframe,
        grouped.index.to_numpy(),
        grouped["open"].to_numpy(),
        grouped["high"].to_numpy(),
        grouped["low"].to_numpy(),
        grouped["close"].to_numpy(),
        grouped["volume"].to_numpy(),
        grouped["spread"].to_numpy(),
    )
