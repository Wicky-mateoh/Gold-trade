"""Modules 8 and 12: independent risk gate and execution safety."""

from datetime import datetime, timezone

from app.execution import BrokerAdapter, ExecutionBook, LiveChecklist
from app.learning import filter_recommendations
from app.risk import AccountState, OrderProposal, assess, propose_stop_update, register_closed_trade

T = datetime(2026, 8, 12, 14, tzinfo=timezone.utc)  # Wednesday overlap


def acct(eq=100_000):
    return AccountState(eq, eq, eq)


def prop(**over):
    base = dict(direction="BUY", entry=2650.0, stop_loss=2645.0, take_profit=2660.0, spread=0.2, timestamp=T)
    base.update(over)
    return OrderProposal(**base)


def test_position_size_respects_risk_cap(settings):
    r = assess(prop(), acct(), settings)
    assert r["approved"]
    # 0.5% of 100k = 500; 5.0 stop * 100oz = 500/lot -> 1.00 lot
    assert r["lots"] == 1.0 and r["risk_amount"] <= 500


def test_size_rounds_down(settings):
    r = assess(prop(stop_loss=2644.0, take_profit=2662.0), acct(), settings)
    assert r["risk_amount"] <= 500 and r["lots"] == 0.83


def test_rejections(settings):
    a = acct()
    assert not assess(prop(spread=1.0), a, settings)["approved"]
    assert not assess(prop(take_profit=2652.0), a, settings)["approved"]
    assert not assess(prop(stop_loss=2655.0), a, settings)["approved"]
    assert not assess(prop(timestamp=datetime(2026, 8, 12, 3, tzinfo=timezone.utc)), a, settings)["approved"]
    assert not assess(prop(requested_risk_pct=2.0), a, settings)["approved"]


def test_recovery_multiplier_rejected(settings):
    r = assess(prop(recovery_multiplier=2.0), acct(), settings)
    assert not r["approved"] and any("Recovery" in x for x in r["reasons"])


def test_daily_loss_limit(settings):
    a = acct()
    assess(prop(), a, settings)  # roll the clock
    a.equity = 97_900
    assert not assess(prop(), a, settings)["approved"]


def test_weekly_drawdown(settings):
    a = acct()
    assess(prop(), a, settings)
    a.equity = 94_000
    a.day_start_equity = 94_000
    r = assess(prop(), a, settings)
    assert any("Weekly" in x for x in r["reasons"])


def test_consecutive_losses_pause_and_never_upsize(settings):
    a = acct()
    sizes = []
    for _ in range(settings.max_consecutive_losses):
        sizes.append(assess(prop(), a, settings)["lots"])
        register_closed_trade(a, -100, settings)
    assert a.loss_pause
    assert not assess(prop(), a, settings)["approved"]
    assert sizes == sorted(sizes, reverse=True)  # size never grows after a loss


def test_max_open_positions(settings):
    a = acct()
    a.open_positions.append({"risk_amount": 0})
    assert not assess(prop(), a, settings)["approved"]


def test_stop_cannot_widen():
    assert not propose_stop_update("BUY", 2645, 2640)["accepted"]
    assert propose_stop_update("BUY", 2645, 2648)["accepted"]
    assert not propose_stop_update("SELL", 2655, 2660)["accepted"]


def test_learning_cannot_touch_risk_limits():
    r = filter_recommendations({"max_risk_per_trade_pct": 5, "ema_fast": 21})
    assert "max_risk_per_trade_pct" not in r["accepted"] and r["accepted"] == {"ema_fast": 21}


def test_live_is_off_by_default():
    book = ExecutionBook()
    assert book.mode == "analysis" and not book.live_enabled
    assert not book.set_mode("live")["accepted"]


def test_live_requires_every_item_and_ready_adapter():
    book = ExecutionBook()
    full = LiveChecklist(True, True, True, True)
    r = book.request_live(full)
    assert not r["accepted"] and "a ready broker adapter" in r["missing"]

    class Ready(BrokerAdapter):
        ready = True

    partial = LiveChecklist(True, False, True, True)
    assert not book.request_live(partial, Ready())["accepted"]
    assert book.request_live(full, Ready())["accepted"]


def test_paper_fill_and_close():
    book = ExecutionBook()
    book.set_mode("paper")
    signal = {"actionable": True, "price": 2650.0, "stop_loss": 2645.0, "take_profits": [2660.0], "direction": "BUY", "spread": 0.2}
    pos = book.open_paper(signal, {"approved": True, "lots": 1.0, "risk_amount": 500}, T)
    assert pos["entry"] == 2650.1
    closed = book.mark_paper(high=2661, low=2649, close=2660.5, slippage=0.05, ounces=100)
    assert closed and closed[0]["pnl"] > 0 and not book.paper_positions
