"""Test builders for the risk engine. No network, no clock, no exchange."""

from __future__ import annotations

from decimal import Decimal

from nomicronix.risk_engine.models import (
    Instrument,
    KillSwitch,
    Position,
    RiskLimits,
    RiskSnapshot,
    Side,
    TradeProposal,
)

NOW_MS = 1_735_689_600_000  # 2025-01-01T00:00:00Z
HOUR_MS = 3_600_000


def make_instrument(
    symbol: str = "BTC/USDT",
    *,
    step: str = "0.00001",
    min_quantity: str = "0.00001",
    min_notional: str = "10",
) -> Instrument:
    return Instrument(
        symbol=symbol,
        quantity_step=Decimal(step),
        min_quantity=Decimal(min_quantity),
        min_notional=Decimal(min_notional),
    )


def make_proposal(
    *,
    signal_id: str = "sig-1",
    strategy_id: str = "test-strategy",
    symbol: str = "BTC/USDT",
    side: Side = Side.BUY,
    entry: str = "100",
    stop: str = "95",
    target: str = "115",
    confidence: float = 0.8,
    created_at_ms: int = NOW_MS,
) -> TradeProposal:
    return TradeProposal(
        signal_id=signal_id,
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        entry_price=Decimal(entry),
        stop_price=Decimal(stop),
        target_price=Decimal(target),
        confidence=confidence,
        created_at_ms=created_at_ms,
    )


def make_position(
    *,
    symbol: str = "BTC/USDT",
    quantity: str = "1",
    entry: str = "100",
    stop: str = "95",
    target: str = "115",
    current: str = "100",
    signal_id: str = "pos-1",
) -> Position:
    return Position(
        symbol=symbol,
        quantity=Decimal(quantity),
        entry_price=Decimal(entry),
        stop_price=Decimal(stop),
        target_price=Decimal(target),
        current_price=Decimal(current),
        opened_at_ms=NOW_MS,
        signal_id=signal_id,
    )


def make_snapshot(
    *,
    equity: str = "1000",
    peak: str | None = None,
    day_start: str | None = None,
    realized: str = "0",
    positions: tuple[Position, ...] = (),
    kill_switch: KillSwitch | None = None,
    as_of_ms: int = NOW_MS,
) -> RiskSnapshot:
    equity_d = Decimal(equity)
    return RiskSnapshot(
        equity=equity_d,
        peak_equity=Decimal(peak) if peak is not None else equity_d,
        day_start_equity=Decimal(day_start) if day_start is not None else equity_d,
        realized_pnl_today=Decimal(realized),
        positions=positions,
        kill_switch=kill_switch,
        as_of_ms=as_of_ms,
    )


def make_limits(**overrides: object) -> RiskLimits:
    return RiskLimits(**overrides)  # type: ignore[arg-type]
