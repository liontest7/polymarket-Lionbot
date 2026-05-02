"""
src/data/binance_feed.py
Real-time multi-asset price feed from Binance combined WebSocket stream.
Tracks BTC, ETH, SOL, XRP, MATIC, DOGE and more.
"""

import asyncio
import json
import time
from collections import deque
from typing import Optional, Dict
import websockets
from loguru import logger

from src.models import AssetPrice, ASSET_BINANCE_SYMBOL, SUPPORTED_ASSETS

BTCPrice = AssetPrice

RECONNECT_DELAY = 3


def _build_ws_url(assets: list[str]) -> str:
    streams = "/".join(f"{ASSET_BINANCE_SYMBOL[a]}@trade" for a in assets if a in ASSET_BINANCE_SYMBOL)
    return f"wss://stream.binance.com:9443/stream?streams={streams}"


class MultiAssetFeed:
    """
    Streams real-time prices for multiple assets from Binance combined stream.
    Maintains rolling price history per asset for delta calculations.
    """

    def __init__(self, assets: Optional[list[str]] = None) -> None:
        self._assets = assets or SUPPORTED_ASSETS
        self._current: Dict[str, AssetPrice] = {}
        self._history: Dict[str, deque] = {a: deque(maxlen=600) for a in self._assets}
        self._running = False

    @property
    def is_healthy(self) -> bool:
        if not self._current:
            return False
        now = time.time()
        return any(now - p.timestamp < 10.0 for p in self._current.values())

    def get_price(self, asset: str) -> Optional[AssetPrice]:
        return self._current.get(asset)

    @property
    def current_price(self) -> Optional[AssetPrice]:
        return self._current.get("BTC")

    def get_delta_pct(self, lookback_seconds: float = 20.0, asset: str = "BTC") -> Optional[float]:
        cur = self._current.get(asset)
        if cur is None:
            return None
        history = self._history.get(asset, deque())
        target_ts = time.time() - lookback_seconds
        best = None
        best_diff = float("inf")
        for p in history:
            diff = abs(p.timestamp - target_ts)
            if diff < best_diff:
                best_diff = diff
                best = p
        if best is None or best.age_seconds < (lookback_seconds * 0.5):
            return None
        delta = (cur.price - best.price) / best.price * 100
        return delta

    def is_asset_healthy(self, asset: str) -> bool:
        p = self._current.get(asset)
        if p is None:
            return False
        return p.age_seconds < 10.0

    async def start(self) -> None:
        self._running = True
        logger.info(f"MultiAssetFeed starting for: {', '.join(self._assets)}")
        while self._running:
            try:
                await self._stream()
            except Exception as e:
                logger.warning(f"MultiAssetFeed disconnected: {e} — reconnecting in {RECONNECT_DELAY}s")
                await asyncio.sleep(RECONNECT_DELAY)

    async def stop(self) -> None:
        self._running = False
        logger.info("MultiAssetFeed stopped")

    async def _stream(self) -> None:
        url = _build_ws_url(self._assets)
        symbol_to_asset = {v: k for k, v in ASSET_BINANCE_SYMBOL.items()}

        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            logger.success(f"MultiAssetFeed connected — tracking {len(self._assets)} assets")
            async for raw in ws:
                if not self._running:
                    break
                try:
                    envelope = json.loads(raw)
                    msg = envelope.get("data", envelope)
                    symbol = msg.get("s", "").lower()
                    asset = symbol_to_asset.get(symbol)
                    if asset is None:
                        continue
                    price = AssetPrice(
                        asset=asset,
                        price=float(msg["p"]),
                        timestamp=float(msg["T"]) / 1000.0,
                        source="binance",
                    )
                    self._current[asset] = price
                    self._history[asset].append(price)
                except (KeyError, ValueError) as e:
                    logger.debug(f"MultiAssetFeed parse error: {e}")


BinanceFeed = MultiAssetFeed
