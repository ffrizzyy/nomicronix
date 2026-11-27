from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

import nomicronix.execution.__main__ as cli
from nomicronix.execution.config import ExecutionConfig
from nomicronix.execution.models import ExchangeError, utc_now_ms
from nomicronix.execution.preflight import FAIL, PASS, WARN, evaluate_preflight
from nomicronix.signal_delivery.readiness import CriterionResult, ReadinessReport
from tests.execution.fakes import NOW_MS, FakeAdapter

CRITERIA = (
    ("closed-trades", "closed paper trades"),
    ("track-record-days", "track record days"),
    ("net-expectancy", "expectancy after costs"),
    ("paper-drawdown", "paper max drawdown"),
    ("delivery-health", "failed or pending deliveries"),
    ("ledger-integrity", "delivered alerts vs paper trades"),
)

LIVE_CONFIG = """
[execution]
exchange = "binance"
market_type = "spot"
dry_run = false
allow_live = true
stage = "approved"
require_readiness = true
"""


def ready_report(*failing: str) -> ReadinessReport:
    criteria = tuple(
        CriterionResult(
            key=key,
            label=label,
            passed=key not in failing,
            actual="30",
            required=">= 30",
        )
        for key, label in CRITERIA
    )
    return ReadinessReport(
        ready=not failing,
        closed_trades=30,
        track_record_days=Decimal("30"),
        gross_pnl=Decimal("100"),
        net_pnl=Decimal("80"),
        expectancy=Decimal("2.666"),
        max_drawdown_pct=Decimal("0.01"),
        costs=Decimal("20"),
        criteria=criteria,
    )


def live_config() -> ExecutionConfig:
    return ExecutionConfig(dry_run=False, allow_live=True, stage="approved")


def check_status(report, name: str) -> str:
    return next(check.status for check in report.checks if check.name == name)


def test_every_gate_passes() -> None:
    adapter = FakeAdapter(permissions={"read", "spot"})
    report = evaluate_preflight(
        live_config(),
        key_present=True,
        key_hint="***1234",
        adapter=adapter,
        readiness=ready_report(),
        now_ms=NOW_MS,
    )

    assert report.ready is True
    assert {check.status for check in report.checks} == {PASS}
    assert "READY" in report.render()


def test_live_switches_block_by_default() -> None:
    report = evaluate_preflight(
        ExecutionConfig(),
        key_present=True,
        key_hint="***1234",
        adapter=FakeAdapter(permissions={"read"}),
        readiness=ready_report(),
        now_ms=NOW_MS,
    )

    assert report.ready is False
    assert check_status(report, "live-switches") == FAIL
    assert "dry_run = true" in report.render()


def test_a_withdrawal_key_blocks() -> None:
    report = evaluate_preflight(
        live_config(),
        key_present=True,
        key_hint="***1234",
        adapter=FakeAdapter(permissions={"read", "withdraw"}),
        readiness=ready_report(),
        now_ms=NOW_MS,
    )

    assert report.ready is False
    assert check_status(report, "key-permissions") == FAIL


def test_unreported_permissions_only_warn() -> None:
    report = evaluate_preflight(
        live_config(),
        key_present=True,
        key_hint="***1234",
        adapter=FakeAdapter(permissions=set()),
        readiness=ready_report(),
        now_ms=NOW_MS,
    )

    assert report.ready is True
    assert check_status(report, "key-permissions") == WARN


def test_a_skewed_clock_blocks() -> None:
    report = evaluate_preflight(
        live_config(),
        key_present=True,
        key_hint="***1234",
        adapter=FakeAdapter(permissions={"read"}),
        readiness=ready_report(),
        now_ms=NOW_MS + 10_000,
    )

    assert report.ready is False
    assert check_status(report, "clock-skew") == FAIL


def test_an_unreachable_trading_api_is_named_in_the_clock_check() -> None:
    adapter = FakeAdapter(permissions={"read"})

    def unreachable() -> int:
        raise ExchangeError("fetch_time failed: offline", code="network_error")

    adapter.server_time_ms = unreachable  # type: ignore[method-assign]

    report = evaluate_preflight(
        live_config(),
        key_present=True,
        key_hint="***1234",
        adapter=adapter,
        readiness=ready_report(),
        now_ms=NOW_MS,
    )

    detail = next(check.detail for check in report.checks if check.name == "clock-skew")
    assert check_status(report, "clock-skew") == FAIL
    assert "unreachable" in detail
    assert "network_error" in detail


