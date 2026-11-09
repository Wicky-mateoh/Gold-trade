"""Modules 9 and 10: backtest, validation, learning safeguards."""

import numpy as np

from app.backtest import compute_metrics, deployment_gate, monte_carlo, run_backtest, validate_out_of_sample
from app.learning import ModelRegistry, calibration_table, detect_drift, regime_label, study_outcomes


def _higher(engine):
    return {"D1": engine.frame("D1"), "H4": engine.frame("H4")}


def test_backtest_trades_follow_trend(uptrend_engine, settings):
    r = run_backtest(uptrend_engine.frame("H1"), settings, higher=_higher(uptrend_engine))
    assert r["metrics"]["trades"] > 0
    assert {t["direction"] for t in r["trades"]} == {"BUY"}
    for t in r["trades"]:
        assert t["opened_index"] > 0 and t["closed_index"] >= t["opened_index"]
        assert t["regime"] != "BUY"


def test_backtest_no_lookahead(uptrend_engine, settings):
    """Trades opened before a cut must be identical whether or not later bars exist."""
    frame = uptrend_engine.frame("H1")
    full = run_backtest(frame, settings, higher=_higher(uptrend_engine))
    cut = int(len(frame) * 0.7)
    from app.backtest import _segment

    part = run_backtest(_segment(frame, 0, cut), settings, higher=_higher(uptrend_engine))
    early_full = [(t["opened_index"], t["direction"]) for t in full["trades"] if t["closed_index"] < cut - 1]
    early_part = [(t["opened_index"], t["direction"]) for t in part["trades"] if t["closed_index"] < cut - 1]
    assert early_full == early_part


def test_random_walk_gives_no_false_confidence(synthetic_engine, settings):
    r = run_backtest(synthetic_engine.frame("H1"), settings, higher=_higher(synthetic_engine))
    assert r["deployment_gate"]["passed"] is False
    assert r["deployment_gate"]["live_permission"] is False


def test_costs_reduce_pnl(uptrend_engine, settings):
    from app.backtest import CostModel

    cheap = run_backtest(uptrend_engine.frame("H1"), settings, CostModel(0, 0), higher=_higher(uptrend_engine))
    dear = run_backtest(uptrend_engine.frame("H1"), settings, CostModel(10, 0.3), higher=_higher(uptrend_engine))
    assert sum(t["pnl"] for t in dear["trades"]) < sum(t["pnl"] for t in cheap["trades"])


def test_metrics_math():
    trades = [{"pnl": p, "reward_risk": 2.0, "session": "london", "regime": "trend"} for p in (100, -50, 100, -50, -50)]
    m = compute_metrics(trades, [1000, 1100, 1050, 1150, 1100, 1050], None)
    assert m["win_rate"] == 0.4 and m["profit_factor"] == round(200 / 150, 3)
    assert m["consecutive_losses"] == 2 and m["expectancy"] == 10.0


def test_monte_carlo_and_gate():
    mc = monte_carlo([10, -5, 8, -3, 12, -7])
    assert mc["terminal_p05"] <= mc["terminal_p50"] <= mc["terminal_p95"]
    assert deployment_gate([{"pnl": 1}] * 5, [100, 101])["passed"] is False


def test_walk_forward_segments(uptrend_engine, settings):
    v = validate_out_of_sample(uptrend_engine.frame("H1"), settings, higher=_higher(uptrend_engine))
    roles = [s["role"] for s in v["segments"]]
    assert roles[0] == "development" and set(roles[1:]) == {"out_of_sample"}
    assert v["live_permission"] is False


def test_calibration_drift_regime():
    table = calibration_table([65, 66, 85, 86], [0, 0, 1, 0])
    assert table[0]["realized_win_rate_pct"] == 0.0
    assert detect_drift(0.3, 0.6)["drift"] is True
    assert regime_label(30, "normal") == "trend" and regime_label(10, "low") == "range"


def test_classifier_needs_history_and_stays_draft():
    assert study_outcomes([{"pnl": 1}] * 10)["status"] == "insufficient_history"
    rng = np.random.default_rng(0)
    rows = [{"pnl": float(rng.normal()), "confidence": 60 + i % 20, "direction": "BUY", "reward_risk": 2, "atr": 3} for i in range(60)]
    r = study_outcomes(rows)
    assert r["status"] == "draft" and r["deployed"] is False


def test_registry_promotion_requires_validation(tmp_path):
    reg = ModelRegistry(tmp_path)
    reg.record("v1", "candidate", {}, "")
    assert reg.promote("v1", oos_gate_passed=False)["promoted"] is False
    assert reg.promote("v1", oos_gate_passed=True)["promoted"] is True
    assert ModelRegistry(tmp_path).versions[-1]["status"] == "validated"  # persisted
