"""A complete offline execution stack: fake exchange, fresh journal, fresh risk DB."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nomicronix.execution.config import ExecutionConfig
from nomicronix.execution.service import ExecutionService
from nomicronix.execution.store import ExecutionStore
from nomicronix.execution.submit import ReadinessChecker
from nomicronix.risk_engine.models import RiskLimits
from nomicronix.risk_engine.service import RiskService
from nomicronix.risk_engine.store import RiskStateStore
from tests.execution.fakes import NOW_MS, FakeAdapter


@dataclass
class Harness:
    service: ExecutionService
    store: ExecutionStore
    risk_store: RiskStateStore
    risk: RiskService
    adapter: FakeAdapter
    config: ExecutionConfig

    def close(self) -> None:
        self.store.close()
        self.risk_store.close()


def live_config(**overrides: object) -> ExecutionConfig:
    defaults: dict[str, object] = {
        "dry_run": False,
        "allow_live": True,
        "stage": "approved",
        "poll_interval_ms": 1,
        "order_timeout_ms": 100,
        # Submission tests focus on order mechanics; the readiness gate has its
        # own tests that enable it explicitly.
        "require_readiness": False,
    }
    defaults.update(overrides)
    return ExecutionConfig(**defaults)  # type: ignore[arg-type]


def build(
    *,
    config: ExecutionConfig | None = None,
    limits: RiskLimits | None = None,
    now_ms: int = NOW_MS,
    readiness: ReadinessChecker | None = None,
) -> Harness:
    active = config if config is not None else ExecutionConfig()
    adapter = FakeAdapter()
    store = ExecutionStore(":memory:")
    risk_store = RiskStateStore(":memory:")
    risk = RiskService(
        risk_store,
        limits if limits is not None else RiskLimits(),
        initial_cash=Decimal("1000"),
        clock=lambda: now_ms,
    )
    adapter.set_ticker("BTC/USDT", bid="100", ask="100.1", at_ms=now_ms)
    service = ExecutionService(
        risk,
        store,
        adapter,
        active,
        clock=lambda: now_ms,
        sleep=lambda _seconds: None,
        readiness=readiness,
    )
    return Harness(service, store, risk_store, risk, adapter, active)
