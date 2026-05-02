"""
src/data/polymarket_feed.py
Multi-asset 5-minute market feed.

LIVE mode: queries Polymarket Gamma + CLOB APIs to find real markets.
DEMO/PAPER mode: generates realistic synthetic 5-minute windows for all
assets using real Binance prices — the bot trades on actual price movements,
resolving each window based on whether the asset closed above or below its open.

This allows full end-to-end paper trading testing in any environment.
"""

import asyncio
import hashlib
import time
from typing import Optional, Dict
import httpx
from loguru import logger

from src.models import PolyWindow, ASSET_KEYWORDS, SUPPORTED_ASSETS


GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"


def _fake_token_id(asset: str, window_ts: int, side: str) -> str:
    """Generate a stable deterministic fake token ID for simulation."""
    key = f"{asset}-{window_ts}-{side}"
    return hashlib.sha256(key.encode()).hexdigest()[:64]


class MultiMarketFeed:
    """
    Discovers and tracks all active 5-minute crypto Up/Down windows.

    In LIVE mode: queries Polymarket APIs.
    In DEMO/PAPER mode: generates synthetic windows from real Binance prices.
    """

    def __init__(self, demo_mode: bool = True) -> None:
        self._windows: Dict[str, PolyWindow] = {}
        self._client = httpx.AsyncClient(timeout=12.0)
        self._running = False
        self._demo_mode = demo_mode
        self._polymarket_ok: Optional[bool] = None
        self._price_feed: Optional[object] = None

    def set_price_feed(self, feed) -> None:
        """Inject the BinanceFeed reference for demo mode."""
        self._price_feed = feed

    @property
    def current_windows(self) -> Dict[str, PolyWindow]:
        now = time.time()
        return {k: v for k, v in self._windows.items() if v.is_active}

    @property
    def current_window(self) -> Optional[PolyWindow]:
        return self._windows.get("BTC")

    def get_window(self, asset: str) -> Optional[PolyWindow]:
        w = self._windows.get(asset)
        return w if (w and w.is_active) else None

    @property
    def is_healthy(self) -> bool:
        return len(self.current_windows) > 0

    async def start(self) -> None:
        self._running = True
        logger.info("MultiMarketFeed starting — checking Polymarket connectivity...")
        await self._check_polymarket_connectivity()

        if self._polymarket_ok:
            logger.info("Polymarket API accessible — using live market data")
            await self._run_live()
        else:
            logger.warning(
                "Polymarket API not accessible from this environment. "
                "Running in SIMULATION mode — real Binance prices, synthetic windows."
            )
            await self._run_demo()

    async def stop(self) -> None:
        self._running = False
        try:
            await self._client.aclose()
        except Exception:
            pass

    async def _check_polymarket_connectivity(self) -> None:
        try:
            resp = await self._client.get(
                f"{GAMMA_API}/markets",
                params={"active": "true", "limit": 1},
                timeout=5.0,
            )
            resp.raise_for_status()
            self._polymarket_ok = True
        except Exception as e:
            logger.debug(f"Polymarket connectivity check failed: {e}")
            self._polymarket_ok = False

    # ─── LIVE MODE (real Polymarket API) ─────────────────────────────────────

    async def _run_live(self) -> None:
        while self._running:
            try:
                await self._refresh_live()
            except Exception as e:
                logger.warning(f"MultiMarketFeed live refresh error: {e}")
            await asyncio.sleep(15)

    async def _refresh_live(self) -> None:
        try:
            resp = await self._client.get(
                f"{GAMMA_API}/markets",
                params={"active": "true", "closed": "false", "tag_slug": "crypto", "limit": 200},
            )
            resp.raise_for_status()
            markets = resp.json()
        except Exception as e:
            logger.error(f"Gamma API error: {e}")
            return

        found: Dict[str, list] = {}
        for market in markets:
            asset = self._classify_market(market)
            if asset:
                found.setdefault(asset, []).append(market)

        now = time.time()
        window_ts = int(now - (now % 300))
        close_ts = window_ts + 300

        for asset, asset_markets in found.items():
            market = asset_markets[0]
            existing = self._windows.get(asset)
            if existing and existing.window_ts == window_ts:
                continue

            tokens = market.get("tokens", [])
            token_up = next(
                (t["token_id"] for t in tokens if t.get("outcome", "").upper() in ("YES", "UP")), None
            )
            token_down = next(
                (t["token_id"] for t in tokens if t.get("outcome", "").upper() in ("NO", "DOWN")), None
            )
            if not token_up or not token_down:
                continue

            self._windows[asset] = PolyWindow(
                window_ts=window_ts,
                close_ts=close_ts,
                market_id=market["id"],
                token_id_up=token_up,
                token_id_down=token_down,
                open_price=self._parse_open_price(market),
                asset=asset,
            )

        if any(self._windows.values()):
            logger.success(f"Active markets: {', '.join(sorted(self.current_windows.keys()))}")

    def _classify_market(self, market: dict) -> Optional[str]:
        question = market.get("question", "").lower()
        slug = market.get("slug", "").lower()
        text = question + " " + slug
        five_min = ["5 min", "5min", "5-min", "next 5", "5 minutes"]
        up_down  = ["up or down", "up/down", "higher or lower", "above or below"]
        if not (any(x in text for x in five_min) and any(x in text for x in up_down)):
            return None
        for asset, keywords in ASSET_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                return asset
        return None

    def _parse_open_price(self, market: dict) -> float:
        for field in ["initialAnswer", "startPrice", "benchmarkPrice", "openPrice"]:
            val = market.get(field)
            if val:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
        return 0.0

    # ─── DEMO / SIMULATION MODE ───────────────────────────────────────────────

    async def _run_demo(self) -> None:
        logger.info("Demo market simulation running — 5-min windows for all 8 assets")
        # Wait for Binance to connect and have real prices before recording open_price
        for _ in range(30):
            if self._price_feed and self._price_feed.get_price("BTC"):
                break
            await asyncio.sleep(0.5)
        while self._running:
            try:
                await self._refresh_demo()
            except Exception as e:
                logger.warning(f"Demo refresh error: {e}")
            await asyncio.sleep(10)

    async def _refresh_demo(self) -> None:
        now = time.time()
        window_ts = int(now - (now % 300))
        close_ts  = window_ts + 300

        updated = []
        for asset in SUPPORTED_ASSETS:
            existing = self._windows.get(asset)
            if existing and existing.window_ts == window_ts:
                continue

            open_price = self._get_open_price(asset)
            token_up   = _fake_token_id(asset, window_ts, "UP")
            token_down = _fake_token_id(asset, window_ts, "DOWN")

            self._windows[asset] = PolyWindow(
                window_ts=window_ts,
                close_ts=close_ts,
                market_id=f"sim-{asset.lower()}-{window_ts}",
                token_id_up=token_up,
                token_id_down=token_down,
                open_price=open_price,
                asset=asset,
            )
            updated.append(asset)

        if updated:
            logger.success(
                f"New simulation windows: {', '.join(updated)} | "
                f"close in {int(close_ts - now)}s"
            )

    def _get_open_price(self, asset: str) -> float:
        if self._price_feed:
            p = self._price_feed.get_price(asset)
            if p:
                return p.price
        return 0.0

    # ─── TOKEN PRICE SIMULATION ───────────────────────────────────────────────

    async def get_token_price(self, token_id: str) -> Optional[float]:
        """
        Return a synthetic token price for demo mode, or real CLOB price for live.
        For demo, simulate a market that's somewhat efficient but not perfect.
        A 50/50 market would be at 0.50; momentum pushes it slightly.
        """
        if self._polymarket_ok:
            return await self._fetch_clob_price(token_id)
        return await self._simulated_token_price(token_id)

    async def _fetch_clob_price(self, token_id: str) -> Optional[float]:
        try:
            resp = await self._client.get(
                f"{CLOB_API}/midpoint",
                params={"token_id": token_id},
            )
            resp.raise_for_status()
            return float(resp.json().get("mid", 0))
        except Exception as e:
            logger.debug(f"CLOB price error ({token_id[:8]}...): {e}")
            return None

    async def _simulated_token_price(self, token_id: str) -> Optional[float]:
        """
        Simulate a token price that reflects the LAG between Binance and Polymarket.

        Key insight: The market prices in the LONGER-TERM trend but NOT the very
        recent short-term move. Our signal detects the recent move BEFORE the
        market reprices — that is the edge we exploit.

        So: token price = f(long-term trend) + window_noise
                        ≠ f(recent 20s spike our signal detected)

        This creates tokens in the 0.28–0.60 range where good risk/reward exists.
        """
        window_info = self._find_window_by_token(token_id)
        if window_info is None:
            return 0.45

        asset, window, side = window_info
        if self._price_feed is None:
            return 0.45

        # Use LONG-TERM delta (120s) = what the market has already priced in
        # The recent 20-30s spike is OUR edge — market hasn't caught up
        delta_long = self._price_feed.get_delta_pct(lookback_seconds=120.0, asset=asset)

        base = 0.50

        if delta_long is not None:
            # Market slowly prices in the 2-minute trend
            consensus_move = min(abs(delta_long) * 1.2, 0.12)
            if (delta_long > 0 and side == "UP") or (delta_long < 0 and side == "DOWN"):
                base = 0.50 + consensus_move
            else:
                base = 0.50 - consensus_move

        # Window-specific noise: each window starts with a different market bias
        # This simulates natural variation in market maker pricing
        import hashlib
        seed_val = int(hashlib.md5(f"{asset}{window.window_ts}{side}".encode()).hexdigest()[:8], 16)
        noise_range = 0.14
        noise = (seed_val % 1000) / 1000.0 * noise_range - (noise_range / 2)
        price = base + noise

        # Realistic Polymarket price bounds (not too close to 0 or 1)
        return round(max(0.10, min(0.78, price)), 3)

    def _find_window_by_token(self, token_id: str):
        for asset, window in self._windows.items():
            if window.token_id_up == token_id:
                return asset, window, "UP"
            if window.token_id_down == token_id:
                return asset, window, "DOWN"
        return None

    async def get_order_book(self, token_id: str) -> Optional[dict]:
        if self._polymarket_ok:
            try:
                resp = await self._client.get(f"{CLOB_API}/book", params={"token_id": token_id})
                resp.raise_for_status()
                return resp.json()
            except Exception:
                return None
        return None


PolymarketFeed = MultiMarketFeed
