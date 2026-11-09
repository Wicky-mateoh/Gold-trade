"""Research memory for trade outcomes.

Models can be drafted and compared. Promotion requires an explicit validation
flag. Risk-limit keys are dropped if a recommendation tries to carry them.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

FORBIDDEN_PARAMETER_KEYS = {
    "max_risk_per_trade_pct",
    "max_daily_loss_pct",
    "max_weekly_drawdown_pct",
    "max_open_positions",
    "max_correlated_exposure_pct",
    "max_spread",
    "max_slippage",
    "min_reward_risk",
    "max_consecutive_losses",
    "emergency_shutdown",
    "recovery_multiplier",
}


class ModelRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.mkdir(parents=True, exist_ok=True)
        self.versions: list[dict] = []
        stored = path / "registry.json"
        if stored.exists():
            self.versions = json.loads(stored.read_text(encoding="utf-8"))

    def record(self, version: str, status: str, metrics: dict, notes: str) -> dict:
        row = {
            "version": version,
            "status": status,
            "metrics": metrics,
            "notes": notes,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.versions.append(row)
        (self.path / "registry.json").write_text(json.dumps(self.versions, indent=2), encoding="utf-8")
        return row

    def promote(self, version: str, oos_gate_passed: bool) -> dict:
        if not oos_gate_passed:
            return {"promoted": False, "reason": "Out-of-sample validation has not passed."}
        matches = [row for row in self.versions if row["version"] == version]
        if not matches:
            return {"promoted": False, "reason": "Unknown model version."}
        matches[-1]["status"] = "validated"
        (self.path / "registry.json").write_text(json.dumps(self.versions, indent=2), encoding="utf-8")
        return {"promoted": True, "version": version, "status": "validated"}


def calibration_table(confidences: list[float], outcomes: list[int]) -> list[dict]:
    if len(confidences) != len(outcomes) or not confidences:
        return []
    bins = [(50, 60), (60, 70), (70, 80), (80, 101)]
    rows = []
    pairs = list(zip(confidences, outcomes))
    for low, high in bins:
        bucket = [(conf, outcome) for conf, outcome in pairs if low <= conf < high]
        if not bucket:
            continue
        predicted = float(np.mean([conf for conf, _ in bucket]))
        realized = float(np.mean([outcome for _, outcome in bucket])) * 100
        rows.append(
            {
                "bucket": f"{low}-{high - 1 if high <= 100 else 100}",
                "samples": len(bucket),
                "average_confidence": round(predicted, 1),
                "realized_win_rate_pct": round(realized, 1),
                "overconfident": predicted - realized > 15,
            }
        )
    return rows


def detect_drift(recent_accuracy: float, baseline_accuracy: float, threshold: float = 0.15) -> dict:
    drop = baseline_accuracy - recent_accuracy
    return {
        "drift": drop > threshold,
        "accuracy_drop": round(drop, 4),
        "action": "Revalidate on a fresh walk-forward window before any deployment review."
        if drop > threshold
        else "No accuracy drop beyond the threshold.",
    }


def regime_label(adx_value: float | None, volatility: str) -> str:
    if volatility in {"extreme", "high"}:
        return "high_volatility"
    if adx_value is None:
        return "unknown"
    if adx_value >= 25 and volatility in {"normal", "low"}:
        return "trend"
    if adx_value < 18:
        return "range"
    return "transition"


def study_outcomes(rows: list[dict]) -> dict:
    if len(rows) < 40:
        return {
            "status": "insufficient_history",
            "samples": len(rows),
            "recommendation": "Collect more closed trades before fitting a classifier.",
        }
    labels = [1 if row["pnl"] > 0 else 0 for row in rows]
    if len(set(labels)) < 2:
        return {"status": "single_class", "samples": len(rows), "recommendation": "The sample has one outcome class."}
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        return {
            "status": "sklearn_unavailable",
            "samples": len(rows),
            "recommendation": "Install scikit-learn before fitting the outcome classifier.",
        }
    matrix = np.array(
        [
            [
                row.get("confidence") or 0.0,
                1.0 if row.get("direction") == "BUY" else 0.0,
                row.get("reward_risk") or 0.0,
                row.get("atr") or 0.0,
            ]
            for row in rows
        ],
        dtype=float,
    )
    model = Pipeline(
        [
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(max_iter=400, class_weight="balanced")),
        ]
    )
    model.fit(matrix, labels)
    probabilities = model.predict_proba(matrix)[:, 1]
    return {
        "status": "draft",
        "samples": len(rows),
        "in_sample_accuracy": round(float(np.mean((probabilities >= 0.5) == np.array(labels))), 3),
        "recommendation": "Keep the classifier in draft until a later sample confirms it. Do not change risk limits from this fit.",
        "deployed": False,
    }


def filter_recommendations(proposals: dict[str, object]) -> dict:
    removed = sorted(key for key in proposals if key in FORBIDDEN_PARAMETER_KEYS)
    kept = {key: value for key, value in proposals.items() if key not in FORBIDDEN_PARAMETER_KEYS}
    return {
        "accepted": kept,
        "removed_risk_keys": removed,
        "note": "Risk limits stay under operator control.",
    }
