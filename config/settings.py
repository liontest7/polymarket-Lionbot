"""
config/settings.py
Central configuration — loaded once at startup from .env
"""

from pydantic_settings import BaseSettings
from pydantic import Field, ConfigDict
from typing import Literal


class Settings(BaseSettings):
    model_config = ConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False)

    # Polymarket credentials (optional in paper mode)
    polymarket_pk: str = Field(default="0x0", description="Private key (0x...)")
    polymarket_api_key: str = Field(default="", description="Polymarket API key")
    polymarket_api_secret: str = Field(default="", description="Polymarket API secret")
    polymarket_api_passphrase: str = Field(default="", description="Polymarket API passphrase")

    # Alchemy
    alchemy_api_key: str = Field(default="", description="Alchemy API key for Polygon")

    # Mode
    trading_mode: Literal["paper", "live"] = Field("paper", description="paper or live")

    # Risk
    capital_usd: float = Field(100.0, description="Total trading capital in USD")
    max_position_pct: float = Field(0.10, description="Max % of capital per trade")
    daily_loss_limit_pct: float = Field(0.05, description="Stop trading after losing X% of capital today")
    max_trades_per_day: int = Field(20, description="Hard cap on daily trades")

    # Signal
    min_btc_delta_pct: float = Field(0.05, description="Min BTC move % to consider signal valid")
    min_edge_after_fees: float = Field(0.03, description="Min expected edge after fees")
    entry_window_seconds: int = Field(25, description="Only trade in last N seconds of 5-min window")

    # Fees
    taker_fee_pct: float = Field(0.0156, description="Taker fee %")
    maker_fee_pct: float = Field(0.0, description="Maker fee % (usually 0 or negative rebate)")

    # Logging
    log_level: str = Field("INFO", description="Logging level")
    log_file: str = Field("logs/bot.log", description="Log file path")

    @property
    def max_position_usd(self) -> float:
        return self.capital_usd * self.max_position_pct

    @property
    def daily_loss_limit_usd(self) -> float:
        return self.capital_usd * self.daily_loss_limit_pct

    @property
    def polygon_rpc_url(self) -> str:
        return f"https://polygon-mainnet.g.alchemy.com/v2/{self.alchemy_api_key}"


# Singleton — import this everywhere
settings = Settings()
