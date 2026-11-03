"""Read-only position access used by execution and reconciliation."""

from __future__ import annotations

from decimal import Decimal

from nomicronix.risk_engine.models import Fill, FillAction, RiskLimits
from nomicronix.risk_engine.service import RiskService
from nomicronix.risk_engine.store import RiskStateStore
from tests.risk_engine.fakes import NOW_MS, make_instrument, make_proposal


def make_service() -> tuple[RiskStateStore, RiskService]:
    store = RiskStateStore(":memory:")
    service = RiskService(
        store,
        RiskLimits(),
        initial_cash=Decimal("1000"),
        clock=lambda: NOW_MS,
    )
    return store, service


def test_positions_is_empty_before_any_fill() -> None:
    store, service = make_service()
    try:
        assert service.positions() == ()
        assert service.has_open_position("BTC/USDT") is False
    finally:
        store.close()


def test_positions_is_a_sorted_read_only_view() -> None:
    store, service = make_service()
    try:
        proposal = make_proposal()
        decision = service.check(proposal, make_instrument(), now_ms=NOW_MS)
        assert decision.approved
        service.record_fill(
            proposal.signal_id,
            Fill(
                FillAction.OPEN,
                proposal.symbol,
                decision.approved_quantity,
                proposal.entry_price,
                Decimal("0"),
                NOW_MS,
            ),
            now_ms=NOW_MS,
        )
        positions = service.positions()
        assert len(positions) == 1
        assert positions[0].symbol == proposal.symbol
        assert service.has_open_position(proposal.symbol) is True
        assert service.has_open_position("ETH/USDT") is False
    finally:
        store.close()
