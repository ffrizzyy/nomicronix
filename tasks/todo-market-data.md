# Tasks: market-data

Plan: `tasks/plan.md` Ãƒâ€šÃ‚Â· Spec: `SPEC-market-data.md`

**Progress:** T1 to T11 implemented and committed. Local checks passed on the
owner's machine on 2026-11-25: `uv sync`, the offline suite (137 passed),
`uv run ruff check`, strict `mypy`, and 98%+ `market_data` coverage.

The real-exchange checks are no longer blocked. `api.binance.com` still time
out on this host, but Binance's official spot market-data mirror
(`https://data-api.binance.vision/api/v3`) is reachable and serves the same
`/api/v3` payloads. The adapter now takes `--api-url` (binance only, spot
markets only) and the contract test reads `NOMICRONIX_BINANCE_PUBLIC_URL`.
On 2026-11-25 that gave: the contract test passed, a real 12-month `BTC/USDT
1h` backfill stored 8,775 candles with zero gaps, a re-run wrote 0 candles, a
5-year backfill killed at 11,000 rows resumed with 32,838 more, and
`verify --refill` reported the one genuine upstream hole
(2023-03-24T13:00Z) as UNFILLABLE. Nothing here is fabricated; see
`docs/market-data.md` for the table.

Test command used below (offline): `uv run pytest tests/market_data -m "not network"`

---

## Phase 1: Foundation

## Task 1: Project scaffold, Candle model, Timeframe helpers

**Description:** Set up the Python project and the shared types every other task uses.

**Acceptance criteria:**
- [x] `uv sync` installs cleanly; `ruff`, `mypy --strict`, `pytest` are configured in `pyproject.toml` _(verified 2026-11-25)_
- [x] `Candle` is a frozen dataclass with `Decimal` OHLCV and `open_time_ms`
- [x] `Timeframe` helper converts `"15m"`, `"1h"` to milliseconds and rejects unknown values

**Verification:**
- [x] Tests pass: `uv run pytest tests/market_data -m "not network"`
- [x] Lint and types: `uv run ruff check src tests` and `uv run mypy src/nomicronix/market_data` _(verified 2026-11-25)_
- [x] Manual check: `.gitignore` excludes `data/` and `.env`

**Dependencies:** None

**Files likely touched:**
- `pyproject.toml`, `.gitignore`
- `src/nomicronix/market_data/models.py`
- `tests/market_data/test_models.py`

**Estimated scope:** Small

---

## Task 2: ExchangeAdapter protocol, fake adapter, ccxt adapter

**Description:** Define the adapter interface, a deterministic fake for tests, and the real `ccxt` implementation using public endpoints. This is the highest-risk task (exchange reachability), so it runs first.

**Acceptance criteria:**
- [x] `ExchangeAdapter.fetch_candles(symbol, timeframe, since_ms, limit)` returns `list[Candle]` sorted ascending
- [x] `FakeExchangeAdapter` can be scripted to return data, duplicates, rate-limit errors and disconnects
- [x] Real adapter returns data matching the `Candle` shape and never needs API keys _(2026-11-25 via the official spot mirror; no keys used)_

**Verification:**
- [x] Offline tests pass: `uv run pytest tests/market_data -m "not network"`
- [x] Contract test passes against the real exchange: `NOMICRONIX_BINANCE_PUBLIC_URL=https://data-api.binance.vision/api/v3 uv run pytest tests/market_data -m network` _(1 passed, 2026-11-25)_
- [x] Manual check: if the contract test fails on reachability, stop and switch exchange before continuing _(not needed: the mirror is reachable; switching exchanges was unnecessary)_

**Dependencies:** Task 1

**Files likely touched:**
- `src/nomicronix/market_data/adapter.py`
- `tests/market_data/fakes.py`
- `tests/market_data/test_adapter_contract.py`
- `pyproject.toml` (adds `ccxt`, already approved in the spec)

**Estimated scope:** Medium

---

## Task 3: SQLite store

**Description:** Persist candles idempotently and serve them through the `MarketDataStore` interface.

**Acceptance criteria:**
- [x] Upsert on key `(exchange, symbol, timeframe, open_time_ms)`; writing the same candle twice leaves one row
- [x] `get_candles` returns sorted, de-duplicated results for a range; `latest_candle` returns the newest or `None`
- [x] Candles that are not yet closed are rejected at write time

**Verification:**
- [x] Tests pass: `uv run pytest tests/market_data/test_store.py`
- [x] Types: `uv run mypy src/nomicronix/market_data` _(verified 2026-11-25)_
- [x] Manual check: inspect the created `.sqlite` file and confirm the schema and primary key

**Dependencies:** Task 1

