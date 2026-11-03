import json
from decimal import Decimal

import pytest

from nomicronix.signal_delivery.models import (
    DeliveryRecord,
    DeliveryStatus,
    PaperTrade,
)
from nomicronix.signal_delivery.readiness import (
    PromotionCriteria,
    evaluate_readiness,
    render_readiness,
)
from nomicronix.signal_delivery.store import DeliveryStore

DAY_MS = 86_400_000
START_MS = 1_735_689_600_000


def trade(
    index: int,
    *,
    pnl: str,
    opened_at_ms: int,
    closed_at_ms: int,
    entry: str = "100",
    quantity: str = "1",
) -> PaperTrade:
    value = Decimal(pnl)
    return PaperTrade(
        signal_id=f"strategy:BTC/USDT:1h:{index}",
        symbol="BTC/USDT",
        quantity=Decimal(quantity),
        entry_price=Decimal(entry),
        stop_price=Decimal("95"),
        target_price=Decimal("115"),
        opened_at_ms=opened_at_ms,
        closed_at_ms=closed_at_ms,
        exit_price=Decimal("115") if value >= 0 else Decimal("95"),
        exit_reason="target" if value >= 0 else "stop",
        realized_pnl=value,
    )


def record(signal_id: str, status: DeliveryStatus = DeliveryStatus.DELIVERED) -> DeliveryRecord:
    return DeliveryRecord(
        signal_id=signal_id,
        status=status,
        payload_json="{}",
        recorded_at_ms=START_MS,
    )


def seed(
    store: DeliveryStore,
    trades: list[PaperTrade],
    *,
    delivered: int | None = None,
    failed: int = 0,
    pending: int = 0,
) -> None:
    for item in trades:
        store.put_trade(item)
    delivered_count = len(trades) if delivered is None else delivered
    for item in trades[:delivered_count]:
        store.put_delivery(record(item.signal_id))
    for index in range(failed):
        store.put_delivery(record(f"failed-{index}", DeliveryStatus.FAILED))
    for index in range(pending):
        store.put_delivery(record(f"pending-{index}", DeliveryStatus.PENDING))


def winning_trades(count: int = 30, *, days: int = 29) -> list[PaperTrade]:
    step = days * DAY_MS // max(count - 1, 1)
    return [
        trade(
            index,
            pnl="15",
            opened_at_ms=START_MS + index * step,
            closed_at_ms=START_MS + index * step + DAY_MS // 2,
        )
        for index in range(count)
    ]


def test_empty_ledger_is_not_ready() -> None:
    store = DeliveryStore(":memory:")
    try:
        report = evaluate_readiness(store, PromotionCriteria())
    finally:
        store.close()

    assert not report.ready
    assert report.closed_trades == 0
    assert report.track_record_days == 0
    assert report.expectancy == 0
    passed = {item.key for item in report.criteria if item.passed}
    assert passed == {"paper-drawdown", "delivery-health", "ledger-integrity"}


def test_a_full_track_record_passes_every_criterion() -> None:
    store = DeliveryStore(":memory:")
    try:
        trades = winning_trades()
        seed(store, trades)
        report = evaluate_readiness(store, PromotionCriteria())
    finally:
        store.close()

    assert report.ready
    assert report.closed_trades == 30
    assert report.track_record_days >= 28
    assert report.gross_pnl == Decimal("450")
    assert report.costs == Decimal("6")
    assert report.net_pnl == Decimal("444")
    assert report.expectancy == Decimal("14.8")
    assert report.max_drawdown_pct == 0
    assert all(item.passed for item in report.criteria)


def test_short_track_record_fails_days_and_count() -> None:
    store = DeliveryStore(":memory:")
    try:
        seed(store, winning_trades(5, days=1))
        report = evaluate_readiness(store, PromotionCriteria())
    finally:
        store.close()

    failed = {item.key for item in report.criteria if not item.passed}
    assert failed == {"closed-trades", "track-record-days"}
    assert not report.ready


