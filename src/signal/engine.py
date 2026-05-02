"""
src/signal/engine.py
Multi-asset signal engine — evaluates ALL active 5-min markets.

תיקונים עיקריים לעומת הגרסה הקודמת:
  1. DELTA_TO_WIN_PROB — הוספת הערה שזו טבלה שצריך לאמת empirically
  2. הסרת time_bonus (לא מוסבר, מגדיל win_prob חינם)
  3. direction_consistent bonus הופחת מ-6% ל-3% (שמרני יותר)
  4. threshold לעסקה הוחמר: win_prob > token_price + 0.10 (היה 0.08)
  5. הוספת velocity_sanity_check — דוחה spikes רועשים מדי
  6. הוספת max_win_prob_cap — לא מעל 0.88 (היה 0.95, לא ריאלי)
  7. הוסף לוג מפורט על כל asset שנדחה ומדוע
"""

import time
from typing import Optional, List, Dict
from loguru import logger

from src.models import Signal, PolyWindow, Side, SUPPORTED_ASSETS
from src.data.binance_feed import MultiAssetFeed
from src.data.polymarket_feed import MultiMarketFeed
from config.settings import Settings


# ─── טבלת delta → win probability ────────────────────────────────────────────
#
# ⚠️  חשוב: הטבלה הזו היא ESTIMATE בלבד!
#     היא מבוססת על ההנחה שתנועה חדה ב-20s מנבאת את כיוון הסגירה.
#     לאמת empirically: הרץ backtesting על נתוני Binance היסטוריים:
#       לכל 5-min window, בדוק: האם delta_20s > X% חזה נכון את סיום החלון?
#     עד שתאמת — השתמש בטבלה הזו בזהירות עם הון קטן.
#
# מה שיודעים:
#   - delta קטן (<0.02%) = שוק רועש, לא אמין
#   - delta גדול (>0.20%) = נדיר מאוד, עלול להיות spike שמתהפך
#   - הטווח הטוב: 0.03%-0.10%
#
DELTA_TO_WIN_PROB = [
    (0.01, 0.51),  # delta 0.01% → 51% — כמעט כלום
    (0.02, 0.53),  # delta 0.02% → 53%
    (0.03, 0.56),  # delta 0.03% → 56%
    (0.05, 0.61),  # delta 0.05% → 61%
    (0.07, 0.66),  # delta 0.07% → 66%
    (0.10, 0.71),  # delta 0.10% → 71%
    (0.15, 0.76),  # delta 0.15% → 76%  ← הפחתה מ-83% לשמרנות
    (0.20, 0.80),  # delta 0.20% → 80%  ← הפחתה מ-89%
    (0.30, 0.84),  # delta 0.30% → 84%  ← הפחתה מ-94% — spike עלול להתהפך
]

# רמת סיכון נוספת לפי נכס (נכסים volatile יותר = פחות ניתן לחזות)
ASSET_VOLATILITY = {
    "BTC": 1.0,  # benchmark
    "ETH": 1.1,
    "SOL": 1.4,  # volatile מאוד
    "XRP": 1.3,
    "MATIC": 1.35,
    "DOGE": 1.45,  # הכי volatile — קשה לחזות
    "LINK": 1.2,
    "AVAX": 1.25,
}

# ── גבולות סיכון ──────────────────────────────────────────────────────────────
MAX_WIN_PROB = 0.88  # לא להאמין ל-95%+ — לא ריאלי בshort-term
MIN_EDGE_HARD = 0.03  # עוגן — edge מינימלי אבסולוטי
EDGE_OVER_MARKET = 0.10  # win_prob חייבת להיות לפחות 10% מעל מחיר הטוקן
MAX_SPIKE_FILTER = 0.50  # delta מעל 0.5% = spike חשוד, לא edge


def estimate_win_probability(delta_abs: float, asset: str = "BTC") -> float:
    """
    ממפה delta% → הסתברות ניצחון, מותאמת לvolatility של הנכס.
    נכס volatile יותר → אותו delta = פחות ודאות.
    """
    vol = ASSET_VOLATILITY.get(asset, 1.0)
    adj = delta_abs / vol  # נרמל ל-BTC

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

    return 0.50


def payout_multiplier(token_price: float) -> float:
    """
    כמה מכפיל הרווח אם נצח.
    token=0.30 → מכפיל 3.33 (רווח 233%)
    token=0.50 → מכפיל 2.0  (רווח 100%)
    """
    return 1.0 / token_price if token_price > 0 else 1.0


