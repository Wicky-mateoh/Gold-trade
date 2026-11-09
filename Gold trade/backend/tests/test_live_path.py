"""End-to-end paper path through TradingService: the pieces must connect correctly.

These tests drive the real service with a controlled signal so each wiring
rule (position limits, marking, flatten, max hold, journaling) is checked on
the same code path the server runs.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.frames import PriceFrame, resample_frame
from app.market import MarketDataEngine
from app.risk import check_fill
from app.service import TradingService
from app.signals import MarketContext, decide
from tests.conftest import ROOT, make_m5

UTC = timezone.utc


def _engine(frame):
    engine = MarketDataEngine()
    engine.load_base(frame, "test", "test")
    return engine


def _flat(times, price=2650.0, spread=0.2):
    n = len(times)
    p = np.full(n, price)
    return PriceFrame("M5", times, p, p + 0.3, p - 0.3, p, np.full(n, 100.0), np.full(n, spread))


def _paper_until(settings, tmp_path, end: str):
    """Paper service whose data ends at `end` (the close of the last bar), as a live feed would."""
    times = pd.date_range("2026-11-02", end, freq="5min", inclusive="left")
    svc = TradingService(settings.model_copy(update={"app_mode": "paper"}), ROOT, engine=_engine(_flat(times)), state_dir=tmp_path)
    svc.book.set_mode("paper")
    return svc


@pytest.fixture
def paper(settings, tmp_path):
    """Data ends Friday 14:00 UTC (London/New York overlap); NFP that day is 13:30, so use Thursday."""
    return _paper_until(settings, tmp_path, "2026-11-05T14:00")


def _force_buy(monkeypatch, stop=2645.0, target=2660.0):
    def fake_decide(context, *args):
        return {
            "instrument": "XAUUSD", "direction": "BUY", "decision": "BUY", "price": context.price,
            "entry_low": context.price - 0.5, "entry_high": context.price + 0.2, "stop_loss": stop,
            "take_profits": [target, target + 5], "reward_risk": 2.0, "evidence": ["forced"], "fundamental_context": [],
            "confidence": 70.0, "invalidation": [], "timestamp": context.timestamp.isoformat(), "component_scores": {},
            "reasons": [], "candidate_direction": "BUY", "actionable": True,
        }

    monkeypatch.setattr("app.service.decide", fake_decide)


def _set_last_bar(svc, high, low, close, count=1):
    """Append `count` bars to the M5 feed as a broker refresh would."""
    base = svc.engine.frame("M5")
    step = np.timedelta64(5, "m")
    new_t = base.time[-1] + step * np.arange(1, count + 1)
    k = count
    f = PriceFrame("M5", np.r_[base.time, new_t], np.r_[base.open, [close] * k], np.r_[base.high, [high] * k],
                   np.r_[base.low, [low] * k], np.r_[base.close, [close] * k], np.r_[base.volume, [100] * k], np.r_[base.spread, [0.2] * k])
    svc.engine.load_base(f, "test", "test")


def test_opens_once_and_respects_max_open_positions(paper, monkeypatch):
    _force_buy(monkeypatch)
    paper.events = []
    r1 = paper.evaluate(now=None)
    assert r1["paper_position"] is not None
    _set_last_bar(paper, 2650.3, 2649.7, 2650.0)
    r2 = paper.evaluate(now=None)
    assert r2["paper_position"] is None
    assert "Maximum open positions has been reached." in r2["risk"]["reasons"]
    assert len(paper.book.paper_positions) == 1


def test_same_bar_is_journaled_once(paper, monkeypatch):
    _force_buy(monkeypatch)
    now = None
    paper.evaluate(now=now)
    rows = len(paper.journal.rows)
    again = paper.evaluate(now=now)
    assert again["repeat_of_journaled_bar"] is True
    assert len(paper.journal.rows) == rows


def test_entry_bar_is_not_used_to_mark_the_position(paper, monkeypatch):
    # The entry bar traded down to 2648.5 BEFORE the entry at its close; that must not stop the trade out.
    base = paper.engine.frame("M5")
    base.low[-1] = 2648.5
    paper.engine.load_base(base, "test", "test")
    _force_buy(monkeypatch, stop=2649.0)
    assert paper.evaluate(now=None)["paper_position"] is not None
    assert paper.mark_paper() == []
    _set_last_bar(paper, 2650.1, 2648.0, 2648.2)
    _set_last_bar(paper, 2650.1, 2649.0, 2649.2)
    closed = paper.mark_paper()
    assert closed and closed[0]["exit_reason"] == "stop"


def test_marks_every_new_bar_not_just_the_last(paper, monkeypatch):
    _force_buy(monkeypatch, stop=2645.0, target=2660.0)
    paper.evaluate(now=None)
    _set_last_bar(paper, 2661.0, 2650.0, 2659.0)  # target hit here
    _set_last_bar(paper, 2652.0, 2640.0, 2641.0)  # stop would hit here
    closed = paper.mark_paper()
    assert len(closed) == 1 and closed[0]["exit_reason"] == "target"


def test_equity_and_journal_reconcile_after_close(paper, monkeypatch):
    _force_buy(monkeypatch)
    paper.evaluate(now=None)
    _set_last_bar(paper, 2661.0, 2650.0, 2659.0)
    closed = paper.mark_paper()
    assert paper.account.equity == pytest.approx(100_000 + closed[0]["pnl"])
    assert paper.performance()["paper"]["closed"] == 1
    assert paper.account.open_positions == []


def test_flatten_before_nfp(settings, tmp_path, monkeypatch):
    svc = _paper_until(settings, tmp_path, "2026-11-06T11:00")  # 2.5h before NFP (13:30 UTC)
    _force_buy(monkeypatch)
    assert svc.evaluate()["paper_position"] is not None
    _set_last_bar(svc, 2650.3, 2649.7, 2650.0, count=28)  # feed advances to 13:20
    r = svc.evaluate()
    assert r["tier1"]["nfp"]["flatten_due"] and r["tier1"]["blackout"]
    # Even a forced BUY is refused by the independent risk gate inside the window.
    assert r["paper_position"] is None and any("NFP or CPI" in x for x in r["risk"]["reasons"])
    assert not svc.book.paper_positions
    paper = svc
    flat = [x for x in paper.journal.rows if x["payload"].get("event") == "event_flatten"]
    assert flat and "Non-Farm" in flat[0]["payload"]["exit_reason"]


def test_no_new_trade_inside_nfp_window(settings, tmp_path):
    """Real signal gate: a clean BUY context is refused during the tier-1 window."""
    svc = TradingService(settings, ROOT, engine=_engine(make_m5(days=5)), state_dir=tmp_path)
    release = datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
    events = svc.calendar.events_between(release - timedelta(days=1), release + timedelta(days=1))
    from app.macro import macro_context

    macro = macro_context(release - timedelta(minutes=30), events, 30, 15)
    ctx = MarketContext(
        instrument="XAUUSD", timestamp=release - timedelta(minutes=30), price=2650.0, spread=0.2, atr=3.0,
        biases={"D1": "bullish", "H4": "bullish", "H1": "bullish"}, conflict=False, structure_event="bos_bullish",
        regime="trend", premium_discount="discount", sweep_side="sell_side", displacement="bullish",
        indicator_agreement=0.7, adx=28.0, volatility="normal", macro_blackout=macro["blackout"], macro_notes=macro["notes"],
        sentiment_label="neutral", sentiment_confidence=0.0, sentiment_usable=False, fib_levels={},
        support=[2645.0], resistance=[2670.0], session="london_new_york_overlap",
    )
    signal = decide(ctx, 62, 1.8, 0.5)
    assert signal["decision"] == "NO_TRADE" and any("event window" in r for r in signal["reasons"])


def test_max_hold_time_exit(paper, monkeypatch):
    _force_buy(monkeypatch)
    paper.evaluate(now=None)
    _set_last_bar(paper, 2650.3, 2649.7, 2650.0, count=12 * 23)
    assert paper.mark_paper() == []  # 23h: still inside the 24h intraday hold
    _set_last_bar(paper, 2650.3, 2649.7, 2650.0, count=12)
    closed = paper.mark_paper()
    assert closed and closed[0]["exit_reason"] == "max hold time"


def test_slippage_protection():
    assert check_fill("BUY", 2649.5, 2650.2, 2650.4, 0.3)["accepted"]
    refused = check_fill("BUY", 2649.5, 2650.2, 2650.8, 0.3)
    assert not refused["accepted"] and refused["adverse_slippage"] == 0.6
    assert not check_fill("SELL", 2649.8, 2650.5, 2649.3, 0.3)["accepted"]


def test_levels_tp2_beyond_tp1_and_rr_survives_rounding():
    base = dict(
        instrument="XAUUSD", timestamp=datetime(2026, 11, 4, 14, tzinfo=UTC), price=2650.123456, spread=0.2, atr=2.7777,
        biases={"D1": "bullish", "H4": "bullish", "H1": "bullish"}, conflict=False, structure_event="bos_bullish",
        regime="trend", premium_discount="discount", sweep_side="sell_side", displacement="bullish",
        indicator_agreement=0.7, adx=28.0, volatility="normal", macro_blackout=False, macro_notes=[],
        sentiment_label="neutral", sentiment_confidence=0.0, sentiment_usable=False, fib_levels={},
        support=[2646.0], resistance=[2700.0], session="london",
    )
    s = decide(MarketContext(**base), 62, 1.8, 0.5)
    assert s["decision"] == "BUY"
    tp1, tp2 = s["take_profits"]
    assert tp2 > tp1
    assert (tp1 - s["price"]) / (s["price"] - s["stop_loss"]) >= 1.8


def test_scalping_profile_uses_h4_h1_m15(settings, tmp_path, uptrend_engine):
    svc = TradingService(settings.model_copy(update={"strategy_profile": "scalping"}), ROOT, engine=uptrend_engine, state_dir=tmp_path)
    payload = svc.analysis()
    assert payload["profile"]["primary"] == "H4" and payload["direction"]["conflict_frames"] == ["H4", "H1", "M15"]
    assert payload["timeframes"]["MN1"]["role"] == "context"
    assert payload["timeframes"]["M15"]["role"] == "confirm + entry"


def test_intraday_roles(service):
    tfs = service.analysis()["timeframes"]
    assert tfs["D1"]["role"] == "direction" and tfs["H4"]["role"] == "direction"
    assert tfs["H1"]["role"] == "confirm" and tfs["M15"]["role"] == "entry"
    assert tfs["W1"]["role"] == "context"


def test_unfinished_higher_bar_is_dropped():
    f = make_m5(days=2)
    cut = len(f) - 30  # leaves a partial H4 and D1 at the end
    part = PriceFrame("M5", f.time[:cut], f.open[:cut], f.high[:cut], f.low[:cut], f.close[:cut], f.volume[:cut], f.spread[:cut])
    last_close = pd.Timestamp(part.time[-1]) + pd.Timedelta(minutes=5)
    for tf, minutes in (("H1", 60), ("H4", 240), ("D1", 1440)):
        out = resample_frame(part, tf)
        assert pd.Timestamp(out.time[-1]) + pd.Timedelta(minutes=minutes) <= last_close, tf


def test_daily_break_does_not_delete_h4_bars():
    f = make_m5(days=5)
    keep = pd.DatetimeIndex(f.time).hour != 21  # one-hour daily break
    gap = PriceFrame("M5", f.time[keep], f.open[keep], f.high[keep], f.low[keep], f.close[keep], f.volume[keep], f.spread[keep])
    # Every H4 bucket survives the missing hour; a strict 48-of-48 rule would drop 5 of them.
    assert len(resample_frame(gap, "H4")) == 6 * 5


def test_backtest_skips_nfp_cpi_windows(settings, uptrend_engine):
    from app.backtest import run_backtest
    from app.econ_calendar import EventCalendar

    frame = uptrend_engine.frame("H1")
    higher = {"D1": uptrend_engine.frame("D1"), "H4": uptrend_engine.frame("H4")}
    cal = EventCalendar(settings, [])
    events = cal.events_between(pd.Timestamp(frame.time[0]).to_pydatetime(), pd.Timestamp(frame.time[-1]).to_pydatetime())
    result = run_backtest(frame, settings, higher=higher, events=events)
    for trade in result["trades"]:
        decided = pd.Timestamp(frame.time[trade["opened_index"] - 1]).tz_localize("UTC") + pd.Timedelta(hours=1)
        for e in events:
            lo, hi = e.window(30, 15)
            assert not (lo <= decided <= hi), (trade, e.name)


def test_refresh_detects_new_csv_bars(settings, tmp_path):
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    f = make_m5(days=40)
    csv = root / "data" / "XAUUSD_M5.csv"

    def write(n):
        pd.DataFrame({"time": pd.DatetimeIndex(f.time[:n]).strftime("%Y-%m-%dT%H:%M:%SZ"), "open": f.open[:n], "high": f.high[:n],
                      "low": f.low[:n], "close": f.close[:n], "volume": f.volume[:n], "spread": f.spread[:n]}).to_csv(csv, index=False)

    write(len(f) - 24)
    svc = TradingService(settings, root, state_dir=tmp_path / "var")
    assert svc.engine.source == "csv"
    write(len(f))
    result = svc.cycle()
    assert result["refresh"]["new_bars"] == 24 and result["evaluated"]
