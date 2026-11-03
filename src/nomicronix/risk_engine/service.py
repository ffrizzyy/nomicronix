"""RiskService: the only public API consumers should use."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal

from nomicronix.risk_engine.engine import evaluate
from nomicronix.risk_engine.models import (
    Approval,
    Fill,
    FillAction,
    Instrument,
    Position,
    RejectReason,
    RiskDecision,
    RiskLimits,
    RiskStatus,
    TradeProposal,
    utc_now_ms,
)
from nomicronix.risk_engine.state import PortfolioState
from nomicronix.risk_engine.store import ApprovalError, RiskStateStore


class RiskService:
    def __init__(
        self,
        store: RiskStateStore,
        limits: RiskLimits,
        *,
        initial_cash: Decimal = Decimal("1000"),
        clock: Callable[[], int] = utc_now_ms,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        self._store = store
        self._limits = limits
        self._initial_cash = initial_cash
        self._clock = clock

    def _now(self, now_ms: int | None) -> int:
        return self._clock() if now_ms is None else now_ms

    def _load(self, now_ms: int) -> PortfolioState:
        return self._store.load_state(now_ms, self._initial_cash)

    def status(self, now_ms: int | None = None) -> RiskStatus:
        now = self._now(now_ms)
        state = self._load(now)
        snapshot = state.snapshot(state.last_mark_ms)
        fresh = now - state.last_mark_ms <= self._limits.max_signal_age_ms
        allowed = (
            snapshot.kill_switch is None
            and fresh
            and snapshot.daily_loss_pct < self._limits.max_daily_loss_pct
            and snapshot.drawdown_pct < self._limits.max_drawdown_pct
        )
        return RiskStatus(
            equity=snapshot.equity,
            peak_equity=snapshot.peak_equity,
            drawdown_pct=snapshot.drawdown_pct,
            day_start_equity=snapshot.day_start_equity,
            daily_pnl=snapshot.equity - snapshot.day_start_equity,
            daily_loss_pct=snapshot.daily_loss_pct,
            open_positions=snapshot.position_count,
            open_risk=snapshot.open_risk,
            exposure=snapshot.exposure,
            trading_allowed=allowed,
            kill_switch=snapshot.kill_switch,
            last_mark_ms=state.last_mark_ms,
        )

    def positions(self, now_ms: int | None = None) -> tuple[Position, ...]:
        """Read-only view of the open positions, sorted by symbol.

        Execution uses this to refuse a second order for a symbol it already
        holds, and to reconcile the local book with the exchange. It never
        mutates the risk state.
        """
        state = self._load(self._now(now_ms))
        return tuple(state.positions[symbol] for symbol in sorted(state.positions))

    def has_open_position(self, symbol: str, now_ms: int | None = None) -> bool:
        return any(position.symbol == symbol for position in self.positions(now_ms))

    def mark(self, prices: Mapping[str, Decimal], now_ms: int | None = None) -> RiskStatus:
        now = self._now(now_ms)
        state = self._load(now)
        state.mark(prices, now, self._limits)
        self._store.save_state(state, now)
        return self.status(now)

    def check(
        self,
        proposal: TradeProposal,
        instrument: Instrument,
        now_ms: int | None = None,
    ) -> RiskDecision:
        now = self._now(now_ms)
        state = self._load(now)
        snapshot = state.snapshot(state.last_mark_ms)
        decision = evaluate(proposal, snapshot, instrument, self._limits, now)
        if RejectReason.MAX_DRAWDOWN in decision.reasons and state.kill_switch is None:
            state.engage_kill("max drawdown reached", now, "risk-engine")
        self._store.persist_check(state, decision, proposal, now)
        return decision

    def record_fill(
        self,
        signal_id: str,
        fill: Fill,
        now_ms: int | None = None,
    ) -> PortfolioState:
        now = self._now(now_ms)
        if fill.quantity <= 0:
            raise ApprovalError("fill quantity must be positive")
        if fill.price <= 0:
            raise ApprovalError("fill price must be positive")
        if fill.fee < 0:
            raise ApprovalError("fill fee must not be negative")

        state = self._load(now)
        approval: Approval | None = None
        if fill.action is FillAction.OPEN:
            approval = self._store.load_approval(signal_id)
            if approval is None:
                raise ApprovalError(f"no approval for signal {signal_id}")
            if approval.consumed_at_ms is not None:
                raise ApprovalError(f"approval {signal_id} was already consumed")
            if approval.symbol != fill.symbol:
                raise ApprovalError("open fill symbol does not match the approval")
            remaining = approval.quantity - approval.filled_quantity
            if fill.quantity > remaining:
                raise ApprovalError(
                    f"open fill quantity {fill.quantity} exceeds the remaining {remaining}"
                )
            state.apply_open(
                signal_id,
                fill,
                stop_price=approval.stop_price,
                target_price=approval.target_price,
            )
        else:
            state.apply_close(signal_id, fill)
        self._store.persist_fill(state, signal_id, fill, now, approval=approval)
        return state

    def expire_approval(self, signal_id: str, now_ms: int | None = None) -> None:
        """Void an approval's unfilled remainder after an order is canceled."""
        self._store.expire_approval(signal_id, self._now(now_ms))

    def engage_kill_switch(
        self,
        reason: str,
        *,
        by: str = "owner",
        now_ms: int | None = None,
    ) -> None:
        now = self._now(now_ms)
        state = self._load(now)
        state.engage_kill(reason, now, by)
        self._store.save_state(state, now)

    def resume(self, confirmation: str, now_ms: int | None = None) -> None:
        now = self._now(now_ms)
        state = self._load(now)
        state.resume(confirmation)
        self._store.save_state(state, now)
