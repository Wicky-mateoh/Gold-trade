"""Paper-trade replay through the LIVE service path.

Feeds M5 history into TradingService a step at a time, as a broker feed
would, and runs the same cycle the server runs: mark paper positions, then
analyse, decide, risk-check, and open in paper mode. It is the end-to-end
check that the backtest cannot be (the backtest uses a faster, simplified
context builder).

Usage (from "Gold trade/"):
    python scripts/replay.py --csv data/XAUUSD_M5.csv --start-days 90 --step-bars 12
    python scripts/replay.py --demo trend          # generated trending data
    python scripts/replay.py --demo random         # random walk (expect ~no trades)

The account, journal, and model registry live in a temporary directory, so
a replay never touches var/.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings  # noqa: E402
from app.execution import CLOSE_EVENTS  # noqa: E402
from app.frames import PriceFrame  # noqa: E402
from app.market import MarketDataEngine, load_csv, synthetic_m5  # noqa: E402
from app.service import TradingService  # noqa: E402


def demo_trend(days: int, seed: int = 11) -> PriceFrame:
    """Weekday M5 bars with alternating multi-week trends, pullbacks, and a daily 21:00 UTC break."""
    rng = np.random.default_rng(seed)
    times = pd.date_range("2025-01-06", periods=days * 288 * 7 // 5 + 900, freq="5min")
    times = times[(times.dayofweek < 5) & (times.hour != 21)][: days * 276]
    n = len(times)
    drift = np.sign(np.sin(np.arange(n) / (276 * 15))) * 0.03
    cycle = np.sin(np.arange(n) / (276 * 1.3)) * 0.2
    close = 2600 + np.cumsum(drift + cycle + rng.normal(0, 0.3, n))
    open_ = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0.15, 0.05, n))
    spread = np.clip(rng.normal(0.25, 0.05, n), 0.15, 0.6)
    return PriceFrame("M5", times, open_, np.maximum(open_, close) + wick, np.minimum(open_, close) - wick, close, rng.integers(80, 400, n).astype(float), spread)


def prefix(frame: PriceFrame, end: int) -> PriceFrame:
    return PriceFrame("M5", frame.time[:end], frame.open[:end], frame.high[:end], frame.low[:end], frame.close[:end], frame.volume[:end], frame.spread[:end])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--demo", choices=["trend", "random"])
    parser.add_argument("--days", type=int, default=240, help="demo history length")
    parser.add_argument("--start-days", type=int, default=150, help="history loaded before the first step")
    parser.add_argument("--step-bars", type=int, default=12, help="M5 bars per step (12 = hourly)")
    parser.add_argument("--max-steps", type=int, default=10_000)
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    parser.add_argument("--profile", choices=["intraday", "scalping"], default="intraday")
    args = parser.parse_args()

    if args.csv:
        full = load_csv(args.csv)
        label = str(args.csv)
    elif args.demo == "random":
        full = synthetic_m5(days=args.days)
        label = "demo random walk"
    else:
        full = demo_trend(args.days)
        label = "demo trend"

    settings = Settings(_env_file=None, app_mode="paper", strategy_profile=args.profile)
    engine = MarketDataEngine()
    bars_per_day = 288
    start = min(len(full) - 1, args.start_days * bars_per_day)
    engine.load_base(prefix(full, start), "replay", label)
    state = Path(tempfile.mkdtemp(prefix="gits-replay-"))
    svc = TradingService(settings, ROOT, engine=engine, state_dir=state)
    # News files belong to a different period, so they are not replayed. The built-in
    # NFP/CPI schedule still protects every month of the history, as it would live.
    svc.news = []

    decisions: Counter = Counter()
    tier1_blocked = 0
    risk_rejections: Counter = Counter()
    nt_reasons: Counter = Counter()
    clock = time.perf_counter()
    steps = 0
    for end in range(start, len(full) + 1, args.step_bars):
        if steps >= args.max_steps:
            break
        if end > start:
            svc.engine.load_base(prefix(full, end), "replay", label)
        result = svc.evaluate()
        steps += 1
        signal = result["signal"]
        tier1_blocked += int(bool((result.get("tier1") or {}).get("blackout")))
        decisions[signal["decision"]] += 1
        if signal["decision"] == "NO_TRADE":
            for reason in signal["reasons"][:1]:
                nt_reasons[reason.split(".")[0][:70]] += 1
        if result["risk"] and not result["risk"]["approved"]:
            for reason in result["risk"]["reasons"]:
                risk_rejections[reason] += 1
    # Close the books on the final bar so the summary is complete.
    svc.mark_paper()
    elapsed = time.perf_counter() - clock

    journal = svc.journal.rows
    opens = [r["payload"] for r in journal if r["kind"] == "execution" and r["payload"].get("event") == "open"]
    closes = [r["payload"] for r in journal if r["kind"] == "execution" and r["payload"].get("event") in CLOSE_EVENTS]
    pnl = float(sum(c["pnl"] for c in closes))
    summary = {
        "data": label,
        "steps": steps,
        "seconds_per_step": round(elapsed / max(steps, 1), 3),
        "decisions": dict(decisions),
        "top_no_trade_reasons": dict(nt_reasons.most_common(6)),
        "risk_rejections": dict(risk_rejections),
        "paper_opened": len(opens),
        "paper_closed": len(closes),
        "still_open": len(svc.book.paper_positions),
        "wins": sum(1 for c in closes if c["pnl"] > 0),
        "exit_reasons": dict(Counter((c.get("exit_reason") or c.get("event", "")).split(":")[0] for c in closes)),
        "tier1_blocked_steps": tier1_blocked,
        "net_pnl": round(pnl, 2),
        "equity_start": settings.account_equity,
        "equity_end": round(svc.account.equity, 2),
        "equity_reconciles": abs(settings.account_equity + pnl - svc.account.equity) < 0.01,
        "max_concurrent_open_limit": settings.max_open_positions,
        "journal_rows": len(journal),
        "journal_dir": str(state),
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        for key, value in summary.items():
            print(f"{key:>26}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
