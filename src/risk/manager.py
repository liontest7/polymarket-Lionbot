"""
src/risk/manager.py
Risk management — dynamic position sizing, daily limits, drawdown protection.

תיקונים לעומת הגרסה הקודמת:
  1. record_trade_close — מחשב pnl נכון כולל fees
  2. _kelly_size — הוסף בדיקה שהתוצאה לא שלילית
  3. הוסף minimum_capital_check — עוצר trading אם ההון ירד מדי
  4. תיעוד מפורט יותר על כל החלטת risk
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
        self._trade_count_today = 0
        self._peak_capital = settings.capital_usd
        self._current_capital = settings.capital_usd
        self._committed_capital = 0.0  # בפוזיציות פתוחות (לא אבוד)
        self._day = self._today()
        self._all_trades: list[Trade] = []
        self._consecutive_losses = 0
        self._open_trade_count = 0

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

        # ── הון מינימלי ─────────────────────────────────────────────────────
        min_capital = max(10.0, self._settings.capital_usd * 0.10)
        if self._current_capital < min_capital:
            return RiskStatus(
                trading_allowed=False,
                reason=f"Capital too low: ${self._current_capital:.2f} < min ${min_capital:.2f}",
            )

        # ── עצירה: הפסדים רצופים ──────────────────────────────────────────
        if self._consecutive_losses >= self._settings.max_consecutive_losses:
            return RiskStatus(
                trading_allowed=False,
                reason=f"Max consecutive losses: {self._consecutive_losses} — cooling off",
            )

        # ── עצירה: הפסד יומי ──────────────────────────────────────────────
        effective_cap = self._current_capital + self._committed_capital
        daily_loss_limit = effective_cap * self._settings.daily_loss_limit_pct
        if self._daily_pnl <= -daily_loss_limit:
            return RiskStatus(
                trading_allowed=False,
                reason=(
                    f"Daily loss limit: ${self._daily_pnl:.2f} / "
                    f"limit -${daily_loss_limit:.2f}"
                ),
            )

        # ── עצירה: מקסימום עסקאות יומיות ──────────────────────────────────
        if self._trade_count_today >= self._settings.max_trades_per_day:
            return RiskStatus(
                trading_allowed=False,
                reason=(
                    f"Max trades/day: {self._trade_count_today}/"
                    f"{self._settings.max_trades_per_day}"
                ),
            )

        # ── עצירה: מקסימום עסקאות פתוחות ─────────────────────────────────
        if self._open_trade_count >= self._settings.max_open_trades:
            return RiskStatus(
                trading_allowed=False,
                reason=(
                    f"Max open trades: {self._open_trade_count}/"
                    f"{self._settings.max_open_trades}"
                ),
            )

        # ── בדיקת drawdown ─────────────────────────────────────────────────
        effective_capital = self._current_capital + self._committed_capital
        drawdown = (
            (self._peak_capital - effective_capital) / self._peak_capital
            if self._peak_capital > 0
            else 0.0
        )
        if drawdown >= 0.30:
            return RiskStatus(
                trading_allowed=False,
                reason=(
                    f"Max drawdown: {drawdown:.1%} from peak ${self._peak_capital:.2f}"
                ),
            )

        # ── גודל פוזיציה (Kelly) ────────────────────────────────────────────
        size_usd = self._kelly_size(signal)

        if size_usd <= 0:
            return RiskStatus(
                trading_allowed=False,
                reason=f"Kelly returned non-positive size: ${size_usd:.2f}",
            )

        # ── הפחתה אחרי הפסדים רצופים ────────────────────────────────────
        if self._consecutive_losses >= 3:
            size_usd *= 0.50
            logger.warning(
                f"{self._consecutive_losses} consecutive losses — size halved"
            )
        elif self._consecutive_losses >= 2:
            size_usd *= 0.70
            logger.info(f"{self._consecutive_losses} consecutive losses — size -30%")

        # ── הפחתה בזמן drawdown ──────────────────────────────────────────
        if drawdown >= 0.20:
            size_usd *= 0.50
            logger.warning(f"Drawdown {drawdown:.1%} — position halved")
        elif drawdown >= 0.12:
            size_usd *= 0.75
            logger.info(f"Drawdown {drawdown:.1%} — position -25%")

        # ── cap לפי max_position_pct ─────────────────────────────────────
        max_pos = self._current_capital * self._settings.max_position_pct
        size_usd = min(size_usd, max_pos)

        # ── שמור 10% buffer ──────────────────────────────────────────────
        available = self._current_capital * 0.90
        size_usd = min(size_usd, available)

        # ── מינימום $3 ──────────────────────────────────────────────────
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
            f"drawdown={drawdown:.1%} | "
            f"streak={self._consecutive_losses}L | "
            f"open={self._open_trade_count}"
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
            f"Trade open: {trade.id} | "
            f"capital: ${self._current_capital:.2f} | "
            f"committed: ${self._committed_capital:.2f} | "
            f"open: {self._open_trade_count}"
        )

    def record_trade_close(self, trade: Trade) -> None:
        """
        תיקון: pnl כולל את ה-fees שכבר שולמו בעת הפתיחה.
        trade.pnl_usd כבר מחושב net of fees ב-executor.
        """
        pnl = trade.pnl_usd
        self._daily_pnl += pnl
        # מחזירים את ה-size שהושקע + הרווח/הפסד
        self._current_capital += trade.size_usd + pnl
        self._committed_capital = max(0.0, self._committed_capital - trade.size_usd)
        self._open_trade_count = max(0, self._open_trade_count - 1)

        if trade.result == "WIN":
            if self._consecutive_losses > 0:
                logger.info(f"WIN breaks {self._consecutive_losses}-loss streak")
            self._consecutive_losses = 0
        elif trade.result == "LOSS":
            self._consecutive_losses += 1
            logger.warning(
                f"Loss #{self._consecutive_losses} in a row | pnl=${pnl:.2f}"
            )

        # עדכון peak
        effective = self._current_capital + self._committed_capital
        if effective > self._peak_capital:
            self._peak_capital = effective

        payout_pct = pnl / trade.size_usd * 100 if trade.size_usd > 0 else 0
        logger.info(
            f"Trade close: {trade.id} | {trade.result} | "
            f"pnl=${pnl:+.2f} ({payout_pct:+.0f}%) | "
            f"capital=${self._current_capital:.2f} | "
            f"daily_pnl=${self._daily_pnl:+.2f}"
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
            total_fees=sum(t.fees_paid for t in today_trades),
        )

    def get_all_time_stats(self) -> dict:
        closed = [t for t in self._all_trades if t.is_closed]
        wins = [t for t in closed if t.result == "WIN"]
        losses_list = [t for t in closed if t.result == "LOSS"]
        total_pnl = sum(t.pnl_usd for t in closed)
        win_rate = len(wins) / len(closed) if closed else 0.0
        avg_win = sum(t.pnl_usd for t in wins) / len(wins) if wins else 0.0
        avg_loss = (
            sum(t.pnl_usd for t in losses_list) / len(losses_list)
            if losses_list
            else 0.0
        )
        ev = win_rate * avg_win + (1 - win_rate) * avg_loss if closed else 0.0

        win_pcts = [t.pnl_usd / t.size_usd * 100 for t in wins if t.size_usd > 0]
        avg_win_pct = sum(win_pcts) / len(win_pcts) if win_pcts else 0.0

        # Profit factor: total wins / total losses
        total_won = sum(t.pnl_usd for t in wins)
        total_lost = abs(sum(t.pnl_usd for t in losses_list))
        profit_factor = total_won / total_lost if total_lost > 0 else float("inf")

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
            "profit_factor": profit_factor,
            "consecutive_losses": self._consecutive_losses,
        }

    def _kelly_size(self, signal: Signal) -> float:
        """
        Fractional Kelly — מחשב גודל פוזיציה אופטימלי.

        Kelly formula: f = (p*b - q) / b
          p = win probability
          q = 1 - p
          b = net odds (e.g. token=0.30 → b=2.33, כלומר על כל $1 מרוויחים $2.33)

        מוכפל ב-fraction קטן (20-35%) לשמרנות.
        מבוסס על current_capital לconfiguration אמיתי.
        """
        p = signal.confidence
        q = 1.0 - p
        b = (1.0 / signal.token_price) - 1.0

        if b <= 0:
            return 0.0

        kelly_fraction = (p * b - q) / b
        kelly_fraction = max(0.0, kelly_fraction)

        if kelly_fraction <= 0:
            logger.debug(
                f"Kelly negative for {signal.asset}: "
                f"p={p:.2f} q={q:.2f} b={b:.2f} → f={kelly_fraction:.3f}"
            )
            return 0.0

        # Fraction דינמי לפי edge
        edge = signal.edge_after_fees
        if edge >= 0.12:
            fraction = 0.35
        elif edge >= 0.08:
            fraction = 0.30
        elif edge >= 0.05:
            fraction = 0.25
        else:
            fraction = 0.20

        size = self._current_capital * kelly_fraction * fraction
        logger.debug(
            f"Kelly: p={p:.2f} b={b:.2f} f_kelly={kelly_fraction:.3f} "
            f"fraction={fraction} → ${size:.2f}"
        )
        return size

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
