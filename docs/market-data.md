# market-data

Clean, gap-free candle data for the rest of Nomicronix. Other modules read candles
from the store in this module and never talk to the exchange for prices themselves.
Public exchange endpoints only: no API keys are needed or used.

## Concepts

- **Candle:** one OHLCV bar. Prices and volume are `Decimal`. Times are UTC epoch
  milliseconds (`open_time_ms`, start of the bar).
- **Closed candles only.** The bar still in progress is never stored and never returned.
  The store refuses to write one and raises `OpenCandleError`.
- **Ranges are half-open:** `start_ms <= open_time_ms < end_ms`.
- **Supported timeframes:** `15m`, `1h`, `4h`, `1d`. Adding one is one line in
  `Timeframe` (`models.py`). Candle slots sit on multiples of the timeframe (UTC).
- **One store per exchange.** `SqliteStore(path, exchange)` only sees its own exchange's rows.

## Commands

All commands take `--exchange` (default `binance`, any ccxt exchange id), `--symbols`,
`--timeframes` (comma separated) and `--db` (default `data/market.sqlite`).

```
# Download history (safe to re-run, resumes where it stopped)
uv run python -m nomicronix.market_data backfill \
    --symbols BTC/USDT,ETH/USDT --timeframes 1h,15m --since 2025-01-01

# Keep it current: polls 5s after each candle closes, runs until Ctrl-C
uv run python -m nomicronix.market_data live \
    --symbols BTC/USDT,ETH/USDT --timeframes 1h,15m

# Check for gaps (exit code 1 if any, or if nothing is stored)
uv run python -m nomicronix.market_data verify \
    --symbols BTC/USDT --timeframes 1h --since 2025-01-01

# Try to fill gaps from the exchange; what is still missing is reported UNFILLABLE
uv run python -m nomicronix.market_data verify \
    --symbols BTC/USDT --timeframes 1h --since 2025-01-01 --refill
```

`backfill` exits 1 if any series failed; the other series are still stored.

## How it behaves

**Backfill and resume.** Backfill pages forward 1000 candles per request. On a re-run it
downloads only the two missing ends of a series: anything before the earliest stored candle
(if `--since` is earlier) and everything after the latest. Interrupting it (Ctrl-C, crash)
is safe because each page is committed as it arrives.

**Gaps in the middle are not retried by `backfill`.** If the exchange had a maintenance
window and skipped candles, `backfill` will not go back for them. Run `verify` to see them
and `verify --refill` to try once. Gaps the exchange cannot supply stay reported, and exit
code 1 tells scripts the series is not clean.

**Retries.** Rate-limit and network errors are retried with exponential backoff
(1s, 2s, 4s, ... capped at 60s, 5 retries). Other exchange errors, such as an unknown
symbol, are not retried and fail only that series.

**Live updates.** After each candle close the updater asks for everything newer than the
latest stored candle, so a missed poll, an outage or a restart is repaired by the next
one. If the exchange has not published a candle yet it asks again after 30s instead of
waiting a whole period. Exchange failures never stop the loop. Errors that are bugs, not
outages, do stop it, on purpose.

**Staleness.** A series is stale when no new candle has arrived for more than 2x its
timeframe, measured from the close of the latest stored candle. The live updater logs
`STALE_DATA <symbol> <timeframe>` at ERROR once when that starts and
`DATA_RECOVERED <symbol> <timeframe>` at INFO when it ends. **Consumers must check
freshness themselves before trusting prices:**

```python
from nomicronix.market_data.models import is_stale

if is_stale(store.latest_candle("BTC/USDT", "1h"), now_ms):
    ...  # do not trade on this
```

## Using the data from another module

Depend on the `MarketDataStore` protocol (`store.py`), not on SQLite:
`get_candles(symbol, timeframe, start_ms, end_ms)`, `latest_candle(symbol, timeframe)`,
`find_gaps(symbol, timeframe, start_ms, end_ms)`. Results are ascending, de-duplicated and
closed-only.

## Exchange notes

- Candles are requested 1000 at a time, the usual maximum on Binance. Other exchanges may
  differ; short pages are handled, but this has not been tried on any exchange yet.
- Exchanges return the in-progress candle as the newest row. It is dropped before storing.
- Rate limiting is also handled by ccxt's built-in limiter (`enableRateLimit`).
- Availability of a given exchange's public API depends on where you are. If Binance is not
  reachable from your network, pass another ccxt id, e.g. `--exchange bybit` or
  `--exchange coinbase`, and run the network contract test against it first.

## Known limitations

- **Use one process per database file.** SQLite is in its default journal mode, so a
  long-running `live` writer and another process (backfill, verify, or a consumer reading
  history) can block each other or fail with "database is locked". Enabling WAL mode would
  fix this and is a small change; it has not been made because it changes how the database
  file behaves. Decide before `backtesting` starts reading while `live` runs.
- Stopping `live` with SIGTERM (not Ctrl-C) just ends the process. That is safe, since the
  next start catches up.
- Spot candles only. Order book depth and funding rates are out of scope for now.
- Local checks that need the real exchange or dev tools (`ruff`, `mypy`, `pytest-cov`,
  the `network` contract test) were not run during the build; see `SPEC-market-data.md`.

## Development

```
uv sync
uv run pytest tests/market_data -m "not network" --cov=nomicronix.market_data --cov-fail-under=85
uv run pytest tests/market_data -m network        # hits the real exchange, run by hand
uv run ruff check src tests && uv run mypy src/nomicronix/market_data
```

Tests use `tests/market_data/fakes.py`: a scriptable fake exchange (failures, duplicate rows,
late-published candles, a fake clock) so nothing touches the network.
