from decimal import Decimal

import pytest

from nomicronix.backtesting.metrics import compute_metrics, periods_per_year
from nomicronix.backtesting.models import EquityPoint, ExitReason, Trade

DAY_MS = 86_400_000


def make_trade(
    pnl: str,
    *,
    fees: str = "1",
    entry: str = "100",
    exit_price: str = "110",
    quantity: str = "1",
) -> Trade:
    return Trade(
        symbol="BTC/USDT",
        quantity=Decimal(quantity),
        entry_time_ms=0,
        entry_price=Decimal(entry),
        exit_time_ms=DAY_MS,
        exit_price=Decimal(exit_price),
        fees=Decimal(fees),
        pnl=Decimal(pnl),
        return_pct=0.1,
        exit_reason=ExitReason.TARGET,
    )


def curve(*values: str) -> tuple[EquityPoint, ...]:
    return tuple(
        EquityPoint(
            time_ms=index * DAY_MS,
            equity=Decimal(value),
            exposed=index > 0,
        )
        for index, value in enumerate(values)
    )


def test_periods_per_year_for_common_timeframes() -> None:
    assert periods_per_year("1d") == 365.0
    assert periods_per_year("1h") == 8760.0


def test_hand_computed_metrics_fixture() -> None:
    metrics = compute_metrics(
        [make_trade("10"), make_trade("-5"), make_trade("20")],
        curve("1000", "1100", "990", "1089"),
        Decimal("1000"),
        "1d",
    )

    assert metrics.total_return == pytest.approx(0.089)
    assert metrics.max_drawdown == pytest.approx(0.1)
    assert metrics.win_rate == pytest.approx(2 / 3)
    assert metrics.profit_factor == pytest.approx(6.0)
    assert metrics.expectancy == pytest.approx(Decimal("8.333333333333333333333333333"))
    assert metrics.average_win == Decimal("15")
    assert metrics.average_loss == Decimal("-5")
    assert metrics.fees_paid == Decimal("3")
    assert metrics.turnover == Decimal("630")
    assert metrics.exposure == pytest.approx(0.75)
    assert metrics.trade_count == 3
    assert metrics.cagr is not None and metrics.cagr > 0
    assert metrics.sharpe is not None and metrics.sharpe > 0
    assert metrics.sortino is not None and metrics.sortino > 0
    assert metrics.calmar is not None and metrics.calmar > 0


def test_max_drawdown_matches_a_manual_equity_curve() -> None:
    metrics = compute_metrics(
        [],
        curve("1000", "1200", "900", "1100", "800", "1300"),
        Decimal("1000"),
        "1d",
    )
    assert metrics.max_drawdown == pytest.approx((1200 - 800) / 1200)


def test_flat_equity_has_no_sharpe_or_sortino() -> None:
    metrics = compute_metrics([], curve("1000", "1000", "1000"), Decimal("1000"), "1d")
    assert metrics.total_return == pytest.approx(0.0)
    assert metrics.max_drawdown == pytest.approx(0.0)
    assert metrics.sharpe is None
    assert metrics.sortino is None
    assert metrics.calmar is None


def test_single_point_curve_has_no_cagr() -> None:
    metrics = compute_metrics([], curve("1000"), Decimal("1000"), "1d")
    assert metrics.total_return == pytest.approx(0.0)
    assert metrics.cagr is None
    assert metrics.sharpe is None
    assert metrics.sortino is None


def test_no_losses_returns_none_profit_factor() -> None:
    metrics = compute_metrics(
        [make_trade("10"), make_trade("5")],
        curve("1000", "1015"),
        Decimal("1000"),
        "1d",
    )
    assert metrics.win_rate == pytest.approx(1.0)
    assert metrics.profit_factor is None
    assert metrics.average_loss is None
    assert metrics.average_win == pytest.approx(Decimal("7.5"))


def test_no_trades_still_reports_the_curve() -> None:
    metrics = compute_metrics([], curve("1000", "1050"), Decimal("1000"), "1d")
    assert metrics.total_return == pytest.approx(0.05)
    assert metrics.win_rate is None
    assert metrics.profit_factor is None
    assert metrics.expectancy is None
    assert metrics.trade_count == 0


def test_empty_curve_returns_empty_metrics() -> None:
    metrics = compute_metrics([], [], Decimal("1000"), "1d")
    assert metrics.total_return is None
    assert metrics.cagr is None
    assert metrics.max_drawdown is None
    assert metrics.sharpe is None
    assert metrics.sortino is None
    assert metrics.exposure is None
    assert metrics.trade_count == 0
    assert metrics.fees_paid == Decimal(0)


def test_downhill_equity_has_negative_return() -> None:
    metrics = compute_metrics([], curve("1000", "800"), Decimal("1000"), "1d")
    assert metrics.total_return == pytest.approx(-0.2)
    assert metrics.max_drawdown == pytest.approx(0.2)
    assert metrics.cagr is not None and metrics.cagr < 0
