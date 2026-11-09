from __future__ import annotations

from decimal import Decimal

import pytest

from nomicronix.execution.fills import FillReporter
from nomicronix.execution.models import ExchangeError, IntentState, OrderStatus
from nomicronix.risk_engine.models import Fill, FillAction
from nomicronix.risk_engine.store import ApprovalError
from tests.execution.fakes import NOW_MS, make_intent, make_order
from tests.execution.harness import build, live_config
from tests.signal_delivery.fakes import make_candidate


def awaiting():
    harness = build(config=live_config(order_timeout_ms=10, poll_interval_ms=1))
    signal = make_candidate()
    intent = harness.service.shadow(signal, now_ms=signal.created_at_ms)
    return harness, signal, intent


def test_a_full_fill_reports_the_approved_quantity_once() -> None:
    harness, signal, intent = awaiting()
    try:
        result = harness.service.submit(intent.intent_id, "PLACE ORDER", live=True)
        approval = harness.risk_store.load_approval(signal.signal_id)
        assert result.intent.state is IntentState.FILLED
        assert approval is not None
        assert approval.filled_quantity == Decimal("1")
        assert approval.consumed_at_ms is not None
        assert len(harness.risk.positions()) == 1
        assert harness.risk.positions()[0].quantity == Decimal("1")
    finally:
        harness.close()


def test_partial_pieces_are_aggregated_and_reported_once() -> None:
    harness, signal, intent = awaiting()
    try:
        harness.adapter.queue(
            intent.intent_id,
            make_order(
                client_order_id=intent.intent_id,
                status=OrderStatus.OPEN,
                filled_quantity="0.4",
                average_price="100",
                fee="0.04",
                raw_status="PARTIALLY_FILLED",
            ),
            make_order(
                client_order_id=intent.intent_id,
                status=OrderStatus.CLOSED,
                filled_quantity="1",
                average_price="102",
                fee="0.1",
                raw_status="FILLED",
            ),
        )

        result = harness.service.submit(intent.intent_id, "PLACE ORDER", live=True)

        assert result.intent.state is IntentState.FILLED
        assert result.intent.filled_quantity == Decimal("1")
        assert result.intent.average_fill_price == Decimal("102")
        approval = harness.risk_store.load_approval(signal.signal_id)
        assert approval is not None and approval.consumed_at_ms is not None
        positions = harness.risk.positions()
        assert len(positions) == 1
        assert positions[0].quantity == Decimal("1")
        assert positions[0].entry_price == Decimal("102")
        assert positions[0].entry_fee == Decimal("0.1")
        events = [str(event.event_type) for event in harness.store.events_for(intent.intent_id)]
        assert events[-3:] == ["submitted", "partially_filled", "filled"]
    finally:
        harness.close()


def test_a_crash_between_risk_and_journal_replays_safely() -> None:
    harness, signal, intent = awaiting()
    try:
        order = make_order(
            client_order_id=intent.intent_id,
            status=OrderStatus.CLOSED,
            quantity="1",
            filled_quantity="1",
            average_price="100",
            raw_status="FILLED",
        )
        reporter = FillReporter(harness.risk, harness.store)
        original = harness.store.update_intent

        def fail_update(*args: object, **kwargs: object) -> None:
            raise RuntimeError("simulated crash after the risk commit")

        harness.store.update_intent = fail_update  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="simulated crash"):
            reporter.apply(intent, order, now_ms=NOW_MS)
        harness.store.update_intent = original  # type: ignore[method-assign]

        # Risk committed the fill; the execution journal did not.
        positions = harness.risk.positions()
        assert len(positions) == 1
        assert positions[0].quantity == Decimal("1")

        replayed = reporter.apply(intent, order, now_ms=NOW_MS)
        assert replayed.state is IntentState.FILLED
        assert harness.risk.positions()[0].quantity == Decimal("1")
    finally:
        harness.close()


def test_a_canceled_remainder_is_expired_and_never_opens_a_second_position() -> None:
    harness, signal, intent = awaiting()
    try:
        harness.adapter.queue(
            intent.intent_id,
            make_order(
                client_order_id=intent.intent_id,
                status=OrderStatus.CANCELED,
                filled_quantity="0.4",
                average_price="100",
                fee="0.04",
                raw_status="CANCELED",
            ),
        )

        result = harness.service.submit(intent.intent_id, "PLACE ORDER", live=True)

        assert result.intent.state is IntentState.CANCELED
        assert result.intent.filled_quantity == Decimal("0.4")
        positions = harness.risk.positions()
        assert len(positions) == 1
        assert positions[0].quantity == Decimal("0.4")

        approval = harness.risk_store.load_approval(signal.signal_id)
        assert approval is not None
        assert approval.filled_quantity == Decimal("0.4")
        assert approval.consumed_at_ms is not None

        with pytest.raises(ApprovalError, match="already consumed"):
            harness.risk.record_fill(
                signal.signal_id,
                Fill(
                    FillAction.OPEN,
                    signal.symbol,
                    Decimal("0.6"),
                    Decimal("100"),
                    Decimal("0"),
                    signal.created_at_ms,
                ),
                now_ms=signal.created_at_ms,
            )
        assert len(harness.risk.positions()) == 1
    finally:
        harness.close()


