"""Backtest, walk-forward, and Monte Carlo checks.

Fills use the next bar after a signal. Stops are assumed to fill before
targets when the same bar touches both. Costs are spread, commission, and
slippage. A result on synthetic data is a software check, not an edge.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.indicators import adx, atr, ema, macd, rsi, volume_ratio
from app.learning import regime_label
from app.risk import AccountState, OrderProposal, assess, register_closed_trade
from app.sessions import session_at
from app.signals import MarketContext, decide, fibonacci_for_context
from app.structure import find_swings


@dataclass
class CostModel:
    commission_per_lot: float = 3.5
    slippage: float = 0.05


BAR_DURATION = {
    "M5": pd.Timedelta(minutes=5),
    "M15": pd.Timedelta(minutes=15),
    "H1": pd.Timedelta(hours=1),
    "H4": pd.Timedelta(hours=4),
    "D1": pd.Timedelta(days=1),
}


def run_backtest(
    frame,
    settings,
    costs: CostModel | None = None,
    warmup: int = 220,
    higher: dict | None = None,
    events: list | None = None,
    roles: dict | None = None,
    max_hold_minutes: int | None = None,
) -> dict:
    """Replay the signal and risk gates bar by bar.

    `higher` maps timeframe -> PriceFrame (e.g. {"D1": ..., "H4": ...}). When
    supplied, each higher-timeframe bias is read only from bars that had fully
    closed before the current entry bar closed, so there is no look-ahead.
    Without it, the entry frame's own bias stands in for every timeframe.
    """
    costs = costs or CostModel()
    if len(frame) <= warmup + 2:
        return _empty("History is shorter than the warmup window.")
    features = _features(frame)
    htf = _higher_features(frame, higher or {})
    blackout = _blackout_mask(frame, events or [], settings.macro_blackout_before_min, settings.macro_blackout_after_min)
    roles = roles or {"primary": "D1", "secondary": "H4", "confirm": "H1"}
    step = BAR_DURATION.get(frame.timeframe, pd.Timedelta(minutes=5))
    max_hold_bars = None if not max_hold_minutes else max(1, int(pd.Timedelta(minutes=max_hold_minutes) / step))
    account = AccountState(settings.account_equity, settings.account_equity, settings.account_equity)
    trades: list[dict] = []
    open_trade: dict | None = None
    equity_curve = [account.equity]
    for i in range(warmup, len(frame) - 1):
        bar_time = pd.Timestamp(frame.time[i]).to_pydatetime()
        if open_trade is not None:
            timed_out = max_hold_bars is not None and i - open_trade["opened_index"] + 1 >= max_hold_bars
            closed = _try_exit(open_trade, frame, i, costs, settings.contract_ounces, timed_out)
            if closed is not None:
                register_closed_trade(account, closed["pnl"], settings)
                trades.append(closed)
                open_trade = None
        if open_trade is None and not account.loss_pause and not account.emergency_shutdown:
            context = _context_at(frame, features, i, settings.instrument, htf, roles)
            context.macro_blackout = bool(blackout[i])
            signal = decide(context, settings.min_signal_confidence, settings.min_reward_risk, settings.max_spread)
            if signal["actionable"]:
                proposal = OrderProposal(
                    direction=signal["direction"],
                    entry=float(frame.open[i + 1]),
                    stop_loss=float(signal["stop_loss"]),
                    take_profit=float(signal["take_profits"][0]),
                    spread=float(frame.spread[i]),
                    timestamp=pd.Timestamp(frame.time[i + 1]).to_pydatetime(),
                )
                decision = assess(proposal, account, settings)
                if decision["approved"]:
                    open_trade = _open_from_next(signal, frame, i + 1, decision, costs)
                    open_trade["regime"] = regime_label(context.adx, context.volatility)
                    open_trade["confidence"] = signal["confidence"]
                    open_trade["atr"] = context.atr
                    open_trade["opened_at"] = pd.Timestamp(frame.time[i + 1]).isoformat()
        equity_curve.append(account.equity if open_trade is None else account.equity + _unrealized(open_trade, frame, i, settings.contract_ounces))
    metrics = compute_metrics(trades, equity_curve, frame)
    return {
        "metrics": metrics,
        "trades": trades,
        "equity_curve": [round(value, 2) for value in equity_curve[:: max(1, len(equity_curve) // 200)]],
        "walk_forward": walk_forward(trades),
        "monte_carlo": monte_carlo([trade["pnl"] for trade in trades]),
        "deployment_gate": deployment_gate(trades, equity_curve),
        "note": "Costs are included. Passing the gate is required before live use and is still not a guarantee of future results.",
    }


def compute_metrics(trades: list[dict], equity_curve: list[float], frame) -> dict:
    if not trades:
        return {
            "trades": 0,
            "win_rate": None,
            "profit_factor": None,
            "net_return_pct": 0.0,
            "max_drawdown_pct": _max_drawdown_pct(equity_curve),
            "average_reward_risk": None,
            "sharpe": None,
            "sortino": None,
            "expectancy": None,
            "consecutive_wins": 0,
            "consecutive_losses": 0,
            "by_session": {},
            "by_regime": {},
            "note": "No trades passed the quality and risk gates.",
        }
    pnls = np.array([trade["pnl"] for trade in trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    gross_profit = float(wins.sum()) if len(wins) else 0.0
    gross_loss = abs(float(losses.sum())) if len(losses) else 0.0
    start = equity_curve[0] if equity_curve else 1.0
    net = float(pnls.sum())
    returns = _equity_returns(equity_curve)
    return {
        "trades": len(trades),
        "win_rate": round(float(np.mean(pnls > 0)), 4),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "net_return_pct": round(net / start * 100, 3),
        "max_drawdown_pct": _max_drawdown_pct(equity_curve),
        "average_reward_risk": round(float(np.mean([trade["reward_risk"] for trade in trades])), 3),
        "sharpe": _ratio(returns, downside=False),
        "sortino": _ratio(returns, downside=True),
        "expectancy": round(float(np.mean(pnls)), 2),
        "consecutive_wins": _streak(pnls, positive=True),
        "consecutive_losses": _streak(pnls, positive=False),
        "by_session": _group(trades, "session"),
        "by_regime": _group(trades, "regime"),
        "note": "Ratios use the equity curve after costs. None means the sample cannot support the statistic.",
    }


def walk_forward(trades: list[dict], folds: int = 4) -> list[dict]:
    if len(trades) < folds:
        return []
    size = len(trades) // folds
    windows = []
    for fold in range(folds):
        chunk = trades[fold * size : (fold + 1) * size]
        pnls = np.array([trade["pnl"] for trade in chunk], dtype=float)
        windows.append(
            {
                "fold": fold + 1,
                "trades": len(chunk),
                "net": round(float(pnls.sum()), 2),
                "win_rate": round(float(np.mean(pnls > 0)), 3),
            }
        )
    return windows


def monte_carlo(pnls: list[float], paths: int = 400, seed: int = 7) -> dict:
    if len(pnls) < 5:
        return {"paths": 0, "note": "Monte Carlo needs at least five closed trades."}
    rng = np.random.default_rng(seed)
    values = np.array(pnls, dtype=float)
    terminals = []
    drawdowns = []
    for _ in range(paths):
        sample = rng.permutation(values)
        equity = np.cumsum(sample)
        peak = np.maximum.accumulate(equity)
        drawdowns.append(float(np.max(peak - equity)))
        terminals.append(float(equity[-1]))
    terminal_arr = np.array(terminals)
    drawdown_arr = np.array(drawdowns)
    return {
        "paths": paths,
        "terminal_p05": round(float(np.percentile(terminal_arr, 5)), 2),
        "terminal_p50": round(float(np.percentile(terminal_arr, 50)), 2),
        "terminal_p95": round(float(np.percentile(terminal_arr, 95)), 2),
        "max_drawdown_p95": round(float(np.percentile(drawdown_arr, 95)), 2),
        "note": "Paths reshuffle realized trade results. They do not create new market paths.",
    }


def deployment_gate(trades: list[dict], equity_curve: list[float], min_trades: int = 30) -> dict:
    reasons = []
    if len(trades) < min_trades:
        reasons.append(f"Out-of-sample sample has {len(trades)} trades; {min_trades} are required.")
    holdout = trades[len(trades) // 2 :]
    if len(holdout) < max(10, min_trades // 3):
        reasons.append("The untouched second half of the trade list is too small.")
    else:
        pnls = np.array([trade["pnl"] for trade in holdout], dtype=float)
        losses = pnls[pnls < 0]
        gross_loss = abs(float(losses.sum())) if len(losses) else 0.0
        gross_profit = float(pnls[pnls > 0].sum()) if np.any(pnls > 0) else 0.0
        if gross_loss == 0 or gross_profit / gross_loss < 1.05:
            reasons.append("Holdout profit factor is below 1.05.")
        if float(pnls.sum()) <= 0:
            reasons.append("Holdout net result is not positive.")
    drawdown = _max_drawdown_pct(equity_curve)
    if drawdown is not None and drawdown > 15:
        reasons.append("Maximum drawdown is above 15%.")
    return {
        "passed": not reasons,
        "reasons": reasons or ["Holdout sample cleared the minimum research gate."],
        "live_permission": False,
        "statement": "This gate does not authorize live orders and does not promise future returns.",
    }


def _features(frame) -> dict:
    close = frame.close
    macd_line, macd_signal, macd_hist = macd(close)
    adx_v, plus_di, minus_di = adx(frame.high, frame.low, close)
    highs, lows = find_swings(frame.high, frame.low, 3, 3)
    return {
        "ema20": ema(close, 20),
        "ema50": ema(close, 50),
        "ema200": ema(close, 200),
        "rsi": rsi(close),
        "macd_hist": macd_hist,
        "atr": atr(frame.high, frame.low, close),
        "adx": adx_v,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "highs": highs,
        "lows": lows,
    }


def _visible_swings(features: dict, index: int) -> tuple[list[int], list[int]]:
    # Swings are confirmed three bars later, so only swings with i + 3 <= index are visible.
    return (
        features["highs"][: bisect_right(features["highs"], index - 3)],
        features["lows"][: bisect_right(features["lows"], index - 3)],
    )


def _bias_from(frame, visible_high: list[int], visible_low: list[int]) -> str:
    if len(visible_high) < 2 or len(visible_low) < 2:
        return "unknown"
    hh = frame.high[visible_high[-1]] > frame.high[visible_high[-2]]
    hl = frame.low[visible_low[-1]] > frame.low[visible_low[-2]]
    lh = frame.high[visible_high[-1]] < frame.high[visible_high[-2]]
    ll = frame.low[visible_low[-1]] < frame.low[visible_low[-2]]
    if hh and hl:
        return "bullish"
    if lh and ll:
        return "bearish"
    return "ranging"


def _blackout_mask(frame, events: list, before_min: int, after_min: int) -> np.ndarray:
    """True where the bar closes inside a high-impact event window (same rule as live macro_context)."""
    mask = np.zeros(len(frame), dtype=bool)
    step = BAR_DURATION.get(frame.timeframe)
    if not events or step is None:
        return mask
    closes = (pd.DatetimeIndex(frame.time) + step).tz_localize("UTC")
    for event in events:
        if event.impact not in {"high", "holiday"}:
            continue
        # Per-event windows: NFP/CPI carry the wider tier-1 window.
        start, end = event.window(before_min, after_min)
        lo, hi = pd.Timestamp(start), pd.Timestamp(end)
        lo = lo.tz_localize("UTC") if lo.tzinfo is None else lo.tz_convert("UTC")
        hi = hi.tz_localize("UTC") if hi.tzinfo is None else hi.tz_convert("UTC")
        mask |= np.asarray((closes >= lo) & (closes <= hi))
    return mask


def _higher_features(entry, higher: dict) -> dict:
    out = {}
    entry_step = BAR_DURATION.get(entry.timeframe)
    if entry_step is None:
        return out
    entry_close = pd.DatetimeIndex(entry.time) + entry_step
    for timeframe, frame in higher.items():
        step = BAR_DURATION.get(timeframe)
        if frame is None or len(frame) == 0 or step is None:
            continue
        ends = (pd.DatetimeIndex(frame.time) + step).to_numpy()
        # completed[i] = number of higher bars closed at or before entry bar i closes.
        completed = np.searchsorted(ends, entry_close.to_numpy(), side="right")
        highs, lows = find_swings(frame.high, frame.low, 3, 3)
        out[timeframe] = {"frame": frame, "completed": completed, "highs": highs, "lows": lows}
    return out


def _higher_bias(htf: dict, timeframe: str, index: int) -> str | None:
    row = htf.get(timeframe)
    if row is None:
        return None
    last = int(row["completed"][index]) - 1
    if last < 0:
        return "unknown"
    vh, vl = _visible_swings(row, last)
    return _bias_from(row["frame"], vh, vl)


def _context_at(frame, features: dict, index: int, instrument: str, htf: dict | None = None, roles: dict | None = None) -> MarketContext:
    roles = roles or {"primary": "D1", "secondary": "H4", "confirm": "H1"}
    visible_high, visible_low = _visible_swings(features, index)
    bias = _bias_from(frame, visible_high, visible_low)
    role_frames = (roles["primary"], roles["secondary"], roles["confirm"])
    # Frames without their own data fall back to the entry frame's bias.
    biases = {tf: bias for tf in ("D1", "H4", "H1", "M15", *role_frames)}
    for timeframe in role_frames:
        if timeframe == frame.timeframe:
            continue
        higher_bias = _higher_bias(htf or {}, timeframe, index)
        if higher_bias is not None:
            biases[timeframe] = higher_bias
    directional = [biases[tf] for tf in role_frames if biases[tf] in {"bullish", "bearish"}]
    conflict = "bullish" in directional and "bearish" in directional
    event = None
    if bias == "bullish" and visible_low and frame.close[index] < frame.low[visible_low[-1]]:
        event = "choch_bearish"
    elif bias == "bearish" and visible_high and frame.close[index] > frame.high[visible_high[-1]]:
        event = "choch_bullish"
    elif bias == "bullish" and visible_high and frame.close[index] > frame.high[visible_high[-1]]:
        event = "bos_bullish"
    elif bias == "bearish" and visible_low and frame.close[index] < frame.low[visible_low[-1]]:
        event = "bos_bearish"
    high = None if not visible_high else float(frame.high[visible_high[-1]])
    low = None if not visible_low else float(frame.low[visible_low[-1]])
    zone = "unknown"
    if high is not None and low is not None and high > low:
        equilibrium = (high + low) / 2
        if abs(frame.close[index] - equilibrium) <= (high - low) * 0.05:
            zone = "equilibrium"
        elif frame.close[index] > equilibrium:
            zone = "premium"
        else:
            zone = "discount"
    agreement = _agreement_at(frame, features, index)
    atr_v = features["atr"][index]
    span = float(frame.high[index] - frame.low[index])
    body = abs(float(frame.close[index] - frame.open[index]))
    displacement = None
    if np.isfinite(atr_v) and span >= 1.5 * float(atr_v) and span > 0 and body / span >= 0.65:
        displacement = "bullish" if frame.close[index] > frame.open[index] else "bearish"
    finite_atr = features["atr"][: index + 1]
    finite_atr = finite_atr[np.isfinite(finite_atr)]
    if len(finite_atr) < 20 or not np.isfinite(atr_v):
        volatility = "unknown"
    else:
        rank = float(np.mean(finite_atr <= atr_v))
        volatility = "extreme" if rank >= 0.97 else "high" if rank >= 0.8 else "low" if rank <= 0.2 else "normal"
    spread = frame.spread[index]
    return MarketContext(
        instrument=instrument,
        timestamp=pd.Timestamp(frame.time[index]).to_pydatetime(),
        price=float(frame.close[index]),
        spread=None if np.isnan(spread) else float(spread),
        atr=None if not np.isfinite(atr_v) else float(atr_v),
        biases=biases,
        conflict=conflict,
        structure_event=event,
        regime=bias if bias in {"bullish", "bearish"} else "consolidation",
        premium_discount=zone,
        sweep_side=None,
        displacement=displacement,
        indicator_agreement=agreement,
        adx=None if not np.isfinite(features["adx"][index]) else float(features["adx"][index]),
        volatility=volatility,
        macro_blackout=False,
        macro_notes=[],
        sentiment_label="neutral",
        sentiment_confidence=0.0,
        sentiment_usable=False,
        fib_levels=fibonacci_for_context(high, low),
        support=[] if low is None else [low],
        resistance=[] if high is None else [high],
        session=session_at(frame.time[index]),
        indicator_snapshot={},
        primary_tf=roles["primary"],
        secondary_tf=roles["secondary"],
        confirm_tf=roles["confirm"],
    )


def _agreement_at(frame, features, index: int) -> float:
    close = float(frame.close[index])
    votes = []
    for key, weight in (("ema20", 1.0), ("ema50", 1.0), ("ema200", 1.2)):
        level = features[key][index]
        if np.isfinite(level):
            votes.append(weight if close > level else -weight)
    rsi_v = features["rsi"][index]
    if np.isfinite(rsi_v) and 25 < rsi_v < 75:
        votes.append(0.8 if rsi_v >= 55 else -0.8 if rsi_v <= 45 else 0.0)
    hist = features["macd_hist"][index]
    if np.isfinite(hist):
        votes.append(1.0 if hist > 0 else -1.0 if hist < 0 else 0.0)
    if np.isfinite(features["adx"][index]) and features["adx"][index] >= 18:
        votes.append(1.0 if features["plus_di"][index] > features["minus_di"][index] else -1.0)
    ratio = volume_ratio(frame.volume, index)
    if ratio is not None and ratio > 1.2 and votes:
        votes.append(float(np.sign(np.mean(votes))))
    if not votes:
        return 0.0
    return float(np.clip(np.mean(votes) / 1.2, -1.0, 1.0))


def _open_from_next(signal: dict, frame, index: int, decision: dict, costs: CostModel) -> dict:
    spread = float(frame.spread[index]) if np.isfinite(frame.spread[index]) else 0.0
    half = spread / 2 + costs.slippage
    raw_open = float(frame.open[index])
    entry = raw_open + half if signal["direction"] == "BUY" else raw_open - half
    return {
        "direction": signal["direction"],
        "entry": entry,
        "stop": float(signal["stop_loss"]),
        "target": float(signal["take_profits"][0]),
        "lots": decision["lots"],
        "risk_amount": decision["risk_amount"],
        "reward_risk": signal["reward_risk"],
        "opened_index": index,
        "session": decision["session"],
        "regime": "unknown",
        "commission": costs.commission_per_lot * decision["lots"],
    }


def _try_exit(trade: dict, frame, index: int, costs: CostModel, ounces: float, timed_out: bool = False) -> dict | None:
    stop_hit = frame.low[index] <= trade["stop"] if trade["direction"] == "BUY" else frame.high[index] >= trade["stop"]
    target_hit = frame.high[index] >= trade["target"] if trade["direction"] == "BUY" else frame.low[index] <= trade["target"]
    if not stop_hit and not target_hit and not timed_out:
        return None
    # Same-bar ambiguity is resolved in favor of the stop. A max-hold exit fills at the bar close.
    exit_price = trade["stop"] if stop_hit else trade["target"] if target_hit else float(frame.close[index])
    slip = costs.slippage
    if trade["direction"] == "BUY":
        exit_price -= slip
        pnl = (exit_price - trade["entry"]) * ounces * trade["lots"] - trade["commission"]
    else:
        exit_price += slip
        pnl = (trade["entry"] - exit_price) * ounces * trade["lots"] - trade["commission"]
    return {
        **trade,
        "exit": exit_price,
        "pnl": round(float(pnl), 2),
        "closed_index": index,
        "outcome": "loss" if pnl < 0 else "win",
        "exit_reason": "stop" if stop_hit else "target" if target_hit else "max hold time",
        "closed_at": pd.Timestamp(frame.time[index]).isoformat(),
    }


def _unrealized(trade: dict, frame, index: int, ounces: float) -> float:
    mark = float(frame.close[index])
    if trade["direction"] == "BUY":
        return (mark - trade["entry"]) * ounces * trade["lots"]
    return (trade["entry"] - mark) * ounces * trade["lots"]


def _max_drawdown_pct(equity_curve: list[float]) -> float | None:
    if not equity_curve:
        return None
    peak = equity_curve[0]
    worst = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak:
            worst = max(worst, (peak - value) / peak * 100)
    return round(worst, 3)


def _equity_returns(equity_curve: list[float]) -> np.ndarray:
    equity = np.array(equity_curve, dtype=float)
    if len(equity) < 3:
        return np.array([])
    previous = equity[:-1]
    return (equity[1:] - previous) / np.where(previous == 0, np.nan, previous)


def _ratio(returns: np.ndarray, downside: bool) -> float | None:
    clean = returns[np.isfinite(returns)]
    if len(clean) < 30:
        return None
    excess = clean
    deviation = clean[clean < 0] if downside else clean
    if len(deviation) < 5:
        return None
    std = float(np.std(deviation))
    if std == 0:
        return None
    return round(float(np.mean(excess) / std * np.sqrt(252)), 3)


def _streak(pnls: np.ndarray, positive: bool) -> int:
    best = 0
    current = 0
    for value in pnls:
        matched = value > 0 if positive else value < 0
        current = current + 1 if matched else 0
        best = max(best, current)
    return int(best)


def _group(trades: list[dict], key: str) -> dict:
    buckets: dict[str, list[float]] = {}
    for trade in trades:
        buckets.setdefault(str(trade.get(key, "unknown")), []).append(trade["pnl"])
    return {
        name: {"trades": len(values), "net": round(float(np.sum(values)), 2)}
        for name, values in buckets.items()
    }


def _empty(note: str) -> dict:
    return {
        "metrics": compute_metrics([], [0.0], None),
        "trades": [],
        "equity_curve": [],
        "walk_forward": [],
        "monte_carlo": {"paths": 0, "note": note},
        "deployment_gate": {"passed": False, "reasons": [note], "live_permission": False, "statement": note},
        "note": note,
    }


def validate_out_of_sample(frame, settings, costs: CostModel | None = None, folds: int = 4, warmup: int = 220, **kwargs) -> dict:
    """Walk-forward over sequential, non-overlapping time segments.

    The rule set has no fitted parameters, so the first segment is the
    development window and every later segment is out of sample. Each segment
    starts from fresh equity and only sees bars up to its own end, so later
    data cannot leak into an earlier result.
    """
    n = len(frame)
    segment = n // (folds + 1)
    if segment <= warmup + 50:
        return {"segments": [], "passed": False, "reasons": ["History is too short for walk-forward segments."]}
    rows = []
    oos_trades: list[dict] = []
    for fold in range(folds + 1):
        start = max(0, fold * segment - warmup)
        end = n if fold == folds else (fold + 1) * segment
        piece = _segment(frame, start, end)
        result = run_backtest(piece, settings, costs, warmup=warmup, **kwargs)
        metrics = result["metrics"]
        role = "development" if fold == 0 else "out_of_sample"
        if role == "out_of_sample":
            oos_trades.extend(result["trades"])
        rows.append(
            {
                "segment": fold + 1,
                "role": role,
                "start": pd.Timestamp(piece.time[0]).isoformat(),
                "end": pd.Timestamp(piece.time[-1]).isoformat(),
                "trades": metrics["trades"],
                "net_return_pct": metrics["net_return_pct"],
                "profit_factor": metrics["profit_factor"],
                "win_rate": metrics["win_rate"],
                "max_drawdown_pct": metrics["max_drawdown_pct"],
            }
        )
    oos = [row for row in rows if row["role"] == "out_of_sample"]
    reasons = []
    positive = sum(1 for row in oos if row["net_return_pct"] > 0)
    if len(oos_trades) < 30:
        reasons.append(f"Out-of-sample segments produced {len(oos_trades)} trades; 30 are required.")
    if positive < max(1, int(np.ceil(len(oos) * 0.75))):
        reasons.append(f"Only {positive} of {len(oos)} out-of-sample segments were net positive.")
    pnls = np.array([trade["pnl"] for trade in oos_trades], dtype=float)
    if len(pnls):
        gross_loss = abs(float(pnls[pnls < 0].sum()))
        gross_profit = float(pnls[pnls > 0].sum())
        if gross_loss == 0 or gross_profit / gross_loss < 1.1:
            reasons.append("Pooled out-of-sample profit factor is below 1.1.")
    return {
        "segments": rows,
        "out_of_sample_trades": len(oos_trades),
        "passed": not reasons,
        "reasons": reasons or ["Every robustness check on the out-of-sample segments passed."],
        "live_permission": False,
        "statement": "Passing walk-forward is a precondition for a live review, not a forecast of returns.",
    }


def _segment(frame, start: int, end: int):
    from app.frames import PriceFrame

    return PriceFrame(
        frame.timeframe,
        frame.time[start:end],
        frame.open[start:end],
        frame.high[start:end],
        frame.low[start:end],
        frame.close[start:end],
        frame.volume[start:end],
        frame.spread[start:end],
    )