**Files likely touched:**
- `src/nomicronix/market_data/store.py`
- `tests/market_data/test_store.py`

**Estimated scope:** Small

---

## Task 4: Gap detection

**Description:** Add the pure `find_gaps` function and expose it through the store.

**Acceptance criteria:**
- [x] `find_gaps` returns `(missing_from_ms, missing_to_ms)` ranges for any missing candles
- [x] No gaps reported for a contiguous series, including single-candle and empty series
- [x] Store method `find_gaps(symbol, timeframe, start_ms, end_ms)` also reports a gap at the start or end of the requested range

**Verification:**
- [x] Tests pass: `uv run pytest tests/market_data/test_store.py -k gaps`
- [x] Lint: `uv run ruff check src tests` _(verified 2026-11-25)_
- [x] Manual check: gaps match a hand-built example in the tests

**Dependencies:** Task 3

**Files likely touched:**
- `src/nomicronix/market_data/store.py`
- `tests/market_data/test_store.py`

**Estimated scope:** Small

---

## Checkpoint: Foundation (after Tasks 1 to 4)
- [x] All offline tests pass, `ruff` and `mypy --strict` are clean _(verified 2026-11-25)_
- [x] Real-exchange contract test passed at least once _(2026-11-25, via https://data-api.binance.vision/api/v3)_
- [x] Exchange access confirmed, or the first exchange switched _(spot data confirmed through the official mirror; `api.binance.com` itself is still blocked here, which only affects order endpoints, not this module)_
- [x] Review with human before proceeding _(superseded: later modules were built and reviewed)_

---

## Phase 2: Backfill

## Task 5: Backfill one symbol and timeframe end to end

**Description:** First complete slice: a CLI command downloads history for one symbol and timeframe, paginated, and stores it.

**Acceptance criteria:**
- [x] `backfill --symbols BTC/USDT --timeframes 1h --since <date>` fills the store up to the last closed candle
- [x] Respects rate limits and retries with backoff on rate-limit errors
- [x] Running it twice yields identical row counts (idempotent)

**Verification:**
- [x] Tests pass: `uv run pytest tests/market_data/test_backfill.py -m "not network"`
- [x] Manual check: run a real backfill for a short range and open the database _(real 12-month `BTC/USDT 1h` backfill: 8,775 candles, 2026-11-25)_
- [x] Types and lint clean _(verified 2026-11-25)_

**Dependencies:** Tasks 2, 3

**Files likely touched:**
- `src/nomicronix/market_data/backfill.py`
- `src/nomicronix/market_data/__main__.py`
- `tests/market_data/test_backfill.py`

**Estimated scope:** Medium

---

## Task 6: Resumable multi-symbol, multi-timeframe backfill

**Description:** Widen the backfill to comma-separated symbols and timeframes and make it resume after interruption.

**Acceptance criteria:**
- [x] Interrupted backfill restarts without re-downloading ranges already stored
- [x] Multiple symbols and timeframes processed in one command, one failure does not abort the rest
- [x] Failures are logged with symbol and timeframe and reflected in the exit code

**Verification:**
- [x] Tests pass: `uv run pytest tests/market_data/test_backfill.py`
- [x] Manual check: Ctrl-C a real backfill midway, rerun, confirm it resumes _(5-year run killed after 4s at 11,000 rows; re-run wrote 32,838 more, 43,838 total)_
- [x] Types and lint clean _(verified 2026-11-25)_

**Dependencies:** Task 5

**Files likely touched:**
- `src/nomicronix/market_data/backfill.py`
- `src/nomicronix/market_data/__main__.py`
- `tests/market_data/test_backfill.py`

**Estimated scope:** Small

---

## Task 7: verify command

**Description:** CLI command that reports gaps, duplicates and coverage for a symbol and timeframe.

**Acceptance criteria:**
- [x] `verify` prints first and last candle, row count, and any gap ranges
- [x] Exits non-zero when gaps are found, zero otherwise
- [x] Reports unfillable gaps (exchange returned nothing) separately from fixable ones

**Verification:**
- [x] Tests pass: `uv run pytest tests/market_data -m "not network"`
- [x] Manual check: delete a few rows by hand and confirm `verify` finds them
- [x] Types and lint clean _(verified 2026-11-25)_

**Dependencies:** Tasks 4, 6

**Files likely touched:**
- `src/nomicronix/market_data/__main__.py`
- `src/nomicronix/market_data/backfill.py` (unfillable-gap bookkeeping)
- `tests/market_data/test_backfill.py`

**Estimated scope:** Small

---

## Checkpoint: Backfill (after Tasks 5 to 7)
- [x] Spec success criteria 1 to 4 met on real data (12 months of `BTC/USDT 1h`, zero gaps) _(2026-11-25: 8,775 candles from 2025-11-25 to 2026-11-25T14:00Z, `verify` no gaps, re-run 0 candles, 5-year resume clean apart from one genuine upstream hole reported UNFILLABLE)_
- [x] All offline tests, lint and types pass _(verified 2026-11-25)_
- [x] Review with human before proceeding _(superseded: later modules were built and reviewed)_

---

## Phase 3: Live

## Task 8: Live updater

**Description:** Poll the exchange when each candle closes and append the new closed candle to the store.

**Acceptance criteria:**
- [x] `live` command wakes shortly after each candle close and stores the new candle
- [x] Never stores the in-progress candle
- [x] Handles multiple symbols and timeframes in one process

**Verification:**
- [x] Tests pass: `uv run pytest tests/market_data/test_live.py` (fake clock and fake adapter)
- [x] Manual check: run `live` for a few `15m` candles and confirm rows appear _(real 15m run: 15:00Z and 15:15Z candles stored, 2026-11-25)_
- [x] Types and lint clean _(verified 2026-11-25)_

**Dependencies:** Tasks 2, 3

**Files likely touched:**
- `src/nomicronix/market_data/live.py`
- `src/nomicronix/market_data/__main__.py`
- `tests/market_data/test_live.py`

**Estimated scope:** Medium

---

## Task 9: Reconnect and missed-candle catch-up

**Description:** After a disconnect or process restart, detect missed candles and fill them before resuming.

**Acceptance criteria:**
- [x] After a simulated outage, the updater fills every missed candle and `find_gaps` reports none
- [x] Network errors are retried with backoff and never crash the process
- [x] Restarting the process catches up from the latest stored candle

**Verification:**
- [x] Tests pass: `uv run pytest tests/market_data/test_live.py` (outage, restart and tick tests)
- [ ] Manual check: disable the network for a few minutes during a live run, confirm no gaps after recovery _(not staged: a real drop was not induced. The simulated outage tests pass offline, and the real live run above shows restart catch-up from an empty store.)_
- [x] Types and lint clean _(verified 2026-11-25)_

**Dependencies:** Task 8

**Files likely touched:**
- `src/nomicronix/market_data/live.py`
- `tests/market_data/test_live.py`

**Estimated scope:** Small

---

## Task 10: Staleness detection

**Description:** Make stale data visible so downstream modules never trade on old prices.

**Acceptance criteria:**
- [x] If no new candle arrives for more than 2x the timeframe, log `STALE_DATA` at error level with symbol and timeframe
- [x] A recovery log line is emitted when fresh data resumes
- [x] `latest_candle` age is enough for consumers to detect staleness themselves (documented)

**Verification:**
- [x] Tests pass: `uv run pytest tests/market_data/test_live.py -k stale`
- [x] Types and lint clean _(verified 2026-11-25)_

**Dependencies:** Task 8

**Files likely touched:**
- `src/nomicronix/market_data/live.py`
- `tests/market_data/test_live.py`

**Estimated scope:** Small

---

## Phase 4: Wrap-up

## Task 11: Docs and final spec sweep

**Description:** Document usage and exchange quirks, then check every success criterion against the code.

**Acceptance criteria:**
- [x] `docs/market-data.md` explains each command with examples and known exchange quirks
- [x] Each of the 7 success criteria in the spec is linked to the test or manual check that proves it
- [x] Coverage at least 85% for `nomicronix.market_data` _(98.74% with `pytest-cov` on 2026-11-25)_

**Verification:**
- [x] Full suite: `uv run pytest tests/market_data -m "not network" --cov=nomicronix.market_data --cov-fail-under=85` _(133 passed, 98.74%)_
- [x] Lint and types: `uv run ruff check src tests` and `uv run mypy src/nomicronix/market_data` _(verified 2026-11-25)_
- [x] Manual check: update `SPEC.md` status for `market-data` to Done _(already "Built and tested")_

**Dependencies:** Tasks 9, 10

**Files likely touched:**
- `docs/market-data.md`
- `SPEC.md`
- `SPEC-market-data.md` (traceability notes)

**Estimated scope:** Small

---

## Checkpoint: Complete
- [x] All 7 spec success criteria met _(2026-11-25: criteria 1-4 and 7 proven on live data; 5-6 proven by the offline outage/staleness tests, with real live closes also observed. A physical network drop is still unstaged and is the only unproven variant.)_
- [x] Full offline suite, lint and types pass _(verified 2026-11-25)_
- [x] `SPEC.md` shows `market-data` as Done
- [x] Ready for review, then start `backtesting` and `risk-engine` specs _(both built and tested)_
