"""Load and validate risk limits from TOML."""

from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

from nomicronix.risk_engine.models import RiskLimits

DEFAULT_CONFIG_PATH = Path("config/risk.toml")


class ConfigError(ValueError):
    """The risk configuration is missing, malformed or outside safe bounds."""


def _decimal(section: dict[str, Any], key: str, default: Decimal) -> Decimal:
    return Decimal(str(section.get(key, default)))


def load_limits(path: str | Path | None = None) -> RiskLimits:
    config_path = DEFAULT_CONFIG_PATH if path is None else Path(path)
    if not config_path.exists():
        if path is None:
            return RiskLimits()
        raise ConfigError(f"risk config not found: {config_path}")

    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    section = data.get("risk")
    if not isinstance(section, dict):
        raise ConfigError("config must contain a [risk] table")

    default = RiskLimits()
    try:
        return RiskLimits(
            risk_per_trade_pct=_decimal(section, "risk_per_trade_pct", default.risk_per_trade_pct),
            max_daily_loss_pct=_decimal(section, "max_daily_loss_pct", default.max_daily_loss_pct),
            max_drawdown_pct=_decimal(section, "max_drawdown_pct", default.max_drawdown_pct),
            max_concurrent_positions=int(
                section.get("max_concurrent_positions", default.max_concurrent_positions)
            ),
            max_position_notional_pct=_decimal(
                section, "max_position_notional_pct", default.max_position_notional_pct
            ),
            max_total_exposure_pct=_decimal(
                section, "max_total_exposure_pct", default.max_total_exposure_pct
            ),
            max_open_risk_pct=_decimal(section, "max_open_risk_pct", default.max_open_risk_pct),
            min_reward_risk=_decimal(section, "min_reward_risk", default.min_reward_risk),
            min_notional_usdt=_decimal(section, "min_notional_usdt", default.min_notional_usdt),
            max_signal_age_ms=int(section.get("max_signal_age_ms", default.max_signal_age_ms)),
            allowed_symbols=frozenset(
                str(symbol)
                for symbol in section.get("allowed_symbols", sorted(default.allowed_symbols))
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid risk config in {config_path}: {exc}") from exc
