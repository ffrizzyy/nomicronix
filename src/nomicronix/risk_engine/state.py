"""Mutable portfolio state: marking, UTC day rollover, fills and kill switch."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from nomicronix.risk_engine.models import (
    Fill,
    FillAction,
    KillSwitch,
    Position,
    RiskLimits,
    RiskSnapshot,
)

RESUME_PHRASE = "RESUME TRADING"


def utc_day(now_ms: int) -> date:
    return datetime.fromtimestamp(now_ms // 1000, tz=UTC).date()


@dataclass(slots=True)
class PortfolioState:
    cash: Decimal
    positions: dict[str, Position]
    peak_equity: Decimal
    day_start_equity: Decimal
    day_utc: date
    realized_pnl_today: Decimal
    kill_switch: KillSwitch | None
    last_mark_ms: int

    @classmethod
    def new(cls, cash: Decimal, now_ms: int) -> PortfolioState:
        if cash <= 0:
            raise ValueError("initial cash must be positive")
        return cls(
            cash=cash,
            positions={},
            peak_equity=cash,
            day_start_equity=cash,
            day_utc=utc_day(now_ms),
            realized_pnl_today=Decimal(0),
            kill_switch=None,
            last_mark_ms=now_ms,
        )

    @property
    def equity(self) -> Decimal:
        return self.cash + sum(
            (position.quantity * position.current_price for position in self.positions.values()),
            Decimal(0),
        )

    def snapshot(self, as_of_ms: int) -> RiskSnapshot:
        return RiskSnapshot(
            equity=self.equity,
            peak_equity=self.peak_equity,
            day_start_equity=self.day_start_equity,
            realized_pnl_today=self.realized_pnl_today,
            positions=tuple(self.positions[symbol] for symbol in sorted(self.positions)),
            kill_switch=self.kill_switch,
            as_of_ms=as_of_ms,
        )

    def mark(
        self,
        prices: Mapping[str, Decimal],
        now_ms: int,
        limits: RiskLimits,
    ) -> RiskSnapshot:
        for symbol, price in prices.items():
            if price <= 0:
                raise ValueError(f"price for {symbol} must be positive")
            position = self.positions.get(symbol)
            if position is not None:
                self.positions[symbol] = Position(
                    symbol=position.symbol,
                    quantity=position.quantity,
                    entry_price=position.entry_price,
                    stop_price=position.stop_price,
                    target_price=position.target_price,
                    current_price=price,
                    opened_at_ms=position.opened_at_ms,
                    signal_id=position.signal_id,
                    entry_fee=position.entry_fee,
                )

        equity = self.equity
        day = utc_day(now_ms)
        if day != self.day_utc:
            self.day_utc = day
            self.day_start_equity = equity
            self.realized_pnl_today = Decimal(0)

        if equity > self.peak_equity:
            self.peak_equity = equity

        drawdown = (
            (self.peak_equity - equity) / self.peak_equity if self.peak_equity > 0 else Decimal(0)
        )
        if drawdown >= limits.max_drawdown_pct and self.kill_switch is None:
            self.engage_kill("max drawdown reached", now_ms, "risk-engine")

        self.last_mark_ms = now_ms
        return self.snapshot(now_ms)

    def apply_open(
        self,
        signal_id: str,
        fill: Fill,
        *,
        stop_price: Decimal,
        target_price: Decimal,
    ) -> None:
        if fill.action is not FillAction.OPEN:
            raise ValueError("apply_open requires an OPEN fill")
        if fill.quantity <= 0 or fill.price <= 0:
            raise ValueError("open fill quantity and price must be positive")
        if fill.fee < 0:
            raise ValueError("fill fee must not be negative")
        if stop_price <= 0 or target_price <= 0 or stop_price >= fill.price:
            raise ValueError("open fill requires a stop below the fill price")

        existing = self.positions.get(fill.symbol)
        if existing is not None and existing.signal_id != signal_id:
            raise ValueError(f"position already open for {fill.symbol}")

        self.cash -= fill.quantity * fill.price + fill.fee
        if existing is None:
            self.positions[fill.symbol] = Position(
                symbol=fill.symbol,
                quantity=fill.quantity,
                entry_price=fill.price,
                stop_price=stop_price,
                target_price=target_price,
                current_price=fill.price,
                opened_at_ms=fill.filled_at_ms,
                signal_id=signal_id,
                entry_fee=fill.fee,
            )
            return

        total = existing.quantity + fill.quantity
        self.positions[fill.symbol] = Position(
            symbol=fill.symbol,
            quantity=total,
            entry_price=(existing.entry_price * existing.quantity + fill.price * fill.quantity)
            / total,
            stop_price=stop_price,
            target_price=target_price,
            current_price=fill.price,
            opened_at_ms=existing.opened_at_ms,
            signal_id=signal_id,
            entry_fee=existing.entry_fee + fill.fee,
        )

    def apply_close(self, signal_id: str, fill: Fill) -> None:
        if fill.action is not FillAction.CLOSE:
            raise ValueError("apply_close requires a CLOSE fill")
        position = next(
            (p for p in self.positions.values() if p.signal_id == signal_id),
            None,
        )
        if position is None:
            raise ValueError(f"no open position for signal {signal_id}")
        if fill.symbol != position.symbol:
            raise ValueError("close fill symbol does not match the position")
        if fill.quantity <= 0 or fill.price <= 0:
            raise ValueError("close fill quantity and price must be positive")
        if fill.fee < 0:
            raise ValueError("fill fee must not be negative")
        if fill.quantity > position.quantity:
            raise ValueError("close quantity exceeds the open position")

        if fill.quantity == position.quantity:
            allocated_entry_fee = position.entry_fee
            del self.positions[position.symbol]
        else:
            allocated_entry_fee = position.entry_fee * fill.quantity / position.quantity
            self.positions[position.symbol] = Position(
                symbol=position.symbol,
                quantity=position.quantity - fill.quantity,
                entry_price=position.entry_price,
                stop_price=position.stop_price,
                target_price=position.target_price,
                current_price=fill.price,
                opened_at_ms=position.opened_at_ms,
                signal_id=position.signal_id,
                entry_fee=position.entry_fee - allocated_entry_fee,
            )

        self.cash += fill.quantity * fill.price - fill.fee
        self.realized_pnl_today += (
            (fill.price - position.entry_price) * fill.quantity - allocated_entry_fee - fill.fee
        )

    def engage_kill(self, reason: str, now_ms: int, engaged_by: str) -> None:
        if self.kill_switch is None:
            self.kill_switch = KillSwitch(
                reason=reason,
                engaged_at_ms=now_ms,
                engaged_by=engaged_by,
            )

    def resume(self, confirmation: str) -> None:
        if confirmation != RESUME_PHRASE:
            raise ValueError(f'resume requires the exact phrase "{RESUME_PHRASE}"')
        self.kill_switch = None