def test_a_canceled_order_with_no_fill_expires_the_approval() -> None:
    harness, signal, intent = awaiting()
    try:
        harness.adapter.queue(
            intent.intent_id,
            make_order(
                client_order_id=intent.intent_id,
                status=OrderStatus.CANCELED,
                raw_status="CANCELED",
            ),
        )
        result = harness.service.submit(intent.intent_id, "PLACE ORDER", live=True)
        assert result.intent.state is IntentState.CANCELED
        assert harness.risk.positions() == ()
        approval = harness.risk_store.load_approval(signal.signal_id)
        assert approval is not None and approval.consumed_at_ms is not None
    finally:
        harness.close()


def test_an_exchange_rejection_never_consumes_the_approval() -> None:
    harness, signal, intent = awaiting()
    try:
        harness.adapter.queue(
            intent.intent_id,
            make_order(
                client_order_id=intent.intent_id,
                status=OrderStatus.REJECTED,
                raw_status="REJECTED",
            ),
        )
        result = harness.service.submit(intent.intent_id, "PLACE ORDER", live=True)
        assert result.intent.state is IntentState.REJECTED
        assert harness.risk.positions() == ()
        approval = harness.risk_store.load_approval(signal.signal_id)
        assert approval is not None and approval.consumed_at_ms is not None
    finally:
        harness.close()


def test_an_overfill_is_refused_before_risk_sees_it() -> None:
    harness, _, intent = awaiting()
    try:
        harness.adapter.queue(
            intent.intent_id,
            make_order(
                client_order_id=intent.intent_id,
                quantity="2",
                status=OrderStatus.CLOSED,
                filled_quantity="2",
                average_price="100",
                raw_status="FILLED",
            ),
        )
        with pytest.raises(ExchangeError) as caught:
            harness.service.submit(intent.intent_id, "PLACE ORDER", live=True)
        assert caught.value.code == "overfilled"
        assert harness.risk.positions() == ()
    finally:
        harness.close()


def test_a_reporter_refuses_an_order_for_another_intent() -> None:
    from nomicronix.execution.fills import FillReporter

    harness, _, intent = awaiting()
    try:
        reporter = FillReporter(harness.risk, harness.store)
        with pytest.raises(ExchangeError) as caught:
            reporter.apply(
                intent,
                make_order(client_order_id="nx-other"),
                now_ms=NOW_MS,
            )
        assert caught.value.code == "wrong_order"
        with pytest.raises(ExchangeError):
            reporter.apply(
                intent,
                make_order(client_order_id=intent.intent_id, symbol="ETH/USDT"),
                now_ms=NOW_MS,
            )
    finally:
        harness.close()


def test_a_reporter_refuses_a_regressed_fill() -> None:
    from nomicronix.execution.fills import FillReporter

    harness, _, _ = awaiting()
    try:
        filled = make_intent(
            signal_id="sig-open",
            state=IntentState.SUBMITTED,
            filled_quantity="0.5",
            average_fill_price="100",
        )
        reporter = FillReporter(harness.risk, harness.store)
        with pytest.raises(ExchangeError) as caught:
            reporter.apply(
                filled,
                make_order(
                    client_order_id=filled.intent_id,
                    status=OrderStatus.OPEN,
                    filled_quantity="0.4",
                    average_price="100",
                ),
                now_ms=NOW_MS,
            )
        assert caught.value.code == "fill_regressed"
    finally:
        harness.close()


def test_a_reporter_refuses_a_backwards_fee() -> None:
    from nomicronix.execution.fills import FillReporter

    harness, _, _ = awaiting()
    try:
        submitted = make_intent(signal_id="sig-fee", state=IntentState.SUBMITTED)
        harness.store.create_intent(submitted)
        harness.store.put_order(
            make_order(
                client_order_id=submitted.intent_id,
                status=OrderStatus.OPEN,
                fee="0.2",
            )
        )
        reporter = FillReporter(harness.risk, harness.store)
        with pytest.raises(ExchangeError) as caught:
            reporter.apply(
                submitted,
                make_order(
                    client_order_id=submitted.intent_id,
                    status=OrderStatus.OPEN,
                    filled_quantity="0.5",
                    average_price="100",
                    fee="0.1",
                ),
                now_ms=NOW_MS,
            )
        assert caught.value.code == "bad_fill"
    finally:
        harness.close()


def test_a_closed_order_with_a_short_fill_still_expires_the_remainder() -> None:
    harness, signal, intent = awaiting()
    try:
        harness.adapter.queue(
            intent.intent_id,
            make_order(
                client_order_id=intent.intent_id,
                status=OrderStatus.CLOSED,
                filled_quantity="0.4",
                average_price="100",
                fee="0.04",
                raw_status="CLOSED",
            ),
        )
        result = harness.service.submit(intent.intent_id, "PLACE ORDER", live=True)
        assert result.intent.state is IntentState.CANCELED
        approval = harness.risk_store.load_approval(signal.signal_id)
        assert approval is not None and approval.consumed_at_ms is not None
    finally:
        harness.close()


def test_a_rejection_after_a_partial_fill_is_journaled_as_failed() -> None:
    harness, signal, intent = awaiting()
    try:
        harness.adapter.queue(
            intent.intent_id,
            make_order(
                client_order_id=intent.intent_id,
                status=OrderStatus.REJECTED,
                filled_quantity="0.4",
                average_price="100",
                fee="0.04",
                raw_status="REJECTED",
            ),
        )
        result = harness.service.submit(intent.intent_id, "PLACE ORDER", live=True)
        assert result.intent.state is IntentState.FAILED
        approval = harness.risk_store.load_approval(signal.signal_id)
        assert approval is not None and approval.consumed_at_ms is not None
    finally:
        harness.close()
