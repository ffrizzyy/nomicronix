"""Execution service: shadow intents, and later human-approved submission."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from nomicronix.execution.adapter import ExchangeAdapter
from nomicronix.execution.config import ExecutionConfig
from nomicronix.execution.fills import FillReporter
from nomicronix.execution.flatten import FlattenResult, flatten
from nomicronix.execution.models import (
    EventType,
    IntentError,
    IntentState,
    OrderEvent,
    OrderIntent,
    Reconciliation,
    Ticker,
    intent_id_for,
    utc_now_ms,
)
from nomicronix.execution.reconcile import reconcile
from nomicronix.execution.shadow import (
    approved_state,
    build_intent,
    build_rejected_intent,
    guard_reasons,
    slippage_bps,
)
from nomicronix.execution.store import ExecutionStore
from nomicronix.execution.submit import ReadinessChecker, SubmissionResult, Submitter
from nomicronix.risk_engine.models import Side, TradeProposal
from nomicronix.risk_engine.service import RiskService
from nomicronix.signal_delivery.signals import load_signals
from nomicronix.strategy_engine.models import SignalCandidate


class ExecutionService:
    """The only object allowed to turn an approved signal into an order intent."""

    def __init__(
        self,
        risk: RiskService,
        store: ExecutionStore,
        adapter: ExchangeAdapter,
        config: ExecutionConfig,
        *,
        clock: Callable[[], int] = utc_now_ms,
        sleep: Callable[[float], None] = time.sleep,
        readiness: ReadinessChecker | None = None,
    ) -> None:
        self._risk = risk
        self._store = store
        self._adapter = adapter
        self._config = config
        self._clock = clock
        self._sleep = sleep
        self._readiness = readiness

    @property
    def config(self) -> ExecutionConfig:
        return self._config

    @property
    def store(self) -> ExecutionStore:
        return self._store

    @property
    def adapter(self) -> ExchangeAdapter:
        return self._adapter

    @property
    def risk(self) -> RiskService:
        return self._risk

    def _proposal(self, signal: SignalCandidate) -> TradeProposal:
        return TradeProposal(
            signal_id=signal.signal_id,
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            side=Side.BUY,
            entry_price=signal.entry_price,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
            confidence=signal.confidence,
            created_at_ms=signal.created_at_ms,
        )

    def _reject(
        self,
        signal: SignalCandidate,
        now_ms: int,
        reasons: tuple[str, ...],
        *,
        ticker: Ticker | None = None,
    ) -> OrderIntent:
        intent = build_rejected_intent(signal)
        payload: dict[str, object] = {"reasons": list(reasons)}
        if ticker is not None:
            payload["ask"] = str(ticker.ask)
            payload["slippage_bps"] = str(slippage_bps(signal.entry_price, ticker.ask))
        self._store.create_intent(
            intent,
            event=OrderEvent(intent.intent_id, EventType.REJECTED, now_ms, payload),
        )
        return intent

    def shadow(self, signal: SignalCandidate, *, now_ms: int | None = None) -> OrderIntent:
        """Build an intent from an approved signal without any private call."""
        now = signal.created_at_ms if now_ms is None else now_ms
        intent_id = intent_id_for(signal.signal_id)
        existing = self._store.get_intent(intent_id)
        if existing is not None:
            return existing

        self._risk.mark({signal.symbol: signal.entry_price}, now_ms=now)
        decision = self._risk.check(
            self._proposal(signal),
            self._config.instrument(signal.symbol),
            now_ms=now,
        )
        if not decision.approved:
            return self._reject(
                signal, now, guard_reasons(signal, decision, None, self._config, now)
            )

        premature = guard_reasons(signal, decision, None, self._config, now)
        if premature:
            return self._reject(signal, now, premature)

        ticker = self._adapter.fetch_ticker(signal.symbol)
        reasons = guard_reasons(signal, decision, ticker, self._config, now)
        if reasons:
            return self._reject(signal, now, reasons, ticker=ticker)

        state = approved_state(self._config)
        intent = build_intent(signal, decision, state=state)
        event_type = EventType.SHADOWED if state is IntentState.SHADOW else EventType.APPROVED
        self._store.create_intent(
            intent,
            event=OrderEvent(
                intent.intent_id,
                event_type,
                now,
                {
                    "ask": str(ticker.ask),
                    "slippage_bps": str(slippage_bps(signal.entry_price, ticker.ask)),
                    "approved_quantity": str(decision.approved_quantity),
                    "approved_notional": str(decision.approved_notional),
                },
            ),
        )
        return intent

    def shadow_file(self, path: str | Path) -> list[OrderIntent]:
        return [self.shadow(signal) for signal in load_signals(path)]

    def reconcile(self, now_ms: int | None = None) -> Reconciliation:
        """Compare the journal, the exchange and the risk book."""
        now = self._clock() if now_ms is None else now_ms
        return reconcile(self._risk, self._store, self._adapter, now)

    def submit(self, intent_id: str, confirmation: str, *, live: bool) -> SubmissionResult:
        """Submit one awaiting intent, but only after every gate passes."""
        submitter = Submitter(
            self._risk,
            self._store,
            self._adapter,
            self._config,
            clock=self._clock,
            sleep=self._sleep,
            reconciler=self.reconcile,
            readiness=self._readiness,
        )
        return submitter.submit(intent_id, confirmation, live=live)

    def flatten(self, confirmation: str) -> FlattenResult:
        """Cancel every open order and close every open position."""
        return flatten(
            self._risk,
            self._store,
            self._adapter,
            self._config,
            confirmation,
            clock=self._clock,
            sleep=self._sleep,
        )

    def cancel(self, intent_id: str) -> OrderIntent:
        """Cancel one open order and journal whatever the exchange reports."""
        intent = self._store.get_intent(intent_id)
        if intent is None:
            raise IntentError(f"unknown intent {intent_id}")
        if intent.terminal:
            raise IntentError(f"intent {intent_id} is {intent.state} and cannot be canceled")
        if intent.exchange_order_id is None:
            raise IntentError(f"intent {intent_id} has no exchange order to cancel")
        order = self._adapter.cancel_order(intent.exchange_order_id, intent.symbol)
        reporter = FillReporter(self._risk, self._store)
        return reporter.apply(intent, order, now_ms=self._clock())

    def status(self) -> dict[str, object]:
        intents = self._store.list_intents()
        by_state = {state.value: 0 for state in IntentState}
        for intent in intents:
            by_state[str(intent.state)] += 1
        return {
            "intents": len(intents),
            "by_state": by_state,
            "open_orders": len(self._store.open_orders()),
            "stage": self._config.stage,
            "dry_run": self._config.dry_run,
        }

    def show(self, intent_id: str) -> tuple[OrderIntent, list[OrderEvent]]:
        intent = self._store.get_intent(intent_id)
        if intent is None:
            raise IntentError(f"unknown intent {intent_id}")
        return intent, self._store.events_for(intent_id)

    def summary(self, intent: OrderIntent) -> dict[str, object]:
        return {
            "intent_id": intent.intent_id,
            "signal_id": intent.signal_id,
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": str(intent.quantity),
            "filled_quantity": str(intent.filled_quantity),
            "entry_price": str(intent.entry_price),
            "stop_price": str(intent.stop_price),
            "target_price": str(intent.target_price),
            "approved_notional": str(intent.approved_notional),
            "state": str(intent.state),
            "exchange_order_id": intent.exchange_order_id,
            "created_at_ms": intent.created_at_ms,
            "expires_at_ms": intent.expires_at_ms,
        }
