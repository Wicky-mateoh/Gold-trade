"""Check the MetaTrader 5 connection before starting the bot. Read-only: sends no orders.

Run on the Windows PC where the MT5 terminal is installed, from "Gold trade/":
    python scripts\\check_mt5.py

It reads MT5_* settings from .env and reports:
  * whether the terminal starts and logs in
  * account type (demo / real), server, currency
  * the exact gold symbol and the broker's alternatives
  * broker server time vs UTC (sessions and NFP/CPI windows depend on it)
  * contract size and lot rules vs CONTRACT_OUNCES / MIN_LOT / LOT_STEP
  * how much M5 history the terminal returns, and the latest closed bars in UTC
  * which data source the bot will actually use
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import load_settings  # noqa: E402
from app.market import MT5Feed, build_engine  # noqa: E402

OK, WARN, FAIL = "[ OK ]", "[WARN]", "[FAIL]"


def main() -> int:
    s = load_settings()
    problems = 0
    print("GITS - MetaTrader 5 connection check (read-only)\n")

    csv_path = ROOT / "data" / f"{s.mt5_symbol}_M5.csv"
    if s.data_source == "auto" and csv_path.exists():
        problems += 1
        print(f"{WARN} {csv_path.name} exists and DATA_SOURCE=auto, so the bot will use the CSV, not MT5.")
        print("       Set DATA_SOURCE=mt5 in .env, or move the CSV away.\n")

    feed = MT5Feed()
    if not feed.installed:
        print(f"{FAIL} The MetaTrader5 Python package is not installed. Run: pip install MetaTrader5")
        print("       (It only exists for Windows. Run this on the PC with the MT5 terminal.)")
        return 1
    print(f"{OK} MetaTrader5 package found.")
    if not s.mt5_login or not s.mt5_server:
        print(f"{FAIL} MT5_LOGIN and MT5_SERVER must be set in .env.")
        return 1

    try:
        mt5 = feed.connect(s.mt5_login, s.mt5_password, s.mt5_server, s.mt5_terminal_path)
    except RuntimeError as exc:
        print(f"{FAIL} {exc}")
        return 1
    try:
        account = mt5.account_info()
        terminal = mt5.terminal_info()
        modes = {0: "DEMO", 1: "CONTEST", 2: "REAL"}
        mode = modes.get(getattr(account, "trade_mode", -1), "unknown")
        print(f"{OK} Logged in: account {account.login} on {account.server} ({mode}), "
              f"balance {account.balance:,.2f} {account.currency}.")
        if mode == "REAL":
            print(f"{WARN} This is a REAL account. The bot only reads prices and simulates trades, but consider "
                  "using the investor (read-only) password.")
        if terminal is not None:
            print(f"{OK} Terminal: {terminal.name}, build {terminal.build}, max bars in chart {terminal.maxbars:,}.")
            if terminal.maxbars < s.mt5_history_bars:
                problems += 1
                print(f"{WARN} MT5_HISTORY_BARS={s.mt5_history_bars:,} but the terminal keeps {terminal.maxbars:,}. "
                      "Raise Tools > Options > Charts > Max bars in chart, or lower MT5_HISTORY_BARS.")

        try:
            info = feed.resolve_symbol(mt5, s.mt5_symbol)
        except RuntimeError as exc:
            print(f"{FAIL} {exc}")
            return 1
        print(f"{OK} Symbol {info.name}: {info.digits} digits, point {info.point}, contract {info.trade_contract_size}, "
              f"lots min {info.volume_min} step {info.volume_step}.")
        for broker, mine, name in (
            (info.trade_contract_size, s.contract_ounces, "CONTRACT_OUNCES"),
            (info.volume_min, s.min_lot, "MIN_LOT"),
            (info.volume_step, s.lot_step, "LOT_STEP"),
        ):
            if abs(float(broker) - float(mine)) > 1e-9:
                problems += 1
                print(f"{WARN} {name}={mine} but the broker uses {broker}. Set {name}={broker} in .env "
                      "(position sizes depend on it).")

        offset, source = feed.server_offset_hours(mt5, s.mt5_symbol, s.mt5_server_utc_offset_hours, ROOT / "var" / "mt5_server_offset.json")
        if source.startswith("UNKNOWN"):
            problems += 1
            print(f"{WARN} Broker server time offset: {source}.")
            print("       Run this again while the market is open, or set MT5_SERVER_UTC_OFFSET_HOURS.")
        else:
            print(f"{OK} Broker server time is UTC{offset:+g} ({source}). Bars are shifted to UTC.")
    finally:
        mt5.shutdown()

    try:
        engine = build_engine(ROOT, s.model_copy(update={"data_source": "mt5"}))
    except Exception as exc:
        print(f"{FAIL} Loading M5 history failed: {exc}")
        return 1
    m5 = engine.frame("M5")
    days = len(m5) / 276
    print(f"{OK} Loaded {len(m5):,} closed M5 bars (~{days:.0f} trading days); "
          + ", ".join(f"{tf} {len(engine.frame(tf) or [])}" for tf in ("M15", "H1", "H4", "D1")) + " bars built.")
    if days < 250:
        problems += 1
        print(f"{WARN} Under a year of history: D1 analysis and the EMA 200 need ~250 daily bars. "
              "Raise MT5_HISTORY_BARS and the terminal's Max bars in chart.")
    for warning in engine.warnings[-5:]:
        problems += 1
        print(f"{WARN} {warning}")
    print("\nLatest closed M5 bars (UTC):")
    for row in m5.to_records(3):
        print(f"   {row['time']}  O {row['open']:.2f}  H {row['high']:.2f}  L {row['low']:.2f}  C {row['close']:.2f}  spread {row['spread']:.2f}")
    print("\nCompare the last close with your MT5 chart (MT5 shows server time, these are UTC).")
    print("\nResult:", "ready" if problems == 0 else f"connected, with {problems} setting(s) to fix above")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
