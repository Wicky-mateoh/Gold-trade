"""Export your broker's XAU/USD M5 history from MT5 to a CSV the bot can test on.

Read-only. Run on the Windows PC with MT5, from "Gold trade/":
    python scripts\\export_mt5_history.py                 # uses MT5_HISTORY_BARS from .env
    python scripts\\export_mt5_history.py --bars 200000   # more history

Writes data/history/XAUUSD_M5_<first>_<last>.csv (times in UTC, spread in price).
It is deliberately NOT written to data/XAUUSD_M5.csv, so it never replaces the
live MT5 feed. Use it with:
    python scripts\\replay.py --csv data\\history\\<file>.csv --start-days 120
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import load_settings  # noqa: E402
from app.frames import validate_frame  # noqa: E402
from app.market import MT5Feed  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, default=None, help="M5 bars to export (default MT5_HISTORY_BARS)")
    args = parser.parse_args()
    s = load_settings()
    feed = MT5Feed()
    try:
        frame = feed.copy_m5(
            s.mt5_symbol, s.mt5_login, s.mt5_password, s.mt5_server,
            count=args.bars or s.mt5_history_bars, terminal_path=s.mt5_terminal_path,
            offset_hours=s.mt5_server_utc_offset_hours, offset_cache=ROOT / "var" / "mt5_server_offset.json",
        )
    except RuntimeError as exc:
        print(f"Export failed: {exc}")
        return 1
    report = validate_frame(frame)
    if report.rejected:
        print("History failed validation:", "; ".join(report.errors))
        return 1
    clean = report.frame
    times = pd.DatetimeIndex(clean.time)
    out_dir = ROOT / "data" / "history"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{s.mt5_symbol}_M5_{times[0]:%Y%m%d}_{times[-1]:%Y%m%d}.csv"
    pd.DataFrame(
        {
            "time": times.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": clean.open, "high": clean.high, "low": clean.low, "close": clean.close,
            "volume": clean.volume, "spread": clean.spread,
        }
    ).to_csv(out, index=False)
    days = len(clean) / 276
    print(f"Wrote {len(clean):,} bars (~{days:.0f} trading days, {times[0]:%Y-%m-%d} to {times[-1]:%Y-%m-%d} UTC) to {out}")
    print(f"Broker server time UTC{feed.info['server_utc_offset_hours']:+g} ({feed.info['offset_source']}).")
    for warning in report.warnings:
        print("Note:", warning)
    if days < 250:
        print("Under a year of history. Raise MT5 Tools > Options > Charts > Max bars in chart and use --bars.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
