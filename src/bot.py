"""
src/bot.py
Main bot orchestrator — multi-asset, start/stop controllable.
"""

import asyncio
import signal as os_signal
import time
from loguru import logger

from src.data.binance_feed import MultiAssetFeed
from src.data.polymarket_feed import MultiMarketFeed
from src.signal.engine import SignalEngine
from src.risk.manager import RiskManager
from src.execution.executor import create_executor
from src.api.server import update_from_bot, broadcast_state
from config.settings import Settings


class PolyBot:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._running = False
        self._binance = MultiAssetFeed()
        self._polymarket = MultiMarketFeed(demo_mode=settings.trading_mode == "paper")
        self._polymarket.set_price_feed(self._binance)
        self._risk = RiskManager(settings)
        self._signal_engine = SignalEngine(self._binance, self._polymarket, settings)
        self._executor = create_executor(
            settings,
            self._risk,
            on_resolve=self._on_trade_resolved,
            binance_feed=self._binance,
        )
        self._tasks: list[asyncio.Task] = []

    async def _on_trade_resolved(self, trade) -> None:
        update_from_bot(
            risk=self._risk,
            settings=self._settings,
            new_trade=trade,
            running=self._running,
            open_trades=self._get_open_trades_list(),
        )
        await broadcast_state()

    def _get_open_trades_list(self) -> list:
        result = []
        for trade in self._executor.open_trades.values():
            asset_price = self._binance.get_price(trade.asset)
            current_price = asset_price.price if asset_price else None
            result.append({
                "id": trade.id,
                "asset": trade.asset,
                "side": trade.side,
                "entry_price": trade.entry_price,
                "size_usd": trade.size_usd,
                "shares": trade.shares,
                "fees_paid": trade.fees_paid,
                "entry_time": trade.entry_time,
                "mode": trade.mode,
                "notes": trade.notes,
                "window_ts": trade.window_ts,
                "current_asset_price": current_price,
            })
        return result

    async def force_resolve_trade(self, trade_id: str) -> bool:
        return await self._executor.force_resolve(trade_id)

    async def run(self) -> None:
        self._running = True
        logger.info("=" * 60)
        logger.info("POLYMARKET MULTI-ASSET BOT  v4.1 PRO")
        logger.info(f"Mode: {self._settings.trading_mode.upper()}")
        logger.info(f"Capital: ${self._settings.capital_usd}")
        logger.info(f"Entry: ANY TIME with valid signal (velocity + edge)")
        logger.info(f"TP: +{self._settings.tp_token_gain:.2f} | SL: -{self._settings.sl_token_loss:.2f} | TimeStop: {self._settings.time_stop_seconds:.0f}s")
        logger.info("Assets: BTC, ETH, SOL, XRP, MATIC, DOGE, LINK, AVAX")
        logger.info("=" * 60)

        try:
            for sig in (os_signal.SIGINT, os_signal.SIGTERM):
                asyncio.get_event_loop().add_signal_handler(
                    sig, lambda: asyncio.create_task(self.stop())
                )
        except (NotImplementedError, RuntimeError, ValueError):
            pass

        feed_tasks = [
            asyncio.create_task(self._binance.start(), name="binance_feed"),
            asyncio.create_task(self._polymarket.start(), name="polymarket_feed"),
        ]
        self._tasks = feed_tasks

        logger.info("Waiting for data feeds to initialize (5s)...")
        await asyncio.sleep(5)

        eval_task = asyncio.create_task(self._evaluation_loop(), name="eval_loop")
        self._tasks.append(eval_task)

        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            logger.info("Bot shutdown requested")

    async def _evaluation_loop(self) -> None:
        logger.info("Evaluation loop started — scanning all 8 assets every second")
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"Evaluation loop error: {e}", exc_info=True)
            await asyncio.sleep(1.0)

    async def _tick(self) -> None:
        windows = self._polymarket.current_windows
        btc_price = self._binance.get_price("BTC")
        delta = self._binance.get_delta_pct(20.0, "BTC")

        asset_prices = {}
        for asset in ["BTC", "ETH", "SOL", "XRP", "MATIC", "DOGE", "LINK", "AVAX"]:
            ap = self._binance.get_price(asset)
            if ap:
                asset_prices[asset] = ap.price

        open_trades = self._get_open_trades_list()

        update_from_bot(
            risk=self._risk,
            settings=self._settings,
            btc=btc_price,
            windows=windows,
            delta=delta,
            running=True,
            open_trades=open_trades,
            asset_prices=asset_prices,
        )

        signal = await self._signal_engine.evaluate()
        if signal is None:
            await broadcast_state()
            return

        asset = signal.asset
        window = windows.get(asset)
        if window is None:
            await broadcast_state()
            return

        risk_status = self._risk.evaluate(signal)
        if not risk_status.trading_allowed:
            logger.warning(f"Trade blocked: {risk_status.reason}")
            await broadcast_state()
            return

        trade = await self._executor.execute(signal, window, risk_status)
        if trade is not None:
            # Mark this window as traded + start asset cooldown
            self._signal_engine.mark_traded(signal.asset, signal.window_ts)

            update_from_bot(
                risk=self._risk,
                settings=self._settings,
                new_trade=trade,
                running=True,
                last_signal=(
                    f"{signal.asset} {signal.side} | "
                    f"delta={signal.btc_delta_pct:+.3f}% | "
                    f"edge={signal.edge_after_fees:.3f} | "
                    f"conf={signal.confidence:.2f}"
                ),
                open_trades=self._get_open_trades_list(),
                asset_prices=asset_prices,
            )
            logger.success(
                f"Trade opened: {trade.id} | {trade.asset} {trade.side} | "
                f"${trade.size_usd:.2f} | {trade.mode.upper()}"
            )

        await broadcast_state()

    async def stop(self) -> None:
        logger.info("Stopping bot...")
        self._running = False
        update_from_bot(risk=self._risk, settings=self._settings, running=False, open_trades=[])
        await broadcast_state()
        await self._binance.stop()
        await self._polymarket.stop()
        for t in self._tasks:
            if not t.done():
                t.cancel()
        stats = self._risk.get_all_time_stats()
        logger.info("=" * 60)
        logger.info("FINAL STATS")
        logger.info(f"Trades:    {stats['total_trades']}")
        logger.info(f"Win Rate:  {stats['win_rate']:.1%}")
        logger.info(f"Avg Win:   ${stats['avg_win']:+.2f}")
        logger.info(f"Avg Loss:  ${stats['avg_loss']:+.2f}")
        logger.info(f"EV/Trade:  ${stats['expected_value']:+.4f}")
        logger.info(f"Total PnL: ${stats['total_pnl']:+.2f}")
        logger.info(f"Capital:   ${stats['current_capital']:.2f}")
        logger.info("=" * 60)
