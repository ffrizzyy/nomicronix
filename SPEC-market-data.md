# Spec: market-data

Module id: `market-data` (depends on: none)

## Objective

Provide clean, gap-free market data to the rest of Nomicronix. Every other
module that needs prices reads them from here and never talks to the exchange
for data directly.

**Consumers:** `backtesting` (historical candles), `strategy-engine`
(historical plus latest candles).

**User stories**
- As the owner, I can backfill N months of OHLCV candles for a list of symbols
  and timeframes with one command.
- As a consumer module, I can ask for candles for a symbol, timeframe and time
  range and get a contiguous, sorted, de-duplicated series.
- As the owner, I can run a live updater that keeps the store current, recovers
  from disconnects, and tells me when data is stale.

**Out of scope for this module:** order book depth and funding rates (add in a
later revision once a strategy needs them), trading, signal logic, any use of
API keys. This module uses public endpoints only.

## Tech Stack

- Python 3.12
- `ccxt` for the exchange adapter (public endpoints)
- SQLite via the standard `sqlite3` module
- `pytest`, `pytest-cov`, `ruff`, `mypy`
- `uv` for dependency and environment management

## Commands

```
Install:      uv sync
Backfill:     uv run python -m nomicronix.market_data backfill --exchange binance --symbols BTC/USDT,ETH/USDT --timeframes 1h,15m --since 2025-01-01
Live update:  uv run python -m nomicronix.market_data live --exchange binance --symbols BTC/USDT,ETH/USDT --timeframes 1h,15m
Verify data:  uv run python -m nomicronix.market_data verify --exchange binance --symbols BTC/USDT --timeframes 1h
Test:         uv run pytest tests/market_data -m "not network" --cov=nomicronix.market_data --cov-fail-under=85
Network test: uv run pytest tests/market_data -m network
Lint:         uv run ruff check src tests --fix
Format:       uv run ruff format src tests
Types:        uv run mypy src/nomicronix/market_data
```

## Project Structure

```
src/nomicronix/market_data/
  models.py        -> Candle dataclass, Timeframe enum
  adapter.py       -> ExchangeAdapter protocol + ccxt implementation
  store.py         -> SQLite store: upsert, query, gap detection
  backfill.py      -> Paginated historical download, resumable
  live.py          -> Live updater with reconnect and staleness check
  __main__.py      -> CLI (backfill, live, verify)
tests/market_data/
  fakes.py         -> FakeExchangeAdapter for deterministic tests
  test_store.py
  test_backfill.py
  test_live.py
  test_adapter_contract.py   -> marked `network`, hits the real exchange
data/               -> SQLite file, gitignored
docs/               -> notes on exchange quirks
```

## Interface Contract (what consumers depend on)

```python
@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str          # "BTC/USDT"
    timeframe: str       # "1h"
    open_time_ms: int    # UTC epoch milliseconds, start of the candle
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

class MarketDataStore(Protocol):
    def get_candles(self, symbol: str, timeframe: str,
                    start_ms: int, end_ms: int) -> list[Candle]: ...
    def latest_candle(self, symbol: str, timeframe: str) -> Candle | None: ...
    def find_gaps(self, symbol: str, timeframe: str,
                  start_ms: int, end_ms: int) -> list[tuple[int, int]]: ...
```

Guarantees: results sorted ascending by `open_time_ms`, no duplicates, only
closed candles (the in-progress candle is never returned), all timestamps UTC.

## Code Style

- Type hints everywhere, `mypy --strict` clean.
- `Decimal` for prices and volumes, never `float`.
- Timestamps are `int` UTC milliseconds, named `*_ms`.
- Pure functions where possible, I/O at the edges.
- No bare `except`. Exchange errors are caught as specific types and logged
  with symbol and timeframe.

```python
def find_gaps(open_times_ms: list[int], step_ms: int) -> list[tuple[int, int]]:
    """Return (missing_from_ms, missing_to_ms) ranges in a sorted candle series."""
    gaps: list[tuple[int, int]] = []
    for prev, curr in zip(open_times_ms, open_times_ms[1:]):
        if curr - prev > step_ms:
            gaps.append((prev + step_ms, curr - step_ms))
    return gaps
```

## Testing Strategy

| Level | What | Tool | Runs in CI |
|---|---|---|---|
| Unit | store upsert/query, gap detection, pagination, timeframe math | pytest | yes |
| Component | backfill and live updater against `FakeExchangeAdapter` (simulated disconnects, duplicates, rate-limit errors) | pytest | yes |
| Contract | real ccxt adapter returns data matching `Candle` shape | pytest `-m network` | no, run manually |

- Coverage: at least 85% line coverage for `nomicronix.market_data`.
- Tests that touch the network must be marked `network` and never run by default.
- Bug fixes start with a failing test (Prove-It pattern).

## Boundaries

