"""
src/models.py
Shared data models across all modules
"""

from dataclasses import dataclass, field
from typing import Literal, Optional
from datetime import datetime
import time


Side = Literal["UP", "DOWN"]
TradeResult = Literal["WIN", "LOSS", "PENDING", "CANCELLED"]
OrderSide = Literal["BUY", "SELL"]

SUPPORTED_ASSETS = ["BTC", "ETH", "SOL", "XRP", "MATIC", "DOGE", "LINK", "AVAX"]

ASSET_BINANCE_SYMBOL = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "SOL": "solusdt",
    "XRP": "xrpusdt",
    "MATIC": "maticusdt",
    "DOGE": "dogeusdt",
    "LINK": "linkusdt",
    "AVAX": "avaxusdt",
}

ASSET_KEYWORDS = {
    "BTC": ["btc", "bitcoin"],
    "ETH": ["eth", "ethereum"],
    "SOL": ["sol", "solana"],
    "XRP": ["xrp", "ripple"],
    "MATIC": ["matic", "polygon"],
    "DOGE": ["doge", "dogecoin"],
    "LINK": ["link", "chainlink"],
    "AVAX": ["avax", "avalanche"],
}


@dataclass
class AssetPrice:
    asset: str
    price: float
    timestamp: float
    source: str = "binance"

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp


BTCPrice = AssetPrice


@dataclass
class PolyWindow:
    """A 5-minute Polymarket Up/Down window for any supported asset."""
    window_ts: int
    close_ts: int
    market_id: str
    token_id_up: str
    token_id_down: str
    open_price: float
    asset: str = "BTC"

    @property
    def seconds_remaining(self) -> float:
        return self.close_ts - time.time()

    @property
    def slug(self) -> str:
        return f"{self.asset.lower()}-updown-5m-{self.window_ts}"

    @property
    def is_active(self) -> bool:
        return 0 < self.seconds_remaining < 300


@dataclass
class Signal:
    """Trading signal output from SignalEngine."""
    side: Side
    btc_delta_pct: float
    token_price: float
    expected_payout: float
    edge_after_fees: float
    confidence: float
    asset: str = "BTC"
    window_ts: int = 0
    timestamp: float = field(default_factory=time.time)

    @property
    def is_tradeable(self) -> bool:
        return self.edge_after_fees > 0 and self.confidence > 0.55


@dataclass
class Trade:
    """A single trade execution record."""
    id: str
    window_ts: int
    side: Side
    token_id: str
    shares: float
    entry_price: float
    entry_time: float
    size_usd: float
    mode: Literal["paper", "live"]
    asset: str = "BTC"
    order_id: Optional[str] = None
    exit_price: Optional[float] = None
    exit_time: Optional[float] = None
    result: TradeResult = "PENDING"
    pnl_usd: float = 0.0
    fees_paid: float = 0.0
    notes: str = ""
    entry_asset_price: float = 0.0

    @property
    def is_closed(self) -> bool:
        return self.result in ("WIN", "LOSS", "CANCELLED")


@dataclass
class DailyStats:
    date: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_fees: float = 0.0
    gross_pnl: float = 0.0

    @property
    def win_rate(self) -> float:
        if self.trades == 0:
            return 0.0
        return self.wins / self.trades

    @property
    def net_pnl(self) -> float:
        return self.total_pnl - self.total_fees
