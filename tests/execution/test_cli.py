from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path

import pytest

import nomicronix.execution.__main__ as cli
from nomicronix.execution.adapter import credentials_from_env
from nomicronix.execution.models import IntentState, OrderStatus
from nomicronix.execution.store import ExecutionStore
from nomicronix.strategy_engine.models import SignalCandidate
from tests.execution.fakes import NOW_MS, FakeAdapter, make_order
from tests.signal_delivery.fakes import make_candidate, write_signals
from tests.strategy_engine.fakes import HOUR_MS

LIVE_CONFIG = """
[execution]
exchange = "binance"
market_type = "spot"
dry_run = false
allow_live = true
stage = "approved"
max_slippage_bps = 50
max_intent_age_ms = 300000
order_timeout_ms = 100
poll_interval_ms = 1
max_clock_skew_ms = 2000
quantity_step = "0.00001"
min_quantity = "0.00001"
min_notional = "10"
"""


def storage(tmp_path: Path) -> list[str]:
    return [
        "--execution-db",
        str(tmp_path / "execution.sqlite"),
        "--risk-db",
        str(tmp_path / "risk.sqlite"),
        "--cash",
        "1000",
    ]


def write_config(tmp_path: Path, body: str = LIVE_CONFIG) -> Path:
    path = tmp_path / "execution.toml"
    path.write_text(body, encoding="utf-8")
    return path


def install_adapter(
    monkeypatch: pytest.MonkeyPatch,
    *,
    balances: dict[str, Decimal] | None = None,
    ghost: bool = False,
) -> None:
    def factory(config: object, *, require_private: bool) -> FakeAdapter:
        fake = FakeAdapter()
        fake.set_ticker("BTC/USDT", bid="100", ask="100.1", at_ms=NOW_MS)
        if balances is not None:
            fake.balance_map = dict(balances)
        if ghost:
            order = make_order(
                client_order_id="nx-ghost",
                exchange_order_id="ex-ghost",
                status=OrderStatus.OPEN,
            )
            fake.orders["nx-ghost"] = order
            fake.by_exchange_id["ex-ghost"] = order
        return fake

    monkeypatch.setattr(cli, "build_adapter", factory)
    monkeypatch.setattr(cli, "credentials_from_env", lambda: ("test-key-1234", "test-secret"))


def recent_candidate(*, stop: str = "95", target: str = "115") -> SignalCandidate:
    start = int(time.time() * 1000) - HOUR_MS
    return make_candidate(start_ms=start, stop=stop, target=target)


def shadow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    install_adapter(monkeypatch)
    signals = write_signals(tmp_path / "signals.json", [recent_candidate()])
    config = write_config(tmp_path)
    code = cli.main(
        [
            "shadow",
            "--signals",
            str(signals),
            *storage(tmp_path),
            "--config",
            str(config),
        ]
    )
    assert code == 0
    store = ExecutionStore(tmp_path / "execution.sqlite")
    try:
        intents = store.list_intents()
        assert len(intents) == 1
        return config, intents[0].intent_id
    finally:
        store.close()


