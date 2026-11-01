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
