from decimal import Decimal

import pytest

from nomicronix.risk_engine.config import ConfigError, load_limits
from nomicronix.risk_engine.models import RiskLimits


def test_missing_default_file_falls_back_to_built_in_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_limits() == RiskLimits()


def test_missing_explicit_file_is_an_error(tmp_path) -> None:
    with pytest.raises(ConfigError):
        load_limits(tmp_path / "nope.toml")


def test_committed_config_matches_defaults() -> None:
    assert load_limits("config/risk.toml") == RiskLimits()


def test_custom_values_are_parsed_exactly(tmp_path) -> None:
    path = tmp_path / "risk.toml"
    path.write_text(
        """
[risk]
risk_per_trade_pct = "0.01"
max_daily_loss_pct = "0.05"
max_drawdown_pct = "0.20"
max_concurrent_positions = 3
max_position_notional_pct = "0.30"
max_total_exposure_pct = "0.60"
max_open_risk_pct = "0.04"
min_reward_risk = "2.5"
min_notional_usdt = "20"
max_signal_age_ms = 600000
allowed_symbols = ["BTC/USDT"]
""",
        encoding="utf-8",
    )
    limits = load_limits(path)
    assert limits.risk_per_trade_pct == Decimal("0.01")
    assert limits.max_concurrent_positions == 3
    assert limits.min_notional_usdt == Decimal("20")
    assert limits.allowed_symbols == frozenset({"BTC/USDT"})


def test_invalid_toml_is_reported(tmp_path) -> None:
    path = tmp_path / "risk.toml"
    path.write_text("[risk\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_limits(path)


def test_missing_risk_table_is_reported(tmp_path) -> None:
    path = tmp_path / "risk.toml"
    path.write_text("[other]\nvalue = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_limits(path)


def test_invalid_percentage_is_reported(tmp_path) -> None:
    path = tmp_path / "risk.toml"
    path.write_text(
        """
[risk]
risk_per_trade_pct = "2.0"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_limits(path)


def test_empty_allowlist_is_reported(tmp_path) -> None:
    path = tmp_path / "risk.toml"
    path.write_text(
        """
[risk]
allowed_symbols = []
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_limits(path)


@pytest.mark.parametrize(
    "body",
    [
        '[risk]\nmin_reward_risk = "nan"\n',
        '[risk]\nmin_notional_usdt = "inf"\n',
        '[risk]\nrisk_per_trade_pct = "abc"\n',
        '[risk]\nmax_signal_age_ms = "soon"\n',
    ],
)
def test_non_finite_or_malformed_decimals_are_reported(tmp_path, body: str) -> None:
    path = tmp_path / "risk.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_limits(path)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_direct_limits_reject_non_finite_decimals(value: str) -> None:
    with pytest.raises(ValueError, match="finite"):
        RiskLimits(min_reward_risk=Decimal(value))