def test_negative_expectancy_after_costs_fails() -> None:
    store = DeliveryStore(":memory:")
    try:
        trades = [
            trade(
                index,
                pnl="0",
                opened_at_ms=START_MS + index * DAY_MS,
                closed_at_ms=START_MS + index * DAY_MS + 1,
            )
            for index in range(30)
        ]
        seed(store, trades)
        report = evaluate_readiness(store, PromotionCriteria())
    finally:
        store.close()

    assert not report.ready
    assert report.net_pnl < 0
    failed = {item.key for item in report.criteria if not item.passed}
    assert failed == {"net-expectancy"}
    assert report.costs == Decimal("6")


def test_paper_drawdown_above_the_limit_fails() -> None:
    store = DeliveryStore(":memory:")
    criteria = PromotionCriteria(
        min_closed_trades=2,
        min_days=0,
        assumed_round_trip_cost_pct=Decimal("0"),
    )
    try:
        seed(
            store,
            [
                trade(0, pnl="-300", opened_at_ms=START_MS, closed_at_ms=START_MS + 1),
                trade(1, pnl="301", opened_at_ms=START_MS + 2, closed_at_ms=START_MS + 3),
            ],
        )
        report = evaluate_readiness(store, criteria)
    finally:
        store.close()

    assert report.expectancy > 0
    assert report.max_drawdown_pct == Decimal("0.3")
    failed = {item.key for item in report.criteria if not item.passed}
    assert failed == {"paper-drawdown"}


def test_failed_or_pending_deliveries_block_readiness() -> None:
    store = DeliveryStore(":memory:")
    try:
        seed(store, winning_trades(), failed=1, pending=1)
        report = evaluate_readiness(store, PromotionCriteria())
    finally:
        store.close()

    assert not report.ready
    failed = {item.key for item in report.criteria if not item.passed}
    assert failed == {"delivery-health"}


def test_missing_delivery_rows_break_ledger_integrity() -> None:
    store = DeliveryStore(":memory:")
    try:
        seed(store, winning_trades(), delivered=29)
        report = evaluate_readiness(store, PromotionCriteria())
    finally:
        store.close()

    assert not report.ready
    failed = {item.key for item in report.criteria if not item.passed}
    assert failed == {"ledger-integrity"}


def test_initial_cash_must_be_positive() -> None:
    store = DeliveryStore(":memory:")
    try:
        with pytest.raises(ValueError, match="initial_cash"):
            evaluate_readiness(store, PromotionCriteria(), initial_cash=Decimal("0"))
    finally:
        store.close()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"min_closed_trades": 0}, "min_closed_trades"),
        ({"min_days": -1}, "min_days"),
        ({"assumed_round_trip_cost_pct": Decimal("-0.1")}, "assumed_round_trip"),
        ({"assumed_round_trip_cost_pct": Decimal("0.1")}, "assumed_round_trip"),
        ({"max_paper_drawdown_pct": Decimal("0")}, "max_paper_drawdown"),
        ({"max_paper_drawdown_pct": Decimal("1")}, "max_paper_drawdown"),
    ],
)
def test_criteria_validation(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        PromotionCriteria(**kwargs)  # type: ignore[arg-type]


def test_render_and_payload_are_stable() -> None:
    store = DeliveryStore(":memory:")
    try:
        seed(store, winning_trades())
        report = evaluate_readiness(store, PromotionCriteria())
    finally:
        store.close()

    text = render_readiness(report)
    assert text.startswith("execution readiness: READY (6 of 6 criteria met)")
    assert "[PASS] closed paper trades: 30 (need >= 30)" in text
    assert "[FAIL]" not in text
    assert "expectancy: 14.8 USDT/trade" in text

    payload = report.payload()
    assert payload["kind"] == "execution-readiness"
    assert payload["ready"] is True
    assert payload["net_pnl"] == "444"
    assert payload["criteria"][0]["key"] == "closed-trades"
    assert json.loads(json.dumps(payload, sort_keys=True)) == payload

    empty = DeliveryStore(":memory:")
    try:
        not_ready = evaluate_readiness(empty, PromotionCriteria())
    finally:
        empty.close()
    assert "NOT READY" in render_readiness(not_ready)
