"""
src/execution/executor.py
Trade execution — paper mode (simulation) and live mode (real orders).
Multi-asset aware: BTC, ETH, SOL, XRP, MATIC, DOGE, etc.
"""

import asyncio
import time
import uuid
from typing import Optional
from loguru import logger

from src.models import Signal, Trade, PolyWindow
from src.risk.manager import RiskManager, RiskStatus
from config.settings import Settings


class PaperExecutor:
    def __init__(self, settings: Settings, risk: RiskManager) -> None:
        self._settings = settings
        self._risk = risk
        self._open_trades: dict[str, Trade] = {}

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

        logger.success(
            f"[PAPER] {signal.asset} {signal.side} opened | "
            f"id={trade_id} | shares={shares:.1f} | price={signal.token_price:.3f} | "
            f"${size_usd:.2f} | closes in {window.seconds_remaining:.0f}s"
        )

        asyncio.create_task(self._wait_for_resolution(trade, window))
        return trade

    async def _wait_for_resolution(self, trade: Trade, window: PolyWindow) -> None:
        wait_seconds = max(0, window.seconds_remaining + 10)
        logger.debug(f"[PAPER] Waiting {wait_seconds:.0f}s for {trade.id}")
        await asyncio.sleep(wait_seconds)
        await self._resolve_trade(trade, window)

    async def _resolve_trade(self, trade: Trade, window: PolyWindow) -> None:
        """
        Resolve a paper trade. For simulated markets, uses the actual Binance
        closing price to determine outcome. For real markets, fetches CLOB price.
        """
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

    async def _resolve_simulated(self, trade: Trade, window: PolyWindow) -> None:
        """
        Simulate resolution using real Binance closing price vs open price.
        If asset went UP vs open → UP wins, DOWN loses. Vice versa.
        """
        from src.data.binance_feed import MultiAssetFeed
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                symbol = trade.asset.lower() + "usdt"
                resp = await client.get(
                    f"https://api.binance.com/api/v3/ticker/price",
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
            trade.pnl_usd = trade.shares * (1.0 - trade.entry_price) - trade.fees_paid
        else:
            trade.pnl_usd = -trade.size_usd - trade.fees_paid

        delta_pct = ((close_price - open_price) / open_price * 100) if open_price > 0 else 0
        logger.info(
            f"[SIM] {trade.asset} open=${open_price:.2f} close=${close_price:.2f} "
            f"delta={delta_pct:+.3f}% → {trade.result}"
        )

        trade.exit_time = time.time()
        self._risk.record_trade_close(trade)
        if trade.id in self._open_trades:
            del self._open_trades[trade.id]

        result_icon = "✓" if trade.result == "WIN" else "✗"
        logger.info(
            f"[PAPER] {result_icon} {trade.asset} {trade.id} | "
            f"result={trade.result} | pnl=${trade.pnl_usd:+.2f}"
        )


class LiveExecutor:
    def __init__(self, settings: Settings, risk: RiskManager) -> None:
        self._settings = settings
        self._risk = risk
        self._client = None
        self._open_trades: dict[str, Trade] = {}

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


def create_executor(settings: Settings, risk: RiskManager):
    if settings.trading_mode == "paper":
        logger.info("Executor: PAPER MODE (demo — no real money)")
        return PaperExecutor(settings, risk)
    else:
        logger.warning("Executor: LIVE MODE — REAL MONEY ACTIVE")
        return LiveExecutor(settings, risk)
