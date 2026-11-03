"""Delivery and paper-trade reporting."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nomicronix.risk_engine.service import RiskService
from nomicronix.signal_delivery.models import DeliveryTotals
from nomicronix.signal_delivery.store import DeliveryStore
from nomicronix.strategy_engine.models import decimal_text


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    totals: DeliveryTotals
    open_trades: int
    closed_trades: int
    realized_pnl: Decimal
    win_rate: float | None
    equity: Decimal
    trading_allowed: bool
    open_risk: Decimal
    drawdown_pct: Decimal


def build_report(store: DeliveryStore, risk: RiskService) -> DeliveryReport:
    trades = store.all_trades()
    closed = [trade for trade in trades if not trade.is_open]
    realized = sum(
        (trade.realized_pnl or Decimal(0) for trade in closed),
        Decimal(0),
    )
    wins = sum(1 for trade in closed if (trade.realized_pnl or Decimal(0)) > 0)
    status = risk.status()
    return DeliveryReport(
        totals=store.delivery_totals(),
        open_trades=len(trades) - len(closed),
        closed_trades=len(closed),
        realized_pnl=realized,
        win_rate=None if not closed else wins / len(closed),
        equity=status.equity,
        trading_allowed=status.trading_allowed,
        open_risk=status.open_risk,
        drawdown_pct=status.drawdown_pct,
    )


def render_report(report: DeliveryReport) -> str:
    totals = report.totals
    win_rate = "n/a" if report.win_rate is None else f"{report.win_rate * 100:.2f}%"
    return "\n".join(
        [
            f"deliveries: {totals.total} total | delivered {totals.delivered} | "
            f"rejected {totals.rejected} | pending {totals.pending} | "
            f"failed {totals.failed}",
            f"paper trades: {report.closed_trades + report.open_trades} total | "
            f"closed {report.closed_trades} | open {report.open_trades}",
            f"realized pnl: {decimal_text(report.realized_pnl)} USDT",
            f"win rate: {win_rate}",
            f"equity: {decimal_text(report.equity)} USDT | "
            f"open risk {decimal_text(report.open_risk)} | "
            f"drawdown {float(report.drawdown_pct) * 100:.2f}%",
            f"trading allowed: {'yes' if report.trading_allowed else 'no'}",
        ]
    )
