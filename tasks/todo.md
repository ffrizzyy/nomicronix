# Tasks: market-data

Plan: `tasks/plan.md` · Spec: `SPEC-market-data.md`

**Progress:** T1 to T4 implemented and committed. Foundation checkpoint is waiting on your review.
Items marked _not verified_ need `uv sync`, `ruff`, `mypy` or the real exchange, none of which
were available in the build sandbox (no network). Please run them locally.

Test command used below (offline): `uv run pytest tests/market_data -m "not network"`

---

## Phase 1: Foundation

## Task 1: Project scaffold, Candle model, Timeframe helpers

**Description:** Set up the Python project and the shared types every other task uses.

**Acceptance criteria:**
- [ ] `uv sync` installs cleanly; `ruff`, `mypy --strict`, `pytest` are configured in `pyproject.toml` _(not verified in sandbox: needs network or dev tools; run on your machine)_
- [x] `Candle` is a frozen dataclass with `Decimal` OHLCV and `open_time_ms`
- [x] `Timeframe` helper converts `"15m"`, `"1h"` to milliseconds and rejects unknown values

**Verification:**
- [x] Tests pass: `uv run pytest tests/market_data -m "not network"`
- [ ] Lint and types: `uv run ruff check src tests` and `uv run mypy src/nomicronix/market_data` _(not verified in sandbox: needs network or dev tools; run on your machine)_
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
- [ ] Real adapter returns data matching the `Candle` shape and never needs API keys _(not verified in sandbox: needs network or dev tools; run on your machine)_

**Verification:**
- [x] Offline tests pass: `uv run pytest tests/market_data -m "not network"`
- [ ] Contract test passes against the real exchange: `uv run pytest tests/market_data -m network`
- [ ] Manual check: if the contract test fails on reachability, stop and switch exchange before continuing _(not verified in sandbox: needs network or dev tools; run on your machine)_

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
- [ ] Types: `uv run mypy src/nomicronix/market_data` _(not verified in sandbox: needs network or dev tools; run on your machine)_
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
- [ ] Lint: `uv run ruff check src tests` _(not verified in sandbox: needs network or dev tools; run on your machine)_
- [x] Manual check: gaps match a hand-built example in the tests

**Dependencies:** Task 3

**Files likely touched:**
- `src/nomicronix/market_data/store.py`
- `tests/market_data/test_store.py`

**Estimated scope:** Small

---

## Checkpoint: Foundation (after Tasks 1 to 4)
- [ ] All offline tests pass, `ruff` and `mypy --strict` are clean
- [ ] Real-exchange contract test passed at least once
- [ ] Exchange access confirmed, or the first exchange switched
- [ ] Review with human before proceeding

---

## Phase 2: Backfill

## Task 5: Backfill one symbol and timeframe end to end

**Description:** First complete slice: a CLI command downloads history for one symbol and timeframe, paginated, and stores it.

**Acceptance criteria:**
- [ ] `backfill --symbols BTC/USDT --timeframes 1h --since <date>` fills the store up to the last closed candle
- [ ] Respects rate limits and retries with backoff on rate-limit errors
- [ ] Running it twice yields identical row counts (idempotent)

**Verification:**
- [ ] Tests pass: `uv run pytest tests/market_data/test_backfill.py -m "not network"`
- [ ] Manual check: run a real backfill for a short range and open the database
- [ ] Types and lint clean

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
- [ ] Interrupted backfill restarts without re-downloading ranges already stored
- [ ] Multiple symbols and timeframes processed in one command, one failure does not abort the rest
- [ ] Failures are logged with symbol and timeframe and reflected in the exit code

**Verification:**
- [ ] Tests pass: `uv run pytest tests/market_data/test_backfill.py`
- [ ] Manual check: Ctrl-C a real backfill midway, rerun, confirm it resumes
- [ ] Types and lint clean

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
- [ ] `verify` prints first and last candle, row count, and any gap ranges
- [ ] Exits non-zero when gaps are found, zero otherwise
- [ ] Reports unfillable gaps (exchange returned nothing) separately from fixable ones

**Verification:**
- [ ] Tests pass: `uv run pytest tests/market_data -m "not network"`
- [ ] Manual check: delete a few rows by hand and confirm `verify` finds them
- [ ] Types and lint clean

**Dependencies:** Tasks 4, 6

**Files likely touched:**
- `src/nomicronix/market_data/__main__.py`
- `src/nomicronix/market_data/backfill.py` (unfillable-gap bookkeeping)
- `tests/market_data/test_backfill.py`

**Estimated scope:** Small

---

## Checkpoint: Backfill (after Tasks 5 to 7)
- [ ] Spec success criteria 1 to 4 met on real data (12 months of `BTC/USDT 1h`, zero gaps)
- [ ] All offline tests, lint and types pass
- [ ] Review with human before proceeding

---

## Phase 3: Live

## Task 8: Live updater

**Description:** Poll the exchange when each candle closes and append the new closed candle to the store.

**Acceptance criteria:**
- [ ] `live` command wakes shortly after each candle close and stores the new candle
- [ ] Never stores the in-progress candle
- [ ] Handles multiple symbols and timeframes in one process

**Verification:**
- [ ] Tests pass: `uv run pytest tests/market_data/test_live.py` (fake clock and fake adapter)
- [ ] Manual check: run `live` for a few `15m` candles and confirm rows appear
- [ ] Types and lint clean

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
- [ ] After a simulated outage, the updater fills every missed candle and `find_gaps` reports none
- [ ] Network errors are retried with backoff and never crash the process
- [ ] Restarting the process catches up from the latest stored candle

**Verification:**
- [ ] Tests pass: `uv run pytest tests/market_data/test_live.py -k reconnect`
- [ ] Manual check: disable the network for a few minutes during a live run, confirm no gaps after recovery
- [ ] Types and lint clean

**Dependencies:** Task 8

**Files likely touched:**
- `src/nomicronix/market_data/live.py`
- `tests/market_data/test_live.py`

**Estimated scope:** Small

---

## Task 10: Staleness detection

**Description:** Make stale data visible so downstream modules never trade on old prices.

**Acceptance criteria:**
- [ ] If no new candle arrives for more than 2x the timeframe, log `STALE_DATA` at error level with symbol and timeframe
- [ ] A recovery log line is emitted when fresh data resumes
- [ ] `latest_candle` age is enough for consumers to detect staleness themselves (documented)

**Verification:**
- [ ] Tests pass: `uv run pytest tests/market_data/test_live.py -k stale`
- [ ] Types and lint clean

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
- [ ] `docs/market-data.md` explains each command with examples and known exchange quirks
- [ ] Each of the 7 success criteria in the spec is linked to the test or manual check that proves it
- [ ] Coverage at least 85% for `nomicronix.market_data`

**Verification:**
- [ ] Full suite: `uv run pytest tests/market_data -m "not network" --cov=nomicronix.market_data --cov-fail-under=85`
- [ ] Lint and types: `uv run ruff check src tests` and `uv run mypy src/nomicronix/market_data`
- [ ] Manual check: update `SPEC.md` status for `market-data` to Done

**Dependencies:** Tasks 9, 10

**Files likely touched:**
- `docs/market-data.md`
- `SPEC.md`
- `SPEC-market-data.md` (traceability notes)

**Estimated scope:** Small

---

## Checkpoint: Complete
- [ ] All 7 spec success criteria met
- [ ] Full offline suite, lint and types pass
- [ ] `SPEC.md` shows `market-data` as Done
- [ ] Ready for review, then start `backtesting` and `risk-engine` specs
