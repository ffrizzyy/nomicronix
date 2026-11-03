# Tasks: signal-delivery

Plan: `tasks/plan-signal-delivery.md` · Spec: `SPEC-signal-delivery.md`

## Phase 1: models and rendering

## Task 1: domain models and rendering

**Description:** Add `Notification`, `PaperTrade`, `DeliveryRecord`,
`DeliveryStatus`, errors, and deterministic text/JSON rendering.

**Acceptance criteria:**
- [x] Notification fields match the spec, with `Decimal` prices
- [x] Rendered text includes signal id, levels, size, reasons and strategy
- [x] JSON is sorted-key and byte-stable
- [x] No wall clock or absolute path appears in a notification

**Verification:** `uv run pytest tests/signal_delivery/test_render.py`

**Dependencies:** strategy-engine, risk-engine

**Files:** `src/nomicronix/signal_delivery/models.py`,
`src/nomicronix/signal_delivery/render.py`, `tests/signal_delivery/fakes.py`,
`tests/signal_delivery/test_render.py`

## Task 2: outbox and paper-trade store

**Description:** Add the SQLite outbox and paper-trade ledger with idempotent
signal ids and status transitions.

**Acceptance criteria:**
- [x] `deliveries` and `paper_trades` survive reopen
- [x] A duplicate signal id updates its row, never duplicates it
- [x] Open, close and query helpers round-trip `Decimal` exactly
- [x] Corrupt rows surface as `StoreError`

**Verification:** `uv run pytest tests/signal_delivery/test_store.py`

**Dependencies:** Task 1

**Files:** `src/nomicronix/signal_delivery/store.py`,
`tests/signal_delivery/test_store.py`

## Phase 2: sinks

## Task 3: console, JSONL and Telegram sinks

**Description:** Add offline console/JSONL sinks and a Telegram sink with an
injected transport and environment credentials.

**Acceptance criteria:**
- [x] Console output is deterministic
- [x] JSONL appends one sorted JSON line per notification
- [x] Telegram without credentials fails before sending
- [x] A fake transport captures exactly the rendered text

**Verification:** `uv run pytest tests/signal_delivery/test_sinks.py`

**Dependencies:** Task 1

**Files:** `src/nomicronix/signal_delivery/sinks.py`,
`tests/signal_delivery/test_sinks.py`

## Phase 3: delivery service

## Task 4: signal loading and risk-gated delivery

**Description:** Load a strategy-engine JSON file, risk-check at candle close,
open a paper trade on approval and persist the outbox.

**Acceptance criteria:**
- [x] Rejected signals get an outbox row but no trade
- [x] Approved signals open exactly one full paper position
- [x] Re-running the same file does not duplicate delivery or trades
- [x] A pending/failed row resends without re-checking risk

**Verification:** `uv run pytest tests/signal_delivery/test_service.py`

**Dependencies:** Tasks 2, 3

**Files:** `src/nomicronix/signal_delivery/service.py`,
`src/nomicronix/signal_delivery/signals.py`,
`tests/signal_delivery/test_service.py`

## Task 5: paper ticks and reporting

**Description:** Close open trades at stop or target, mark the risk account and
report delivery/paper totals.

**Acceptance criteria:**
- [x] Stops are checked before targets
- [x] A gap below the stop closes at the worse price
- [x] Closes call `RiskService.record_fill` with the full quantity
- [x] The report totals match the ledger

**Verification:** `uv run pytest tests/signal_delivery/test_service.py tests/signal_delivery/test_report.py`

**Dependencies:** Task 4

**Files:** `src/nomicronix/signal_delivery/service.py`,
`src/nomicronix/signal_delivery/report.py`,
`tests/signal_delivery/test_report.py`

## Phase 4: CLI and repository integration

## Task 6: CLI and configuration

**Description:** Add `deliver`, `tick` and `report`, TOML defaults and exit
codes.

**Acceptance criteria:**
- [x] `deliver` exits `0` for an all-rejected file
- [x] A sink failure exits `2` and leaves the row retryable
- [x] `tick` accepts a file or repeated `--price`
- [x] Flags override `config/delivery.toml`

**Verification:** `uv run pytest tests/signal_delivery/test_cli.py`

**Dependencies:** Tasks 4, 5

**Files:** `src/nomicronix/signal_delivery/__main__.py`,
`src/nomicronix/signal_delivery/config.py`, `config/delivery.toml`,
`tests/signal_delivery/test_cli.py`

## Task 7: docs, examples and status

**Description:** Add the module guide, an example script, README/SPEC updates
and run the full quality sweep.

**Acceptance criteria:**
- [x] `docs/signal-delivery.md` documents the outbox, replay and paper rules
- [x] `examples/deliver_signals.py` shows the library path
- [x] README lists the module as built
- [x] Full suite, ruff, mypy and 90% module coverage pass

**Verification:** `uv run pytest -m "not network"`, `uv run ruff check src tests
examples`, `uv run mypy src/nomicronix`

**Dependencies:** Tasks 1-6

**Files:** `README.md`, `docs/signal-delivery.md`, `examples/`, `config/`

## Task 8: execution promotion gate

**Description:** Make the project-wide rule that `execution` waits for a credible
paper record measurable: add readiness criteria, a scorer, a `readiness` command
and TOML defaults.

**Acceptance criteria:**
- [x] Six criteria: closed trades, track-record days, expectancy after costs,
      paper drawdown, delivery health and ledger integrity
- [x] `readiness` exits `0` only when every criterion passes and `2` otherwise
- [x] `--json` emits a stable machine-readable report
- [x] Thresholds live in `[promotion]` and are validated
- [x] Docs and specs state that the gate authorizes building, not trading

**Verification:** `uv run pytest tests/signal_delivery/test_readiness.py
tests/signal_delivery/test_config.py tests/signal_delivery/test_cli.py`

**Dependencies:** Tasks 1-7

**Files:** `src/nomicronix/signal_delivery/readiness.py`,
`src/nomicronix/signal_delivery/config.py`, `src/nomicronix/signal_delivery/__main__.py`,
`config/delivery.toml`, `tests/signal_delivery/test_readiness.py`