class SignalEngine:
    """
    סורק את כל השווקים הפעילים כל שנייה.
    מעדיף טוקנים זולים (payout גבוה) עם edge ברור.
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
        self._skip_count: Dict[str, int] = {}  # לדיבוג — כמה פעמים נדחה כל asset

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

        # דרוג: edge × confidence × payout_multiplier
        # = עדיפות לעסקאות עם edge גבוה, סיכוי גבוה, ותשלום גבוה
        candidates.sort(
            key=lambda s: s.edge_after_fees
            * s.confidence
            * payout_multiplier(s.token_price),
            reverse=True,
        )
        best = candidates[0]

        payout_pct = (payout_multiplier(best.token_price) - 1.0) * 100
        logger.success(
            f"✦ SIGNAL → {best.asset} {best.side} | "
            f"token={best.token_price:.3f} | payout=+{payout_pct:.0f}% | "
            f"edge={best.edge_after_fees:.3f} | conf={best.confidence:.2f}"
        )
        return best

    async def _evaluate_asset(self, asset: str, window: PolyWindow) -> Optional[Signal]:
        window_key = f"{asset}:{window.window_ts}"

        # ── חלון כבר נסחר ─────────────────────────────────────────────────
        if self._traded_windows.get(window_key):
            return None

        secs_remaining = window.seconds_remaining

        # ── זמן ─────────────────────────────────────────────────────────────
        if secs_remaining < self._settings.min_seconds_remaining:
            return None
        if secs_remaining > 300:
            return None

        # ── cooldown ────────────────────────────────────────────────────────
        if self.is_in_cooldown(asset):
            return None

        # ── בריאות feed ────────────────────────────────────────────────────
        if not self._binance.is_asset_healthy(asset):
            return None

        # ── delta מרובה timeframes ──────────────────────────────────────────
        delta_20s = self._binance.get_delta_pct(lookback_seconds=20.0, asset=asset)
        delta_60s = self._binance.get_delta_pct(lookback_seconds=60.0, asset=asset)
        delta_120s = self._binance.get_delta_pct(lookback_seconds=120.0, asset=asset)

        if delta_20s is None:
            return None

        delta_abs = abs(delta_20s)

        # ── סף מינימלי ──────────────────────────────────────────────────────
        if delta_abs < self._settings.min_btc_delta_pct:
            return None

        # ── Spike filter: delta גבוה מדי = חשוד, עלול להתהפך ─────────────
        if delta_abs > MAX_SPIKE_FILTER:
            logger.debug(
                f"{asset}: delta={delta_abs:.3f}% > {MAX_SPIKE_FILTER}% — "
                f"spike too large, likely reversal risk, skip"
            )
            return None

        # ── Velocity filter ─────────────────────────────────────────────────
        velocity = delta_abs / 20.0
        if velocity < self._settings.min_velocity_pct_per_sec:
            return None

        # ── עקביות כיוון בין timeframes ────────────────────────────────────
        direction_consistent = False
        also_60s_consistent = False

        if delta_60s is not None:
            direction_consistent = (delta_20s > 0 and delta_60s > 0) or (
                delta_20s < 0 and delta_60s < 0
            )
        if delta_120s is not None:
            also_60s_consistent = (delta_20s > 0 and delta_120s > 0) or (
                delta_20s < 0 and delta_120s < 0
            )

        # ── צד ──────────────────────────────────────────────────────────────
        side: Side = "UP" if delta_20s > 0 else "DOWN"
        token_id = window.token_id_up if side == "UP" else window.token_id_down

        token_price = await self._polymarket.get_token_price(token_id)
        if token_price is None or token_price <= 0:
            return None

        # ── פילטר מחיר טוקן ─────────────────────────────────────────────────
        if token_price > self._settings.max_token_price:
            logger.debug(
                f"{asset}: token={token_price:.3f} > max {self._settings.max_token_price} — "
                f"payout too low ({(1 / token_price - 1) * 100:.0f}%), skip"
            )
            return None

        if token_price < self._settings.min_token_price:
            logger.debug(
                f"{asset}: token={token_price:.3f} < min {self._settings.min_token_price} — "
                f"suspiciously cheap, skip"
            )
            return None

        # ── הסתברות ניצחון ───────────────────────────────────────────────────
        win_prob = estimate_win_probability(delta_abs, asset)

        # Bonus קטן לעקביות כיוון ב-60s (3% בלבד, לא 6%)
        if direction_consistent:
            win_prob = min(MAX_WIN_PROB, win_prob * 1.03)

        # Bonus נוסף קטן אם גם 120s מסכים (אות חזק יותר)
        if also_60s_consistent:
            win_prob = min(MAX_WIN_PROB, win_prob * 1.02)

        # ← הסרנו את time_bonus — לא הגיוני לתת bonus רק כי נכנסנו מוקדם

        # Cap מקסימלי — לא להאמין ל-95%+ בשוק אמיתי
        win_prob = min(win_prob, MAX_WIN_PROB)

        # ── בדיקת edge מול השוק ─────────────────────────────────────────────
        # token_price = מה השוק מאמין שהסיכוי הוא
        # win_prob    = מה אנחנו מאמינים
        # אנחנו נסחר רק כשיש הפרש משמעותי (EDGE_OVER_MARKET)
        if win_prob <= token_price + EDGE_OVER_MARKET:
            logger.debug(
                f"{asset}: win_prob={win_prob:.2f} not enough above "
                f"token={token_price:.2f} (need +{EDGE_OVER_MARKET}) — skip"
            )
            return None

        # ── חישוב edge אמיתי ────────────────────────────────────────────────
        # edge = (P(win) × $1) - cost - fees
        fee = self._settings.taker_fee_pct * token_price
        edge = win_prob * 1.0 - token_price - fee

        payout_pct = (payout_multiplier(token_price) - 1.0) * 100
        logger.info(
            f"{asset} eval: {side} | "
            f"Δ20s={delta_20s:+.3f}% Δ60s={delta_60s:+.3f}% | "
            f"vel={velocity:.5f}%/s | consistent={direction_consistent}/{also_60s_consistent} | "
            f"token={token_price:.3f} (+{payout_pct:.0f}% if WIN) | "
            f"win_prob={win_prob:.2f} | edge={edge:.3f} | {secs_remaining:.0f}s left"
        )

        # ── edge מינימלי ────────────────────────────────────────────────────
        min_edge = max(self._settings.min_edge_after_fees, MIN_EDGE_HARD)
        if edge < min_edge:
            logger.debug(f"{asset}: edge={edge:.3f} < min={min_edge:.3f} — skip")
            return None

        # ── בניית הסיגנל ────────────────────────────────────────────────────
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
