"""Runtime service.

Owns the single in-process state: market data, account, paper book, journal,
and model registry. The API layer is a thin wrapper around these methods so
the same flow can be driven from tests or a scheduler.

Flow per evaluation:
    market data -> analysis context -> signal gate -> independent risk gate
    -> execution book (analysis: record only, paper: simulate) -> journal
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from app.analysis import build_context
from app.econ_calendar import EventCalendar
from app.backtest import CostModel, run_backtest, validate_out_of_sample
from app.execution import CLOSE_EVENTS, ExecutionBook, LiveChecklist
from app.journal import Cache, Journal, to_jsonable
from app.learning import (
    ModelRegistry,
    calibration_table,
    detect_drift,
    filter_recommendations,
    regime_label,
    study_outcomes,
)
from app.macro import EconomicEvent, load_calendar
from app.market import MarketDataEngine, build_engine
from app.profiles import get_profile
from app.risk import AccountState, OrderProposal, assess, propose_stop_update, register_closed_trade, roll_account_clock
from app.sentiment import NewsItem, load_news_directory
from app.signals import decide


class TradingService:
    def __init__(self, settings, root: Path, engine: MarketDataEngine | None = None, state_dir: Path | None = None) -> None:
        self.settings = settings
        self.root = root
        self.state_dir = state_dir or root / "var"
        self.lock = threading.RLock()
        self.engine = engine or build_engine(root, settings)
        self.events: list[EconomicEvent] = load_calendar(root / "data" / "economic_calendar.csv")
        self.news: list[NewsItem] = load_news_directory(root / "data" / "news")
        self.content_note = "Calendar and news come from operator-supplied files."
        if self.engine.source == "synthetic" and not self.events and not self.news:
            # Demo mode only: labelled example rows so the dashboard panels have content.
            self.events = load_calendar(root / "data" / "economic_calendar.example.csv")
            self.news = load_news_directory(root / "data" / "news", include_examples=True)
            self.content_note = "Synthetic demo: calendar and news are EXAMPLE rows, not real events."
        self.calendar = EventCalendar(settings, self.events, self.state_dir / "calendar_cache.json")
        self.calendar.refresh()
        self.profile = get_profile(settings.strategy_profile)
        self.account = AccountState(settings.account_equity, settings.account_equity, settings.account_equity)
        self.account.emergency_shutdown = False
        self.book = ExecutionBook()
        self.journal = Journal(self.state_dir / "journal.jsonl", settings.database_url)
        self.cache = Cache(settings.redis_url)
        self.registry = ModelRegistry(self.state_dir / "models")
        self.equity_history: list[dict] = [{"time": datetime.now(timezone.utc).isoformat(), "equity": self.account.equity}]
        self.last_backtest: dict | None = None
        self.last_validation: dict | None = None
        self._last_recorded_bar: str | None = None
        if settings.app_mode == "paper":
            self.book.set_mode("paper")
        elif settings.app_mode == "live":
            # Live is never armed from configuration alone.
            result = self.book.request_live(self._checklist_from_settings())
            self.journal.record("mode_change", result, note="Startup requested live mode from APP_MODE.")

    # ------------------------------------------------------------------ status
    def _checklist_from_settings(self) -> LiveChecklist:
        s = self.settings
        return LiveChecklist(
            explicit_authorization=bool(s.live_trading_authorized),
            broker_connected=bool(s.broker_connected),
            account_risk_limits_set=True,
            emergency_shutdown_armed=bool(s.emergency_shutdown_armed),
        )

    def status(self) -> dict:
        return {
            "instrument": self.settings.instrument,
            "mode": self.book.mode,
            "live_enabled": self.book.live_enabled,
            "live_checklist_missing": self._checklist_from_settings().missing()
            + ([] if self.book.adapter.ready else ["a ready broker adapter"]),
            "data_source": self.engine.source,
            "data_note": self.engine.source_note,
            "data_warnings": self.engine.warnings[-10:],
            "mt5": self.engine.mt5_info or None,
            "content_note": self.content_note,
            "journal_backend": self.journal.backend,
            "cache_backend": self.cache.backend,
            "emergency_shutdown": self.account.emergency_shutdown,
            "loss_pause": self.account.loss_pause,
            "risk_limits": self.settings.risk_snapshot(),
            "events_loaded": len(self.events),
            "calendar_feed": self.calendar.feed_status,
            "strategy_profile": self.profile.name,
            "news_loaded": len(self.news),
        }

    # ---------------------------------------------------------------- analysis
    def analysis(self, now: datetime | None = None) -> dict:
        with self.lock:
            _, payload = build_context(self.engine, self.calendar, self.news, self.settings, now)
            self.cache.set_json("analysis", payload, ttl=30)
            return to_jsonable(payload)

    def evaluate(self, now: datetime | None = None, record: bool = True) -> dict:
        """Run the full decision pipeline once and journal the outcome.

        A decision is journaled once per closed bar: re-polling the same bar
        returns the same answer without duplicating journal rows or opening a
        second position. record=False is a preview (used by the live stream).
        """
        with self.lock:
            closed = self.mark_paper() if record and self.book.mode == "paper" else []
            context, payload = build_context(self.engine, self.calendar, self.news, self.settings, now)
            tier1 = payload.get("tier1") or {}
            if record and tier1.get("flatten_due") and self.book.paper_positions:
                closed += self._event_flatten(tier1)
            bar_key = context.timestamp.astimezone(timezone.utc).isoformat()
            repeat = record and bar_key == self._last_recorded_bar
            persist = record and not repeat
            signal = decide(
                context,
                self.settings.min_signal_confidence,
                self.settings.min_reward_risk,
                self.settings.max_spread,
            )
            signal["spread"] = context.spread
            signal["session"] = context.session
            signal["regime"] = regime_label(context.adx, context.volatility)
            signal["atr"] = context.atr
            risk = None
            position = None
            # The risk gate counts the book's real open positions.
            self.account.open_positions = [dict(row) for row in self.book.paper_positions]
            if signal["actionable"]:
                proposal = OrderProposal(
                    direction=signal["direction"],
                    entry=float(signal["price"]),
                    stop_loss=float(signal["stop_loss"]),
                    take_profit=float(signal["take_profits"][0]),
                    spread=float(context.spread or 0.0),
                    timestamp=context.timestamp,
                    event_blackout=bool(context.macro_blackout or tier1.get("blackout") or tier1.get("flatten_due")),
                )
                risk = assess(proposal, self.account, self.settings)
                if persist:
                    self.journal.record("signal", signal)
                    self.journal.record("risk_decision", {"signal_timestamp": signal["timestamp"], **risk})
                if persist and risk["approved"] and self.book.mode == "paper":
                    fills_before = len(self.book.fills)
                    position = self.book.open_paper(signal, risk, context.timestamp, self.settings.max_slippage)
                    if position:
                        # Entry is at this bar's close, so marking starts with the next bar.
                        position["marked_until"] = bar_key
                        self.account.open_positions = [dict(row) for row in self.book.paper_positions]
                        self.journal.record(
                            "execution",
                            {**position, "event": "open", "confidence": signal["confidence"], "regime": signal["regime"]},
                        )
                    else:
                        for row in self.book.fills[fills_before:]:
                            self.journal.record("execution", row, note="Paper fill refused.")
            elif persist:
                self.journal.record("rejected_setup", signal)
            if persist:
                self._last_recorded_bar = bar_key
            result = {
                "signal": signal,
                "risk": risk,
                "paper_position": position,
                "paper_closed": closed,
                "mode": self.book.mode,
                "orders_sent_to_broker": 0,
                "analysis_timestamp": payload["timestamp"],
                "repeat_of_journaled_bar": repeat,
                "tier1": tier1,
            }
            return to_jsonable(result)

    def mark_paper(self) -> list[dict]:
        """Mark each open paper position against every M5 bar it has not seen yet."""
        with self.lock:
            base = self.engine.frame("M5")
            if base is None or len(base) == 0 or not self.book.paper_positions:
                return []
            # Compare bar CLOSE times: entry and marked_until are recorded at a bar's close.
            closes = pd.DatetimeIndex(base.time).tz_localize("UTC") + pd.Timedelta(minutes=5)
            closed = []
            max_hold = pd.Timedelta(minutes=self.profile.max_hold_minutes)
            for position in list(self.book.paper_positions):
                since = pd.Timestamp(position.get("marked_until") or position["opened_at"])
                opened = pd.Timestamp(position["opened_at"])
                for i in np.flatnonzero(closes > since):
                    bar_close = closes[i]
                    row = self.book.mark_position(
                        position,
                        float(base.high[i]),
                        float(base.low[i]),
                        float(base.close[i]),
                        self.settings.max_slippage / 2,
                        self.settings.contract_ounces,
                        time_exit=bar_close - opened >= max_hold,
                    )
                    position["marked_until"] = bar_close.isoformat()
                    if row is not None:
                        row["closed_at"] = bar_close.isoformat()
                        closed.append(row)
                        break
            for row in closed:
                register_closed_trade(self.account, row["pnl"], self.settings)
                self.journal.record("execution", row)
            self.account.open_positions = [dict(row) for row in self.book.paper_positions]
            if closed:
                self.equity_history.append({"time": datetime.now(timezone.utc).isoformat(), "equity": self.account.equity})
            return to_jsonable(closed)

    def refresh_data(self) -> dict:
        """Reload the configured source (CSV or MT5). Synthetic data is static."""
        if self.engine.source == "synthetic":
            return {"refreshed": False, "new_bars": 0, "reason": "Synthetic data does not update."}
        base = self.engine.frame("M5")
        before = None if base is None or len(base) == 0 else base.time[-1]
        s = self.settings
        try:
            fresh = build_engine(self.root, s)
        except Exception as exc:  # a failed pull keeps the last good data
            self.journal.record("operator_note", {"refresh_error": str(exc)}, note="Market data refresh failed.")
            return {"refreshed": False, "new_bars": 0, "reason": str(exc)}
        new_base = fresh.frame("M5")
        new_bars = 0 if before is None else int(np.sum(new_base.time > before))
        with self.lock:
            self.engine = fresh
        return {"refreshed": True, "new_bars": new_bars, "last_bar": pd.Timestamp(new_base.time[-1]).isoformat()}

    def _event_flatten(self, tier1: dict) -> list[dict]:
        """Close simulated positions shortly before NFP/CPI: gold can gap through stops on the release."""
        base = self.engine.frame("M5")
        if base is None or len(base) == 0:
            return []
        closed = self.book.flatten_paper(
            float(base.close[-1]), self.settings.max_slippage, self.settings.contract_ounces, event="event_flatten"
        )
        names = [tier1[k]["next"]["name"] for k in ("nfp", "cpi") if tier1.get(k, {}).get("flatten_due") and tier1[k].get("next")]
        for row in closed:
            row["exit_reason"] = f"pre-release flatten: {', '.join(names)}"
            register_closed_trade(self.account, row["pnl"], self.settings)
            self.journal.record("execution", row, note="Closed before a tier-1 release.")
        self.account.open_positions = [dict(r) for r in self.book.paper_positions]
        if closed:
            self.equity_history.append({"time": datetime.now(timezone.utc).isoformat(), "equity": self.account.equity})
        return closed

    def cycle(self) -> dict:
        """One autonomous step: refresh calendar and data, then mark and evaluate if a new bar closed."""
        self.calendar.refresh()
        refresh = self.refresh_data()
        result = {"refresh": refresh, "evaluated": False}
        if refresh["new_bars"] or self._last_recorded_bar is None:
            evaluation = self.evaluate()
            result.update({"evaluated": True, "decision": evaluation["signal"]["decision"]})
        return result

    def tighten_stop(self, position_id: str, new_stop: float) -> dict:
        with self.lock:
            for position in self.book.paper_positions:
                if position["id"] == position_id:
                    result = propose_stop_update(position["direction"], position["stop_loss"], new_stop)
                    if result["accepted"]:
                        position["stop_loss"] = result["stop"]
                    self.journal.record("execution", {"event": "stop_update", "id": position_id, **result})
                    return result
            return {"accepted": False, "reason": "Unknown paper position."}

    # ------------------------------------------------------------------ modes
    def set_mode(self, mode: str) -> dict:
        with self.lock:
            result = self.book.set_mode(mode)
            self.journal.record("mode_change", result)
            return result

    def request_live(self, checklist: LiveChecklist) -> dict:
        with self.lock:
            # The operator payload cannot assert broker connectivity on its own:
            # the configured adapter must also report ready.
            result = self.book.request_live(checklist)
            self.journal.record("mode_change", result, note="Operator requested live mode.")
            return result

    def emergency_shutdown(self, reason: str) -> dict:
        with self.lock:
            self.account.emergency_shutdown = True
            base = self.engine.frame("M5")
            price = float(base.close[-1]) if base is not None and len(base) else None
            closed = []
            if price is not None:
                closed = self.book.flatten_paper(price, self.settings.max_slippage, self.settings.contract_ounces)
                for row in closed:
                    register_closed_trade(self.account, row["pnl"], self.settings)
                    self.journal.record("execution", row)
            self.account.open_positions = []
            self.book.live_enabled = False
            if self.book.mode == "live":
                self.book.mode = "analysis"
            result = {"emergency_shutdown": True, "flattened": len(closed), "reason": reason, "mode": self.book.mode}
            self.journal.record("emergency", result)
            return to_jsonable(result)

    def reset_emergency(self, operator: str, acknowledgement: str) -> dict:
        with self.lock:
            if acknowledgement.strip().lower() != "i have reviewed the shutdown cause":
                return {"reset": False, "reason": "The acknowledgement phrase does not match."}
            self.account.emergency_shutdown = False
            result = {"reset": True, "operator": operator}
            self.journal.record("emergency", result, note="Emergency shutdown cleared by operator.")
            return result

    def acknowledge_loss_pause(self, operator: str) -> dict:
        with self.lock:
            self.account.loss_pause = False
            self.account.consecutive_losses = 0
            result = {"loss_pause": False, "operator": operator, "note": "Position size is unchanged after the pause."}
            self.journal.record("operator_note", result, note="Consecutive-loss pause acknowledged.")
            return result

    # ------------------------------------------------------------------ risk
    def risk_view(self) -> dict:
        with self.lock:
            moment = datetime.now(timezone.utc)
            roll_account_clock(self.account, moment)
            open_risk = sum(float(p.get("risk_amount", 0.0)) for p in self.book.paper_positions)
            equity = self.account.equity
            return to_jsonable(
                {
                    "equity": round(equity, 2),
                    "day_start_equity": round(self.account.day_start_equity, 2),
                    "week_start_equity": round(self.account.week_start_equity, 2),
                    "daily_pnl": round(equity - self.account.day_start_equity, 2),
                    "weekly_pnl": round(equity - self.account.week_start_equity, 2),
                    "daily_drawdown_pct": round(max(0.0, (self.account.day_start_equity - equity) / self.account.day_start_equity * 100), 3)
                    if self.account.day_start_equity
                    else 0.0,
                    "weekly_drawdown_pct": round(max(0.0, (self.account.week_start_equity - equity) / self.account.week_start_equity * 100), 3)
                    if self.account.week_start_equity
                    else 0.0,
                    "open_positions": self.book.paper_positions,
                    "open_risk_amount": round(open_risk, 2),
                    "open_risk_pct": round(open_risk / equity * 100, 3) if equity else 0.0,
                    "consecutive_losses": self.account.consecutive_losses,
                    "loss_pause": self.account.loss_pause,
                    "emergency_shutdown": self.account.emergency_shutdown,
                    "limits": self.settings.risk_snapshot(),
                    "equity_history": self.equity_history[-200:],
                }
            )

    # -------------------------------------------------------------- research
    def backtest(self, timeframe: str | None = None, commission_per_lot: float = 3.5, slippage: float = 0.05) -> dict:
        timeframe = timeframe or self.profile.entry
        frame = self.engine.frame(timeframe)
        if frame is None:
            return {"error": f"{timeframe} frame is unavailable."}
        costs = CostModel(commission_per_lot=commission_per_lot, slippage=slippage)
        roles = {"primary": self.profile.primary, "secondary": self.profile.secondary, "confirm": self.profile.confirm}
        higher = {tf: self.engine.frame(tf) for tf in set(roles.values()) if tf != timeframe}
        higher = {tf: f for tf, f in higher.items() if f is not None}
        # Same calendar as live, including the built-in NFP/CPI schedule for every month in the history.
        events = self.calendar.events_between(pd.Timestamp(frame.time[0]).to_pydatetime(), pd.Timestamp(frame.time[-1]).to_pydatetime())
        kwargs = dict(higher=higher, events=events, roles=roles, max_hold_minutes=self.profile.max_hold_minutes)
        result = run_backtest(frame, self.settings, costs, **kwargs)
        validation = validate_out_of_sample(frame, self.settings, costs, **kwargs)
        payload = {
            "timeframe": timeframe,
            "data_source": self.engine.source,
            "bars": len(frame),
            "higher_timeframes": sorted(higher),
            "profile": self.profile.name,
            "calendar_events_applied": len(events),
            "costs": {"commission_per_lot": commission_per_lot, "slippage": slippage, "spread": "per-bar spread column"},
            **result,
            "trades": result["trades"][-200:],
            "out_of_sample": validation,
        }
        with self.lock:
            self.last_backtest = to_jsonable(payload)
            self.last_validation = validation
            version = f"rules-{timeframe}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
            self.registry.record(
                version,
                "candidate",
                {k: result["metrics"].get(k) for k in ("trades", "win_rate", "profit_factor", "net_return_pct", "max_drawdown_pct")},
                f"Backtest on {self.engine.source} {timeframe}. Walk-forward passed={validation['passed']}.",
            )
            self.journal.record("model_version", {"version": version, "walk_forward_passed": validation["passed"]})
            self.last_backtest["model_version"] = version
        return self.last_backtest

    def learning_view(self) -> dict:
        trades = (self.last_backtest or {}).get("trades", [])
        rows = [
            {
                "pnl": t["pnl"],
                "confidence": t.get("confidence"),
                "direction": t["direction"],
                "reward_risk": t.get("reward_risk"),
                "atr": t.get("atr"),
                "regime": t.get("regime"),
            }
            for t in trades
        ]
        calibration = calibration_table(
            [r["confidence"] for r in rows if r["confidence"] is not None],
            [1 if r["pnl"] > 0 else 0 for r in rows if r["confidence"] is not None],
        )
        drift = None
        if len(rows) >= 20:
            half = len(rows) // 2
            base = float(np.mean([r["pnl"] > 0 for r in rows[:half]]))
            recent = float(np.mean([r["pnl"] > 0 for r in rows[half:]]))
            drift = detect_drift(recent, base)
        by_regime: dict[str, dict] = {}
        for r in rows:
            bucket = by_regime.setdefault(r["regime"] or "unknown", {"trades": 0, "wins": 0, "net": 0.0})
            bucket["trades"] += 1
            bucket["wins"] += int(r["pnl"] > 0)
            bucket["net"] = round(bucket["net"] + r["pnl"], 2)
        return to_jsonable(
            {
                "samples": len(rows),
                "calibration": calibration,
                "drift": drift,
                "by_regime": by_regime,
                "classifier": study_outcomes(rows),
                "registry": self.registry.versions[-20:],
                "policy": "Model output is advisory. Risk limits are operator-controlled and cannot be written by this module.",
            }
        )

    def promote_model(self, version: str) -> dict:
        passed = bool(self.last_validation and self.last_validation.get("passed"))
        result = self.registry.promote(version, passed)
        self.journal.record("model_version", result, note="Promotion requested.")
        return result

    def recommend(self, proposals: dict) -> dict:
        result = filter_recommendations(proposals)
        self.journal.record("operator_note", result, note="Parameter recommendation filtered.")
        return result

    # --------------------------------------------------------------- content
    def candles(self, timeframe: str, limit: int) -> dict:
        frame = self.engine.frame(timeframe)
        if frame is None:
            return {"timeframe": timeframe, "candles": []}
        return {"timeframe": timeframe, "candles": frame.to_records(limit)}

    def add_news(self, item: NewsItem) -> dict:
        with self.lock:
            self.news.append(item)
            self.news.sort(key=lambda row: row.published_at)
        from app.sentiment import classify_news

        return classify_news(item)

    def performance(self) -> dict:
        closes = [r["payload"] for r in self.journal.rows if r["kind"] == "execution" and r["payload"].get("event") in CLOSE_EVENTS]
        daily: dict[str, float] = {}
        weekly: dict[str, float] = {}
        for row in closes:
            stamp = pd.Timestamp(row.get("opened_at") or datetime.now(timezone.utc))
            daily[stamp.strftime("%Y-%m-%d")] = round(daily.get(stamp.strftime("%Y-%m-%d"), 0.0) + float(row["pnl"]), 2)
            weekly[stamp.strftime("%G-W%V")] = round(weekly.get(stamp.strftime("%G-W%V"), 0.0) + float(row["pnl"]), 2)
        counts = {kind: sum(1 for r in self.journal.rows if r["kind"] == kind) for kind in ("signal", "rejected_setup", "risk_decision", "execution")}
        backtest_metrics = (self.last_backtest or {}).get("metrics")
        return to_jsonable(
            {
                "paper": self.journal.signal_accuracy(),
                "daily": daily,
                "weekly": weekly,
                "decision_counts": counts,
                "backtest": backtest_metrics,
                "note": "Paper and backtest results are research evidence, not a promise of future returns.",
            }
        )