**Always**
- Write idempotently: re-running backfill never creates duplicates.
- Respect exchange rate limits (use ccxt's built-in rate limiter).
- Store and compare time in UTC milliseconds.
- Exclude the still-open candle from all query results.
- Run tests and lint before every commit.

**Ask first**
- Adding a new dependency.
- Changing the SQLite schema or the `Candle` / `MarketDataStore` contract
  (consumers depend on it).
- Adding data types beyond OHLCV.
- Supporting a second exchange.

**Never**
- Use or require API keys in this module.
- Commit the `data/` directory or any secrets.
- Return partial or in-progress candles.
- Silently drop data on error. Failures must be logged and surfaced.
- Remove or skip a failing test without approval.

## Success Criteria

1. Backfilling 12 months of `1h` candles for `BTC/USDT` completes with zero
   gaps (`verify` reports 0 gaps) and no duplicate rows.
2. Running backfill twice in a row produces identical row counts.
3. If backfill is interrupted and restarted, it resumes without re-downloading
   completed ranges.
4. `get_candles` returns sorted, contiguous candles and never includes the
   open candle (covered by a unit test).
5. In a simulated disconnect test, the live updater reconnects and fills any
   missed candles with no gaps.
6. If no new candle arrives for more than 2x the timeframe, the live updater
   logs a `STALE_DATA` error. Consumers can detect this via `latest_candle`.
7. `uv run pytest ... --cov-fail-under=85`, `ruff check`, and `mypy` all pass.

## Open Questions

1. **Symbols and timeframes:** is `BTC/USDT, ETH/USDT` on `1h` and `15m` the
   right starting set? Anything else (e.g. SOL, `4h`, `1d`)?
2. **History depth:** is back to `2025-01-01` enough, or do you want multiple
   years for more robust backtests?
3. **Exchange access:** please confirm the Binance public API is reachable
   from your network and jurisdiction. If not, we start with Bybit or
   Coinbase instead. The adapter is generic, so this only changes the first
   integration.
4. **Live mode:** polling the REST API once per candle close is simpler and
   enough for 15m and 1h strategies. Websockets are only needed for
   sub-minute data. Is polling acceptable for now?
5. **Tooling:** is `uv` fine, or do you prefer plain `pip` and `venv`?

## Verification Status (added at build time)

Built with test-first development. "Offline" means the tests that run without the
network (`-m "not network"`, 137 in this module); they pass on the owner's machine.
The real-exchange checks were run on 2026-11-25 through Binance's official spot
market-data mirror (`https://data-api.binance.vision/api/v3`), because
`api.binance.com` is blocked on that host. The mirror serves the same `/api/v3`
payloads; it only lacks order endpoints, which this module never uses.

| # | Success criterion | Proof | Status |
|---|---|---|---|
| 1 | 12 months of `1h` BTC/USDT backfills with zero gaps and no duplicates | `test_twelve_months_of_hourly_candles_backfill_with_no_gaps`; `test_backfill_fills_the_store_up_to_the_last_closed_candle_across_pages` | Real, 2026-11-25: 8,775 candles 2025-11-25 to 2026-11-25T14:00Z, `verify` reported no gaps; offline unit tests also pass |
| 2 | Running backfill twice gives identical row counts | `test_running_backfill_twice_gives_identical_rows`; `test_a_rerun_with_nothing_new_makes_no_requests` | Real, 2026-11-25: immediate re-run wrote 0 candles; offline unit tests also pass |
| 3 | An interrupted backfill resumes without re-downloading completed ranges | `test_an_interrupted_backfill_resumes_without_redownloading_stored_ranges`; `test_a_second_run_downloads_only_the_new_candles`; `test_an_earlier_since_fills_in_the_missing_start` | Real, 2026-11-25: a 5-year run killed after 4s at 11,000 rows resumed with 32,838 more, 43,838 total; offline unit tests also pass |
| 4 | `get_candles` is sorted, contiguous, and never includes the open candle | `test_get_candles_returns_ascending_order_even_when_written_out_of_order`; `test_upsert_rejects_a_candle_that_has_not_closed`; `test_backfill_never_stores_the_candle_that_is_still_open`; `test_update_series_never_stores_the_candle_that_is_still_open` | Offline: passes |
| 5 | After a simulated disconnect the live updater reconnects and fills missed candles with no gaps | `test_a_three_hour_outage_in_a_simulated_day_leaves_no_gaps_and_is_reported`; `test_after_a_long_outage_the_next_tick_fills_every_missed_candle`; `test_an_outage_longer_than_the_retry_budget_does_not_crash_the_loop` | Offline: passes. Real 15m live close checked on 2026-11-25; a real network drop has not been staged |
| 6 | `STALE_DATA` is logged when no candle arrives for more than 2x the timeframe | `test_stale_data_is_logged_at_error_level_with_symbol_and_timeframe`; `test_the_live_loop_logs_stale_data_during_an_outage_and_recovery_after_it`; the `is_stale` tests in `test_models.py` | Offline: passes |
| 7 | `pytest --cov-fail-under=85`, `ruff check` and `mypy` all pass | Real, 2026-11-25: 137 passed, `nomicronix.market_data` 99% with `pytest-cov` (adapter 100%), `ruff check` and strict `mypy` clean |

The real-exchange contract test (`pytest -m network`) passed on 2026-11-25 through the spot mirror (1 passed: 24 well-formed hourly candles).
