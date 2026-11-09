"""The shipped .env.example must load as-is (README quick start and docker-compose use it)."""

from pathlib import Path

from app.config import Settings

TEMPLATE = Path(__file__).resolve().parents[2] / ".env.example"


def test_env_example_loads_with_safe_defaults():
    s = Settings(_env_file=TEMPLATE)
    assert s.app_mode == "paper" and s.strategy_profile == "intraday"
    assert s.live_trading_authorized is False
    assert s.dxy_session_change is None and s.database_url == ""
    assert s.max_risk_per_trade_pct == 0.5 and s.min_reward_risk == 1.8


def test_env_example_covers_every_setting():
    keys = {line.split("=", 1)[0].strip().lower() for line in TEMPLATE.read_text().splitlines() if "=" in line and not line.startswith("#")}
    missing = set(Settings.model_fields) - keys
    assert not missing, f".env.example is missing {sorted(missing)}"
