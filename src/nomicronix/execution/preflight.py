"""Read-only live-readiness checks: could this host place an order right now?

`preflight` places nothing, cancels nothing and calls no write endpoint. It
reports each gate separately so the owner sees every blocker in one run instead
of discovering them one failed `submit` at a time.

A `warn` check does not block: for example, an exchange that does not expose key
permissions cannot be verified in code, so the trade-only rule stays a manual
dashboard check. A `fail` check means a live order is not possible yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nomicronix.execution.adapter import API_KEY_ENV, API_SECRET_ENV, ExchangeAdapter
from nomicronix.execution.config import ExecutionConfig
from nomicronix.execution.models import utc_now_ms
from nomicronix.signal_delivery.readiness import ReadinessReport

PASS = "pass"
WARN = "warn"
FAIL = "fail"

_WITHDRAW_PERMISSIONS = frozenset({"withdraw", "withdrawal"})


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    name: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class PreflightReport:
    checks: tuple[PreflightCheck, ...]
    readiness: ReadinessReport

    @property
    def ready(self) -> bool:
        return all(check.status != FAIL for check in self.checks)

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "execution-preflight",
            "ready": self.ready,
            "checks": [
                {"name": check.name, "status": check.status, "detail": check.detail}
                for check in self.checks
            ],
            "readiness": self.readiness.payload(),
        }

    def render(self) -> str:
        passed = sum(1 for check in self.checks if check.status == PASS)
        headline = "READY" if self.ready else "NOT READY"
        lines = [
            f"execution preflight: {headline} ({passed} of {len(self.checks)} checks pass)",
        ]
        for check in self.checks:
            lines.append(f"  [{check.status.upper()}] {check.name}: {check.detail}")
        failing = [item for item in self.readiness.criteria if not item.passed]
        if failing:
            lines.append("")
            lines.append("failing readiness criteria:")
            for item in failing:
                lines.append(f"  [FAIL] {item.label}: {item.actual} (need {item.required})")
        if self.ready:
            lines.extend(
                [
                    "",
                    "Next: run `reconcile`, then submit one intent with the exact phrase.",
                ]
            )
        return "\n".join(lines)


def _error_detail(prefix: str, exc: Exception) -> str:
    code = getattr(exc, "code", None)
    return f"{prefix}: {code or type(exc).__name__}"


def _live_switches(config: ExecutionConfig) -> PreflightCheck:
    disabled: list[str] = []
    if config.dry_run:
        disabled.append("dry_run = true")
    if not config.allow_live:
        disabled.append("allow_live = false")
    if config.stage != "approved":
        disabled.append(f'stage = "{config.stage}"')
    if disabled:
        return PreflightCheck(
            name="live-switches",
            status=FAIL,
            detail="live orders are disabled: " + ", ".join(disabled),
        )
    return PreflightCheck(
        name="live-switches",
        status=PASS,
        detail='stage = "approved", dry_run = false, allow_live = true',
    )


def _credentials(key_present: bool, key_hint: str | None) -> PreflightCheck:
    if key_present and key_hint:
        return PreflightCheck(
            name="credentials",
            status=PASS,
            detail=f"key {key_hint} found in the environment",
        )
    return PreflightCheck(
        name="credentials",
        status=FAIL,
        detail=f"set {API_KEY_ENV} and {API_SECRET_ENV} in the environment",
    )


def _spot_market(adapter: ExchangeAdapter | None) -> PreflightCheck:
    if adapter is None:
        return PreflightCheck(
            name="spot-market",
            status=FAIL,
            detail="not checked: the exchange adapter is unavailable",
        )
    try:
        market_type = adapter.market_type()
    except Exception as exc:  # a broken adapter must not crash the report
        return PreflightCheck(
            name="spot-market",
            status=FAIL,
            detail=_error_detail("not checked", exc),
        )
    if market_type != "spot":
        return PreflightCheck(
            name="spot-market",
            status=FAIL,
            detail=f"exchange market type is {market_type!r}, not spot",
        )
    return PreflightCheck(name="spot-market", status=PASS, detail="spot only")


def _key_permissions(adapter: ExchangeAdapter | None, *, key_present: bool) -> PreflightCheck:
    if not key_present:
        return PreflightCheck(
            name="key-permissions",
            status=FAIL,
            detail="not checked: credentials are missing",
        )
    if adapter is None:
        return PreflightCheck(
            name="key-permissions",
            status=FAIL,
            detail="not checked: the exchange adapter is unavailable",
        )
    try:
        permissions = adapter.fetch_permissions()
    except Exception as exc:
        return PreflightCheck(
            name="key-permissions",
            status=FAIL,
            detail=_error_detail("not checked", exc),
        )
    if permissions & _WITHDRAW_PERMISSIONS:
        return PreflightCheck(
            name="key-permissions",
            status=FAIL,
            detail="the key can withdraw; create a trade-only key with withdrawals disabled",
        )
    if not permissions:
        return PreflightCheck(
            name="key-permissions",
            status=WARN,
            detail=(
                "the exchange did not report permissions; confirm withdrawals are disabled "
                "and the host IP is pinned in the exchange dashboard"
            ),
        )
    return PreflightCheck(
        name="key-permissions",
        status=PASS,
        detail="granted: " + ", ".join(sorted(permissions)),
    )


def _clock(
    adapter: ExchangeAdapter | None,
    config: ExecutionConfig,
    now_ms: int,
) -> PreflightCheck:
    if adapter is None:
        return PreflightCheck(
            name="clock-skew",
            status=FAIL,
            detail="not checked: the exchange adapter is unavailable",
        )
    try:
        server_time = adapter.server_time_ms()
    except Exception as exc:
        return PreflightCheck(
            name="clock-skew",
            status=FAIL,
            detail=_error_detail("not checked", exc),
        )
    if server_time is None:
        return PreflightCheck(
            name="clock-skew",
            status=WARN,
            detail=(
                "the exchange did not report server time; keep the host clock on NTP "
                f"(limit {config.max_clock_skew_ms} ms)"
            ),
        )
    skew = abs(server_time - now_ms)
    if skew > config.max_clock_skew_ms:
        return PreflightCheck(
            name="clock-skew",
            status=FAIL,
            detail=(
                f"local clock is {skew} ms from the exchange "
                f"(limit {config.max_clock_skew_ms} ms); sync it before submitting"
            ),
        )
    return PreflightCheck(
        name="clock-skew",
        status=PASS,
        detail=f"skew {skew} ms",
    )


def _readiness(readiness: ReadinessReport) -> PreflightCheck:
    passed = sum(1 for item in readiness.criteria if item.passed)
    total = len(readiness.criteria)
    if readiness.ready:
        return PreflightCheck(
            name="readiness",
            status=PASS,
            detail=f"all {total} paper-record criteria met",
        )
    failing = [item.label for item in readiness.criteria if not item.passed]
    return PreflightCheck(
        name="readiness",
        status=FAIL,
        detail=f"{passed} of {total} criteria met; failing: " + ", ".join(failing),
    )


def evaluate_preflight(
    config: ExecutionConfig,
    *,
    key_present: bool,
    key_hint: str | None,
    adapter: ExchangeAdapter | None,
    readiness: ReadinessReport,
    now_ms: int | None = None,
) -> PreflightReport:
    """Run every read-only gate and return one report; never raises for a gate."""
    current_ms = utc_now_ms() if now_ms is None else now_ms
    checks = (
        _live_switches(config),
        _credentials(key_present, key_hint),
        _spot_market(adapter),
        _key_permissions(adapter, key_present=key_present),
        _clock(adapter, config, current_ms),
        _readiness(readiness),
    )
    return PreflightReport(checks=checks, readiness=readiness)
