"""
src/risk/manager.py
Risk management — position sizing, daily limits, drawdown protection.

Uses Kelly Criterion (fractional) for position sizing.
Hard stops on daily loss and total drawdown.
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

    @property
    def current_capital(self) -> float:
        return self._current_capital

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def trade_count_today(self) -> int:
        return self._trade_count_today

    def evaluate(self, signal: Signal) -> RiskStatus:
        """
        Evaluate whether a trade is allowed and what size to use.
        Returns RiskStatus with decision and sizing.
        """
        self._check_day_rollover()

        # Hard stops
        if self._daily_pnl <= -self._settings.daily_loss_limit_usd:
            return RiskStatus(
                trading_allowed=False,
                reason=f"Daily loss limit hit: ${self._daily_pnl:.2f} / ${-self._settings.daily_loss_limit_usd:.2f}",
            )

        if self._trade_count_today >= self._settings.max_trades_per_day:
            return RiskStatus(
                trading_allowed=False,
                reason=f"Max trades/day reached: {self._trade_count_today}/{self._settings.max_trades_per_day}",
            )

        # Total drawdown check (25% from peak)
        drawdown = (self._peak_capital - self._current_capital) / self._peak_capital
        if drawdown >= 0.25:
            return RiskStatus(
                trading_allowed=False,
                reason=f"Max drawdown hit: {drawdown:.1%} from peak (${self._peak_capital:.2f})",
            )

        # Position sizing — fractional Kelly
        size_usd = self._kelly_size(signal)

        # Cap at max_position_usd
        size_usd = min(size_usd, self._settings.max_position_usd)

        # Cap at available capital
        size_usd = min(size_usd, self._current_capital)

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
            f"RiskManager: APPROVED | size=${size_usd:.2f} | shares={shares:.1f} | "
            f"daily_pnl=${self._daily_pnl:+.2f} | trades_today={self._trade_count_today}"
        )

        return RiskStatus(
            trading_allowed=True,
            reason="OK",
            suggested_size_usd=size_usd,
            suggested_shares=shares,
        )

    def record_trade_open(self, trade: Trade) -> None:
        """Call when a trade is opened."""
        self._trade_count_today += 1
        self._current_capital -= trade.size_usd
        self._all_trades.append(trade)
        logger.info(f"Trade opened: {trade.id} | capital remaining: ${self._current_capital:.2f}")

    def record_trade_close(self, trade: Trade) -> None:
        """Call when a trade resolves (win/loss).
        trade.pnl_usd is already net of fees (set by executor).
        """
        pnl = trade.pnl_usd  # already net of fees
        self._daily_pnl += pnl
        self._current_capital += trade.size_usd + pnl  # return stake + net profit/loss

        # Update peak
        if self._current_capital > self._peak_capital:
            self._peak_capital = self._current_capital

        logger.info(
            f"Trade closed: {trade.id} | result={trade.result} | "
            f"pnl=${pnl:+.2f} | capital=${self._current_capital:.2f}"
        )

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
            total_pnl=sum(t.pnl_usd for t in today_trades),  # already net of fees
            total_fees=0.0,  # fees already deducted in pnl_usd; tracked separately below
        )

    def get_all_time_stats(self) -> dict:
        closed = [t for t in self._all_trades if t.is_closed]
        wins = [t for t in closed if t.result == "WIN"]
        total_pnl = sum(t.pnl_usd for t in closed)  # pnl_usd is already net of fees
        win_rate = len(wins) / len(closed) if closed else 0.0
        return {
            "total_trades": len(closed),
            "wins": len(wins),
            "losses": len(closed) - len(wins),
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "total_fees": sum(t.fees_paid for t in closed),
            "current_capital": self._current_capital,
            "peak_capital": self._peak_capital,
        }

    def _kelly_size(self, signal: Signal) -> float:
        """
        Fractional Kelly Criterion: f = (p * b - q) / b * fraction
        p = win probability, b = payout ratio, q = 1 - p
        We use 25% Kelly (conservative) to avoid overbetting.
        """
        p = signal.confidence
        q = 1.0 - p
        b = (1.0 / signal.token_price) - 1  # net odds
        if b <= 0:
            return 0.0
        kelly_fraction = (p * b - q) / b
        kelly_fraction = max(0.0, kelly_fraction)
        quarter_kelly = kelly_fraction * 0.25  # conservative
        return self._current_capital * quarter_kelly

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
