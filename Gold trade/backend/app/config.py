"""Runtime configuration.

Risk limits are loaded once from the environment. The learning module can
recommend research changes, but it has no writer for these fields.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_mode: str = "analysis"
    instrument: str = "XAUUSD"
    account_equity: float = 100_000.0

    live_trading_authorized: bool = False
    broker_connected: bool = False
    emergency_shutdown_armed: bool = True
    broker_adapter: str = "none"

    database_url: str = ""
    redis_url: str = ""

    mt5_login: str = ""
    mt5_password: str = ""
    mt5_server: str = ""
    mt5_symbol: str = "XAUUSD"

    max_risk_per_trade_pct: float = 0.5
    max_daily_loss_pct: float = 2.0
    max_weekly_drawdown_pct: float = 5.0
    max_open_positions: int = 1
    max_correlated_exposure_pct: float = 0.5
    max_spread: float = 0.50
    max_slippage: float = 0.30
    min_reward_risk: float = 1.8
    max_consecutive_losses: int = 3
    allowed_sessions: str = "london,new_york,london_new_york_overlap"
    macro_blackout_before_min: int = 30
    macro_blackout_after_min: int = 15
    min_signal_confidence: float = 62.0

    dxy_session_change: float | None = None
    us10y_session_change: float | None = None

    contract_ounces: float = 100.0
    min_lot: float = 0.01
    lot_step: float = 0.01

    @field_validator("app_mode")
    @classmethod
    def _mode(cls, value: str) -> str:
        allowed = {"analysis", "paper", "live"}
        normalized = value.strip().lower()
        if normalized not in allowed:
            raise ValueError(f"APP_MODE must be one of {sorted(allowed)}")
        return normalized

    @property
    def session_allowlist(self) -> frozenset[str]:
        return frozenset(
            part.strip() for part in self.allowed_sessions.split(",") if part.strip()
        )

    def risk_snapshot(self) -> dict[str, float | int | str]:
        return {
            "max_risk_per_trade_pct": self.max_risk_per_trade_pct,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "max_weekly_drawdown_pct": self.max_weekly_drawdown_pct,
            "max_open_positions": self.max_open_positions,
            "max_correlated_exposure_pct": self.max_correlated_exposure_pct,
            "max_spread": self.max_spread,
            "max_slippage": self.max_slippage,
            "min_reward_risk": self.min_reward_risk,
            "max_consecutive_losses": self.max_consecutive_losses,
            "allowed_sessions": ",".join(sorted(self.session_allowlist)),
        }


def load_settings() -> Settings:
    return Settings()
