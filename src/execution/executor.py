"""
src/execution/executor.py
Trade execution — paper mode (simulation) and live mode (real orders).
Multi-asset aware: BTC, ETH, SOL, XRP, MATIC, DOGE, etc.
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
    ) -> None:
        self._settings = settings
        self._risk = risk
        self._on_resolve = on_resolve
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
            notes=f"{signal.asset} delta={signal.btc_delta_pct:+.3f}% edge={signal.edge_after_fees:.3f}",
        )

        self._open_trades[trade_id] = trade
        self._risk.record_trade_open(trade)

        projected_win = round(shares * (1.0 - signal.token_price) - fee, 2)
        projected_loss = round(-size_usd, 2)

        logger.success(
            f"[PAPER] {signal.asset} {signal.side} opened | "
            f"id={trade_id} | size=${size_usd:.2f} | "
            f"if WIN: +${projected_win:.2f} | if LOSS: ${projected_loss:.2f} | "
            f"closes in {window.seconds_remaining:.0f}s"
        )

        asyncio.create_task(self._wait_for_resolution(trade, window))
        return trade

    async def force_resolve(self, trade_id: str) -> bool:
        trade = self._open_trades.get(trade_id)
        if not trade:
            return False
        import httpx
        open_price = 0.0
        close_price = 0.0
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                symbol = trade.asset.upper() + "USDT"
                now_ms = int(time.time() * 1000)
                entry_ms = int(trade.entry_time * 1000)
                r_close = await client.get(
                    "https://api.binance.com/api/v3/ticker/price",
                    params={"symbol": symbol}, timeout=5.0
                )
                close_price = float(r_close.json()["price"])
                r_kline = await client.get(
                    "https://api.binance.com/api/v3/klines",
                    params={"symbol": symbol, "interval": "1m", "startTime": entry_ms, "limit": 1},
                    timeout=5.0
                )
                klines = r_kline.json()
                if klines:
                    open_price = float(klines[0][1])
                else:
                    open_price = close_price
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
            await self._resolve_simulated_with_prices(trade, window, open_price, close_price)
        else:
            await self._resolve_simulated(trade, window)
        return True

    async def _resolve_simulated_with_prices(self, trade: Trade, window: PolyWindow, open_price: float, close_price: float) -> None:
        window.open_price = open_price
        went_up = close_price >= open_price
        trade_won = (went_up and trade.side == "UP") or (not went_up and trade.side == "DOWN")
        trade.result = "WIN" if trade_won else "LOSS"
        trade.exit_price = 1.0 if trade_won else 0.0
        if trade.result == "WIN":
            trade.pnl_usd = round(trade.shares * (1.0 - trade.entry_price) - trade.fees_paid, 4)
        else:
            trade.pnl_usd = round(-trade.size_usd, 4)
        delta_pct = ((close_price - open_price) / open_price * 100) if open_price > 0 else 0
        logger.info(f"[FORCE] {trade.result} | {trade.asset} {trade.side} | open=${open_price:.4f} close=${close_price:.4f} delta={delta_pct:+.3f}% | pnl=${trade.pnl_usd:+.2f}")
        trade.exit_time = time.time()
        self._risk.record_trade_close(trade)
        if trade.id in self._open_trades:
            del self._open_trades[trade.id]
        if self._on_resolve:
            await self._on_resolve(trade)

    async def _wait_for_resolution(self, trade: Trade, window: PolyWindow) -> None:
        wait_seconds = max(5, window.seconds_remaining + 8)
        logger.debug(f"[PAPER] Waiting {wait_seconds:.0f}s for {trade.id}")
        await asyncio.sleep(wait_seconds)
        await self._resolve_trade(trade, window)

    async def _resolve_trade(self, trade: Trade, window: PolyWindow) -> None:
        is_simulated = window.market_id.startswith("sim-")

        if is_simulated:
            await self._resolve_simulated(trade, window)
            return

        import httpx
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://clob.polymarket.com/midpoint",
                    params={"token_id": trade.token_id},
                )
                data = resp.json()
                final_price = float(data.get("mid", 0))
        except Exception as e:
            logger.warning(f"Could not fetch resolution for {trade.id}: {e}")
            final_price = trade.entry_price

        if final_price >= 0.85:
            trade.result = "WIN"
            trade.exit_price = 1.0
            trade.pnl_usd = trade.shares * (1.0 - trade.entry_price) - trade.fees_paid
        elif final_price <= 0.15:
            trade.result = "LOSS"
            trade.exit_price = 0.0
            trade.pnl_usd = -trade.size_usd - trade.fees_paid
        else:
            logger.debug(f"Still resolving {trade.id} (mid={final_price:.3f}), retry in 60s")
            await asyncio.sleep(60)
            await self._resolve_trade(trade, window)
            return

        trade.exit_time = time.time()
        self._risk.record_trade_close(trade)
        if trade.id in self._open_trades:
            del self._open_trades[trade.id]

        result_icon = "✓" if trade.result == "WIN" else "✗"
        logger.info(
            f"[PAPER] {result_icon} {trade.asset} {trade.id} | "
            f"result={trade.result} | pnl=${trade.pnl_usd:+.2f}"
        )
        if self._on_resolve:
            await self._on_resolve(trade)

    async def _resolve_simulated(self, trade: Trade, window: PolyWindow) -> None:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                symbol = trade.asset.lower() + "usdt"
                resp = await client.get(
                    "https://api.binance.com/api/v3/ticker/price",
                    params={"symbol": symbol.upper()},
                    timeout=5.0,
                )
                close_price = float(resp.json()["price"])
        except Exception as e:
            logger.warning(f"Could not fetch close price for {trade.asset}: {e}")
            close_price = window.open_price if window.open_price > 0 else 0

        open_price = window.open_price
        if open_price <= 0 or close_price <= 0:
            trade.result = "WIN" if trade.side == "UP" else "LOSS"
            trade.exit_price = 1.0 if trade.result == "WIN" else 0.0
        else:
            went_up = close_price >= open_price
            trade_won = (went_up and trade.side == "UP") or (not went_up and trade.side == "DOWN")
            trade.result = "WIN" if trade_won else "LOSS"
            trade.exit_price = 1.0 if trade_won else 0.0

        if trade.result == "WIN":
            trade.pnl_usd = round(trade.shares * (1.0 - trade.entry_price) - trade.fees_paid, 4)
        else:
            trade.pnl_usd = round(-trade.size_usd, 4)

        delta_pct = ((close_price - open_price) / open_price * 100) if open_price > 0 else 0
        result_icon = "✓ WIN" if trade.result == "WIN" else "✗ LOSS"
        logger.info(
            f"[SIM] {result_icon} | {trade.asset} {trade.side} | "
            f"open=${open_price:.4f} close=${close_price:.4f} delta={delta_pct:+.3f}% | "
            f"pnl=${trade.pnl_usd:+.2f}"
        )

        trade.exit_time = time.time()
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
    ) -> None:
        self._settings = settings
        self._risk = risk
        self._on_resolve = on_resolve
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


def create_executor(
    settings: Settings,
    risk: RiskManager,
    on_resolve: Optional[Callable] = None,
):
    if settings.trading_mode == "paper":
        logger.info("Executor: PAPER MODE (demo — no real money)")
        return PaperExecutor(settings, risk, on_resolve=on_resolve)
    else:
        logger.warning("Executor: LIVE MODE — REAL MONEY ACTIVE")
        return LiveExecutor(settings, risk, on_resolve=on_resolve)
