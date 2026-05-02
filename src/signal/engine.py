"""
src/signal/engine.py
Multi-asset signal engine — evaluates ALL active 5-min markets,
picks the best edge opportunity, prioritising Win Rate then volume.

Strategy per asset:
  1. Wait until T-25 seconds before window close
  2. Calculate asset price delta (% move in last 20 seconds)
  3. If delta > threshold → signal candidate
  4. Calculate token price and expected edge after fees
  5. Return best edge signal across all assets
"""

import time
from typing import Optional, List
from loguru import logger

from src.models import Signal, PolyWindow, Side, SUPPORTED_ASSETS
from src.data.binance_feed import MultiAssetFeed
from src.data.polymarket_feed import MultiMarketFeed
from config.settings import Settings


DELTA_TO_WIN_PROB = [
    (0.02, 0.54),
    (0.03, 0.57),
    (0.05, 0.62),
    (0.07, 0.67),
    (0.10, 0.73),
    (0.15, 0.81),
    (0.20, 0.87),
    (0.30, 0.93),
]

ASSET_VOLATILITY = {
    "BTC":  1.0,
    "ETH":  1.1,
    "SOL":  1.3,
    "XRP":  1.2,
    "MATIC":1.25,
    "DOGE": 1.3,
    "LINK": 1.15,
    "AVAX": 1.2,
}


def estimate_win_probability(delta_abs: float, asset: str = "BTC") -> float:
    vol = ASSET_VOLATILITY.get(asset, 1.0)
    adj = delta_abs / vol

    if adj <= DELTA_TO_WIN_PROB[0][0]:
        return DELTA_TO_WIN_PROB[0][1]
    if adj >= DELTA_TO_WIN_PROB[-1][0]:
        return DELTA_TO_WIN_PROB[-1][1]

    for i in range(len(DELTA_TO_WIN_PROB) - 1):
        d0, p0 = DELTA_TO_WIN_PROB[i]
        d1, p1 = DELTA_TO_WIN_PROB[i + 1]
        if d0 <= adj <= d1:
            t = (adj - d0) / (d1 - d0)
            return p0 + t * (p1 - p0)

    return 0.5


class SignalEngine:
    """
    Evaluates all active markets every second and produces the best signal.
    Prioritises: Win Rate first, then edge magnitude.
    """

    def __init__(
        self,
        binance: MultiAssetFeed,
        polymarket: MultiMarketFeed,
        settings: Settings,
    ) -> None:
        self._binance = binance
        self._polymarket = polymarket
        self._settings = settings
        self._traded_windows: dict[str, int] = {}

    async def evaluate(self) -> Optional[Signal]:
        """
        Evaluate all active markets. Return the best-edge signal found, or None.
        """
        windows = self._polymarket.current_windows
        if not windows:
            logger.debug("No active windows")
            return None

        candidates: List[Signal] = []

        for asset, window in windows.items():
            sig = await self._evaluate_asset(asset, window)
            if sig is not None:
                candidates.append(sig)

        if not candidates:
            return None

        # Rank: highest confidence first (win rate proxy), then edge
        candidates.sort(key=lambda s: (s.confidence, s.edge_after_fees), reverse=True)
        best = candidates[0]
        logger.success(
            f"BEST SIGNAL → {best.asset} {best.side} | "
            f"edge={best.edge_after_fees:.3f} | confidence={best.confidence:.2f}"
        )
        return best

    async def _evaluate_asset(self, asset: str, window: PolyWindow) -> Optional[Signal]:
        window_key = f"{asset}:{window.window_ts}"

        if self._traded_windows.get(window_key):
            return None

        secs_remaining = window.seconds_remaining
        if secs_remaining > self._settings.entry_window_seconds:
            return None
        if secs_remaining < 5:
            return None

        if not self._binance.is_asset_healthy(asset):
            logger.debug(f"{asset}: feed unhealthy, skipping")
            return None

        delta_pct = self._binance.get_delta_pct(lookback_seconds=20.0, asset=asset)
        if delta_pct is None:
            logger.debug(f"{asset}: insufficient price history")
            return None

        delta_abs = abs(delta_pct)

        min_delta = self._settings.min_btc_delta_pct
        if delta_abs < min_delta:
            logger.debug(f"{asset}: delta {delta_abs:.3f}% < {min_delta}% threshold")
            return None

        side: Side = "UP" if delta_pct > 0 else "DOWN"
        token_id = window.token_id_up if side == "UP" else window.token_id_down

        token_price = await self._polymarket.get_token_price(token_id)
        if token_price is None or token_price <= 0:
            logger.debug(f"{asset}: could not fetch token price")
            return None

        if token_price >= 0.98:
            logger.debug(f"{asset}: token price {token_price} too high — already priced in")
            return None

        if token_price <= 0.02:
            logger.debug(f"{asset}: token price {token_price} suspiciously low")
            return None

        win_prob = estimate_win_probability(delta_abs, asset)
        fee = self._settings.taker_fee_pct * token_price
        edge = win_prob * 1.0 - token_price - fee

        logger.info(
            f"{asset} signal: {side} | delta={delta_pct:+.3f}% | "
            f"token={token_price:.3f} | win_prob={win_prob:.2f} | "
            f"edge={edge:.3f} | {secs_remaining:.0f}s left"
        )

        if edge < self._settings.min_edge_after_fees:
            return None

        signal = Signal(
            side=side,
            btc_delta_pct=delta_pct,
            token_price=token_price,
            expected_payout=1.0,
            edge_after_fees=edge,
            confidence=win_prob,
            asset=asset,
            window_ts=window.window_ts,
        )

        if signal.is_tradeable:
            self._traded_windows[window_key] = 1
            return signal

        return None
