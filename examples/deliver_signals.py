"""Deliver signal candidates through the risk gate and print the paper report.

Offline by design: in-memory databases and the console sink. Without an
argument it builds one deterministic candidate so the whole path is visible.

Usage:
    uv run python examples/deliver_signals.py [signals.json]
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

from nomicronix.risk_engine.models import RiskLimits
from nomicronix.risk_engine.service import RiskService
from nomicronix.risk_engine.store import RiskStateStore
from nomicronix.signal_delivery import (
    ConsoleSink,
    DeliveryService,
    DeliveryStore,
    InstrumentRules,
    build_report,
    render_report,
)
from nomicronix.strategy_engine.models import Side, SignalCandidate

CANDLE_OPEN_MS = 1_735_776_000_000
CANDLE_CLOSE_MS = CANDLE_OPEN_MS + 3_600_000


def demo_candidate() -> SignalCandidate:
    return SignalCandidate(
        signal_id=f"sma-cross:BTC/USDT:1h:{CANDLE_OPEN_MS}",
        strategy_id="sma-cross",
        symbol="BTC/USDT",
        timeframe="1h",
        side=Side.BUY,
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        target_price=Decimal("115"),
        confidence=0.6,
        rationale="example fast SMA crossed above slow SMA",
        features={"atr": "2.5"},
        created_at_ms=CANDLE_CLOSE_MS,
        expires_at_ms=CANDLE_CLOSE_MS + 300_000,
        candle_open_time_ms=CANDLE_OPEN_MS,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    signal = demo_candidate()
    clock = lambda: signal.created_at_ms  # noqa: E731 - deterministic demo clock

    risk_store = RiskStateStore(":memory:")
    risk = RiskService(
        risk_store,
        RiskLimits(),
        initial_cash=Decimal("1000"),
        clock=clock,
    )
    delivery_store = DeliveryStore(":memory:")
    service = DeliveryService(
        risk,
        delivery_store,
        [ConsoleSink()],
        InstrumentRules(),
        clock=clock,
    )
    try:
        if arguments:
            outcomes = service.deliver_file(Path(arguments[0]))
        else:
            outcomes = [service.deliver(signal)]
        for outcome in outcomes:
            print(f"{outcome.action}: {outcome.record.signal_id}")

        # A stop tick on paper, so the report has a closed trade to total.
        service.tick({"BTC/USDT": Decimal("94")}, now_ms=signal.created_at_ms + 60_000)
        print()
        print(render_report(build_report(delivery_store, risk)))
        return 0
    finally:
        delivery_store.close()
        risk_store.close()


if __name__ == "__main__":
    raise SystemExit(main())
