"""
src/execution/executor.py
Trade execution — paper mode and live mode.

תיקונים עיקריים:
  1. LiveExecutor — מוסיף _monitor_live_trade עם TP/SL/settlement אמיתי
  2. LiveExecutor — שולף מחיר טוקן אמיתי מ-CLOB כל 5 שניות
  3. LiveExecutor — מנסה לשלוח פקודת SELL לסגירת פוזיציה
  4. PaperExecutor — settlement מבוסס Binance בלבד (הסרת random fallback)
  5. PaperExecutor — TP/SL sensitivity מתוקן ומוסבר
"""

import asyncio
import time
import uuid
from typing import Optional, Callable, Awaitable
from loguru import logger

import httpx

from src.models import Signal, Trade, PolyWindow
from src.risk.manager import RiskManager, RiskStatus
from config.settings import Settings


# ─── Helpers ─────────────────────────────────────────────────────────────────


async def _fetch_binance_price(symbol: str) -> Optional[float]:
    """שולף מחיר נוכחי מ-Binance REST (fallback כשה-feed לא זמין)."""
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": symbol.upper() + "USDT"},
            )
            return float(r.json()["price"])
    except Exception as e:
        logger.warning(f"Binance price fetch failed ({symbol}): {e}")
        return None


async def _fetch_clob_midpoint(token_id: str) -> Optional[float]:
    """שולף מחיר טוקן אמיתי מ-CLOB של Polymarket."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                "https://clob.polymarket.com/midpoint",
                params={"token_id": token_id},
            )
            r.raise_for_status()
            mid = r.json().get("mid")
            if mid is not None:
                return float(mid)
    except Exception as e:
        logger.debug(f"CLOB midpoint fetch failed ({token_id[:10]}...): {e}")
    return None


# ─── Paper Executor ───────────────────────────────────────────────────────────


class PaperExecutor:
    def __init__(
        self,
        settings: Settings,
        risk: RiskManager,
        on_resolve: Optional[Callable[[Trade], Awaitable[None]]] = None,
        binance_feed=None,
    ) -> None:
        self._settings = settings
        self._risk = risk
        self._on_resolve = on_resolve
        self._binance = binance_feed
        self._open_trades: dict[str, Trade] = {}

    @property
    def open_trades(self) -> dict[str, Trade]:
        return self._open_trades

    async def execute(
        self,
        signal: Signal,
        window: PolyWindow,
        risk_status: RiskStatus,
    ) -> Optional[Trade]:
        trade_id = f"paper-{signal.asset}-{uuid.uuid4().hex[:6]}"
        token_id = window.token_id_up if signal.side == "UP" else window.token_id_down
        shares = risk_status.suggested_shares
        size_usd = risk_status.suggested_size_usd
        fee = size_usd * self._settings.taker_fee_pct

        # רשום מחיר כניסה של הנכס (לחישוב TP/SL מאוחר יותר)
        entry_asset_price = 0.0
        if self._binance:
            ap = self._binance.get_price(signal.asset)
            if ap:
                entry_asset_price = ap.price

        # fallback ל-Binance REST אם הfeed לא זמין
        if entry_asset_price <= 0:
            fetched = await _fetch_binance_price(signal.asset)
            if fetched:
                entry_asset_price = fetched

        trade = Trade(
            id=trade_id,
            window_ts=window.window_ts,
            side=signal.side,
            token_id=token_id,
            shares=shares,
            entry_price=signal.token_price,
            entry_time=time.time(),
            size_usd=size_usd,
            mode="paper",
            asset=signal.asset,
            fees_paid=fee,
            entry_asset_price=entry_asset_price,
            notes=(
                f"{signal.asset} delta={signal.btc_delta_pct:+.3f}% "
                f"edge={signal.edge_after_fees:.3f} "
                f"{window.seconds_remaining:.0f}s_left"
            ),
        )

        self._open_trades[trade_id] = trade
        self._risk.record_trade_open(trade)

        payout_pct = (1.0 / signal.token_price - 1.0) * 100
        projected_win = round(shares * (1.0 - signal.token_price) - fee, 2)

        logger.success(
            f"[PAPER] {signal.asset} {signal.side} OPEN | "
            f"${size_usd:.2f} @ {signal.token_price:.3f} | "
            f"WIN: +${projected_win:.2f} (+{payout_pct:.0f}%) | "
            f"{window.seconds_remaining:.0f}s left"
        )

        asyncio.create_task(
            self._monitor_trade(trade, window, entry_asset_price),
            name=f"monitor-{trade_id}",
        )
        return trade

    async def force_resolve(self, trade_id: str) -> bool:
        trade = self._open_trades.get(trade_id)
        if not trade:
            return False
        close_price = await _fetch_binance_price(trade.asset)
        open_price = (
            trade.entry_asset_price
            if trade.entry_asset_price > 0
            else (close_price or 0.0)
        )
        window = PolyWindow(
            window_ts=trade.window_ts,
            close_ts=int(time.time()),
            market_id=f"sim-{trade.asset.lower()}-{trade.window_ts}",
            token_id_up=trade.token_id if trade.side == "UP" else "x",
            token_id_down=trade.token_id if trade.side == "DOWN" else "x",
            open_price=open_price,
            asset=trade.asset,
        )
        await self._settle_at_close(
            trade, window, close_price or 0.0, open_price, reason="FORCE"
        )
        return True

    async def _monitor_trade(
        self, trade: Trade, window: PolyWindow, entry_asset_price: float
    ) -> None:
        """
        בודק כל 2 שניות אם צריך לסגור את העסקה:
          - TP: הנכס זז מספיק לטובתנו
          - SL: הנכס זז נגדנו
          - Time stop: עברו יותר מדי שניות
          - Window close: החלון נסגר — settlement לפי תוצאה אמיתית
        """
        check_interval = 2.0
        tp_gain = self._settings.tp_token_gain  # למשל 0.18
        sl_loss = self._settings.sl_token_loss  # למשל 0.10
        time_stop = self._settings.time_stop_seconds  # למשל 260
        entry_token = trade.entry_price

        while True:
            await asyncio.sleep(check_interval)

            if trade.id not in self._open_trades:
                return  # כבר נסגר

            now = time.time()
            secs_in_trade = now - trade.entry_time
            secs_to_close = window.close_ts - now

            # ── חלון נסגר → settlement אמיתי ──────────────────────────────
            if secs_to_close <= 0:
                await self._resolve_at_settlement(trade, window, reason="WINDOW_CLOSE")
                return

            # ── TP / SL לפי מחיר Binance ─────────────────────────────────
            current_price = None
            if self._binance and entry_asset_price > 0:
                ap = self._binance.get_price(trade.asset)
                if ap and ap.price > 0:
                    current_price = ap.price

            # fallback ל-REST כל 10 שניות אם ה-feed מת
            if current_price is None and entry_asset_price > 0:
                if int(secs_in_trade) % 10 == 0:
                    current_price = await _fetch_binance_price(trade.asset)

            if current_price and entry_asset_price > 0:
                price_move_pct = (current_price - entry_asset_price) / entry_asset_price

                # Sensitivity: גדל ככל שהזמן עובר (Binary outcome מתקרב)
                # מבוסס על הנחה: קרוב לסגירה, תנועה קטנה = שינוי גדול בסיכוי
                # טווח: 8 (בהתחלה) עד 30 (קרוב לסוף)
                # כלומר: 0.1% תנועה בתחילה = +0.8% בטוקן, בסוף = +3% בטוקן
                sensitivity = 8.0 + min(22.0, secs_in_trade * 0.12)

                if trade.side == "UP":
                    our_gain = price_move_pct * sensitivity
                else:
                    our_gain = -price_move_pct * sensitivity

                # ── Take Profit ─────────────────────────────────────────────
                if our_gain >= tp_gain:
                    exit_token = min(0.95, entry_token + our_gain)
                    pnl = round(
                        trade.shares * (exit_token - entry_token) - trade.fees_paid, 4
                    )
                    trade.result = "WIN"
                    trade.exit_price = exit_token
                    trade.pnl_usd = pnl
                    trade.exit_time = now
                    payout_pct = pnl / trade.size_usd * 100 if trade.size_usd > 0 else 0
                    logger.success(
                        f"[TP] {trade.asset} {trade.side} | "
                        f"token {entry_token:.3f}→{exit_token:.3f} | "
                        f"BTC {price_move_pct:+.3%} | pnl=+${pnl:.2f} (+{payout_pct:.0f}%)"
                    )
                    await self._close_trade(trade)
                    return

                # ── Stop Loss (partial exit) ────────────────────────────────
                if our_gain <= -sl_loss:
                    # יוצאים לפי מחיר implied, לא בהפסד מלא
                    exit_token = max(0.03, entry_token + our_gain)
                    recovery = trade.shares * exit_token
                    pnl = round(recovery - trade.size_usd - trade.fees_paid, 4)
                    trade.result = "LOSS"
                    trade.exit_price = exit_token
                    trade.pnl_usd = pnl
                    trade.exit_time = now
                    loss_pct = (
                        abs(pnl) / trade.size_usd * 100 if trade.size_usd > 0 else 0
                    )
                    logger.warning(
                        f"[SL] {trade.asset} {trade.side} | "
                        f"token {entry_token:.3f}→{exit_token:.3f} | "
                        f"BTC {price_move_pct:+.3%} | pnl=${pnl:.2f} (-{loss_pct:.0f}%)"
                    )
                    await self._close_trade(trade)
                    return

            # ── Time Stop ──────────────────────────────────────────────────
            if secs_in_trade >= time_stop:
                logger.info(
                    f"[TIME_STOP] {trade.asset} {trade.id} — {secs_in_trade:.0f}s, settling"
                )
                await self._resolve_at_settlement(trade, window, reason="TIME_STOP")
                return

    async def _resolve_at_settlement(
        self, trade: Trade, window: PolyWindow, reason: str = "SETTLE"
    ) -> None:
        if trade.id not in self._open_trades:
            return
        # שולף מחיר סגירה אמיתי מ-Binance
        close_price = None
        if self._binance:
            ap = self._binance.get_price(trade.asset)
            if ap:
                close_price = ap.price
        if close_price is None:
            close_price = await _fetch_binance_price(trade.asset)

        await self._settle_at_close(
            trade, window, close_price or 0.0, window.open_price, reason=reason
        )

    async def _settle_at_close(
        self,
        trade: Trade,
        window: PolyWindow,
        close_price: float,
        open_price: float,
        reason: str = "SETTLE",
    ) -> None:
        if trade.id not in self._open_trades:
            return

        # אם כבר נסגר ע"י TP/SL — רק מסיר מהרשימה
        if trade.result in ("WIN", "LOSS"):
            await self._close_trade(trade)
            return

        effective_open = open_price if open_price > 0 else trade.entry_asset_price

        if effective_open > 0 and close_price > 0:
            went_up = close_price >= effective_open
            trade_won = (went_up and trade.side == "UP") or (
                not went_up and trade.side == "DOWN"
            )
            delta_pct = (close_price - effective_open) / effective_open * 100
        else:
            # אין מחיר — מניחים תוצאה שמרנית (הפסד)
            # זה מכוון: אם אין נתונים, אסור להניח ניצחון
            trade_won = False
            delta_pct = 0.0
            logger.warning(
                f"[{reason}] No price data for {trade.asset} — assuming LOSS (conservative)"
            )

        if trade_won:
            trade.result = "WIN"
            trade.exit_price = 1.0
            trade.pnl_usd = round(
                trade.shares * (1.0 - trade.entry_price) - trade.fees_paid, 4
            )
        else:
            trade.result = "LOSS"
            trade.exit_price = 0.0
            trade.pnl_usd = round(-trade.size_usd - trade.fees_paid, 4)

        trade.exit_time = time.time()
        icon = "✓ WIN" if trade.result == "WIN" else "✗ LOSS"
        payout_pct = trade.pnl_usd / trade.size_usd * 100 if trade.size_usd > 0 else 0
        logger.info(
            f"[{reason}] {icon} | {trade.asset} {trade.side} | "
            f"Δ={delta_pct:+.3f}% | pnl=${trade.pnl_usd:+.2f} ({payout_pct:+.0f}%)"
        )
        await self._close_trade(trade)

    async def _close_trade(self, trade: Trade) -> None:
        self._risk.record_trade_close(trade)
        self._open_trades.pop(trade.id, None)
        if self._on_resolve:
            await self._on_resolve(trade)


# ─── Live Executor ────────────────────────────────────────────────────────────


class LiveExecutor:
    """
    מבצע עסקאות אמיתיות על Polymarket CLOB.

    תיקונים מרכזיים לעומת הגרסה הקודמת:
      - מפעיל _monitor_live_trade אחרי כל פתיחה
      - מושך מחיר טוקן אמיתי מ-CLOB כל 5 שניות
      - שולח פקודת SELL לסגירה כשTP/SL/settlement מופעל
      - settlement לפי מחיר Binance אמיתי
    """

    def __init__(
        self,
        settings: Settings,
        risk: RiskManager,
        on_resolve: Optional[Callable[[Trade], Awaitable[None]]] = None,
        binance_feed=None,
    ) -> None:
        self._settings = settings
        self._risk = risk
        self._on_resolve = on_resolve
        self._binance = binance_feed
        self._client = None
        self._open_trades: dict[str, Trade] = {}

    @property
    def open_trades(self) -> dict[str, Trade]:
        return self._open_trades

    def _get_client(self):
        if self._client is None:
            try:
                from py_clob_client.client import ClobClient
                from py_clob_client.clob_types import ApiCreds

                self._client = ClobClient(
                    host="https://clob.polymarket.com",
                    chain_id=137,
                    key=self._settings.polymarket_pk,
                    creds=ApiCreds(
                        api_key=self._settings.polymarket_api_key,
                        api_secret=self._settings.polymarket_api_secret,
                        api_passphrase=self._settings.polymarket_api_passphrase,
                    ),
                )
                logger.success("CLOB client initialized")
            except ImportError:
                logger.error("py-clob-client not installed.")
                raise
        return self._client

    async def execute(
        self,
        signal: Signal,
        window: PolyWindow,
        risk_status: RiskStatus,
    ) -> Optional[Trade]:
        client = self._get_client()
        trade_id = f"live-{signal.asset}-{uuid.uuid4().hex[:6]}"
        token_id = window.token_id_up if signal.side == "UP" else window.token_id_down
        limit_price = round(signal.token_price, 2)
        shares = round(risk_status.suggested_shares, 0)
        size_usd = shares * limit_price
        payout_pct = (1.0 / limit_price - 1.0) * 100

        logger.warning(
            f"[LIVE] {signal.asset} {signal.side} | "
            f"shares={shares} @ {limit_price} | ${size_usd:.2f} | "
            f"WIN would pay +{payout_pct:.0f}%"
        )

        # שולף מחיר כניסה של הנכס לצורך monitoring
        entry_asset_price = 0.0
        if self._binance:
            ap = self._binance.get_price(signal.asset)
            if ap:
                entry_asset_price = ap.price
        if entry_asset_price <= 0:
            fetched = await _fetch_binance_price(signal.asset)
            if fetched:
                entry_asset_price = fetched

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            order_args = OrderArgs(
                token_id=token_id,
                price=limit_price,
                size=shares,
                side="BUY",
                order_type=OrderType.GTC,
            )
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None, lambda: client.create_and_post_order(order_args)
            )
            order_id = resp.get("orderID") if isinstance(resp, dict) else str(resp)
            fee = size_usd * self._settings.maker_fee_pct

            trade = Trade(
                id=trade_id,
                window_ts=window.window_ts,
                side=signal.side,
                token_id=token_id,
                shares=shares,
                entry_price=limit_price,
                entry_time=time.time(),
                size_usd=size_usd,
                mode="live",
                asset=signal.asset,
                order_id=order_id,
                fees_paid=fee,
                entry_asset_price=entry_asset_price,
                notes=f"{signal.asset} delta={signal.btc_delta_pct:+.3f}% edge={signal.edge_after_fees:.3f}",
            )
            self._open_trades[trade_id] = trade
            self._risk.record_trade_open(trade)
            logger.success(f"[LIVE] Order placed | {trade_id} | order_id={order_id}")

            # ← תיקון מרכזי: מפעיל monitoring על העסקה החיה
            asyncio.create_task(
                self._monitor_live_trade(trade, window, entry_asset_price),
                name=f"live-monitor-{trade_id}",
            )
            return trade

        except Exception as e:
            logger.error(f"[LIVE] Order failed: {e}")
            return None

    async def _monitor_live_trade(
        self, trade: Trade, window: PolyWindow, entry_asset_price: float
    ) -> None:
        """
        מנטר עסקה חיה כל 5 שניות.
        מושך מחיר טוקן אמיתי מ-CLOB.
        מחליט לסגור לפי TP/SL/settlement.
        """
        check_interval = 5.0  # CLOB rate limit — לא יותר מדי בקשות
        tp_gain = self._settings.tp_token_gain
        sl_loss = self._settings.sl_token_loss
        time_stop = self._settings.time_stop_seconds
        entry_token = trade.entry_price

        logger.info(
            f"[LIVE-MON] Starting monitor for {trade.id} | "
            f"TP=+{tp_gain:.2f} | SL=-{sl_loss:.2f} | stop={time_stop}s"
        )

        while True:
            await asyncio.sleep(check_interval)

            if trade.id not in self._open_trades:
                return

            now = time.time()
            secs_in_trade = now - trade.entry_time
            secs_to_close = window.close_ts - now

            # ── חלון נסגר — settlement ─────────────────────────────────────
            if secs_to_close <= 0:
                logger.info(f"[LIVE-MON] {trade.id} — window closed, settling")
                await self._settle_live(trade, window, reason="WINDOW_CLOSE")
                return

            # ── מחיר טוקן אמיתי מ-CLOB ────────────────────────────────────
            current_token = await _fetch_clob_midpoint(trade.token_id)

            if current_token is not None and current_token > 0:
                token_change = current_token - entry_token

                # מנקודת מבטנו: האם הטוקן שקנינו עלה או ירד?
                our_gain = token_change  # קנינו את הצד הזה

                logger.debug(
                    f"[LIVE-MON] {trade.asset} {trade.side} | "
                    f"token: {entry_token:.3f}→{current_token:.3f} | "
                    f"gain={our_gain:+.3f} | {secs_to_close:.0f}s left"
                )

                # ── Take Profit ─────────────────────────────────────────────
                if our_gain >= tp_gain:
                    logger.success(
                        f"[LIVE-TP] {trade.asset} {trade.side} | "
                        f"token {entry_token:.3f}→{current_token:.3f}"
                    )
                    await self._close_live_position(trade, current_token, "WIN", "TP")
                    return

                # ── Stop Loss ───────────────────────────────────────────────
                if our_gain <= -sl_loss:
                    logger.warning(
                        f"[LIVE-SL] {trade.asset} {trade.side} | "
                        f"token {entry_token:.3f}→{current_token:.3f}"
                    )
                    await self._close_live_position(trade, current_token, "LOSS", "SL")
                    return

            else:
                logger.debug(f"[LIVE-MON] {trade.id} — CLOB price unavailable, waiting")

            # ── Time Stop ──────────────────────────────────────────────────
            if secs_in_trade >= time_stop:
                logger.info(
                    f"[LIVE-MON] {trade.id} — time stop at {secs_in_trade:.0f}s"
                )
                await self._settle_live(trade, window, reason="TIME_STOP")
                return

    async def _close_live_position(
        self,
        trade: Trade,
        exit_token_price: float,
        result: str,
        reason: str,
    ) -> None:
        """
        שולח פקודת SELL על Polymarket CLOB לסגירת פוזיציה.
        אם הSELL נכשל — מתעד ומסמן לסגירה ידנית.
        """
        if trade.id not in self._open_trades:
            return

        client = self._get_client()
        sell_price = round(max(0.02, exit_token_price - 0.01), 2)  # aggressive sell

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            sell_args = OrderArgs(
                token_id=trade.token_id,
                price=sell_price,
                size=trade.shares,
                side="SELL",
                order_type=OrderType.FOK,  # Fill or Kill — לא להשאיר פקודה פתוחה
            )
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None, lambda: client.create_and_post_order(sell_args)
            )
            sell_order_id = resp.get("orderID") if isinstance(resp, dict) else str(resp)
            logger.info(
                f"[LIVE-SELL] {trade.id} | sell_order={sell_order_id} @ {sell_price}"
            )

        except Exception as e:
            logger.error(
                f"[LIVE-SELL] FAILED for {trade.id}: {e}\n"
                f"⚠️  MANUAL ACTION REQUIRED: sell {trade.shares} tokens {trade.token_id[:16]}..."
            )
            # לא מפסיקים — ממשיכים לסגור מבחינת הbookeeping הפנימי

        # עדכון תוצאה
        pnl = round(
            trade.shares * (exit_token_price - trade.entry_price) - trade.fees_paid, 4
        )
        trade.result = result
        trade.exit_price = exit_token_price
        trade.pnl_usd = pnl
        trade.exit_time = time.time()
        payout_pct = pnl / trade.size_usd * 100 if trade.size_usd > 0 else 0
        icon = "✓" if result == "WIN" else "✗"
        logger.info(
            f"[LIVE-{reason}] {icon} {trade.asset} {trade.side} | "
            f"pnl=${pnl:+.2f} ({payout_pct:+.0f}%)"
        )
        await self._close_trade(trade)

    async def _settle_live(
        self, trade: Trade, window: PolyWindow, reason: str = "SETTLE"
    ) -> None:
        """
        Settlement לפי מחיר Binance (כשהחלון נסגר).
        נכס עלה מהפתיחה → UP wins, DOWN loses.
        """
        if trade.id not in self._open_trades:
            return

        close_price = None
        if self._binance:
            ap = self._binance.get_price(trade.asset)
            if ap:
                close_price = ap.price
        if close_price is None:
            close_price = await _fetch_binance_price(trade.asset)

        effective_open = (
            window.open_price if window.open_price > 0 else trade.entry_asset_price
        )

        if effective_open > 0 and close_price and close_price > 0:
            went_up = close_price >= effective_open
            trade_won = (went_up and trade.side == "UP") or (
                not went_up and trade.side == "DOWN"
            )
            delta_pct = (close_price - effective_open) / effective_open * 100
        else:
            # אין נתוני מחיר — הנחה שמרנית: הפסד
            trade_won = False
            delta_pct = 0.0
            logger.warning(
                f"[LIVE-{reason}] No price data for {trade.asset} — assuming LOSS"
            )

        if trade_won:
            trade.result = "WIN"
            trade.exit_price = 1.0
            trade.pnl_usd = round(
                trade.shares * (1.0 - trade.entry_price) - trade.fees_paid, 4
            )
        else:
            trade.result = "LOSS"
            trade.exit_price = 0.0
            trade.pnl_usd = round(-trade.size_usd - trade.fees_paid, 4)

        trade.exit_time = time.time()
        icon = "✓ WIN" if trade.result == "WIN" else "✗ LOSS"
        payout_pct = trade.pnl_usd / trade.size_usd * 100 if trade.size_usd > 0 else 0
        logger.info(
            f"[LIVE-{reason}] {icon} | {trade.asset} {trade.side} | "
            f"Δ={delta_pct:+.3f}% | pnl=${trade.pnl_usd:+.2f} ({payout_pct:+.0f}%)"
        )
        await self._close_trade(trade)

    async def force_resolve(self, trade_id: str) -> bool:
        trade = self._open_trades.get(trade_id)
        if not trade:
            return False
        current_token = await _fetch_clob_midpoint(trade.token_id)
        if current_token:
            result = "WIN" if current_token > trade.entry_price else "LOSS"
            await self._close_live_position(trade, current_token, result, "FORCE")
        else:
            # אין מחיר CLOB — settlement לפי Binance
            window = PolyWindow(
                window_ts=trade.window_ts,
                close_ts=int(time.time()),
                market_id="force",
                token_id_up=trade.token_id if trade.side == "UP" else "x",
                token_id_down=trade.token_id if trade.side == "DOWN" else "x",
                open_price=trade.entry_asset_price,
                asset=trade.asset,
            )
            await self._settle_live(trade, window, reason="FORCE")
        return True

    async def _close_trade(self, trade: Trade) -> None:
        self._risk.record_trade_close(trade)
        self._open_trades.pop(trade.id, None)
        if self._on_resolve:
            await self._on_resolve(trade)


# ─── Factory ──────────────────────────────────────────────────────────────────


def create_executor(
    settings: Settings,
    risk: RiskManager,
    on_resolve: Optional[Callable] = None,
    binance_feed=None,
):
    if settings.trading_mode == "paper":
        logger.info("Executor: PAPER MODE (demo — no real money)")
        return PaperExecutor(
            settings, risk, on_resolve=on_resolve, binance_feed=binance_feed
        )
    else:
        logger.warning("Executor: LIVE MODE — REAL MONEY ACTIVE")
        return LiveExecutor(
            settings, risk, on_resolve=on_resolve, binance_feed=binance_feed
        )
