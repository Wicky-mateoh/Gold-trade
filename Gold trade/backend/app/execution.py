"""Execution modes.

Analysis records decisions and sends nothing. Paper trading simulates fills
with spread and slippage. The live adapter refuses to transmit an order
unless a concrete broker implementation is supplied and the checklist passes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class LiveChecklist:
    explicit_authorization: bool = False
    broker_connected: bool = False
    account_risk_limits_set: bool = False
    emergency_shutdown_armed: bool = False

    def missing(self) -> list[str]:
        gaps = []
        if not self.explicit_authorization:
            gaps.append("explicit authorization")
        if not self.broker_connected:
            gaps.append("validated broker connectivity")
        if not self.account_risk_limits_set:
            gaps.append("account-specific risk limits")
        if not self.emergency_shutdown_armed:
            gaps.append("an armed emergency shutdown")
        return gaps


class BrokerAdapter:
    name = "none"
    ready = False

    def submit(self, order: dict) -> dict:
        raise RuntimeError("No broker adapter is configured. The order was not sent.")


@dataclass
class ExecutionBook:
    mode: str = "analysis"
    checklist: LiveChecklist = field(default_factory=LiveChecklist)
    adapter: BrokerAdapter = field(default_factory=BrokerAdapter)
    paper_positions: list[dict] = field(default_factory=list)
    fills: list[dict] = field(default_factory=list)
    live_enabled: bool = False

    def set_mode(self, mode: str) -> dict:
        if mode not in {"analysis", "paper"}:
            return {"accepted": False, "mode": self.mode, "reason": "Use the live checklist for any dealing mode."}
        self.mode = mode
        self.live_enabled = False
        return {"accepted": True, "mode": self.mode, "reason": f"Mode is {mode}. No live orders are enabled."}

    def request_live(self, checklist: LiveChecklist, adapter: BrokerAdapter | None = None) -> dict:
        adapter = adapter or self.adapter
        gaps = checklist.missing()
        if not adapter.ready:
            gaps.append("a ready broker adapter")
        if gaps:
            self.live_enabled = False
            self.mode = "analysis" if self.mode == "live" else self.mode
            return {
                "accepted": False,
                "live_enabled": False,
                "orders_sent": 0,
                "missing": gaps,
            }
        self.checklist = checklist
        self.adapter = adapter
        self.live_enabled = True
        self.mode = "live"
        return {
            "accepted": True,
            "live_enabled": True,
            "orders_sent": 0,
            "missing": [],
            "message": "Live mode is armed for the supplied adapter. This process still requires that adapter's own submit path.",
        }

    def open_paper(self, signal: dict, risk: dict, moment: datetime) -> dict | None:
        if self.mode != "paper" or not signal.get("actionable") or not risk.get("approved"):
            return None
        if signal["price"] is None or signal["stop_loss"] is None:
            return None
        half_spread = 0.0 if signal.get("spread") is None else float(signal["spread"]) / 2
        entry = signal["price"] + half_spread if signal["direction"] == "BUY" else signal["price"] - half_spread
        position = {
            "id": f"paper-{len(self.fills) + len(self.paper_positions) + 1}",
            "direction": signal["direction"],
            "entry": round(float(entry), 3),
            "stop_loss": float(signal["stop_loss"]),
            "take_profit": float(signal["take_profits"][0]),
            "lots": risk["lots"],
            "risk_amount": risk["risk_amount"],
            "opened_at": moment.astimezone(timezone.utc).isoformat(),
            "spread": signal.get("spread"),
        }
        self.paper_positions.append(position)
        self.fills.append({**position, "event": "open"})
        return position

    def mark_paper(self, high: float, low: float, close: float, slippage: float, ounces: float) -> list[dict]:
        closed = []
        still_open = []
        for position in self.paper_positions:
            stop_hit = low <= position["stop_loss"] if position["direction"] == "BUY" else high >= position["stop_loss"]
            target_hit = high >= position["take_profit"] if position["direction"] == "BUY" else low <= position["take_profit"]
            if not stop_hit and not target_hit:
                position["mark"] = close
                still_open.append(position)
                continue
            exit_price = position["stop_loss"] if stop_hit else position["take_profit"]
            if position["direction"] == "BUY":
                exit_price -= slippage
                pnl = (exit_price - position["entry"]) * ounces * position["lots"]
            else:
                exit_price += slippage
                pnl = (position["entry"] - exit_price) * ounces * position["lots"]
            result = {**position, "event": "close", "exit": round(exit_price, 3), "pnl": round(pnl, 2)}
            self.fills.append(result)
            closed.append(result)
        self.paper_positions = still_open
        return closed

    def flatten_paper(self, price: float, slippage: float, ounces: float) -> list[dict]:
        closed = []
        for position in self.paper_positions:
            exit_price = price - slippage if position["direction"] == "BUY" else price + slippage
            if position["direction"] == "BUY":
                pnl = (exit_price - position["entry"]) * ounces * position["lots"]
            else:
                pnl = (position["entry"] - exit_price) * ounces * position["lots"]
            result = {**position, "event": "emergency_flatten", "exit": round(exit_price, 3), "pnl": round(pnl, 2)}
            self.fills.append(result)
            closed.append(result)
        self.paper_positions = []
        return closed
