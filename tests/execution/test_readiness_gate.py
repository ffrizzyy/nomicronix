from __future__ import annotations

from decimal import Decimal

import pytest

from nomicronix.execution.models import IntentState, OrderStatus, RiskRejected
from nomicronix.signal_delivery.readiness import CriterionResult, ReadinessReport
from tests.execution.fakes import NOW_MS, make_order
from tests.execution.harness import build, live_config
from tests.signal_delivery.fakes import make_candidate

PHRASE = "PLACE ORDER"


def report(*, ready: bool) -> ReadinessReport:
    return ReadinessReport(
        ready=ready,
        closed_trades=30 if ready else 0,
        track_record_days=Decimal("28") if ready else Decimal(0),
        gross_pnl=Decimal("10"),
        net_pnl=Decimal("5"),
        expectancy=Decimal("0.5") if ready else Decimal(0),
        max_drawdown_pct=Decimal("0.01"),
        costs=Decimal("5"),
        criteria=(
            CriterionResult(
                key="closed-trades",
                label="closed paper trades",
                passed=ready,
                actual="30" if ready else "0",
                required=">= 30",
            ),
        ),
    )


def armed(readiness=None, **config: object):
    harness = build(
        config=live_config(require_readiness=True, **config),
        readiness=readiness,
    )
    signal = make_candidate()
    intent = harness.service.shadow(signal, now_ms=NOW_MS)
    return harness, intent


def test_live_submission_without_a_readiness_checker_fails_closed() -> None:
    harness, intent = armed()
    try:
        with pytest.raises(RiskRejected) as caught:
            harness.service.submit(intent.intent_id, PHRASE, live=True)

        assert caught.value.code == "readiness_unavailable"
        assert caught.value.exit_code == 2
        assert harness.adapter.create_calls == []
        assert harness.adapter.private_calls == []
        stored = harness.store.get_intent(intent.intent_id)
        assert stored is not None and stored.state is IntentState.AWAITING_APPROVAL
    finally:
        harness.close()


def test_a_failing_readiness_report_blocks_the_order_and_is_journaled() -> None:
    harness, intent = armed(readiness=lambda: report(ready=False))
    try:
        with pytest.raises(RiskRejected, match="closed-trades") as caught:
            harness.service.submit(intent.intent_id, PHRASE, live=True)

        assert caught.value.code == "readiness"
        assert caught.value.exit_code == 2
        assert harness.adapter.create_calls == []
        assert harness.adapter.private_calls == []

        refusal = [
            event
            for event in harness.store.events_for(intent.intent_id)
            if str(event.event_type) == "refused"
        ]
        assert refusal[-1].payload == {
            "reason": "readiness",
            "failed": ["closed-trades"],
            "closed_trades": 0,
            "track_record_days": "0",
        }
    finally:
        harness.close()


def test_a_passing_readiness_report_allows_the_order() -> None:
    harness, intent = armed(readiness=lambda: report(ready=True))
    try:
        result = harness.service.submit(intent.intent_id, PHRASE, live=True)

        assert result.intent.state is IntentState.FILLED
        assert len(harness.adapter.create_calls) == 1
        assert len(harness.risk.positions()) == 1
    finally:
        harness.close()


def test_the_gate_can_be_waived_explicitly_and_the_checker_is_not_called() -> None:
    calls: list[int] = []

    def checker() -> ReadinessReport:
        calls.append(1)
        return report(ready=False)

    harness = build(
        config=live_config(require_readiness=False),
        readiness=checker,
    )
    try:
        signal = make_candidate()
        intent = harness.service.shadow(signal, now_ms=NOW_MS)
        result = harness.service.submit(intent.intent_id, PHRASE, live=True)

        assert result.intent.state is IntentState.FILLED
        assert calls == []
    finally:
        harness.close()


def test_an_already_submitted_order_is_resumed_even_if_readiness_changes() -> None:
    state = {"ready": True}

    def checker() -> ReadinessReport:
        return report(ready=state["ready"])

    harness, intent = armed(readiness=checker, order_timeout_ms=3, poll_interval_ms=1)
    try:
        opened = make_order(
            client_order_id=intent.intent_id,
            status=OrderStatus.OPEN,
            raw_status="NEW",
        )
        harness.adapter.queue(intent.intent_id, opened)

        first = harness.service.submit(intent.intent_id, PHRASE, live=True)
        assert first.intent.state is IntentState.SUBMITTED

        state["ready"] = False
        second = harness.service.submit(intent.intent_id, PHRASE, live=True)

        assert second.intent.state is IntentState.SUBMITTED
        assert second.timed_out is True
        assert len(harness.adapter.create_calls) == 1
    finally:
        harness.close()
