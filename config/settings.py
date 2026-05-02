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

    # Capital & Risk
    capital_usd: float = Field(100.0, description="Initial trading capital in USD")
    max_position_pct: float = Field(0.08, description="Max % of CURRENT capital per trade (dynamic compounding)")
    daily_loss_limit_pct: float = Field(0.20, description="Stop trading after losing X% of capital today")
    max_trades_per_day: int = Field(50, description="Hard cap on daily trades")
    max_open_trades: int = Field(5, description="Max simultaneous open positions")
    max_consecutive_losses: int = Field(5, description="Stop bot after N losses in a row")
    cooldown_seconds: float = Field(20.0, description="Seconds to wait before re-entering same asset")

    # Signal — entry allowed ANY time in window
    min_btc_delta_pct: float = Field(0.04, description="Min price move % in 20s lookback")
    min_velocity_pct_per_sec: float = Field(0.0015, description="Min price velocity (%/sec)")
    min_edge_after_fees: float = Field(0.04, description="Min expected edge after fees (stricter = better quality trades)")
    min_seconds_remaining: int = Field(10, description="Do not enter if less than N seconds to close")

    # Token price filtering — controls risk/reward ratio
    max_token_price: float = Field(0.55, description="Only buy tokens below this price (higher payout ratio)")
    min_token_price: float = Field(0.08, description="Avoid tokens this cheap (likely already resolved)")
    preferred_token_max: float = Field(0.45, description="Tokens below this get a payout bonus in ranking")

    # Exit management
    tp_token_gain: float = Field(0.20, description="Take profit when implied token rises by this much")
    sl_token_loss: float = Field(0.10, description="Stop loss when implied token falls by this much")
    time_stop_seconds: float = Field(260.0, description="Max seconds to hold — near window close")

    # Fees
    taker_fee_pct: float = Field(0.0156, description="Taker fee %")
    maker_fee_pct: float = Field(0.0, description="Maker fee % (usually 0)")

    # Logging
    log_level: str = Field("INFO", description="Logging level")
    log_file: str = Field("logs/bot.log", description="Log file path")

    @property
    def daily_loss_limit_usd(self) -> float:
        return self.capital_usd * self.daily_loss_limit_pct

    @property
    def polygon_rpc_url(self) -> str:
        return f"https://polygon-mainnet.g.alchemy.com/v2/{self.alchemy_api_key}"


# Singleton — import this everywhere
settings = Settings()
