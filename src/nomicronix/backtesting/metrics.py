"""Pure performance metrics over trades and the equity curve."""

from __future__ import annotations

import math
import statistics
from decimal import Decimal

from nomicronix.backtesting.models import EquityPoint, Metrics, Trade
from nomicronix.market_data.models import Timeframe

_MS_PER_YEAR = 365 * 24 * 60 * 60 * 1000


def periods_per_year(timeframe: str) -> float:
    return _MS_PER_YEAR / Timeframe(timeframe).ms


def compute_metrics(
    trades: list[Trade] | tuple[Trade, ...],
    equity_curve: list[EquityPoint] | tuple[EquityPoint, ...],
    initial_cash: Decimal,
    timeframe: str,
) -> Metrics:
    if not equity_curve:
        return _empty_metrics()

    initial = float(initial_cash)
    final = float(equity_curve[-1].equity)
    total_return = final / initial - 1 if initial > 0 else None

    elapsed_ms = equity_curve[-1].time_ms - equity_curve[0].time_ms
    elapsed_years = elapsed_ms / _MS_PER_YEAR if elapsed_ms > 0 else 0.0
    if elapsed_years > 0 and initial > 0 and final > 0:
        cagr = (final / initial) ** (1 / elapsed_years) - 1
    else:
        cagr = None

    max_drawdown = _max_drawdown([float(point.equity) for point in equity_curve])
    returns = _period_returns([float(point.equity) for point in equity_curve])
    sharpe = _sharpe(returns, timeframe)
    sortino = _sortino(returns, timeframe)
    calmar = cagr / max_drawdown if cagr is not None and max_drawdown > 0 else None

    wins = [trade.pnl for trade in trades if trade.pnl > 0]
    losses = [trade.pnl for trade in trades if trade.pnl < 0]
    win_rate = len(wins) / len(trades) if trades else None
    gross_profit = float(sum(wins, Decimal(0)))
    gross_loss = abs(float(sum(losses, Decimal(0))))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    net_pnl = sum((trade.pnl for trade in trades), Decimal(0))
    expectancy = net_pnl / len(trades) if trades else None
    average_win = sum(wins, Decimal(0)) / len(wins) if wins else None
    average_loss = sum(losses, Decimal(0)) / len(losses) if losses else None

    fees_paid = sum((trade.fees for trade in trades), Decimal(0))
    turnover = sum(
        (
            trade.entry_price * trade.quantity + trade.exit_price * trade.quantity
            for trade in trades
        ),
        Decimal(0),
    )
    exposure = (
        sum(1 for point in equity_curve if point.exposed) / len(equity_curve)
        if equity_curve
        else None
    )

    return Metrics(
        total_return=total_return,
        cagr=cagr,
        max_drawdown=max_drawdown,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        win_rate=win_rate,
        profit_factor=profit_factor,
        expectancy=expectancy,
        average_win=average_win,
        average_loss=average_loss,
        fees_paid=fees_paid,
        turnover=turnover,
        exposure=exposure,
        trade_count=len(trades),
    )


def _empty_metrics() -> Metrics:
    return Metrics(
        total_return=None,
        cagr=None,
        max_drawdown=None,
        sharpe=None,
        sortino=None,
        calmar=None,
        win_rate=None,
        profit_factor=None,
        expectancy=None,
        average_win=None,
        average_loss=None,
        fees_paid=Decimal(0),
        turnover=Decimal(0),
        exposure=None,
        trade_count=0,
    )


def _max_drawdown(equities: list[float]) -> float:
    peak = equities[0]
    worst = 0.0
    for equity in equities:
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def _period_returns(equities: list[float]) -> list[float]:
    returns: list[float] = []
    for previous, current in zip(equities, equities[1:], strict=False):
        if previous > 0:
            returns.append(current / previous - 1)
    return returns


def _sharpe(returns: list[float], timeframe: str) -> float | None:
    if len(returns) < 2:
        return None
    deviation = statistics.stdev(returns)
    if deviation == 0:
        return None
    return statistics.fmean(returns) / deviation * math.sqrt(periods_per_year(timeframe))


def _sortino(returns: list[float], timeframe: str) -> float | None:
    if len(returns) < 2:
        return None
    downside = [value for value in returns if value < 0]
    if not downside:
        return None
    downside_deviation = math.sqrt(sum(value * value for value in downside) / len(returns))
    if downside_deviation == 0:
        return None
    return statistics.fmean(returns) / downside_deviation * math.sqrt(periods_per_year(timeframe))
