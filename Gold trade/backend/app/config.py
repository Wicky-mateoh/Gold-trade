"""Runtime configuration.

Risk limits are loaded once from the environment. The learning module can
recommend research changes, but it has no writer for these fields.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Empty values in .env (e.g. "DXY_SESSION_CHANGE=") mean "not set", not "".
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    # Analysis + simulated (paper) trading is the operating default.
    app_mode: str = "paper"
    # intraday: D1/H4 direction, H1 confirmation, M15 entry.
    # scalping: H4/H1 direction, M15 confirmation and entry, short holds.
    strategy_profile: str = "intraday"
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
    # Spread may use at most this share of the stop distance (protects short-stop scalps).
    max_spread_stop_ratio: float = 0.2

    # Tier-1 releases (NFP, CPI): wider no-new-risk window and a pre-release flatten.
    tier1_blackout_before_min: int = 60
    tier1_blackout_after_min: int = 60
    flatten_before_tier1_min: int = 15
    # Optional JSON calendar feed (ForexFactory-style rows). Empty = file + built-in schedule only.
    calendar_feed_url: str = ""
    calendar_refresh_minutes: int = 60

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

    @field_validator("strategy_profile")
    @classmethod
    def _profile(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"intraday", "scalping"}:
            raise ValueError("STRATEGY_PROFILE must be intraday or scalping")
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
            "max_spread_stop_ratio": self.max_spread_stop_ratio,
            "tier1_blackout_before_min": self.tier1_blackout_before_min,
            "tier1_blackout_after_min": self.tier1_blackout_after_min,
            "flatten_before_tier1_min": self.flatten_before_tier1_min,
        }


def load_settings() -> Settings:
    return Settings()
