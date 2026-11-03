from decimal import Decimal
from pathlib import Path

import pytest

from nomicronix.signal_delivery.models import (
    DeliveryAction,
    DeliveryError,
    DeliveryStatus,
)
from tests.signal_delivery.fakes import Harness, make_candidate


def test_approved_signal_is_delivered_and_opened(harness: Harness) -> None:
    signal = make_candidate()

    outcome = harness.service.deliver(signal)

    assert outcome.action is DeliveryAction.DELIVERED
    assert outcome.record.status is DeliveryStatus.DELIVERED
    assert harness.sinks[0].sent[0].signal_id == signal.signal_id
    assert harness.sinks[0].sent[0].approved
    trade = harness.delivery_store.get_trade(signal.signal_id)
    assert trade is not None
    assert trade.is_open
    assert trade.quantity == Decimal("1")
    assert harness.risk.status().open_positions == 1
    assert harness.delivery_store.delivery_totals().delivered == 1


def test_rejected_signal_is_recorded_but_not_sent(harness: Harness) -> None:
    signal = make_candidate(stop="97", target="103")

    outcome = harness.service.deliver(signal)

    assert outcome.action is DeliveryAction.REJECTED
    assert outcome.record.status is DeliveryStatus.REJECTED
    assert harness.sinks[0].sent == []
    assert harness.delivery_store.get_trade(signal.signal_id) is None
    assert "reward_risk_too_low" in outcome.record.payload_json
    assert harness.risk.status().open_positions == 0


def test_second_run_skips_an_already_delivered_signal(harness: Harness) -> None:
    signal = make_candidate()
    first = harness.service.deliver(signal)
    second = harness.service.deliver(signal)

    assert first.action is DeliveryAction.DELIVERED
    assert second.action is DeliveryAction.SKIPPED
    assert len(harness.sinks[0].sent) == 1
    assert len(harness.delivery_store.all_trades()) == 1


def test_failed_sink_is_retried_from_the_outbox(harness: Harness) -> None:
    signal = make_candidate()
    harness.sinks[0].fail = True

    with pytest.raises(DeliveryError, match="fake sink failed"):
        harness.service.deliver(signal)

    failed = harness.delivery_store.get_delivery(signal.signal_id)
    assert failed is not None
    assert failed.status is DeliveryStatus.FAILED
    assert len(harness.delivery_store.all_trades()) == 1

    harness.sinks[0].fail = False
    outcome = harness.service.deliver(signal)

    assert outcome.action is DeliveryAction.RETRIED
    assert outcome.record.status is DeliveryStatus.DELIVERED
    assert len(harness.sinks[0].sent) == 1
    assert len(harness.delivery_store.all_trades()) == 1


def test_duplicate_symbol_is_rejected_by_risk(harness: Harness) -> None:
    first = make_candidate(0)
    second = make_candidate(1)
    assert harness.service.deliver(first).action is DeliveryAction.DELIVERED

    outcome = harness.service.deliver(second)

    assert outcome.action is DeliveryAction.REJECTED
    assert "duplicate_position" in outcome.record.payload_json
    assert harness.delivery_store.get_trade(second.signal_id) is None
    assert len(harness.delivery_store.all_trades()) == 1


def test_a_second_allowed_symbol_can_open(harness: Harness) -> None:
    btc = make_candidate(0)
    eth = make_candidate(1, symbol="ETH/USDT")

    assert harness.service.deliver(btc).action is DeliveryAction.DELIVERED
    assert harness.service.deliver(eth).action is DeliveryAction.DELIVERED
    assert harness.risk.status().open_positions == 2


def test_a_signal_checked_late_is_stale(harness: Harness) -> None:
    signal = make_candidate()
    outcome = harness.service.deliver(signal, now_ms=signal.created_at_ms + 600_000)
    assert outcome.action is DeliveryAction.REJECTED
    assert "stale_signal" in outcome.record.payload_json


