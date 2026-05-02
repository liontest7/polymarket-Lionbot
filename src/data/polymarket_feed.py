"""
src/data/polymarket_feed.py
Multi-asset 5-minute market feed.

תיקונים עיקריים לעומת הגרסה הקודמת:
  1. get_token_price — ב-paper mode מנסה CLOB אמיתי תחילה (read-only, ללא מפתחות)
  2. _simulated_token_price — שמרני יותר, פחות "ידידותי" לסיגנלים
  3. הוסר noise קבוע לפי window_ts (היה אותם מחירים בכל ריצה)
  4. הוסף _clob_price_cache — לא לפנות ל-CLOB יותר מפעם בשנייה לכל טוקן
  5. DEMO mode כעת מאמת תוצאה לפי Binance אמיתי (לא random)
"""

import asyncio
import hashlib
import random
import time
from typing import Optional, Dict, Tuple
import httpx
from loguru import logger

from src.models import PolyWindow, ASSET_KEYWORDS, SUPPORTED_ASSETS


GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Cache TTL למחיר CLOB (שניות)
CLOB_CACHE_TTL = 3.0


def _fake_token_id(asset: str, window_ts: int, side: str) -> str:
    """Fake token ID דטרמיניסטי לסימולציה."""
    key = f"{asset}-{window_ts}-{side}"
    return hashlib.sha256(key.encode()).hexdigest()[:64]


