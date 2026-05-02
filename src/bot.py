"""
src/bot.py
Main bot orchestrator — multi-asset, start/stop controllable.
All feeds, signals, execution wired here.
Dashboard: http://localhost:8080
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
        self._executor = create_executor(settings, self._risk)
        self._tasks: list[asyncio.Task] = []

    async def run(self) -> None:
        self._running = True
        logger.info("=" * 60)
        logger.info("POLYMARKET MULTI-ASSET BOT  v4.0")
        logger.info(f"Mode: {self._settings.trading_mode.upper()}")
        logger.info(f"Capital: ${self._settings.capital_usd}")
        logger.info(f"Assets: BTC, ETH, SOL, XRP, MATIC, DOGE, LINK, AVAX")
        logger.info(f"Dashboard: http://localhost:5000")
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
        logger.info("Evaluation loop started")
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

        update_from_bot(
            risk=self._risk,
            settings=self._settings,
            btc=btc_price,
            windows=windows,
            delta=delta,
            running=True,
        )

        signal = await self._signal_engine.evaluate()
        if signal is None:
            return

        asset = signal.asset
        window = windows.get(asset)
        if window is None:
            return

        risk_status = self._risk.evaluate(signal)
        if not risk_status.trading_allowed:
            logger.warning(f"Trade blocked: {risk_status.reason}")
            return

        trade = await self._executor.execute(signal, window, risk_status)
        if trade is not None:
            update_from_bot(
                risk=self._risk,
                settings=self._settings,
                new_trade=trade,
                running=True,
                last_signal=f"{signal.asset} {signal.side} | delta={signal.btc_delta_pct:+.3f}% | edge={signal.edge_after_fees:.3f}",
            )
            await broadcast_state()
            logger.success(
                f"Trade: {trade.id} | {trade.asset} {trade.side} | "
                f"${trade.size_usd:.2f} | {trade.mode}"
            )

    async def stop(self) -> None:
        logger.info("Stopping bot...")
        self._running = False
        update_from_bot(risk=self._risk, settings=self._settings, running=False)
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
        logger.info(f"Total PnL: ${stats['total_pnl']:+.2f}")
        logger.info(f"Capital:   ${stats['current_capital']:.2f}")
        logger.info("=" * 60)