def test_tick_closes_a_gap_through_the_stop_at_the_worse_price(harness: Harness) -> None:
    signal = make_candidate()
    harness.service.deliver(signal)

    result = harness.service.tick({"BTC/USDT": Decimal("94")}, now_ms=signal.created_at_ms + 60_000)

    assert len(result.closed) == 1
    closed = result.closed[0]
    assert closed.exit_reason == "stop"
    assert closed.exit_price == Decimal("94")
    assert closed.realized_pnl == Decimal("-6")
    assert result.still_open == ()
    assert harness.risk.status().open_positions == 0


def test_tick_closes_at_the_target(harness: Harness) -> None:
    signal = make_candidate()
    harness.service.deliver(signal)

    result = harness.service.tick(
        {"BTC/USDT": Decimal("116")}, now_ms=signal.created_at_ms + 60_000
    )

    assert len(result.closed) == 1
    assert result.closed[0].exit_reason == "target"
    assert result.closed[0].exit_price == Decimal("115")
    assert result.closed[0].realized_pnl == Decimal("15")


def test_tick_keeps_a_trade_open_between_the_levels(harness: Harness) -> None:
    signal = make_candidate()
    harness.service.deliver(signal)

    result = harness.service.tick(
        {"BTC/USDT": Decimal("105")}, now_ms=signal.created_at_ms + 60_000
    )

    assert result.closed == ()
    assert len(result.still_open) == 1


def test_tick_ignores_symbols_without_a_price(harness: Harness) -> None:
    signal = make_candidate()
    harness.service.deliver(signal)

    result = harness.service.tick({"ETH/USDT": Decimal("50")}, now_ms=signal.created_at_ms + 60_000)

    assert result.closed == ()
    assert len(result.still_open) == 1


def test_tick_rejects_empty_or_invalid_prices(harness: Harness) -> None:
    with pytest.raises(ValueError, match="at least one price"):
        harness.service.tick({})
    with pytest.raises(ValueError, match="positive"):
        harness.service.tick({"BTC/USDT": Decimal("0")})


def test_service_requires_a_sink(harness: Harness) -> None:
    from nomicronix.signal_delivery.models import InstrumentRules
    from nomicronix.signal_delivery.service import DeliveryService

    with pytest.raises(ValueError, match="sink"):
        DeliveryService(
            harness.risk,
            harness.delivery_store,
            [],
            InstrumentRules(),
        )


def test_a_corrupt_outbox_payload_is_marked_failed(harness: Harness) -> None:
    from nomicronix.signal_delivery.models import DeliveryRecord

    signal = make_candidate()
    harness.delivery_store.put_delivery(
        DeliveryRecord(
            signal_id=signal.signal_id,
            status=DeliveryStatus.PENDING,
            payload_json="[]",
            recorded_at_ms=signal.created_at_ms,
        )
    )

    with pytest.raises(DeliveryError, match="stored notification is invalid"):
        harness.service.deliver(signal)

    record = harness.delivery_store.get_delivery(signal.signal_id)
    assert record is not None
    assert record.status is DeliveryStatus.FAILED
    assert harness.sinks[0].sent == []
    assert harness.delivery_store.get_trade(signal.signal_id) is None


def test_an_unexpected_sink_error_is_wrapped(harness: Harness) -> None:
    class ExplodingSink:
        name = "exploding"

        def send(self, notification: object) -> None:
            raise RuntimeError("boom")

    from nomicronix.signal_delivery.models import InstrumentRules
    from nomicronix.signal_delivery.service import DeliveryService

    service = DeliveryService(
        harness.risk,
        harness.delivery_store,
        [ExplodingSink()],
        InstrumentRules(),
        clock=harness.clock,
    )
    signal = make_candidate()

    with pytest.raises(DeliveryError, match="exploding sink failed: boom"):
        service.deliver(signal)

    record = harness.delivery_store.get_delivery(signal.signal_id)
    assert record is not None
    assert record.status is DeliveryStatus.FAILED
    assert record.error == "exploding sink failed: boom"


def test_deliver_file_reads_a_strategy_engine_result(harness: Harness, tmp_path: Path) -> None:
    from tests.signal_delivery.fakes import write_signals

    path = write_signals(tmp_path / "signals.json", [make_candidate(), make_candidate(1)])
    outcomes = harness.service.deliver_file(path)
    assert [outcome.action for outcome in outcomes] == [
        DeliveryAction.DELIVERED,
        DeliveryAction.REJECTED,
    ]