class MultiMarketFeed:
    """
    מגלה ועוקב אחרי כל חלונות 5 דקות הפעילים.

    LIVE mode: שואל Polymarket APIs.
    PAPER mode: מייצר חלונות synthetic, מנסה CLOB לקבלת מחירים,
                ונופל ל-simulation רק אם CLOB לא נגיש.
    """

    def __init__(self, demo_mode: bool = True) -> None:
        self._windows: Dict[str, PolyWindow] = {}
        self._client = httpx.AsyncClient(timeout=12.0)
        self._running = False
        self._demo_mode = demo_mode
        self._polymarket_ok: Optional[bool] = None
        self._price_feed: Optional[object] = None
        # Cache: token_id → (price, timestamp)
        self._clob_price_cache: Dict[str, Tuple[float, float]] = {}

    def set_price_feed(self, feed) -> None:
        """מחבר את ה-BinanceFeed לשימוש בsimulation."""
        self._price_feed = feed

    @property
    def current_windows(self) -> Dict[str, PolyWindow]:
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
                "Polymarket API not accessible. "
                "Paper mode: synthetic windows + Binance prices for settlement."
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

    # ─── LIVE MODE ────────────────────────────────────────────────────────────

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
                params={
                    "active": "true",
                    "closed": "false",
                    "tag_slug": "crypto",
                    "limit": 200,
                },
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
                (
                    t["token_id"]
                    for t in tokens
                    if t.get("outcome", "").upper() in ("YES", "UP")
                ),
                None,
            )
            token_down = next(
                (
                    t["token_id"]
                    for t in tokens
                    if t.get("outcome", "").upper() in ("NO", "DOWN")
                ),
                None,
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
            logger.success(
                f"Active markets: {', '.join(sorted(self.current_windows.keys()))}"
            )

    def _classify_market(self, market: dict) -> Optional[str]:
        question = market.get("question", "").lower()
        slug = market.get("slug", "").lower()
        text = question + " " + slug
        five_min = ["5 min", "5min", "5-min", "next 5", "5 minutes"]
        up_down = ["up or down", "up/down", "higher or lower", "above or below"]
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

    # ─── DEMO / PAPER MODE ────────────────────────────────────────────────────

    async def _run_demo(self) -> None:
        logger.info("Paper market simulation — 5-min windows for all assets")
        # ממתין למחירי Binance אמיתיים לפני תחילת העבודה
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
        close_ts = window_ts + 300

        updated = []
        for asset in SUPPORTED_ASSETS:
            existing = self._windows.get(asset)
            if existing and existing.window_ts == window_ts:
                continue

            open_price = self._get_open_price(asset)
            token_up = _fake_token_id(asset, window_ts, "UP")
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

    # ─── TOKEN PRICE ─────────────────────────────────────────────────────────

    async def get_token_price(self, token_id: str) -> Optional[float]:
        """
        מחיר טוקן:
          - LIVE mode:  CLOB midpoint אמיתי
          - PAPER mode: מנסה CLOB תחילה (read-only, ללא מפתחות),
                        נופל לsimulation אם CLOB לא נגיש
        """
        if self._polymarket_ok:
            return await self._fetch_clob_price(token_id)

        # Paper mode — מנסה CLOB אמיתי תחילה (קריאה בלבד, ללא auth)
        clob_price = await self._try_clob_readonly(token_id)
        if clob_price is not None:
            return clob_price

        # Fallback — simulation
        return await self._simulated_token_price(token_id)

    async def _fetch_clob_price(self, token_id: str) -> Optional[float]:
        """CLOB midpoint עם cache."""
        # בדיקת cache
        cached = self._clob_price_cache.get(token_id)
        if cached:
            price, ts = cached
            if time.time() - ts < CLOB_CACHE_TTL:
                return price

        try:
            resp = await self._client.get(
                f"{CLOB_API}/midpoint",
                params={"token_id": token_id},
            )
            resp.raise_for_status()
            mid = resp.json().get("mid")
            if mid is not None:
                price = float(mid)
                self._clob_price_cache[token_id] = (price, time.time())
                return price
        except Exception as e:
            logger.debug(f"CLOB price error ({token_id[:8]}...): {e}")
        return None

    async def _try_clob_readonly(self, token_id: str) -> Optional[float]:
        """
        מנסה לשאול את CLOB ב-paper mode (ללא authentication).
        חלק מהendpoints של Polymarket נגישים read-only.
        """
        # בדיקת cache
        cached = self._clob_price_cache.get(token_id)
        if cached:
            price, ts = cached
            if time.time() - ts < CLOB_CACHE_TTL:
                return price

        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(
                    f"{CLOB_API}/midpoint",
                    params={"token_id": token_id},
                )
                if resp.status_code == 200:
                    mid = resp.json().get("mid")
                    if mid is not None:
                        price = float(mid)
                        self._clob_price_cache[token_id] = (price, time.time())
                        logger.debug(
                            f"CLOB readonly price: {token_id[:10]}... → {price:.3f}"
                        )
                        return price
        except Exception:
            pass
        return None

    async def _simulated_token_price(self, token_id: str) -> Optional[float]:
        """
        מחיר מדומה לpaper mode כשCLOB לא נגיש.

        תיקון מרכזי לעומת הגרסה הקודמת:
          - noise אקראי בכל קריאה (לא קבוע לפי window_ts)
          - שמרני יותר: range צר יותר (0.35–0.65 במקום 0.10–0.78)
          - הטיה קטנה בלבד לפי הtrend הארוך (120s)
          - מטרה: לא "לעזור" לסיגנל לעבור threshold

        ⚠️  הסימולציה הזו לא מחליפה בדיקה על CLOB אמיתי!
            אם הבוט מראה תוצאות טובות בsimulation אבל רעות בlive —
            הסיבה היא שמחירי הsimulation לא מייצגים שוק אמיתי.
        """
        window_info = self._find_window_by_token(token_id)
        if window_info is None:
            return 0.50  # ברירת מחדל: שוק מאוזן

        asset, window, side = window_info
        if self._price_feed is None:
            return 0.50

        # Trend ארוך (120s) — מה השוק כבר מתמחר
        delta_long = self._price_feed.get_delta_pct(lookback_seconds=120.0, asset=asset)

        base = 0.50

        if delta_long is not None:
            # הטיה קטנה לפי trend (מקסימום ±0.08)
            consensus = min(abs(delta_long) * 0.8, 0.08)
            if (delta_long > 0 and side == "UP") or (delta_long < 0 and side == "DOWN"):
                base = 0.50 + consensus
            else:
                base = 0.50 - consensus

        # noise אקראי בכל קריאה — לא קבוע!
        # טווח: ±0.06 (לא ±0.07 כבעבר)
        noise = (random.random() - 0.5) * 0.12

        price = base + noise

        # bounds שמרניים: לא פחות מ-0.35 ולא יותר מ-0.65
        # (market שחושב <35% או >65% בחלון 5 דקות בלבד הוא נדיר)
        return round(max(0.35, min(0.65, price)), 3)

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
                resp = await self._client.get(
                    f"{CLOB_API}/book", params={"token_id": token_id}
                )
                resp.raise_for_status()
                return resp.json()
            except Exception:
                return None
        return None


PolymarketFeed = MultiMarketFeed
