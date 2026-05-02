"""
src/execution/executor.py
Trade execution — paper mode (simulation) and live mode (real orders).

Paper mode includes:
  - TP/SL monitoring using live Binance price vs entry price
  - Time stop: exit after N seconds if no resolution
  - Smart resolution at window close
"""

import asyncio
import time
import uuid
from typing import Optional, Callable, Awaitable
from loguru import logger

from src.models import Signal, Trade, PolyWindow
from src.risk.manager import RiskManager, RiskStatus
from config.settings import Settings


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
            notes=(
                f"{signal.asset} delta={signal.btc_delta_pct:+.3f}% "
                f"edge={signal.edge_after_fees:.3f} "
                f"{window.seconds_remaining:.0f}s_left"
            ),
        )

        # Record entry asset price for TP/SL tracking
        entry_asset_price = 0.0
        if self._binance:
            ap = self._binance.get_price(signal.asset)
            if ap:
                entry_asset_price = ap.price

        self._open_trades[trade_id] = trade
        self._risk.record_trade_open(trade)

        projected_win = round(shares * (1.0 - signal.token_price) - fee, 2)
        projected_loss = round(-size_usd, 2)

        logger.success(
            f"[PAPER] {signal.asset} {signal.side} OPEN | "
            f"id={trade_id} | ${size_usd:.2f} | {shares:.1f} shares | "
            f"WIN:+${projected_win:.2f} LOSS:${projected_loss:.2f} | "
            f"{window.seconds_remaining:.0f}s left in window"
        )

        asyncio.create_task(
            self._monitor_trade(trade, window, entry_asset_price),
            name=f"monitor-{trade_id}"
        )
        return trade

    async def force_resolve(self, trade_id: str) -> bool:
        trade = self._open_trades.get(trade_id)
        if not trade:
            return False

        open_price = 0.0
        close_price = 0.0
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                symbol = trade.asset.upper() + "USDT"
                entry_ms = int(trade.entry_time * 1000)
                r_close = await client.get(
                    "https://api.binance.com/api/v3/ticker/price",
                    params={"symbol": symbol},
                )
                close_price = float(r_close.json()["price"])
                r_kline = await client.get(
                    "https://api.binance.com/api/v3/klines",
                    params={"symbol": symbol, "interval": "1m", "startTime": entry_ms, "limit": 1},
                )
                klines = r_kline.json()
                open_price = float(klines[0][1]) if klines else close_price
        except Exception as e:
            logger.warning(f"Force resolve price fetch failed: {e}")
            open_price = close_price if close_price > 0 else 1.0

        window = PolyWindow(
            window_ts=trade.window_ts,
            close_ts=int(time.time()),
            market_id=f"sim-{trade.asset.lower()}-{trade.window_ts}",
            token_id_up=trade.token_id if trade.side == "UP" else "x",
            token_id_down=trade.token_id if trade.side == "DOWN" else "x",
            open_price=open_price,
            asset=trade.asset,
        )
        if close_price > 0:
            await self._resolve_with_prices(trade, window, open_price, close_price, reason="FORCE")
        else:
            await self._resolve_simulated(trade, window, reason="FORCE")
        return True

    async def _monitor_trade(self, trade: Trade, window: PolyWindow, entry_asset_price: float) -> None:
        """
        Monitor open trade for TP/SL conditions and time stop.
        Checks every 2 seconds.
        """
        check_interval = 2.0
        time_stop = self._settings.time_stop_seconds
        tp_gain = self._settings.tp_token_gain
        sl_loss = self._settings.sl_token_loss
        entry_token = trade.entry_price
        start_time = trade.entry_time

        while True:
            await asyncio.sleep(check_interval)

            if trade.id not in self._open_trades:
                return

            now = time.time()
            secs_in_trade = now - start_time
            secs_to_close = window.close_ts - now

            # ── Window closed → resolve at settlement ─────────────────────
            if secs_to_close <= 0:
                await self._resolve_simulated(trade, window, reason="WINDOW_CLOSE")
                return

            # ── TP / SL via token price simulation ───────────────────────
            if self._binance:
                ap = self._binance.get_price(trade.asset)
                if ap and entry_asset_price > 0:
                    price_move_pct = (ap.price - entry_asset_price) / entry_asset_price

                    # Estimate current token price based on Binance move
                    if trade.side == "UP":
                        implied_token = entry_token + price_move_pct * 0.8
                    else:
                        implied_token = entry_token - price_move_pct * 0.8

                    implied_token = max(0.02, min(0.98, implied_token))
                    token_gain = implied_token - entry_token if trade.side == "UP" else entry_token - implied_token

                    # Take Profit
                    if token_gain >= tp_gain:
                        pnl = round(trade.shares * token_gain - trade.fees_paid, 4)
                        trade.result = "WIN"
                        trade.exit_price = implied_token
                        trade.pnl_usd = pnl
                        trade.exit_time = now
                        logger.success(
                            f"[TP] {trade.asset} {trade.side} | "
                            f"token {entry_token:.3f}→{implied_token:.3f} (+{token_gain:.3f}) | "
                            f"pnl=+${pnl:.2f}"
                        )
                        await self._close_trade(trade)
                        return

                    # Stop Loss
                    if token_gain <= -sl_loss:
                        pnl = round(-trade.size_usd * 0.6, 4)
                        trade.result = "LOSS"
                        trade.exit_price = implied_token
                        trade.pnl_usd = pnl
                        trade.exit_time = now
                        logger.warning(
                            f"[SL] {trade.asset} {trade.side} | "
                            f"token {entry_token:.3f}→{implied_token:.3f} ({token_gain:.3f}) | "
                            f"pnl=${pnl:.2f}"
                        )
                        await self._close_trade(trade)
                        return

            # ── Time Stop ────────────────────────────────────────────────
            if secs_in_trade >= time_stop:
                # At time stop: resolve based on actual price movement
                logger.info(f"[TIME_STOP] {trade.asset} {trade.id} — {secs_in_trade:.0f}s elapsed")
                await self._resolve_simulated(trade, window, reason="TIME_STOP")
                return

    async def _resolve_simulated(self, trade: Trade, window: PolyWindow, reason: str = "WINDOW_CLOSE") -> None:
        if trade.id not in self._open_trades:
            return

        import httpx
        close_price = 0.0
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                symbol = trade.asset.upper() + "USDT"
                resp = await client.get(
                    "https://api.binance.com/api/v3/ticker/price",
                    params={"symbol": symbol},
                )
                close_price = float(resp.json()["price"])
        except Exception as e:
            logger.warning(f"Could not fetch close price for {trade.asset}: {e}")

        open_price = window.open_price
        await self._resolve_with_prices(trade, window, open_price, close_price, reason=reason)

    async def _resolve_with_prices(
        self,
        trade: Trade,
        window: PolyWindow,
        open_price: float,
        close_price: float,
        reason: str = "SETTLE",
    ) -> None:
        if trade.id not in self._open_trades:
            return

        if open_price <= 0 or close_price <= 0:
            # Fallback: coin flip weighted by signal
            import random
            trade_won = random.random() < trade.entry_price + 0.05
        else:
            went_up = close_price >= open_price
            trade_won = (went_up and trade.side == "UP") or (not went_up and trade.side == "DOWN")

        if trade.result not in ("WIN", "LOSS"):
            trade.result = "WIN" if trade_won else "LOSS"
            trade.exit_price = 1.0 if trade_won else 0.0
            if trade.result == "WIN":
                trade.pnl_usd = round(trade.shares * (1.0 - trade.entry_price) - trade.fees_paid, 4)
            else:
                trade.pnl_usd = round(-trade.size_usd, 4)

        delta_pct = ((close_price - open_price) / open_price * 100) if open_price > 0 else 0
        icon = "✓ WIN" if trade.result == "WIN" else "✗ LOSS"
        logger.info(
            f"[{reason}] {icon} | {trade.asset} {trade.side} | "
            f"open=${open_price:.4f} close=${close_price:.4f} Δ={delta_pct:+.3f}% | "
            f"pnl=${trade.pnl_usd:+.2f}"
        )

        trade.exit_time = time.time()
        await self._close_trade(trade)

    async def _close_trade(self, trade: Trade) -> None:
        self._risk.record_trade_close(trade)
        if trade.id in self._open_trades:
            del self._open_trades[trade.id]
        if self._on_resolve:
            await self._on_resolve(trade)


class LiveExecutor:
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

        logger.warning(
            f"[LIVE] {signal.asset} {signal.side} | shares={shares} | "
            f"price={limit_price} | ${size_usd:.2f}"
        )

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
                notes=f"{signal.asset} delta={signal.btc_delta_pct:+.3f}% edge={signal.edge_after_fees:.3f}",
            )
            self._open_trades[trade_id] = trade
            self._risk.record_trade_open(trade)
            logger.success(f"[LIVE] Order placed | {trade_id} | order_id={order_id}")
            return trade
        except Exception as e:
            logger.error(f"[LIVE] Order failed: {e}")
            return None

    async def force_resolve(self, trade_id: str) -> bool:
        return False


def create_executor(
    settings: Settings,
    risk: RiskManager,
    on_resolve: Optional[Callable] = None,
    binance_feed=None,
):
    if settings.trading_mode == "paper":
        logger.info("Executor: PAPER MODE (demo — no real money)")
        return PaperExecutor(settings, risk, on_resolve=on_resolve, binance_feed=binance_feed)
    else:
        logger.warning("Executor: LIVE MODE — REAL MONEY ACTIVE")
        return LiveExecutor(settings, risk, on_resolve=on_resolve, binance_feed=binance_feed)
