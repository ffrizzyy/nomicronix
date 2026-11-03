"""Risk-gated delivery, outbox retries and paper-trade tracking."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path

from nomicronix.risk_engine.models import (
    Fill,
    FillAction,
    RiskDecision,
    Side,
    TradeProposal,
    utc_now_ms,
)
from nomicronix.risk_engine.service import RiskService
from nomicronix.signal_delivery.models import (
    DeliveryAction,
    DeliveryError,
    DeliveryOutcome,
    DeliveryRecord,
    DeliveryStatus,
    InstrumentRules,
    Notification,
    PaperTrade,
    TickResult,
)
from nomicronix.signal_delivery.render import (
    notification_from_payload,
    render_json,
)
from nomicronix.signal_delivery.signals import load_signals
from nomicronix.signal_delivery.sinks import Sink
from nomicronix.signal_delivery.store import DeliveryStore
from nomicronix.strategy_engine.models import SignalCandidate


class DeliveryService:
    """The only object that turns an approved candidate into an alert and trade."""

    def __init__(
        self,
        risk: RiskService,
        store: DeliveryStore,
        sinks: Sequence[Sink],
        instrument_rules: InstrumentRules,
        *,
        clock: Callable[[], int] = utc_now_ms,
    ) -> None:
        if not sinks:
            raise ValueError("at least one sink is required")
        self._risk = risk
        self._store = store
        self._sinks = tuple(sinks)
        self._instrument_rules = instrument_rules
        self._clock = clock

    def _notification(self, signal: SignalCandidate, decision: RiskDecision) -> Notification:
        return Notification(
            signal_id=signal.signal_id,
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            timeframe=signal.timeframe,
            side=signal.side.value,
            entry_price=signal.entry_price,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
            confidence=signal.confidence,
            rationale=signal.rationale,
            created_at_ms=signal.created_at_ms,
            expires_at_ms=signal.expires_at_ms,
            approved=decision.approved,
            approved_quantity=decision.approved_quantity,
            approved_notional=decision.approved_notional,
            risk_amount=decision.risk_amount,
            reward_risk=decision.reward_risk,
            reasons=tuple(str(reason.value) for reason in decision.reasons),
        )

    def _send(self, record: DeliveryRecord, action: DeliveryAction) -> DeliveryOutcome:
        try:
            payload = json.loads(record.payload_json)
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            notification = notification_from_payload(payload)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            message = f"stored notification is invalid: {exc}"
            self._store.mark_status(
                record.signal_id,
                DeliveryStatus.FAILED,
                recorded_at_ms=record.recorded_at_ms,
                error=message,
            )
            raise DeliveryError(message) from exc

        for sink in self._sinks:
            try:
                sink.send(notification)
            except DeliveryError as exc:
                self._store.mark_status(
                    record.signal_id,
                    DeliveryStatus.FAILED,
                    recorded_at_ms=record.recorded_at_ms,
                    error=str(exc),
                )
                raise
            except Exception as exc:  # noqa: BLE001 - a sink must never fake delivery
                message = f"{sink.name} sink failed: {exc}"
                self._store.mark_status(
                    record.signal_id,
                    DeliveryStatus.FAILED,
                    recorded_at_ms=record.recorded_at_ms,
                    error=message,
                )
                raise DeliveryError(message) from exc

        self._store.mark_status(
            record.signal_id,
            DeliveryStatus.DELIVERED,
            recorded_at_ms=record.recorded_at_ms,
        )
        updated = self._store.get_delivery(record.signal_id)
        if updated is None:
            raise DeliveryError(f"delivery row disappeared for {record.signal_id}")
        return DeliveryOutcome(record=updated, action=action)

    def deliver(self, signal: SignalCandidate, *, now_ms: int | None = None) -> DeliveryOutcome:
        existing = self._store.get_delivery(signal.signal_id)
        if existing is not None:
            if existing.status in {DeliveryStatus.DELIVERED, DeliveryStatus.REJECTED}:
                return DeliveryOutcome(record=existing, action=DeliveryAction.SKIPPED)
            return self._send(existing, DeliveryAction.RETRIED)

        now = signal.created_at_ms if now_ms is None else now_ms
        self._risk.mark({signal.symbol: signal.entry_price}, now_ms=now)
        proposal = TradeProposal(
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
        decision = self._risk.check(
            proposal,
            self._instrument_rules.for_symbol(signal.symbol),
            now_ms=now,
        )
        notification = self._notification(signal, decision)
        payload = render_json(notification)

        if not decision.approved:
            record = DeliveryRecord(
                signal_id=signal.signal_id,
                status=DeliveryStatus.REJECTED,
                payload_json=payload,
                recorded_at_ms=now,
            )
            self._store.put_delivery(record)
            return DeliveryOutcome(record=record, action=DeliveryAction.REJECTED)

        fill = Fill(
            action=FillAction.OPEN,
            symbol=signal.symbol,
            quantity=decision.approved_quantity,
            price=signal.entry_price,
            fee=Decimal(0),
            filled_at_ms=now,
        )
        self._risk.record_fill(signal.signal_id, fill, now_ms=now)
        self._store.put_trade(
            PaperTrade(
                signal_id=signal.signal_id,
                symbol=signal.symbol,
                quantity=decision.approved_quantity,
                entry_price=signal.entry_price,
                stop_price=signal.stop_price,
                target_price=signal.target_price,
                opened_at_ms=now,
            )
        )
        record = DeliveryRecord(
            signal_id=signal.signal_id,
            status=DeliveryStatus.PENDING,
            payload_json=payload,
            recorded_at_ms=now,
        )
        self._store.put_delivery(record)
        return self._send(record, DeliveryAction.DELIVERED)

    def deliver_file(self, path: str | Path) -> list[DeliveryOutcome]:
        return [self.deliver(signal) for signal in load_signals(path)]

    def tick(self, prices: Mapping[str, Decimal], *, now_ms: int | None = None) -> TickResult:
        if not prices:
            raise ValueError("at least one price is required")
        for symbol, price in prices.items():
            if price <= 0:
                raise ValueError(f"price for {symbol} must be positive")

        now = self._clock() if now_ms is None else now_ms
        closed: list[PaperTrade] = []
        for trade in self._store.open_trades():
            mark = prices.get(trade.symbol)
            if mark is None:
                continue
            if mark <= trade.stop_price:
                exit_price = min(trade.stop_price, mark)
                reason = "stop"
            elif mark >= trade.target_price:
                exit_price = trade.target_price
                reason = "target"
            else:
                continue

            realized = trade.pnl_at(exit_price)
            self._risk.record_fill(
                trade.signal_id,
                Fill(
                    action=FillAction.CLOSE,
                    symbol=trade.symbol,
                    quantity=trade.quantity,
                    price=exit_price,
                    fee=Decimal(0),
                    filled_at_ms=now,
                ),
                now_ms=now,
            )
            self._store.close_trade(
                trade.signal_id,
                exit_price=exit_price,
                exit_reason=reason,
                closed_at_ms=now,
                realized_pnl=realized,
            )
            updated = self._store.get_trade(trade.signal_id)
            if updated is not None:
                closed.append(updated)

        self._risk.mark(prices, now_ms=now)
        return TickResult(closed=tuple(closed), still_open=tuple(self._store.open_trades()))
