"""Simulated spot broker: next-open fills, fees, slippage, stops and targets."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from nomicronix.backtesting.models import (
    BacktestConfig,
    EquityPoint,
    ExitReason,
    Signal,
    SimEvent,
    SkipReason,
    Trade,
)
from nomicronix.backtesting.sizing import SizingPolicy
from nomicronix.market_data.models import Candle


def apply_slippage(price: Decimal, bps: Decimal, *, buying: bool) -> Decimal:
    """Move the fill price against the trader by `bps` basis points."""
    factor = bps / Decimal(10_000)
    return price * (1 + factor) if buying else price * (1 - factor)


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    units = (value / step).to_integral_value(rounding=ROUND_FLOOR)
    return units * step


@dataclass(slots=True)
class OpenPosition:
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    entry_fee: Decimal
    stop_price: Decimal
    target_price: Decimal | None
    opened_at_ms: int


class Broker:
    def __init__(self, config: BacktestConfig, sizer: SizingPolicy) -> None:
        self.config = config
        self.sizer = sizer
        self.cash = config.initial_cash
        self.position: OpenPosition | None = None
        self.trades: list[Trade] = []
        self.events: list[SimEvent] = []
        self.fees_paid = Decimal(0)
        self.turnover = Decimal(0)

    @property
    def fee_rate(self) -> Decimal:
        return self.config.fee_bps / Decimal(10_000)

    def equity(self, mark_price: Decimal) -> Decimal:
        if self.position is None:
            return self.cash
        return self.cash + self.position.quantity * mark_price

    def equity_point(self, candle: Candle) -> EquityPoint:
        return EquityPoint(
            time_ms=candle.open_time_ms,
            equity=self.equity(candle.close),
            exposed=self.position is not None,
        )

    def try_open(self, candle: Candle, signal: Signal) -> bool:
        if self.position is not None:
            self._event(candle, "skip", SkipReason.IGNORED_ENTRY, signal.reason)
            return False
        if signal.stop_price is None:
            self._event(candle, "skip", SkipReason.INVALID_SIGNAL, "entry has no stop")
            return False

        fill_price = apply_slippage(candle.open, self.config.slippage_bps, buying=True)
        stop = signal.stop_price
        target = signal.target_price
        if fill_price <= stop or (target is not None and fill_price >= target):
            self._event(candle, "skip", SkipReason.SKIPPED_GAP, signal.reason)
            return False

        budget = self.sizer.notional(self.cash, self.cash)
        quantity = floor_to_step(budget / fill_price, self.config.quantity_step)
        affordable = floor_to_step(
            self.cash / (fill_price * (1 + self.fee_rate)),
            self.config.quantity_step,
        )
        quantity = min(quantity, affordable)
        notional = quantity * fill_price
        if quantity <= 0 or notional < self.config.min_notional:
            self._event(candle, "skip", SkipReason.INSUFFICIENT_CASH, signal.reason)
            return False

        fee = notional * self.fee_rate
        self.cash -= notional + fee
        self.position = OpenPosition(
            symbol=self.config.symbol,
            quantity=quantity,
            entry_price=fill_price,
            entry_fee=fee,
            stop_price=stop,
            target_price=target,
            opened_at_ms=candle.open_time_ms,
        )
        self.fees_paid += fee
        self.turnover += notional
        self._event(
            candle,
            "entry",
            "entry",
            f"qty={quantity} price={fill_price} stop={stop}",
        )
        return True

    def try_close(self, candle: Candle, price: Decimal, reason: ExitReason) -> bool:
        if self.position is None:
            self._event(candle, "skip", SkipReason.IGNORED_EXIT, str(reason))
            return False

        fill_price = apply_slippage(price, self.config.slippage_bps, buying=False)
        position = self.position
        notional = position.quantity * fill_price
        fee = notional * self.fee_rate
        self.cash += notional - fee
        pnl = (fill_price - position.entry_price) * position.quantity - position.entry_fee - fee
        cost = position.entry_price * position.quantity + position.entry_fee
        return_pct = float(pnl / cost) if cost > 0 else 0.0
        self.trades.append(
            Trade(
                symbol=position.symbol,
                quantity=position.quantity,
                entry_time_ms=position.opened_at_ms,
                entry_price=position.entry_price,
                exit_time_ms=candle.open_time_ms,
                exit_price=fill_price,
                fees=position.entry_fee + fee,
                pnl=pnl,
                return_pct=return_pct,
                exit_reason=reason,
            )
        )
        self.fees_paid += fee
        self.turnover += notional
        self.position = None
        self._event(
            candle,
            "exit",
            reason.value,
            f"qty={position.quantity} price={fill_price}",
        )
        return True

    def check_stop_target(self, candle: Candle) -> bool:
        position = self.position
        if position is None:
            return False
        stop_hit = candle.low <= position.stop_price
        target_hit = position.target_price is not None and candle.high >= position.target_price
        if stop_hit:
            return self.try_close(
                candle,
                min(candle.open, position.stop_price),
                ExitReason.STOP,
            )
        if target_hit and position.target_price is not None:
            return self.try_close(
                candle,
                max(candle.open, position.target_price),
                ExitReason.TARGET,
            )
        return False

    def force_close(self, candle: Candle) -> None:
        if self.position is not None:
            self.try_close(candle, candle.close, ExitReason.END_OF_DATA)

    def skip(self, candle: Candle, reason: SkipReason, detail: str = "") -> None:
        self._event(candle, "skip", reason, detail)

    def _event(
        self,
        candle: Candle,
        kind: str,
        reason: SkipReason | str,
        detail: str,
    ) -> None:
        self.events.append(
            SimEvent(
                time_ms=candle.open_time_ms,
                kind=kind,
                reason=reason.value if isinstance(reason, SkipReason) else reason,
                detail=detail,
            )
        )
