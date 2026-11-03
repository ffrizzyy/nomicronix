"""Measurable promotion gate: is the paper record strong enough for execution?"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from nomicronix.signal_delivery.models import DeliveryTotals, PaperTrade
from nomicronix.signal_delivery.store import DeliveryStore
from nomicronix.strategy_engine.models import decimal_text

MS_PER_DAY = 86_400_000


@dataclass(frozen=True, slots=True)
class PromotionCriteria:
    """Thresholds the owner sets before `execution` may be built."""

    min_closed_trades: int = 30
    min_days: int = 28
    assumed_round_trip_cost_pct: Decimal = Decimal("0.002")
    max_paper_drawdown_pct: Decimal = Decimal("0.05")

    def __post_init__(self) -> None:
        if self.min_closed_trades < 1:
            raise ValueError("min_closed_trades must be at least 1")
        if self.min_days < 0:
            raise ValueError("min_days must not be negative")
        if not 0 <= self.assumed_round_trip_cost_pct < Decimal("0.05"):
            raise ValueError("assumed_round_trip_cost_pct must be inside [0, 0.05)")
        if not 0 < self.max_paper_drawdown_pct < 1:
            raise ValueError("max_paper_drawdown_pct must be inside (0, 1)")


@dataclass(frozen=True, slots=True)
class CriterionResult:
    key: str
    label: str
    passed: bool
    actual: str
    required: str


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    ready: bool
    closed_trades: int
    track_record_days: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    expectancy: Decimal
    max_drawdown_pct: Decimal
    costs: Decimal
    criteria: tuple[CriterionResult, ...]

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "execution-readiness",
            "ready": self.ready,
            "closed_trades": self.closed_trades,
            "track_record_days": decimal_text(self.track_record_days),
            "gross_pnl": decimal_text(self.gross_pnl),
            "net_pnl": decimal_text(self.net_pnl),
            "expectancy": decimal_text(self.expectancy),
            "max_drawdown_pct": decimal_text(self.max_drawdown_pct),
            "assumed_costs": decimal_text(self.costs),
            "criteria": [
                {
                    "key": item.key,
                    "label": item.label,
                    "passed": item.passed,
                    "actual": item.actual,
                    "required": item.required,
                }
                for item in self.criteria
            ],
        }


def _closed_trades(store: DeliveryStore) -> list[PaperTrade]:
    trades = [trade for trade in store.all_trades() if not trade.is_open]
    return sorted(
        trades,
        key=lambda trade: (trade.closed_at_ms or 0, trade.signal_id),
    )


def _drawdown_pct(
    trades: list[PaperTrade], initial_cash: Decimal, net_pnls: list[Decimal]
) -> Decimal:
    equity = initial_cash
    peak = initial_cash
    worst = Decimal(0)
    for pnl in net_pnls:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def evaluate_readiness(
    store: DeliveryStore,
    criteria: PromotionCriteria,
    *,
    initial_cash: Decimal = Decimal("1000"),
) -> ReadinessReport:
    if initial_cash <= 0:
        raise ValueError("initial_cash must be positive")

    closed = _closed_trades(store)
    totals: DeliveryTotals = store.delivery_totals()
    gross = sum((trade.realized_pnl or Decimal(0) for trade in closed), Decimal(0))
    costs = sum(
        (
            trade.entry_price * trade.quantity * criteria.assumed_round_trip_cost_pct
            for trade in closed
        ),
        Decimal(0),
    )
    net_pnls = [
        (trade.realized_pnl or Decimal(0))
        - trade.entry_price * trade.quantity * criteria.assumed_round_trip_cost_pct
        for trade in closed
    ]
    net = gross - costs
    expectancy = net / len(closed) if closed else Decimal(0)
    if closed:
        first_open = min(trade.opened_at_ms for trade in closed)
        last_close = max(trade.closed_at_ms or 0 for trade in closed)
        days = Decimal(last_close - first_open) / MS_PER_DAY
    else:
        days = Decimal(0)
    drawdown = _drawdown_pct(closed, initial_cash, net_pnls)
    all_trades = len(store.all_trades())

    checks = (
        CriterionResult(
            key="closed-trades",
            label="closed paper trades",
            passed=len(closed) >= criteria.min_closed_trades,
            actual=str(len(closed)),
            required=f">= {criteria.min_closed_trades}",
        ),
        CriterionResult(
            key="track-record-days",
            label="track record days",
            passed=days >= criteria.min_days,
            actual=f"{days:.1f}",
            required=f">= {criteria.min_days}",
        ),
        CriterionResult(
            key="net-expectancy",
            label="expectancy after costs",
            passed=expectancy > 0,
            actual=f"{decimal_text(expectancy)} USDT/trade",
            required="> 0",
        ),
        CriterionResult(
            key="paper-drawdown",
            label="paper max drawdown",
            passed=drawdown <= criteria.max_paper_drawdown_pct,
            actual=f"{float(drawdown) * 100:.2f}%",
            required=f"<= {float(criteria.max_paper_drawdown_pct) * 100:.2f}%",
        ),
        CriterionResult(
            key="delivery-health",
            label="failed or pending deliveries",
            passed=totals.failed == 0 and totals.pending == 0,
            actual=f"{totals.failed} failed, {totals.pending} pending",
            required="0 failed, 0 pending",
        ),
        CriterionResult(
            key="ledger-integrity",
            label="delivered alerts vs paper trades",
            passed=totals.delivered == all_trades,
            actual=f"{totals.delivered} delivered, {all_trades} trades",
            required="equal",
        ),
    )

    return ReadinessReport(
        ready=all(item.passed for item in checks),
        closed_trades=len(closed),
        track_record_days=days,
        gross_pnl=gross,
        net_pnl=net,
        expectancy=expectancy,
        max_drawdown_pct=drawdown,
        costs=costs,
        criteria=checks,
    )


def render_readiness(report: ReadinessReport) -> str:
    status = "READY" if report.ready else "NOT READY"
    passed = sum(1 for item in report.criteria if item.passed)
    lines = [
        f"execution readiness: {status} ({passed} of {len(report.criteria)} criteria met)",
        "",
    ]
    for item in report.criteria:
        mark = "PASS" if item.passed else "FAIL"
        lines.append(f"  [{mark}] {item.label}: {item.actual} (need {item.required})")
    lines.extend(
        [
            "",
            f"closed paper trades: {report.closed_trades} over {report.track_record_days:.1f} days",
            f"gross pnl: {decimal_text(report.gross_pnl)} USDT | "
            f"assumed costs: {decimal_text(report.costs)} USDT | "
            f"net: {decimal_text(report.net_pnl)} USDT",
            f"expectancy: {decimal_text(report.expectancy)} USDT/trade | "
            f"paper max drawdown: {float(report.max_drawdown_pct) * 100:.2f}%",
        ]
    )
    return "\n".join(lines)
