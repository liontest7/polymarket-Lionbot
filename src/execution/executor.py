"""
src/execution/executor.py
Trade execution — paper mode and live mode.

Paper mode features:
  - Real-time TP/SL monitoring using implied token price from Binance moves
  - SL exits at PARTIAL recovery (not -100%) — realistic market exit
  - Time stop near window close if no TP/SL triggered
  - Full payout at settlement if held to window close
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

        entry_asset_price = 0.0
        if self._binance:
            ap = self._binance.get_price(signal.asset)
            if ap:
                entry_asset_price = ap.price

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
        projected_loss_sl = round(-size_usd * (self._settings.sl_token_loss / signal.token_price), 2)

        logger.success(
            f"[PAPER] {signal.asset} {signal.side} OPEN | "
            f"${size_usd:.2f} @ {signal.token_price:.3f} | "
            f"WIN: +${projected_win:.2f} (+{payout_pct:.0f}%) | "
            f"SL: ~${projected_loss_sl:.2f} partial | "
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

        open_price, close_price = 0.0, 0.0
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                symbol = trade.asset.upper() + "USDT"
                entry_ms = int(trade.entry_time * 1000)
                r = await client.get(
                    "https://api.binance.com/api/v3/ticker/price",
                    params={"symbol": symbol},
                )
                close_price = float(r.json()["price"])
                r2 = await client.get(
                    "https://api.binance.com/api/v3/klines",
                    params={"symbol": symbol, "interval": "1m", "startTime": entry_ms, "limit": 1},
                )
                klines = r2.json()
                open_price = float(klines[0][1]) if klines else close_price
        except Exception as e:
            logger.warning(f"Force resolve fetch failed: {e}")
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
        await self._settle_at_close(trade, window, close_price, open_price, reason="FORCE")
        return True

    async def _monitor_trade(self, trade: Trade, window: PolyWindow, entry_asset_price: float) -> None:
        """
        Monitor every 2 seconds for TP/SL conditions.
        Stays open until: TP hit, SL hit, time stop, or window close.
        SL exits at PARTIAL recovery, not full loss.
        """
        check_interval = 2.0
        tp_gain = self._settings.tp_token_gain      # e.g. 0.20
        sl_loss = self._settings.sl_token_loss       # e.g. 0.10
        time_stop = self._settings.time_stop_seconds # e.g. 260s
        entry_token = trade.entry_price

        while True:
            await asyncio.sleep(check_interval)

            if trade.id not in self._open_trades:
                return

            now = time.time()
            secs_in_trade = now - trade.entry_time
            secs_to_close = window.close_ts - now

            # Window has closed → settle at final price
            if secs_to_close <= 0:
                await self._resolve_at_settlement(trade, window, reason="WINDOW_CLOSE")
                return

            # ── TP / SL via implied token price ──────────────────────────
            if self._binance and entry_asset_price > 0:
                ap = self._binance.get_price(trade.asset)
                if ap:
                    price_move_pct = (ap.price - entry_asset_price) / entry_asset_price

                    # Dynamic sensitivity: as the trade ages and window closes,
                    # even small Binance moves have a larger impact on the binary
                    # outcome probability. Sensitivity grows from ~10 to ~35 over
                    # the course of a typical 260s hold.
                    sensitivity = 10.0 + min(25.0, secs_in_trade * 0.15)

                    # Implied token price: how much the market probability has shifted
                    # UP bet wins when price goes up; DOWN bet wins when price goes down
                    if trade.side == "UP":
                        implied_token = entry_token + price_move_pct * sensitivity
                    else:
                        implied_token = entry_token - price_move_pct * sensitivity

                    implied_token = max(0.02, min(0.98, implied_token))
                    token_change = implied_token - entry_token

                    # Positive change = good for us (our direction winning)
                    our_gain = token_change if trade.side == "UP" else -token_change

                    # ── Take Profit ─────────────────────────────────────
                    if our_gain >= tp_gain:
                        exit_token = min(0.96, entry_token + our_gain)
                        pnl = round(trade.shares * (exit_token - entry_token) - trade.fees_paid, 4)
                        trade.result = "WIN"
                        trade.exit_price = exit_token
                        trade.pnl_usd = pnl
                        trade.exit_time = now
                        payout_pct = pnl / trade.size_usd * 100
                        logger.success(
                            f"[TP] {trade.asset} {trade.side} | "
                            f"token {entry_token:.3f}→{exit_token:.3f} | "
                            f"pnl=+${pnl:.2f} (+{payout_pct:.0f}%)"
                        )
                        await self._close_trade(trade)
                        return

                    # ── Stop Loss — PARTIAL RECOVERY ─────────────────────
                    # We exit at implied_token price, NOT at zero
                    # Recovery = shares × exit_token_price
                    if our_gain <= -sl_loss:
                        exit_token = max(0.02, implied_token)
                        recovery = trade.shares * exit_token
                        pnl = round(recovery - trade.size_usd - trade.fees_paid, 4)
                        trade.result = "LOSS"
                        trade.exit_price = exit_token
                        trade.pnl_usd = pnl
                        trade.exit_time = now
                        loss_pct = abs(pnl) / trade.size_usd * 100
                        logger.warning(
                            f"[SL] {trade.asset} {trade.side} | "
                            f"token {entry_token:.3f}→{exit_token:.3f} (partial exit) | "
                            f"pnl=${pnl:.2f} (-{loss_pct:.0f}% — saved ${recovery:.2f})"
                        )
                        await self._close_trade(trade)
                        return

            # ── Time Stop — settle at current window state ────────────────
            if secs_in_trade >= time_stop:
                logger.info(
                    f"[TIME_STOP] {trade.asset} {trade.id} — {secs_in_trade:.0f}s elapsed, "
                    f"settling at window state"
                )
                await self._resolve_at_settlement(trade, window, reason="TIME_STOP")
                return

    async def _resolve_at_settlement(self, trade: Trade, window: PolyWindow, reason: str = "SETTLE") -> None:
        if trade.id not in self._open_trades:
            return

        import httpx
        close_price = 0.0
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    "https://api.binance.com/api/v3/ticker/price",
                    params={"symbol": trade.asset.upper() + "USDT"},
                )
                close_price = float(resp.json()["price"])
        except Exception as e:
            logger.warning(f"Settlement price fetch failed for {trade.asset}: {e}")

        await self._settle_at_close(trade, window, close_price, window.open_price, reason=reason)

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

        if trade.result in ("WIN", "LOSS"):
            # Already resolved by TP/SL — just close
            await self._close_trade(trade)
            return

        # Use trade's entry_asset_price as fallback when window open_price wasn't
        # recorded (happens in demo mode if Binance wasn't ready at window creation)
        effective_open = open_price if open_price > 0 else trade.entry_asset_price

        if effective_open > 0 and close_price > 0:
            went_up = close_price >= effective_open
            trade_won = (went_up and trade.side == "UP") or (not went_up and trade.side == "DOWN")
        else:
            # Last resort: estimate from signal confidence
            import random
            trade_won = random.random() < trade.entry_price

        if trade_won:
            trade.result = "WIN"
            trade.exit_price = 1.0
            # Full settlement payout: shares × (1 - entry_price)
            trade.pnl_usd = round(trade.shares * (1.0 - trade.entry_price) - trade.fees_paid, 4)
        else:
            trade.result = "LOSS"
            trade.exit_price = 0.0
            # At settlement — token goes to 0, full loss
            trade.pnl_usd = round(-trade.size_usd, 4)

        trade.exit_time = time.time()
        delta_pct = ((close_price - open_price) / open_price * 100) if open_price > 0 else 0
        icon = "✓ WIN" if trade.result == "WIN" else "✗ LOSS"
        payout_pct = trade.pnl_usd / trade.size_usd * 100 if trade.size_usd > 0 else 0
        logger.info(
            f"[{reason}] {icon} | {trade.asset} {trade.side} | "
            f"Δ={delta_pct:+.3f}% | pnl=${trade.pnl_usd:+.2f} ({payout_pct:+.0f}%)"
        )
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
        payout_pct = (1.0 / limit_price - 1.0) * 100

        logger.warning(
            f"[LIVE] {signal.asset} {signal.side} | "
            f"shares={shares} @ {limit_price} | ${size_usd:.2f} | "
            f"WIN would pay +{payout_pct:.0f}%"
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
