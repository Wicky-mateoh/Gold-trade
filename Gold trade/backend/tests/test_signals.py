"""Module 7: signal gate. NO TRADE is the default; BUY/SELL carry full detail."""

from datetime import datetime, timezone

import pytest

from app.signals import MarketContext, decide


def ctx(**over):
    base = dict(
        instrument="XAUUSD", timestamp=datetime(2026, 8, 14, 14, tzinfo=timezone.utc), price=2650.0, spread=0.2, atr=3.0,
        biases={"D1": "bullish", "H4": "bullish", "H1": "bullish"}, conflict=False, structure_event="bos_bullish",
        regime="trend", premium_discount="discount", sweep_side="sell_side", displacement="bullish",
        indicator_agreement=0.7, adx=28.0, volatility="normal", macro_blackout=False, macro_notes=["note"],
        sentiment_label="neutral", sentiment_confidence=0.0, sentiment_usable=False, fib_levels={},
        support=[2645.0], resistance=[2670.0], session="london",
    )
    base.update(over)
    return MarketContext(**base)


def run(c):
    return decide(c, 62.0, 1.8, 0.5)


def test_strong_long_setup_has_full_detail():
    s = run(ctx())
    assert s["decision"] == "BUY" and s["actionable"]
    for key in ("instrument", "price", "entry_low", "entry_high", "stop_loss", "take_profits", "reward_risk",
                "evidence", "fundamental_context", "confidence", "invalidation", "timestamp"):
        assert s[key] not in (None, [], ""), key
    assert s["stop_loss"] < s["price"] < s["take_profits"][0]
    assert s["reward_risk"] >= 1.8
    assert s["confidence"] <= 90


def test_mirror_short_setup():
    s = run(ctx(biases={"D1": "bearish", "H4": "bearish", "H1": "bearish"}, structure_event="bos_bearish",
                premium_discount="premium", sweep_side="buy_side", displacement="bearish", indicator_agreement=-0.7,
                support=[2630.0], resistance=[2655.0]))
    assert s["decision"] == "SELL"
    assert s["take_profits"][0] < s["price"] < s["stop_loss"]


@pytest.mark.parametrize("override,phrase", [
    ({"price": None}, "price"),
    ({"atr": None}, "ATR"),
    ({"spread": None}, "Spread"),
    ({"macro_blackout": True}, "event window"),
    ({"spread": 0.9}, "wider"),
    ({"volatility": "extreme"}, "extreme"),
    ({"biases": {"D1": "ranging", "H4": "bullish", "H1": "bullish"}}, "Daily bias"),
    ({"data_problems": ["H4 frame is unavailable."]}, "unavailable"),
])
def test_no_trade_conditions(override, phrase):
    s = run(ctx(**override))
    assert s["decision"] == "NO_TRADE" and not s["actionable"]
    assert any(phrase in r for r in s["reasons"])


def test_conflict_reduces_confidence():
    clean = run(ctx())
    conflicted = run(ctx(conflict=True, biases={"D1": "bullish", "H4": "bearish", "H1": "bullish"}))
    assert (conflicted["confidence"] or 0) < clean["confidence"]


def test_opposing_sentiment_reduces_confidence():
    clean = run(ctx())
    opposed = run(ctx(sentiment_usable=True, sentiment_label="bearish", sentiment_confidence=0.8))
    assert opposed["confidence"] < clean["confidence"]


def test_smc_alone_is_not_a_signal():
    # Sweep + discount but indicators disagree and a bearish displacement prints.
    s = run(ctx(indicator_agreement=-0.4, displacement="bearish", structure_event=None))
    assert s["decision"] == "NO_TRADE"
