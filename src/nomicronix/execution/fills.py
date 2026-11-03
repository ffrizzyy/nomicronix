"""Aggregate exchange fills and report them to the risk engine exactly once.

Spot market orders can fill in pieces, and a canceled order can leave a partial
position behind. ``risk-engine`` v1.1 accepts one positive piece at a time and
consumes the approval only when the pieces add up to the approved quantity, so
this module reports the *increment* on every exchange update, never the
cumulative quantity twice.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from nomicronix.execution.models import (
    TERMINAL_STATES,
    EventType,
    ExchangeError,
    ExchangeOrder,
    IntentState,
    OrderEvent,
    OrderIntent,
    OrderStatus,
)
from nomicronix.execution.store import ExecutionStore
from nomicronix.risk_engine.models import Fill, FillAction
from nomicronix.risk_engine.service import RiskService

_ZERO = Decimal(0)


def _event(
    intent: OrderIntent, event_type: EventType, now_ms: int, payload: dict[str, object]
) -> OrderEvent:
    return OrderEvent(intent.intent_id, event_type, now_ms, payload)


class FillReporter:
    """Turn exchange order updates into risk fills and journal transitions."""

    def __init__(self, risk: RiskService, store: ExecutionStore) -> None:
        self._risk = risk
        self._store = store

    def _terminal_state_and_event(
        self, order: ExchangeOrder, filled: Decimal
    ) -> tuple[IntentState, EventType]:
        if order.status is OrderStatus.REJECTED:
            if filled == 0:
                return IntentState.REJECTED, EventType.REJECTED
            return IntentState.FAILED, EventType.FAILED
        if order.status in (OrderStatus.CANCELED, OrderStatus.EXPIRED):
            return IntentState.CANCELED, EventType.CANCELED
        # A closed order with a short fill is a canceled remainder in disguise.
        return IntentState.CANCELED, EventType.CANCELED

    def apply(self, intent: OrderIntent, order: ExchangeOrder, *, now_ms: int) -> OrderIntent:
        """Apply one exchange snapshot. Safe to call with an unchanged snapshot."""
        if order.client_order_id != intent.intent_id:
            raise ExchangeError("exchange order does not belong to this intent", code="wrong_order")
        if order.symbol != intent.symbol:
            raise ExchangeError("exchange order symbol mismatch", code="wrong_order")
        if order.quantity > intent.quantity:
            raise ExchangeError(
                "exchange order quantity exceeds the approved quantity",
                code="overfilled",
            )

        previous_order = self._store.get_order(intent.intent_id)
        previous_fee = _ZERO if previous_order is None else previous_order.fee
        filled = order.filled_quantity
        if filled < intent.filled_quantity:
            raise ExchangeError(
                "exchange reported a fill below the journaled quantity",
                code="fill_regressed",
            )
        if filled > intent.quantity:
            raise ExchangeError(
                "exchange reported more than the approved quantity", code="overfilled"
            )
        if order.fee < previous_fee:
            raise ExchangeError("exchange fee went backwards", code="bad_fill")

        current = intent
        increment = filled - intent.filled_quantity
        if increment > 0:
            if order.average_price is None:
                raise ExchangeError(
                    "exchange reported a fill without an average price",
                    code="bad_fill",
                )
            previous_average = intent.average_fill_price or _ZERO
            incremental_notional = (
                filled * order.average_price - intent.filled_quantity * previous_average
            )
            incremental_price = incremental_notional / increment
            if incremental_price <= 0:
                raise ExchangeError("exchange reported a non-positive fill price", code="bad_fill")
            fee_increment = order.fee - previous_fee
            self._risk.record_fill(
                intent.signal_id,
                Fill(
                    action=FillAction.OPEN,
                    symbol=intent.symbol,
                    quantity=increment,
                    price=incremental_price,
                    fee=fee_increment,
                    filled_at_ms=now_ms,
                ),
                now_ms=now_ms,
            )
            complete = filled == intent.quantity
            state = IntentState.FILLED if complete else IntentState.SUBMITTED
            event_type = EventType.FILLED if complete else EventType.PARTIALLY_FILLED
            event = _event(
                intent,
                event_type,
                now_ms,
                {
                    "filled_quantity": str(filled),
                    "increment_quantity": str(increment),
                    "increment_price": str(incremental_price),
                    "average_price": str(order.average_price),
                    "fee_increment": str(fee_increment),
                },
            )
            self._store.update_intent(
                intent.intent_id,
                state=state,
                filled_quantity=filled,
                average_fill_price=order.average_price,
                event=event,
            )
            current = replace(
                intent,
                state=state,
                filled_quantity=filled,
                average_fill_price=order.average_price,
            )

        if order.terminal and current.state not in TERMINAL_STATES:
            state, event_type = self._terminal_state_and_event(order, filled)
            remainder = intent.quantity - filled
            payload: dict[str, object] = {
                "status": str(order.status),
                "raw_status": order.raw_status,
                "filled_quantity": str(filled),
                "remaining_quantity": str(remainder),
            }
            if remainder > 0:
                # No later fill may use the canceled remainder.
                self._risk.expire_approval(intent.signal_id, now_ms=now_ms)
                payload["expired_quantity"] = str(remainder)
            event = _event(intent, event_type, now_ms, payload)
            self._store.update_intent(
                intent.intent_id,
                state=state,
                event=event,
            )
            current = replace(current, state=state)

        self._store.put_order(order)
        return current
