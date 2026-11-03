"""Shared fixtures for signal-delivery tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from nomicronix.risk_engine.models import RiskLimits
from nomicronix.risk_engine.service import RiskService
from nomicronix.risk_engine.store import RiskStateStore
from nomicronix.signal_delivery.models import DeliveryError, InstrumentRules, Notification
from nomicronix.signal_delivery.service import DeliveryService
from nomicronix.signal_delivery.store import DeliveryStore
from nomicronix.strategy_engine.models import (
    ScanConfig,
    Side,
    SignalBatch,
    SignalCandidate,
)
from nomicronix.strategy_engine.results import write_result
from tests.strategy_engine.fakes import HOUR_MS, JAN_1_2025_MS


def make_candidate(
    index: int = 0,
    *,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    entry: str = "100",
    stop: str = "95",
    target: str = "115",
    confidence: float = 0.6,
    strategy_id: str = "sma-cross",
    start_ms: int = JAN_1_2025_MS,
) -> SignalCandidate:
    open_ms = start_ms + index * HOUR_MS
    created = open_ms + HOUR_MS
    return SignalCandidate(
        signal_id=f"{strategy_id}:{symbol}:{timeframe}:{open_ms}",
        strategy_id=strategy_id,
        symbol=symbol,
        timeframe=timeframe,
        side=Side.BUY,
        entry_price=Decimal(entry),
        stop_price=Decimal(stop),
        target_price=Decimal(target),
        confidence=confidence,
        rationale="test signal",
        features={"atr": "2.5"},
        created_at_ms=created,
        expires_at_ms=created + 300_000,
        candle_open_time_ms=open_ms,
    )


def write_signals(
    path: Path,
    candidates: list[SignalCandidate],
    *,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
) -> Path:
    config = ScanConfig(
        symbol=symbol,
        timeframe=timeframe,
        start_ms=JAN_1_2025_MS,
        end_ms=JAN_1_2025_MS + 100 * HOUR_MS,
        min_history=1,
        min_confidence=0.0,
    )
    batch = SignalBatch(
        config=config,
        strategy_id=candidates[0].strategy_id if candidates else "empty",
        first_candle_ms=JAN_1_2025_MS,
        last_candle_ms=JAN_1_2025_MS + 99 * HOUR_MS,
        candle_count=100,
        signals=tuple(candidates),
    )
    return write_result(batch, path, force=True)


def make_notification(
    *,
    approved: bool = True,
    reasons: tuple[str, ...] | None = None,
) -> Notification:
    signal = make_candidate()
    if approved:
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
            approved=True,
            approved_quantity=Decimal("1"),
            approved_notional=Decimal("100"),
            risk_amount=Decimal("5"),
            reward_risk=Decimal("3"),
        )
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
        approved=False,
        approved_quantity=Decimal("0"),
        approved_notional=Decimal("0"),
        risk_amount=Decimal("0"),
        reward_risk=Decimal("1.5"),
        reasons=("reward_risk_too_low",) if reasons is None else reasons,
    )


@dataclass
class FakeClock:
    now: int = JAN_1_2025_MS + 10 * HOUR_MS

    def __call__(self) -> int:
        return self.now


@dataclass
class FakeSink:
    name: str = "fake"
    fail: bool = False
    sent: list[Notification] = field(default_factory=list)

    def send(self, notification: Notification) -> None:
        if self.fail:
            raise DeliveryError("fake sink failed")
        self.sent.append(notification)


@dataclass
class Harness:
    service: DeliveryService
    delivery_store: DeliveryStore
    risk_store: RiskStateStore
    risk: RiskService
    clock: FakeClock
    sinks: tuple[FakeSink, ...]

    def close(self) -> None:
        self.delivery_store.close()
        self.risk_store.close()


def build_harness(
    *,
    sinks: tuple[FakeSink, ...] | None = None,
    limits: RiskLimits | None = None,
    now: int = JAN_1_2025_MS + 10 * HOUR_MS,
    clock: FakeClock | None = None,
) -> Harness:
    sink_list = sinks if sinks is not None else (FakeSink(),)
    risk_store = RiskStateStore(":memory:")
    delivery_store = DeliveryStore(":memory:")
    active_clock = FakeClock(now) if clock is None else clock
    risk = RiskService(
        risk_store,
        limits if limits is not None else RiskLimits(),
        initial_cash=Decimal("1000"),
        clock=active_clock,
    )
    service = DeliveryService(
        risk,
        delivery_store,
        list(sink_list),
        InstrumentRules(),
        clock=active_clock,
    )
    return Harness(
        service=service,
        delivery_store=delivery_store,
        risk_store=risk_store,
        risk=risk,
        clock=active_clock,
        sinks=sink_list,
    )
