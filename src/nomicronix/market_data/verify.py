"""Check a stored series for gaps, optionally trying to refill them from the exchange."""

from dataclasses import dataclass
from datetime import UTC, datetime

from nomicronix.market_data.adapter import ExchangeAdapter
from nomicronix.market_data.backfill import DEFAULT_CONFIG, BackfillConfig, backfill_range
from nomicronix.market_data.models import Timeframe
from nomicronix.market_data.store import SqliteStore


@dataclass(frozen=True)
class VerifyReport:
    symbol: str
    timeframe: str
    count: int
    first_open_ms: int | None
    last_open_ms: int | None
    gaps: list[tuple[int, int]]  # still missing after any refill attempt
    refill_attempted: bool
    refilled_candles: int

    @property
    def ok(self) -> bool:
        return self.count > 0 and not self.gaps


def verify_series(
    store: SqliteStore,
    symbol: str,
    timeframe: str,
    *,
    since_ms: int | None = None,
    adapter: ExchangeAdapter | None = None,
    config: BackfillConfig = DEFAULT_CONFIG,
) -> VerifyReport:
    """Look for gaps between `since_ms` (default: the first stored candle) and the last candle.

    With an `adapter`, each gap is requested from the exchange once. Gaps that are still
    missing afterwards are ones the exchange could not supply.
    """
    step_ms = Timeframe(timeframe).ms
    earliest = store.earliest_candle(symbol, timeframe)
    latest = store.latest_candle(symbol, timeframe)
    if earliest is None or latest is None:
        return VerifyReport(symbol, timeframe, 0, None, None, [], False, 0)

    start_ms = earliest.open_time_ms if since_ms is None else -(-since_ms // step_ms) * step_ms
    end_ms = latest.open_time_ms + step_ms  # exclusive
    gaps = store.find_gaps(symbol, timeframe, start_ms, end_ms)

    refill_attempted = adapter is not None and bool(gaps)
    refilled = 0
    if adapter is not None and gaps:
        for gap_start, gap_end in gaps:
            refilled += backfill_range(
                adapter, store, symbol, timeframe, gap_start,
                until_ms=gap_end + step_ms, config=config,
            )
        gaps = store.find_gaps(symbol, timeframe, start_ms, end_ms)

    count = len(store.get_candles(symbol, timeframe, start_ms, end_ms))
    return VerifyReport(
        symbol, timeframe, count, earliest.open_time_ms, latest.open_time_ms,
        gaps, refill_attempted, refilled,
    )


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%MZ")


def format_report(report: VerifyReport) -> str:
    label = f"{report.symbol} {report.timeframe}"
    if report.first_open_ms is None or report.last_open_ms is None:
        return f"{label}: no candles stored"

    step_ms = Timeframe(report.timeframe).ms
    summary = (
        f"{label}: {report.count} candles, "
        f"{_fmt(report.first_open_ms)} to {_fmt(report.last_open_ms)}"
    )
    if report.refill_attempted:
        summary += f", refilled {report.refilled_candles} candles"
    if not report.gaps:
        return f"{summary}, no gaps"

    kind = "UNFILLABLE (exchange returned nothing)" if report.refill_attempted else "GAP"
    lines = [f"{summary}, {len(report.gaps)} gap(s)"]
    for gap_start, gap_end in report.gaps:
        size = (gap_end - gap_start) // step_ms + 1
        noun = "candle" if size == 1 else "candles"
        lines.append(f"  {kind} {_fmt(gap_start)} to {_fmt(gap_end)} ({size} {noun})")
    return "\n".join(lines)
