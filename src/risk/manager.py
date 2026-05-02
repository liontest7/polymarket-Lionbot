"""
src/risk/manager.py
Risk management — position sizing, daily limits, drawdown protection.

Uses fractional Kelly Criterion for sizing.
Hard stops: daily loss, total drawdown, consecutive losses.
"""

import time
from typing import Optional
from dataclasses import dataclass, field
from loguru import logger

from src.models import Signal, Trade, DailyStats
from config.settings import Settings


@dataclass
class RiskStatus:
    trading_allowed: bool
    reason: str
    suggested_size_usd: float = 0.0
    suggested_shares: float = 0.0


class RiskManager:
    """
    Controls position sizing and enforces trading limits.
    Must be consulted before every trade.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._daily_pnl: float = 0.0
        self._trade_count_today: int = 0
        self._peak_capital: float = settings.capital_usd
        self._current_capital: float = settings.capital_usd
        self._day: str = self._today()
        self._all_trades: list[Trade] = []
        self._consecutive_losses: int = 0
        self._open_trade_count: int = 0

    @property
    def current_capital(self) -> float:
        return self._current_capital

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def trade_count_today(self) -> int:
        return self._trade_count_today

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    def evaluate(self, signal: Signal) -> RiskStatus:
        """
        Evaluate whether a trade is allowed and what size to use.
        """
        self._check_day_rollover()

        # Hard stop: consecutive losses
        if self._consecutive_losses >= self._settings.max_consecutive_losses:
            return RiskStatus(
                trading_allowed=False,
                reason=f"Max consecutive losses hit: {self._consecutive_losses} in a row — cooling off",
            )

        # Hard stop: daily loss limit
        if self._daily_pnl <= -self._settings.daily_loss_limit_usd:
            return RiskStatus(
                trading_allowed=False,
                reason=f"Daily loss limit: ${self._daily_pnl:.2f} / limit ${-self._settings.daily_loss_limit_usd:.2f}",
            )

        # Hard stop: max trades per day
        if self._trade_count_today >= self._settings.max_trades_per_day:
            return RiskStatus(
                trading_allowed=False,
                reason=f"Max trades/day reached: {self._trade_count_today}/{self._settings.max_trades_per_day}",
            )

        # Total drawdown check — reduce at 15%, stop at 25%
        drawdown = (self._peak_capital - self._current_capital) / self._peak_capital if self._peak_capital > 0 else 0
        if drawdown >= 0.25:
            return RiskStatus(
                trading_allowed=False,
                reason=f"Max drawdown: {drawdown:.1%} from peak ${self._peak_capital:.2f}",
            )

        # Don't stack too many open trades simultaneously
        if self._open_trade_count >= 3:
            return RiskStatus(
                trading_allowed=False,
                reason=f"Too many open trades: {self._open_trade_count} active",
            )

        # Position sizing — fractional Kelly, scaled by signal strength
        size_usd = self._kelly_size(signal)

        # Reduce size after consecutive losses (ratchet down)
        if self._consecutive_losses >= 3:
            size_usd *= 0.50
            logger.warning(f"{self._consecutive_losses} consecutive losses — size halved to ${size_usd:.2f}")
        elif self._consecutive_losses >= 2:
            size_usd *= 0.75
            logger.info(f"{self._consecutive_losses} consecutive losses — size reduced 25%")

        # Reduce position size as drawdown increases
        if drawdown >= 0.15:
            size_usd *= 0.5
            logger.warning(f"Drawdown {drawdown:.1%} — position halved")
        elif drawdown >= 0.10:
            size_usd *= 0.75
            logger.info(f"Drawdown {drawdown:.1%} — position reduced 25%")

        # Cap at max_position_usd
        size_usd = min(size_usd, self._settings.max_position_usd)

        # Cap at available capital (with buffer)
        available = self._current_capital * 0.95
        size_usd = min(size_usd, available)

        if size_usd < 5.0:
            return RiskStatus(
                trading_allowed=False,
                reason=f"Position size too small: ${size_usd:.2f} (min $5)",
            )

        # Shares = size / token_price (Polymarket min = 5 shares)
        shares = size_usd / signal.token_price
        if shares < 5.0:
            shares = 5.0
            size_usd = shares * signal.token_price

        logger.info(
            f"Risk APPROVED | ${size_usd:.2f} ({shares:.1f} shares) | "
            f"pnl_today=${self._daily_pnl:+.2f} | streak={self._consecutive_losses}L | "
            f"trades={self._trade_count_today}/{self._settings.max_trades_per_day}"
        )

        return RiskStatus(
            trading_allowed=True,
            reason="OK",
            suggested_size_usd=size_usd,
            suggested_shares=shares,
        )

    def record_trade_open(self, trade: Trade) -> None:
        self._trade_count_today += 1
        self._current_capital -= trade.size_usd
        self._open_trade_count += 1
        self._all_trades.append(trade)
        logger.info(f"Trade opened: {trade.id} | capital: ${self._current_capital:.2f} | open: {self._open_trade_count}")

    def record_trade_close(self, trade: Trade) -> None:
        pnl = trade.pnl_usd
        self._daily_pnl += pnl
        self._current_capital += trade.size_usd + pnl
        self._open_trade_count = max(0, self._open_trade_count - 1)

        # Track consecutive losses
        if trade.result == "LOSS":
            self._consecutive_losses += 1
            logger.warning(f"Loss recorded — consecutive losses: {self._consecutive_losses}")
        elif trade.result == "WIN":
            if self._consecutive_losses > 0:
                logger.info(f"Win breaks losing streak of {self._consecutive_losses}")
            self._consecutive_losses = 0

        # Update peak
        if self._current_capital > self._peak_capital:
            self._peak_capital = self._current_capital

        logger.info(
            f"Trade closed: {trade.id} | {trade.result} | "
            f"pnl=${pnl:+.2f} | capital=${self._current_capital:.2f}"
        )

    def reset_consecutive_losses(self) -> None:
        """Manually reset the consecutive loss counter (via dashboard)."""
        self._consecutive_losses = 0
        logger.info("Consecutive loss counter reset manually")

    def get_daily_stats(self) -> DailyStats:
        today = self._today()
        today_trades = [t for t in self._all_trades if self._trade_date(t) == today]
        wins = [t for t in today_trades if t.result == "WIN"]
        losses = [t for t in today_trades if t.result == "LOSS"]

        return DailyStats(
            date=today,
            trades=len(today_trades),
            wins=len(wins),
            losses=len(losses),
            total_pnl=sum(t.pnl_usd for t in today_trades),
            total_fees=0.0,
        )

    def get_all_time_stats(self) -> dict:
        closed = [t for t in self._all_trades if t.is_closed]
        wins = [t for t in closed if t.result == "WIN"]
        losses_list = [t for t in closed if t.result == "LOSS"]
        total_pnl = sum(t.pnl_usd for t in closed)
        win_rate = len(wins) / len(closed) if closed else 0.0

        avg_win = sum(t.pnl_usd for t in wins) / len(wins) if wins else 0.0
        avg_loss = sum(t.pnl_usd for t in losses_list) / len(losses_list) if losses_list else 0.0
        ev = win_rate * avg_win + (1 - win_rate) * avg_loss if closed else 0.0

        return {
            "total_trades": len(closed),
            "wins": len(wins),
            "losses": len(closed) - len(wins),
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "total_fees": sum(t.fees_paid for t in closed),
            "current_capital": self._current_capital,
            "peak_capital": self._peak_capital,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "expected_value": ev,
            "consecutive_losses": self._consecutive_losses,
        }

    def _kelly_size(self, signal: Signal) -> float:
        """
        Fractional Kelly: f = (p*b - q) / b * fraction
        Dynamic fraction: stronger signal → slightly larger fraction (max 35% Kelly).
        """
        p = signal.confidence
        q = 1.0 - p
        b = (1.0 / signal.token_price) - 1
        if b <= 0:
            return 0.0
        kelly_fraction = (p * b - q) / b
        kelly_fraction = max(0.0, kelly_fraction)

        # Dynamic Kelly fraction: 20-35% based on edge strength
        edge = signal.edge_after_fees
        if edge >= 0.10:
            fraction = 0.35
        elif edge >= 0.06:
            fraction = 0.28
        elif edge >= 0.04:
            fraction = 0.23
        else:
            fraction = 0.20

        return self._current_capital * kelly_fraction * fraction

    def _check_day_rollover(self) -> None:
        today = self._today()
        if today != self._day:
            logger.info(f"Day rollover: resetting daily stats for {today}")
            self._day = today
            self._daily_pnl = 0.0
            self._trade_count_today = 0

    @staticmethod
    def _today() -> str:
        from datetime import date
        return date.today().isoformat()

    @staticmethod
    def _trade_date(trade: Trade) -> str:
        from datetime import datetime
        return datetime.fromtimestamp(trade.entry_time).date().isoformat()
