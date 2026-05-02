"""
src/signal/engine.py
Multi-asset signal engine — evaluates ALL active 5-min markets.

Key philosophy:
  - Only buy UNDERPRICED tokens (market thinks <55% chance, we disagree)
  - Prefer low-priced tokens: buy at 0.30 → win pays 233%, buy at 0.55 → win pays 82%
  - Entry allowed ANY TIME in the window with valid velocity + edge
  - Rank by Expected Value (EV): edge × confidence × payout_multiplier
"""

import time
from typing import Optional, List, Dict
from loguru import logger

from src.models import Signal, PolyWindow, Side, SUPPORTED_ASSETS
from src.data.binance_feed import MultiAssetFeed
from src.data.polymarket_feed import MultiMarketFeed
from config.settings import Settings


# Calibrated delta → win probability table
DELTA_TO_WIN_PROB = [
    (0.01, 0.52),
    (0.02, 0.55),
    (0.03, 0.58),
    (0.05, 0.63),
    (0.07, 0.69),
    (0.10, 0.75),
    (0.15, 0.83),
    (0.20, 0.89),
    (0.30, 0.94),
]

ASSET_VOLATILITY = {
    "BTC":  1.0,
    "ETH":  1.1,
    "SOL":  1.3,
    "XRP":  1.2,
    "MATIC": 1.25,
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


def payout_multiplier(token_price: float) -> float:
    """
    Returns the payout ratio of a winning trade.
    token at 0.30 → win pays 233% (multiplier 3.33)
    token at 0.50 → win pays 100% (multiplier 2.0)
    token at 0.55 → win pays 82%  (multiplier 1.82)
    """
    return 1.0 / token_price if token_price > 0 else 1.0


class SignalEngine:
    """
    Evaluates all active markets every second.
    Prefers low-priced tokens for high payout ratios.
    Entry allowed at any time when conditions are met.
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
        self._traded_windows: Dict[str, int] = {}
        self._last_trade_time: Dict[str, float] = {}

    def mark_traded(self, asset: str, window_ts: int) -> None:
        key = f"{asset}:{window_ts}"
        self._traded_windows[key] = 1
        self._last_trade_time[asset] = time.time()

    def is_in_cooldown(self, asset: str) -> bool:
        last = self._last_trade_time.get(asset, 0.0)
        return (time.time() - last) < self._settings.cooldown_seconds

    async def evaluate(self) -> Optional[Signal]:
        windows = self._polymarket.current_windows
        if not windows:
            return None

        candidates: List[Signal] = []

        for asset, window in windows.items():
            sig = await self._evaluate_asset(asset, window)
            if sig is not None:
                candidates.append(sig)

        if not candidates:
            return None

        # Rank by: edge × confidence × payout_multiplier
        # This means: prefer high-edge, high-confidence, HIGH-PAYOUT (low token price) trades
        candidates.sort(
            key=lambda s: s.edge_after_fees * s.confidence * payout_multiplier(s.token_price),
            reverse=True,
        )
        best = candidates[0]

        payout_pct = (payout_multiplier(best.token_price) - 1.0) * 100
        logger.success(
            f"SIGNAL → {best.asset} {best.side} | "
            f"token={best.token_price:.3f} | payout=+{payout_pct:.0f}% | "
            f"edge={best.edge_after_fees:.3f} | conf={best.confidence:.2f}"
        )
        return best

    async def _evaluate_asset(self, asset: str, window: PolyWindow) -> Optional[Signal]:
        window_key = f"{asset}:{window.window_ts}"

        if self._traded_windows.get(window_key):
            return None

        secs_remaining = window.seconds_remaining

        if secs_remaining < self._settings.min_seconds_remaining:
            return None

        if secs_remaining > 300:
            return None

        if self.is_in_cooldown(asset):
            return None

        if not self._binance.is_asset_healthy(asset):
            return None

        # Multi-timeframe delta
        delta_20s = self._binance.get_delta_pct(lookback_seconds=20.0, asset=asset)
        delta_60s = self._binance.get_delta_pct(lookback_seconds=60.0, asset=asset)

        if delta_20s is None:
            return None

        delta_abs = abs(delta_20s)

        if delta_abs < self._settings.min_btc_delta_pct:
            return None

        # Velocity filter — reject slow/dead markets
        velocity = delta_abs / 20.0
        if velocity < self._settings.min_velocity_pct_per_sec:
            return None

        # Direction consistency check
        direction_consistent = False
        if delta_60s is not None:
            direction_consistent = (delta_20s > 0 and delta_60s > 0) or (delta_20s < 0 and delta_60s < 0)

        side: Side = "UP" if delta_20s > 0 else "DOWN"
        token_id = window.token_id_up if side == "UP" else window.token_id_down

        token_price = await self._polymarket.get_token_price(token_id)
        if token_price is None or token_price <= 0:
            return None

        # ── Token price filters ──────────────────────────────────────────
        # Only trade underpriced tokens (market wrong, we see value)
        if token_price > self._settings.max_token_price:
            logger.debug(
                f"{asset}: token={token_price:.3f} > max {self._settings.max_token_price} — "
                f"payout too low, skip"
            )
            return None

        if token_price < self._settings.min_token_price:
            logger.debug(f"{asset}: token={token_price:.3f} < min — suspiciously low, skip")
            return None

        # ── Win probability ───────────────────────────────────────────────
        win_prob = estimate_win_probability(delta_abs, asset)

        # Bonus for direction consistency (both timeframes agree)
        if direction_consistent:
            win_prob = min(0.95, win_prob * 1.06)

        # Bonus for entering earlier in window (more time = more likely to be right)
        time_bonus = min(0.025, secs_remaining / 12000.0)
        win_prob = min(0.95, win_prob + time_bonus)

        # CRITICAL: only trade when our win probability > market implied probability
        # Token price IS the market's implied probability
        # We need meaningful edge over the market
        if win_prob <= token_price + 0.08:
            logger.debug(
                f"{asset}: win_prob={win_prob:.2f} not enough above token={token_price:.2f} — skip"
            )
            return None

        fee = self._settings.taker_fee_pct * token_price
        edge = win_prob * 1.0 - token_price - fee

        payout_pct = (payout_multiplier(token_price) - 1.0) * 100
        logger.info(
            f"{asset} eval: {side} | delta={delta_20s:+.3f}% | vel={velocity:.5f}%/s | "
            f"token={token_price:.3f} (+{payout_pct:.0f}% if WIN) | "
            f"win_prob={win_prob:.2f} | edge={edge:.3f} | {secs_remaining:.0f}s left"
        )

        if edge < self._settings.min_edge_after_fees:
            return None

        signal = Signal(
            side=side,
            btc_delta_pct=delta_20s,
            token_price=token_price,
            expected_payout=1.0,
            edge_after_fees=edge,
            confidence=win_prob,
            asset=asset,
            window_ts=window.window_ts,
        )

        if signal.is_tradeable:
            return signal

        return None
