"""Load and validate execution configuration from TOML."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from nomicronix.execution.models import ConfigError
from nomicronix.risk_engine.models import Instrument

DEFAULT_CONFIG_PATH = Path("config/execution.toml")
STAGES = frozenset({"shadow", "approved"})
_EXECUTION_KEYS = frozenset(
    {
        "exchange",
        "market_type",
        "dry_run",
        "allow_live",
        "stage",
        "max_slippage_bps",
        "max_intent_age_ms",
        "order_timeout_ms",
        "poll_interval_ms",
        "max_clock_skew_ms",
        "require_readiness",
        "quantity_step",
        "min_quantity",
        "min_notional",
    }
)
_RISK_KEYS = frozenset({"require_kill_switch_clear"})


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """Every switch that can make an order possible, in one validated place."""

    exchange: str = "binance"
    market_type: str = "spot"
    dry_run: bool = True
    allow_live: bool = False
    stage: str = "shadow"
    max_slippage_bps: int = 50
    max_intent_age_ms: int = 300_000
    order_timeout_ms: int = 60_000
    poll_interval_ms: int = 1_000
    max_clock_skew_ms: int = 2_000
    require_readiness: bool = True
    require_kill_switch_clear: bool = True
    quantity_step: Decimal = Decimal("0.00001")
    min_quantity: Decimal = Decimal("0.00001")
    min_notional: Decimal = Decimal("10")

    def __post_init__(self) -> None:
        if not self.exchange.strip():
            raise ConfigError("exchange must not be empty")
        if self.market_type != "spot":
            raise ConfigError("execution is spot only")
        if self.stage not in STAGES:
            raise ConfigError("stage must be shadow or approved; autonomy is out of scope")
        if not self.dry_run and not self.allow_live:
            raise ConfigError("dry_run = false requires allow_live = true")
        if self.max_slippage_bps < 0:
            raise ConfigError("max_slippage_bps must not be negative")
        for name in ("max_intent_age_ms", "order_timeout_ms", "poll_interval_ms"):
            if getattr(self, name) <= 0:
                raise ConfigError(f"{name} must be positive")
        if self.max_clock_skew_ms < 0:
            raise ConfigError("max_clock_skew_ms must not be negative")
        for name in ("quantity_step", "min_quantity", "min_notional"):
            if not getattr(self, name).is_finite():
                raise ConfigError(f"{name} must be finite")
        if self.quantity_step <= 0:
            raise ConfigError("quantity_step must be positive")
        if self.min_quantity < 0:
            raise ConfigError("min_quantity must not be negative")
        if self.min_notional < 0:
            raise ConfigError("min_notional must not be negative")

    def instrument(self, symbol: str) -> Instrument:
        return Instrument(
            symbol=symbol,
            quantity_step=self.quantity_step,
            min_quantity=self.min_quantity,
            min_notional=self.min_notional,
        )


def _str(section: dict[str, Any], key: str, default: str) -> str:
    raw = section.get(key, default)
    if not isinstance(raw, str):
        raise ConfigError(f"{key} must be a string")
    return raw


def _bool(section: dict[str, Any], key: str, default: bool) -> bool:
    raw = section.get(key, default)
    if not isinstance(raw, bool):
        raise ConfigError(f"{key} must be a boolean")
    return raw


def _int(section: dict[str, Any], key: str, default: int) -> int:
    raw = section.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(f"{key} must be an integer")
    return raw


def _decimal(section: dict[str, Any], key: str, default: Decimal) -> Decimal:
    try:
        value = Decimal(str(section.get(key, default)))
    except ArithmeticError as exc:
        raise ConfigError(f"{key} must be a decimal number") from exc
    if not value.is_finite():
        raise ConfigError(f"{key} must be finite")
    return value


def _reject_unknown(section: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = set(section) - allowed
    if unknown:
        raise ConfigError(f"unknown {label} keys: {sorted(unknown)}")


def load_execution_config(path: str | Path | None = None) -> ExecutionConfig:
    config_path = DEFAULT_CONFIG_PATH if path is None else Path(path)
    if not config_path.exists():
        if path is None:
            return ExecutionConfig()
        raise ConfigError(f"execution config not found: {config_path}")

    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    unknown_tables = set(data) - {"execution", "risk"}
    if unknown_tables:
        raise ConfigError(f"unknown config tables: {sorted(unknown_tables)}")

    section = data.get("execution")
    if not isinstance(section, dict):
        raise ConfigError("config must contain an [execution] table")
    _reject_unknown(section, _EXECUTION_KEYS, "execution")

    risk = data.get("risk", {})
    if not isinstance(risk, dict):
        raise ConfigError("[risk] must be a table")
    _reject_unknown(risk, _RISK_KEYS, "risk")

    default = ExecutionConfig()
    try:
        return ExecutionConfig(
            exchange=_str(section, "exchange", default.exchange),
            market_type=_str(section, "market_type", default.market_type),
            dry_run=_bool(section, "dry_run", default.dry_run),
            allow_live=_bool(section, "allow_live", default.allow_live),
            stage=_str(section, "stage", default.stage),
            max_slippage_bps=_int(section, "max_slippage_bps", default.max_slippage_bps),
            max_intent_age_ms=_int(section, "max_intent_age_ms", default.max_intent_age_ms),
            order_timeout_ms=_int(section, "order_timeout_ms", default.order_timeout_ms),
            poll_interval_ms=_int(section, "poll_interval_ms", default.poll_interval_ms),
            max_clock_skew_ms=_int(section, "max_clock_skew_ms", default.max_clock_skew_ms),
            require_readiness=_bool(section, "require_readiness", default.require_readiness),
            require_kill_switch_clear=_bool(
                risk, "require_kill_switch_clear", default.require_kill_switch_clear
            ),
            quantity_step=_decimal(section, "quantity_step", default.quantity_step),
            min_quantity=_decimal(section, "min_quantity", default.min_quantity),
            min_notional=_decimal(section, "min_notional", default.min_notional),
        )
    except (ConfigError, TypeError, ValueError, ArithmeticError) as exc:
        raise ConfigError(f"invalid execution config in {config_path}: {exc}") from exc
