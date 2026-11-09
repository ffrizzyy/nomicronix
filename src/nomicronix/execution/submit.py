"""Human-approved submission of one spot market order.

The four gates are all required before the exchange is touched:

1. the exact phrase ``PLACE ORDER``;
2. ``dry_run = false`` in the validated config;
3. ``allow_live = true`` and ``stage = "approved"`` in that config;
4. the ``--live`` flag on this invocation.

A duplicate submit never creates a second order: the intent id is the client
order id, and an intent already ``submitted`` is only polled.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import NoReturn

from nomicronix.execution.adapter import ExchangeAdapter
from nomicronix.execution.config import ExecutionConfig
from nomicronix.execution.fills import FillReporter
from nomicronix.execution.models import (
    EventType,
    ExchangeError,
    ExchangeOrder,
    IntentError,
    IntentState,
    LiveDisabled,
    OrderEvent,
    OrderIntent,
    Reconciliation,
    ReconciliationError,
    RiskRejected,
    Ticker,
)
from nomicronix.execution.shadow import slippage_bps
from nomicronix.execution.store import ExecutionStore
from nomicronix.risk_engine.service import RiskService
from nomicronix.signal_delivery.readiness import ReadinessReport

CONFIRM_PHRASE = "PLACE ORDER"
ReadinessChecker = Callable[[], ReadinessReport]


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    intent: OrderIntent
    order: ExchangeOrder | None
    timed_out: bool


class Submitter:
    def __init__(
        self,
        risk: RiskService,
        store: ExecutionStore,
        adapter: ExchangeAdapter,
        config: ExecutionConfig,
        *,
        clock: Callable[[], int],
        sleep: Callable[[float], None] = time.sleep,
        reconciler: Callable[[int], Reconciliation] | None = None,
        readiness: ReadinessChecker | None = None,
    ) -> None:
        self._risk = risk
        self._store = store
        self._adapter = adapter
        self._config = config
        self._clock = clock
        self._sleep = sleep
        self._reconciler = reconciler
        self._readiness = readiness
        self._fills = FillReporter(risk, store)

    def _journal(
        self,
        intent: OrderIntent,
        event_type: EventType,
        now_ms: int,
        payload: dict[str, object],
    ) -> None:
        self._store.append_event(OrderEvent(intent.intent_id, event_type, now_ms, payload))

    def _refuse(self, intent: OrderIntent, now_ms: int, reason: str) -> NoReturn:
        self._journal(intent, EventType.REFUSED, now_ms, {"reason": reason})
        raise RiskRejected(f"submission refused: {reason}", code=reason)

    def _reject(self, intent: OrderIntent, now_ms: int, reason: str) -> NoReturn:
        self._store.update_intent(
            intent.intent_id,
            state=IntentState.REJECTED,
            event=OrderEvent(
                intent.intent_id,
                EventType.REJECTED,
                now_ms,
                {"reasons": [reason]},
            ),
        )
        raise RiskRejected(f"intent rejected: {reason}", code=reason)

    def _check_readiness(self, intent: OrderIntent, now_ms: int) -> None:
        """Fail closed unless the paper record meets the promotion criteria.

        Readiness is a global promotion gate, not an order-level rule, so it is
        checked once a live order is otherwise ready to be sent. It is skipped
        only when the owner deliberately sets ``require_readiness = false``.
        """
        if not self._config.require_readiness:
            return
        if self._readiness is None:
            self._refuse(intent, now_ms, "readiness_unavailable")
        report = self._readiness()
        if report.ready:
            return
        failed = [item.key for item in report.criteria if not item.passed]
        self._journal(
            intent,
            EventType.REFUSED,
            now_ms,
            {
                "reason": "readiness",
                "failed": failed,
                "closed_trades": report.closed_trades,
                "track_record_days": str(report.track_record_days),
            },
        )
        raise RiskRejected(
            "execution readiness gate failed: " + ", ".join(failed),
            code="readiness",
        )

    def submit(
        self,
        intent_id: str,
        confirmation: str,
        *,
        live: bool,
    ) -> SubmissionResult:
        if confirmation != CONFIRM_PHRASE:
            raise IntentError(f'submission requires the exact phrase "{CONFIRM_PHRASE}"')

        intent = self._store.get_intent(intent_id)
        if intent is None:
            raise IntentError(f"unknown intent {intent_id}")
        now = self._clock()

        if intent.terminal:
            return SubmissionResult(intent, self._current_order(intent), False)

        if (
            not live
            or self._config.dry_run
            or not self._config.allow_live
            or self._config.stage != "approved"
        ):
            self._journal(
                intent,
                EventType.REFUSED,
                now,
                {
                    "reason": "live_disabled",
                    "live_flag": live,
                    "dry_run": self._config.dry_run,
                    "allow_live": self._config.allow_live,
                    "stage": self._config.stage,
                },
            )
            raise LiveDisabled(
                "live submission is disabled: it needs --live, dry_run = false, "
                "allow_live = true and stage = approved"
            )

        if intent.state is IntentState.SUBMITTED:
            return self._resume(intent, now)

        if intent.state is not IntentState.AWAITING_APPROVAL:
            raise IntentError(f"intent {intent_id} is {intent.state} and cannot be submitted")

        if now >= intent.expires_at_ms:
            self._reject(intent, now, "expired")
        if now - intent.created_at_ms > self._config.max_intent_age_ms:
            self._reject(intent, now, "stale_intent")
        if self._risk.has_open_position(intent.symbol, now_ms=now):
            self._refuse(intent, now, "duplicate_position")

        self._check_readiness(intent, now)

        adapter = self._adapter
        ticker = adapter.fetch_ticker(intent.symbol)
        if slippage_bps(intent.entry_price, ticker.ask) > Decimal(self._config.max_slippage_bps):
            self._reject(intent, now, "slippage")

        self._risk.mark({intent.symbol: ticker.last}, now_ms=now)
        status = self._risk.status(now_ms=now)
        if not status.trading_allowed:
            reason = "kill_switch" if status.kill_switch is not None else "risk_blocked"
            self._refuse(intent, now, reason)

        if self._reconciler is None:
            raise ReconciliationError("submission requires a reconciliation")
        report = self._reconciler(now)
        if not report.clean:
            self._journal(
                intent,
                EventType.REFUSED,
                now,
                {
                    "reason": "reconciliation_unclean",
                    "detail": list(report.detail),
                },
            )
            raise ReconciliationError("reconciliation is not clean; resolve it before submitting")

        return self._send(intent, ticker, now)

    def _current_order(self, intent: OrderIntent) -> ExchangeOrder | None:
        if intent.exchange_order_id is None:
            return None
        return self._store.get_order(intent.intent_id)

    def _resume(self, intent: OrderIntent, now_ms: int) -> SubmissionResult:
        order = self._current_order(intent)
        if order is None:
            raise ExchangeError(
                "intent is submitted but has no exchange order id; run reconcile",
                code="unconfirmed_submission",
            )
        updated = self._fills.apply(intent, order, now_ms=now_ms)
        if updated.terminal:
            return SubmissionResult(updated, order, False)
        return self._poll(updated, order, now_ms)

    def _send(self, intent: OrderIntent, ticker: Ticker, now_ms: int) -> SubmissionResult:
        self._store.update_intent(
            intent.intent_id,
            state=IntentState.SUBMITTED,
            event=OrderEvent(
                intent.intent_id,
                EventType.SUBMITTED,
                now_ms,
                {
                    "client_order_id": intent.intent_id,
                    "symbol": intent.symbol,
                    "quantity": str(intent.quantity),
                    "ask": str(ticker.ask),
                },
            ),
        )
        submitted = replace(intent, state=IntentState.SUBMITTED)
        try:
            order = self._adapter.create_market_order(
                intent.symbol, "buy", intent.quantity, intent.intent_id
            )
        except ExchangeError as exc:
            if exc.code in {"network_error", "rate_limit"}:
                # The order may exist at the exchange. Leave the intent
                # submitted so reconciliation resolves it; never resubmit.
                self._journal(
                    submitted,
                    EventType.FAILED,
                    now_ms,
                    {"code": exc.code, "unconfirmed": True},
                )
                raise ExchangeError(
                    f"submission outcome unknown ({exc.code}); run reconcile",
                    code=exc.code,
                ) from exc
            self._store.update_intent(
                intent.intent_id,
                state=IntentState.FAILED,
                event=OrderEvent(
                    intent.intent_id,
                    EventType.FAILED,
                    now_ms,
                    {"code": exc.code},
                ),
            )
            raise

        self._store.update_intent(
            intent.intent_id,
            exchange_order_id=order.exchange_order_id,
        )
        submitted = replace(submitted, exchange_order_id=order.exchange_order_id)
        updated = self._fills.apply(submitted, order, now_ms=now_ms)
        if updated.terminal:
            return SubmissionResult(updated, order, False)
        return self._poll(updated, order, now_ms)

    def _poll(self, intent: OrderIntent, order: ExchangeOrder, now_ms: int) -> SubmissionResult:
        deadline = now_ms + self._config.order_timeout_ms
        max_attempts = max(1, self._config.order_timeout_ms // self._config.poll_interval_ms)
        attempts = 0
        current = intent
        latest = order
        while not latest.terminal and attempts < max_attempts:
            if self._clock() >= deadline:
                break
            self._sleep(self._config.poll_interval_ms / 1000)
            attempts += 1
            latest = self._adapter.fetch_order(order.exchange_order_id, order.symbol)
            current = self._fills.apply(current, latest, now_ms=self._clock())
        return SubmissionResult(current, latest, not latest.terminal)
