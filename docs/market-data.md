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
`--api-url` overrides Binance's spot public base URL; see
[If api.binance.com is unreachable](#if-apibinancecom-is-unreachable).

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

### If api.binance.com is unreachable

Some networks block `api.binance.com` while Binance's official spot market-data
mirror stays reachable. The mirror serves the same `/api/v3` endpoints and
payloads; it just cannot place orders, which this module never does:

```
uv run python -m nomicronix.market_data backfill \
    --symbols BTC/USDT --timeframes 1h --since 2025-11-25 \
    --api-url https://data-api.binance.vision/api/v3
```

`--api-url` is binance-only and keeps ccxt on spot markets (the mirror has no
futures or options endpoints). Everything else, including `verify --refill` and
`live`, behaves the same. To run the network contract test the same way:

```
$env:NOMICRONIX_BINANCE_PUBLIC_URL = "https://data-api.binance.vision/api/v3"   # PowerShell
NOMICRONIX_BINANCE_PUBLIC_URL=https://data-api.binance.vision/api/v3 uv run pytest tests/market_data -m network
```

## Real-exchange verification

Run on 2026-11-25 against live Binance data through the official spot mirror
(`--api-url https://data-api.binance.vision/api/v3`), because `api.binance.com`
is blocked on that host:

| Check | Result |
|---|---|
| `pytest tests/market_data -m network` | 1 passed: 24 well-formed `1h` candles from 2025-01-01 |
| `backfill --symbols BTC/USDT --timeframes 1h --since 2025-11-25` | 8,775 candles written, 2025-11-25T00:00Z to 2026-11-25T14:00Z |
| `verify --since 2025-11-25` | no gaps, exit `0` |
| `backfill` run again | 0 candles written, exit `0` |
| 5-year backfill killed after 4s at 11,000 rows, then re-run | 32,838 more written, 43,838 total; no re-download |
| `verify --since 2021-11-25 --refill` on that 5-year series | 1 gap, `UNFILLABLE`: 2023-03-24T13:00Z |
| `live --timeframes 15m` | 2 candles stored: 15:00Z (`1636.28`) and 15:15Z (`1633.19`) |

The 2023-03-24T13:00Z hole is real: Binance's own `/api/v3/klines` returns
`12:00`, then `14:00`. `verify` reports it and `--refill` marks it unfillable
instead of hiding it, which is the intended behaviour.

## Known limitations

- **Use one process per database file.** SQLite is in its default journal mode, so a
  long-running `live` writer and another process (backfill, verify, or a consumer reading
  history) can block each other or fail with "database is locked". Enabling WAL mode would
  fix this and is a small change; it has not been made because it changes how the database
  file behaves. Decide before `backtesting` starts reading while `live` runs.
- Stopping `live` with SIGTERM (not Ctrl-C) just ends the process. That is safe, since the
  next start catches up.
- Spot candles only. Order book depth and funding rates are out of scope for now.
- The contract test, a 12-month backfill, gap verification, an idempotent re-run and a
  real `15m` live close were checked on 2026-11-25 (see above). A multi-day soak, a real
  reconnect window and an interruption mid-backfill are still only proven against the
  fake exchange.

## Development

```
uv sync
uv run pytest tests/market_data -m "not network" --cov=nomicronix.market_data --cov-fail-under=85
uv run pytest tests/market_data -m network        # hits the real exchange, run by hand
uv run ruff check src tests && uv run mypy src/nomicronix/market_data
```

Tests use `tests/market_data/fakes.py`: a scriptable fake exchange (failures, duplicate rows,
late-published candles, a fake clock) so nothing touches the network.