def test_shadow_status_and_show(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    config, intent_id = shadow(tmp_path, monkeypatch)
    capsys.readouterr()

    assert cli.main(["status", *storage(tmp_path), "--config", str(config), "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["intents"] == 1
    assert status["by_state"]["awaiting_approval"] == 1
    assert status["stage"] == "approved"

    assert (
        cli.main(
            [
                "show",
                "--intent",
                intent_id,
                *storage(tmp_path),
                "--config",
                str(config),
                "--json",
            ]
        )
        == 0
    )
    shown = json.loads(capsys.readouterr().out)
    assert shown["intent"]["intent_id"] == intent_id
    assert [event["type"] for event in shown["events"]] == ["approved"]


def test_submit_requires_live_and_the_phrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    config, intent_id = shadow(tmp_path, monkeypatch)

    assert (
        cli.main(
            [
                "submit",
                "--intent",
                intent_id,
                "--confirm",
                "PLACE ORDER",
                *storage(tmp_path),
                "--config",
                str(config),
            ]
        )
        == 4
    )
    assert "live submission is disabled" in capsys.readouterr().err

    assert (
        cli.main(
            [
                "submit",
                "--intent",
                intent_id,
                "--confirm",
                "place order",
                "--live",
                *storage(tmp_path),
                "--config",
                str(config),
            ]
        )
        == 1
    )
    assert "exact phrase" in capsys.readouterr().err


def test_submit_fills_and_status_reflects_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    config, intent_id = shadow(tmp_path, monkeypatch)
    capsys.readouterr()

    assert (
        cli.main(
            [
                "submit",
                "--intent",
                intent_id,
                "--confirm",
                "PLACE ORDER",
                "--live",
                *storage(tmp_path),
                "--config",
                str(config),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["intent"]["state"] == "filled"
    assert Decimal(payload["order"]["filled_quantity"]) == Decimal("1")

    assert cli.main(["status", *storage(tmp_path), "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["by_state"]["filled"] == 1


def test_missing_credentials_exit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    config, intent_id = shadow(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "build_adapter", lambda *a, **k: FakeAdapter())
    monkeypatch.setattr(cli, "credentials_from_env", credentials_from_env)
    monkeypatch.delenv("NOMICRONIX_BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("NOMICRONIX_BINANCE_API_SECRET", raising=False)

    code = cli.main(
        [
            "submit",
            "--intent",
            intent_id,
            "--confirm",
            "PLACE ORDER",
            "--live",
            *storage(tmp_path),
            "--config",
            str(config),
        ]
    )
    assert code == 1
    assert "missing exchange credentials" in capsys.readouterr().err


def test_reconcile_clean_and_unclean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    install_adapter(monkeypatch)
    assert cli.main(["reconcile", *storage(tmp_path), "--config", str(write_config(tmp_path))]) == 0
    assert "clean" in capsys.readouterr().out

    install_adapter(monkeypatch, ghost=True)
    assert (
        cli.main(
            [
                "reconcile",
                *storage(tmp_path),
                "--config",
                str(write_config(tmp_path)),
                "--json",
            ]
        )
        == 3
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["clean"] is False
    assert payload["unknown_orders"] == ["nx-ghost"]


def test_cancel_refuses_an_unknown_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    install_adapter(monkeypatch)
    code = cli.main(
        [
            "cancel",
            "--intent",
            "nx-missing",
            *storage(tmp_path),
            "--config",
            str(write_config(tmp_path)),
        ]
    )
    assert code == 1
    assert "unknown intent" in capsys.readouterr().err


def test_flatten_requires_the_phrase_and_is_empty_when_flat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    install_adapter(monkeypatch)
    config = write_config(tmp_path)
    assert (
        cli.main(
            ["flatten", "--confirm", "FLATTEN NOW", *storage(tmp_path), "--config", str(config)]
        )
        == 0
    )
    assert "canceled" not in capsys.readouterr().out

    assert (
        cli.main(
            ["flatten", "--confirm", "flatten now", *storage(tmp_path), "--config", str(config)]
        )
        == 1
    )
    assert "FLATTEN NOW" in capsys.readouterr().err


def test_a_broken_config_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    install_adapter(monkeypatch)
    broken = tmp_path / "broken.toml"
    broken.write_text('[execution]\nmarket_type = "future"\n', encoding="utf-8")
    code = cli.main(["status", *storage(tmp_path), "--config", str(broken)])
    assert code == 1
    assert "spot only" in capsys.readouterr().err


def test_shadow_reports_a_rejected_signal_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    install_adapter(monkeypatch)
    signals = write_signals(tmp_path / "signals.json", [recent_candidate(stop="97", target="103")])
    code = cli.main(
        [
            "shadow",
            "--signals",
            str(signals),
            *storage(tmp_path),
            "--config",
            str(write_config(tmp_path)),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "rejected:" in out
    store = ExecutionStore(tmp_path / "execution.sqlite")
    try:
        assert store.list_intents()[0].state is IntentState.REJECTED
    finally:
        store.close()
