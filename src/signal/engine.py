"""
src/signal/engine.py
Multi-asset signal engine — evaluates ALL active 5-min markets.

Strategy:
  - Entry allowed at ANY point in the 5-minute window (not just last 25s)
  - Requires: price velocity + delta threshold + minimum edge
  - Filters out: dead markets, already-traded windows, cooldown periods
  - Ranks candidates by: edge * confidence (EV proxy)
"""

import time
from typing import Optional, List, Dict
from loguru import logger

from src.models import Signal, PolyWindow, Side, SUPPORTED_ASSETS
from src.data.binance_feed import MultiAssetFeed
from src.data.polymarket_feed import MultiMarketFeed
from config.settings import Settings


DELTA_TO_WIN_PROB = [
    (0.01, 0.52),
    (0.02, 0.55),
    (0.03, 0.58),
    (0.05, 0.63),
    (0.07, 0.68),
    (0.10, 0.74),
    (0.15, 0.82),
    (0.20, 0.88),
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


class SignalEngine:
    """
    Evaluates all active markets every second.
    Entry allowed at ANY time during the window when signal conditions are met.
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
        """Call after a trade is opened to prevent re-entry."""
        key = f"{asset}:{window_ts}"
        self._traded_windows[key] = 1
        self._last_trade_time[asset] = time.time()

    def is_in_cooldown(self, asset: str) -> bool:
        last = self._last_trade_time.get(asset, 0.0)
        return (time.time() - last) < self._settings.cooldown_seconds

    async def evaluate(self) -> Optional[Signal]:
        """
        Evaluate all active markets. Return the best-edge signal found, or None.
        """
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

        # Rank by EV proxy: edge * confidence — maximises expected value
        candidates.sort(key=lambda s: s.edge_after_fees * s.confidence, reverse=True)
        best = candidates[0]
        logger.success(
            f"SIGNAL → {best.asset} {best.side} | "
            f"edge={best.edge_after_fees:.3f} | conf={best.confidence:.2f} | "
            f"ev={best.edge_after_fees * best.confidence:.4f}"
        )
        return best

    async def _evaluate_asset(self, asset: str, window: PolyWindow) -> Optional[Signal]:
        window_key = f"{asset}:{window.window_ts}"

        if self._traded_windows.get(window_key):
            return None

        secs_remaining = window.seconds_remaining

        # Don't enter if window is almost closed
        if secs_remaining < self._settings.min_seconds_remaining:
            return None

        # Don't enter if window is not active
        if secs_remaining > 300:
            return None

        # Cooldown per asset
        if self.is_in_cooldown(asset):
            logger.debug(f"{asset}: in cooldown — skip")
            return None

        if not self._binance.is_asset_healthy(asset):
            logger.debug(f"{asset}: feed unhealthy, skipping")
            return None

        # ── Multi-timeframe delta analysis ──────────────────────────────
        delta_20s = self._binance.get_delta_pct(lookback_seconds=20.0, asset=asset)
        delta_60s = self._binance.get_delta_pct(lookback_seconds=60.0, asset=asset)

        if delta_20s is None:
            logger.debug(f"{asset}: insufficient price history")
            return None

        delta_abs_20s = abs(delta_20s)

        # Primary delta threshold
        if delta_abs_20s < self._settings.min_btc_delta_pct:
            logger.debug(f"{asset}: delta {delta_abs_20s:.3f}% < {self._settings.min_btc_delta_pct}% threshold")
            return None

        # ── Price Velocity filter ────────────────────────────────────────
        # velocity = % move per second over 20s window
        velocity = delta_abs_20s / 20.0
        if velocity < self._settings.min_velocity_pct_per_sec:
            logger.debug(f"{asset}: velocity {velocity:.5f}%/s < {self._settings.min_velocity_pct_per_sec}%/s — slow market")
            return None

        # ── Direction consistency check ──────────────────────────────────
        # If 60s delta confirms the same direction as 20s — higher confidence
        direction_consistent = False
        if delta_60s is not None:
            direction_consistent = (delta_20s > 0 and delta_60s > 0) or (delta_20s < 0 and delta_60s < 0)

        side: Side = "UP" if delta_20s > 0 else "DOWN"
        token_id = window.token_id_up if side == "UP" else window.token_id_down

        token_price = await self._polymarket.get_token_price(token_id)
        if token_price is None or token_price <= 0:
            logger.debug(f"{asset}: could not fetch token price")
            return None

        # Filter already-priced-in or suspiciously extreme markets
        if token_price >= 0.95:
            logger.debug(f"{asset}: token price {token_price:.3f} already priced in")
            return None
        if token_price <= 0.05:
            logger.debug(f"{asset}: token price {token_price:.3f} suspiciously low")
            return None

        # ── Win probability with consistency bonus ───────────────────────
        win_prob = estimate_win_probability(delta_abs_20s, asset)
        if direction_consistent:
            win_prob = min(0.95, win_prob * 1.05)

        # Bonus for entries earlier in the window (more time = more chance to be right)
        time_bonus = min(0.03, secs_remaining / 10000.0)
        win_prob = min(0.95, win_prob + time_bonus)

        fee = self._settings.taker_fee_pct * token_price
        edge = win_prob * 1.0 - token_price - fee

        logger.info(
            f"{asset} eval: {side} | delta={delta_20s:+.3f}% | vel={velocity:.5f}%/s | "
            f"token={token_price:.3f} | win_prob={win_prob:.2f} | "
            f"edge={edge:.3f} | {secs_remaining:.0f}s left | "
            f"consistent={'YES' if direction_consistent else 'no'}"
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
