"""
src/risk/manager.py
Risk management — dynamic position sizing, daily limits, drawdown protection.

Key upgrade: max_position_usd is calculated from CURRENT capital (compounding).
As the portfolio grows, position sizes grow proportionally.
"""

import time
from typing import Optional
from dataclasses import dataclass
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
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._daily_pnl: float = 0.0
        self._trade_count_today: int = 0
        self._peak_capital: float = settings.capital_usd
        self._current_capital: float = settings.capital_usd
        self._committed_capital: float = 0.0   # funds currently in open trades (not lost)
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
        self._check_day_rollover()

        # Hard stop: consecutive losses
        if self._consecutive_losses >= self._settings.max_consecutive_losses:
            return RiskStatus(
                trading_allowed=False,
                reason=f"Max consecutive losses: {self._consecutive_losses} — cooling off",
            )

        # Hard stop: daily loss (based on effective capital = current + committed)
        effective_cap = self._current_capital + self._committed_capital
        daily_loss_limit = effective_cap * self._settings.daily_loss_limit_pct
        if self._daily_pnl <= -daily_loss_limit:
            return RiskStatus(
                trading_allowed=False,
                reason=f"Daily loss limit: ${self._daily_pnl:.2f} / limit -${daily_loss_limit:.2f}",
            )

        # Hard stop: max trades per day
        if self._trade_count_today >= self._settings.max_trades_per_day:
            return RiskStatus(
                trading_allowed=False,
                reason=f"Max trades/day: {self._trade_count_today}/{self._settings.max_trades_per_day}",
            )

        # Hard stop: max simultaneous open trades
        if self._open_trade_count >= self._settings.max_open_trades:
            return RiskStatus(
                trading_allowed=False,
                reason=f"Max open trades: {self._open_trade_count}/{self._settings.max_open_trades}",
            )

        # Drawdown check from peak — use EFFECTIVE capital (available + in open trades)
        # Committed capital is not lost, it's just locked in open positions
        effective_capital = self._current_capital + self._committed_capital
        drawdown = (
            (self._peak_capital - effective_capital) / self._peak_capital
            if self._peak_capital > 0 else 0
        )
        if drawdown >= 0.30:
            return RiskStatus(
                trading_allowed=False,
                reason=f"Max drawdown: {drawdown:.1%} from peak ${self._peak_capital:.2f}",
            )

        # ── Dynamic position sizing from CURRENT capital (compounding) ────
        # As portfolio grows, positions grow automatically
        size_usd = self._kelly_size(signal)

        # Reduce after consecutive losses
        if self._consecutive_losses >= 3:
            size_usd *= 0.50
            logger.warning(f"{self._consecutive_losses} losses in a row — size halved")
        elif self._consecutive_losses >= 2:
            size_usd *= 0.70
            logger.info(f"{self._consecutive_losses} losses in a row — size reduced 30%")

        # Reduce during drawdown
        if drawdown >= 0.20:
            size_usd *= 0.50
            logger.warning(f"Drawdown {drawdown:.1%} — position halved")
        elif drawdown >= 0.12:
            size_usd *= 0.75
            logger.info(f"Drawdown {drawdown:.1%} — position reduced 25%")

        # Cap at max % of CURRENT capital (compounding)
        max_pos = self._current_capital * self._settings.max_position_pct
        size_usd = min(size_usd, max_pos)

        # Reserve buffer — never risk more than 90% of available capital
        available = self._current_capital * 0.90
        size_usd = min(size_usd, available)

        if size_usd < 3.0:
            return RiskStatus(
                trading_allowed=False,
                reason=f"Position too small: ${size_usd:.2f} (min $3)",
            )

        shares = size_usd / signal.token_price
        if shares < 5.0:
            shares = 5.0
            size_usd = shares * signal.token_price

        payout_pct = (1.0 / signal.token_price - 1.0) * 100
        logger.info(
            f"Risk APPROVED | ${size_usd:.2f} ({shares:.1f} shares) | "
            f"payout=+{payout_pct:.0f}% if WIN | "
            f"capital=${self._current_capital:.2f} | "
            f"streak={self._consecutive_losses}L | open={self._open_trade_count}"
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
        self._committed_capital += trade.size_usd
        self._open_trade_count += 1
        self._all_trades.append(trade)
        logger.info(
            f"Trade open: {trade.id} | capital: ${self._current_capital:.2f} | "
            f"open positions: {self._open_trade_count}"
        )

    def record_trade_close(self, trade: Trade) -> None:
        pnl = trade.pnl_usd
        self._daily_pnl += pnl
        self._current_capital += trade.size_usd + pnl
        self._committed_capital = max(0.0, self._committed_capital - trade.size_usd)
        self._open_trade_count = max(0, self._open_trade_count - 1)

        if trade.result == "WIN":
            if self._consecutive_losses > 0:
                logger.info(f"WIN breaks {self._consecutive_losses}-loss streak")
            self._consecutive_losses = 0
        elif trade.result == "LOSS":
            self._consecutive_losses += 1
            logger.warning(f"Loss #{self._consecutive_losses} in a row")

        if self._current_capital > self._peak_capital:
            self._peak_capital = self._current_capital

        payout_pct = ((trade.size_usd + pnl) / trade.size_usd - 1.0) * 100 if trade.size_usd > 0 else 0
        logger.info(
            f"Trade close: {trade.id} | {trade.result} | "
            f"pnl=${pnl:+.2f} ({payout_pct:+.0f}%) | capital=${self._current_capital:.2f}"
        )

    def reset_consecutive_losses(self) -> None:
        self._consecutive_losses = 0
        logger.info("Consecutive loss counter reset")

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

        # Average payout %
        win_pcts = []
        for t in wins:
            if t.size_usd > 0:
                win_pcts.append(t.pnl_usd / t.size_usd * 100)
        avg_win_pct = sum(win_pcts) / len(win_pcts) if win_pcts else 0.0

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
            "avg_win_pct": avg_win_pct,
            "expected_value": ev,
            "consecutive_losses": self._consecutive_losses,
        }

    def _kelly_size(self, signal: Signal) -> float:
        """
        Fractional Kelly using payout ratio b = (1/token_price) - 1.
        Dynamic fraction: 20-35% Kelly based on edge quality.
        Uses CURRENT capital — grows with portfolio.
        """
        p = signal.confidence
        q = 1.0 - p
        b = (1.0 / signal.token_price) - 1.0  # net odds (e.g. 0.30 token → b=2.33)
        if b <= 0:
            return 0.0

        kelly_fraction = (p * b - q) / b
        kelly_fraction = max(0.0, kelly_fraction)

        # Dynamic Kelly fraction based on edge quality
        edge = signal.edge_after_fees
        if edge >= 0.12:
            fraction = 0.35
        elif edge >= 0.08:
            fraction = 0.30
        elif edge >= 0.05:
            fraction = 0.25
        else:
            fraction = 0.20

        # Use CURRENT capital for true compounding
        return self._current_capital * kelly_fraction * fraction

    def _check_day_rollover(self) -> None:
        today = self._today()
        if today != self._day:
            logger.info(f"Day rollover → resetting daily stats for {today}")
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
