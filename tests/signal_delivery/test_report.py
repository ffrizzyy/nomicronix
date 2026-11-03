from decimal import Decimal

from nomicronix.signal_delivery.report import build_report, render_report
from tests.signal_delivery.fakes import Harness, make_candidate


def test_report_totals_a_closed_winner(harness: Harness) -> None:
    signal = make_candidate()
    harness.service.deliver(signal)
    harness.service.tick({"BTC/USDT": Decimal("116")}, now_ms=signal.created_at_ms + 60_000)

    report = build_report(harness.delivery_store, harness.risk)

    assert report.totals.total == 1
    assert report.totals.delivered == 1
    assert report.closed_trades == 1
    assert report.open_trades == 0
    assert report.realized_pnl == Decimal("15")
    assert report.win_rate == 1.0
    assert report.equity == Decimal("1015")

    text = render_report(report)
    assert "deliveries: 1 total | delivered 1" in text
    assert "realized pnl: 15 USDT" in text
    assert "win rate: 100.00%" in text
    assert "equity: 1015 USDT" in text


def test_report_with_no_trades_has_no_win_rate(harness: Harness) -> None:
    report = build_report(harness.delivery_store, harness.risk)
    assert report.win_rate is None
    assert report.realized_pnl == Decimal("0")
    assert "win rate: n/a" in render_report(report)


def test_rejected_signals_appear_in_the_outbox_totals(harness: Harness) -> None:
    harness.service.deliver(make_candidate(stop="97", target="103"))

    report = build_report(harness.delivery_store, harness.risk)

    assert report.totals.rejected == 1
    assert report.closed_trades == 0
    assert report.open_trades == 0