def test_an_unverifiable_key_blocks_instead_of_warning() -> None:
    adapter = FakeAdapter(permissions=set())
    adapter.failures["fetch_permissions"] = ExchangeError(
        "fetch_permissions failed: offline", code="network_error"
    )

    report = evaluate_preflight(
        live_config(),
        key_present=True,
        key_hint="***1234",
        adapter=adapter,
        readiness=ready_report(),
        now_ms=NOW_MS,
    )

    assert report.ready is False
    assert check_status(report, "key-permissions") == FAIL


def test_missing_server_time_only_warns() -> None:
    adapter = FakeAdapter(permissions={"read"})
    adapter.server_time_ms = lambda: None  # type: ignore[method-assign]

    report = evaluate_preflight(
        live_config(),
        key_present=True,
        key_hint="***1234",
        adapter=adapter,
        readiness=ready_report(),
        now_ms=NOW_MS,
    )

    assert report.ready is True
    assert check_status(report, "clock-skew") == WARN


def test_a_failing_readiness_record_blocks() -> None:
    report = evaluate_preflight(
        live_config(),
        key_present=True,
        key_hint="***1234",
        adapter=FakeAdapter(permissions={"read"}),
        readiness=ready_report("closed-trades"),
        now_ms=NOW_MS,
    )

    assert report.ready is False
    assert check_status(report, "readiness") == FAIL
    assert "failing readiness criteria" in report.render()


def test_missing_credentials_and_adapter_block() -> None:
    report = evaluate_preflight(
        live_config(),
        key_present=False,
        key_hint=None,
        adapter=None,
        readiness=ready_report(),
        now_ms=NOW_MS,
    )

    assert report.ready is False
    for name in ("credentials", "spot-market", "key-permissions", "clock-skew"):
        assert check_status(report, name) == FAIL


class CurrentClockFake(FakeAdapter):
    """A fake whose server time is the real local clock."""

    def server_time_ms(self) -> int | None:
        return utc_now_ms()


def storage(tmp_path: Path) -> list[str]:
    return [
        "--delivery-db",
        str(tmp_path / "delivery.sqlite"),
        "--risk-db",
        str(tmp_path / "risk.sqlite"),
        "--cash",
        "1000",
    ]


def install_adapter(
    monkeypatch: pytest.MonkeyPatch,
    adapter: FakeAdapter | None = None,
) -> FakeAdapter:
    fake = adapter or CurrentClockFake(permissions={"read"})
    monkeypatch.setattr(cli, "make_adapter", lambda config, credentials=None: fake)
    return fake


def test_cli_preflight_is_ready_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    config = tmp_path / "execution.toml"
    config.write_text(LIVE_CONFIG, encoding="utf-8")
    fake = install_adapter(monkeypatch)
    monkeypatch.setenv("NOMICRONIX_BINANCE_API_KEY", "test-key-1234")
    monkeypatch.setenv("NOMICRONIX_BINANCE_API_SECRET", "test-secret")
    monkeypatch.setattr(cli, "evaluate_readiness", lambda *a, **k: ready_report())

    code = cli.main(
        [
            "preflight",
            *storage(tmp_path),
            "--config",
            str(config),
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["kind"] == "execution-preflight"
    assert {check["status"] for check in payload["checks"]} == {PASS}
    assert fake.private_calls == ["fetch_permissions"]


def test_cli_preflight_without_credentials_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    config = tmp_path / "execution.toml"
    config.write_text(LIVE_CONFIG, encoding="utf-8")
    fake = install_adapter(monkeypatch)
    monkeypatch.delenv("NOMICRONIX_BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("NOMICRONIX_BINANCE_API_SECRET", raising=False)
    monkeypatch.setattr(cli, "evaluate_readiness", lambda *a, **k: ready_report())

    code = cli.main(["preflight", *storage(tmp_path), "--config", str(config)])

    out = capsys.readouterr().out
    assert code == 2
    assert "NOT READY" in out
    assert "credentials: set NOMICRONIX_BINANCE_API_KEY" in out
    assert fake.private_calls == []


def test_cli_preflight_reports_a_blocked_paper_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    config = tmp_path / "execution.toml"
    config.write_text(LIVE_CONFIG, encoding="utf-8")
    install_adapter(monkeypatch)
    monkeypatch.setenv("NOMICRONIX_BINANCE_API_KEY", "test-key-1234")
    monkeypatch.setenv("NOMICRONIX_BINANCE_API_SECRET", "test-secret")
    monkeypatch.setattr(
        cli,
        "evaluate_readiness",
        lambda *a, **k: ready_report("closed-trades"),
    )

    code = cli.main(["preflight", *storage(tmp_path), "--config", str(config), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["ready"] is False
    readiness = next(check for check in payload["checks"] if check["name"] == "readiness")
    assert readiness["status"] == FAIL
    assert "closed paper trades" in readiness["detail"]
