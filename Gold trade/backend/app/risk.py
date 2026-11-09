"""Independent risk gate.

This module does not look for trades. It accepts or rejects a proposal.
Position size is equity times the fixed risk percent, divided by stop
distance. Loss streaks pause new risk. They never increase size.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.sessions import session_at


@dataclass
class AccountState:
    equity: float
    day_start_equity: float
    week_start_equity: float
    open_positions: list[dict] = field(default_factory=list)
    consecutive_losses: int = 0
    loss_pause: bool = False
    emergency_shutdown: bool = False
    day_key: str = ""
    week_key: str = ""


@dataclass
class OrderProposal:
    direction: str
    entry: float
    stop_loss: float
    take_profit: float
    spread: float
    timestamp: datetime
    recovery_multiplier: float = 1.0
    requested_risk_pct: float | None = None


def roll_account_clock(account: AccountState, moment: datetime) -> None:
    day_key = moment.strftime("%Y-%m-%d")
    week_key = moment.strftime("%G-W%V")
    if account.day_key != day_key:
        account.day_start_equity = account.equity
        account.day_key = day_key
    if account.week_key != week_key:
        account.week_start_equity = account.equity
        account.week_key = week_key


def assess(proposal: OrderProposal, account: AccountState, settings) -> dict:
    roll_account_clock(account, proposal.timestamp)
    reasons: list[str] = []
    if account.emergency_shutdown:
        reasons.append("Emergency shutdown is active.")
    if proposal.recovery_multiplier != 1:
        reasons.append("Recovery multipliers are rejected. Size is not increased after a loss.")
    if account.loss_pause or account.consecutive_losses >= settings.max_consecutive_losses:
        account.loss_pause = True
        reasons.append("Consecutive-loss pause is active. New trades stay off until an operator acknowledges it.")
    session = session_at(proposal.timestamp)
    if session not in settings.session_allowlist:
        reasons.append(f"Session {session} is outside the allowed trading sessions.")
    if proposal.spread > settings.max_spread:
        reasons.append("Spread protection rejected the proposal.")
    risk_pct = settings.max_risk_per_trade_pct
    if proposal.requested_risk_pct is not None and proposal.requested_risk_pct - risk_pct > 1e-9:
        reasons.append("Requested risk is above the configured per-trade limit.")
    elif proposal.requested_risk_pct is not None:
        risk_pct = proposal.requested_risk_pct
    stop_distance = abs(proposal.entry - proposal.stop_loss)
    if stop_distance <= 0:
        reasons.append("Stop distance must be positive.")
    if proposal.direction == "BUY" and proposal.stop_loss >= proposal.entry:
        reasons.append("A long stop must sit below entry.")
    if proposal.direction == "SELL" and proposal.stop_loss <= proposal.entry:
        reasons.append("A short stop must sit above entry.")
    reward = abs(proposal.take_profit - proposal.entry)
    reward_risk = reward / stop_distance if stop_distance else 0.0
    if reward_risk < settings.min_reward_risk:
        reasons.append("Reward-to-risk is below the minimum.")
    day_loss_pct = _drawdown_pct(account.day_start_equity, account.equity)
    week_loss_pct = _drawdown_pct(account.week_start_equity, account.equity)
    if day_loss_pct >= settings.max_daily_loss_pct:
        reasons.append("Daily loss limit has been reached.")
    if week_loss_pct >= settings.max_weekly_drawdown_pct:
        reasons.append("Weekly drawdown limit has been reached.")
    if len(account.open_positions) >= settings.max_open_positions:
        reasons.append("Maximum open positions has been reached.")
    size = _position_size(account.equity, risk_pct, stop_distance, settings.contract_ounces, settings.min_lot, settings.lot_step)
    if size is None:
        reasons.append("Calculated size is below the minimum lot after applying the risk cap.")
        risk_amount = 0.0
    else:
        risk_amount = size * stop_distance * settings.contract_ounces
    open_risk = sum(float(position.get("risk_amount", 0.0)) for position in account.open_positions)
    projected = (open_risk + risk_amount) / account.equity * 100 if account.equity else 100
    if projected - settings.max_correlated_exposure_pct > 1e-9:
        reasons.append("Correlated XAU/USD exposure would exceed the book limit.")
    approved = not reasons and size is not None
    return {
        "approved": approved,
        "reasons": reasons or ["Risk checks passed."],
        "lots": size if approved else 0.0,
        "risk_amount": round(risk_amount, 2) if approved else 0.0,
        "risk_pct": risk_pct,
        "reward_risk": round(reward_risk, 2),
        "session": session,
        "daily_drawdown_pct": round(day_loss_pct, 3),
        "weekly_drawdown_pct": round(week_loss_pct, 3),
        "stop_policy": "Protective stops may be tightened. They cannot be widened.",
    }


def propose_stop_update(direction: str, current_stop: float, proposed_stop: float) -> dict:
    widened = (direction == "BUY" and proposed_stop < current_stop) or (
        direction == "SELL" and proposed_stop > current_stop
    )
    if widened:
        return {
            "accepted": False,
            "stop": current_stop,
            "reason": "A protective stop cannot be widened to avoid a loss.",
        }
    return {"accepted": True, "stop": proposed_stop, "reason": "Stop tightened."}


def register_closed_trade(account: AccountState, pnl: float, settings) -> None:
    if pnl < 0:
        account.consecutive_losses += 1
        if account.consecutive_losses >= settings.max_consecutive_losses:
            account.loss_pause = True
    else:
        account.consecutive_losses = 0
    account.equity += pnl


def _position_size(equity: float, risk_pct: float, stop_distance: float, ounces: float, min_lot: float, lot_step: float) -> float | None:
    if equity <= 0 or stop_distance <= 0 or ounces <= 0:
        return None
    risk_amount = equity * (risk_pct / 100.0)
    raw = risk_amount / (stop_distance * ounces)
    steps = int(raw / lot_step)
    size = round(steps * lot_step, 2)
    if size < min_lot:
        return None
    # Rounding down keeps the actual risk at or under the cap.
    return size


def _drawdown_pct(start_equity: float, equity: float) -> float:
    if start_equity <= 0:
        return 0.0
    return max(0.0, (start_equity - equity) / start_equity * 100.0)
