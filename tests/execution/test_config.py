from decimal import Decimal
from pathlib import Path

import pytest

from nomicronix.execution.config import ExecutionConfig, load_execution_config
from nomicronix.execution.models import ConfigError


def write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_missing_default_config_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_execution_config() == ExecutionConfig()


def test_missing_explicit_config_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_execution_config(tmp_path / "missing.toml")


def test_repository_config_loads() -> None:
    assert load_execution_config(Path("config/execution.toml")) == ExecutionConfig()


def test_overrides_load(tmp_path: Path) -> None:
    path = write(
        tmp_path / "execution.toml",
        "[execution]\n"
        'exchange = "binance"\n'
        'market_type = "spot"\n'
        "dry_run = false\n"
        "allow_live = true\n"
        'stage = "approved"\n'
        "max_slippage_bps = 25\n"
        "max_intent_age_ms = 60000\n"
        "order_timeout_ms = 30000\n"
        "poll_interval_ms = 500\n"
        "max_clock_skew_ms = 1000\n"
        'quantity_step = "0.001"\n'
        'min_quantity = "0.01"\n'
        'min_notional = "20"\n'
        "[risk]\n"
        "require_kill_switch_clear = false\n",
    )
    config = load_execution_config(path)
    assert config.dry_run is False
    assert config.allow_live is True
    assert config.stage == "approved"
    assert config.max_slippage_bps == 25
    assert config.require_kill_switch_clear is False
    assert config.quantity_step == Decimal("0.001")


def test_instrument_uses_the_configured_rules() -> None:
    config = ExecutionConfig(
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.01"),
        min_notional=Decimal("20"),
    )
    instrument = config.instrument("ETH/USDT")
    assert instrument.symbol == "ETH/USDT"
    assert instrument.quantity_step == Decimal("0.001")


@pytest.mark.parametrize(
    "body",
    [
        "[other]\nx = 1\n",
        "[execution]\nunknown_key = 1\n",
        '[execution]\nmarket_type = "future"\n',
        '[execution]\nmarket_type = "margin"\n',
        '[execution]\nstage = "autonomous"\n',
        '[execution]\nstage = "live"\n',
        "[execution]\ndry_run = false\n",
        "[execution]\nmax_slippage_bps = -1\n",
        "[execution]\nmax_intent_age_ms = 0\n",
        "[execution]\norder_timeout_ms = 0\n",
        "[execution]\npoll_interval_ms = 0\n",
        "[execution]\nmax_clock_skew_ms = -1\n",
        '[execution]\nquantity_step = "0"\n',
        '[execution]\nmin_quantity = "-1"\n',
        '[execution]\nmin_notional = "-1"\n',
        '[execution]\nquantity_step = "nan"\n',
        '[execution]\nmin_quantity = "inf"\n',
        '[execution]\nmin_notional = "-inf"\n',
        '[execution]\nquantity_step = "abc"\n',
        '[execution]\ndry_run = "yes"\n',
        "[execution]\nmax_intent_age_ms = true\n",
        '[execution]\nmax_intent_age_ms = "5"\n',
        '[risk]\nrequire_kill_switch_clear = "yes"\n',
        "[risk]\nother = true\n",
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, body: str) -> None:
    path = write(tmp_path / "execution.toml", body)
    with pytest.raises(ConfigError):
        load_execution_config(path)


def test_malformed_toml_is_rejected(tmp_path: Path) -> None:
    path = write(tmp_path / "execution.toml", "[execution\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_execution_config(path)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_direct_construction_rejects_non_finite_decimals(value: str) -> None:
    with pytest.raises(ConfigError, match="finite"):
        ExecutionConfig(quantity_step=Decimal(value))
