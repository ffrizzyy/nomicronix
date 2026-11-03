# Tasks: strategy-engine

Plan: `tasks/plan-strategy-engine.md` · Spec: `SPEC-strategy-engine.md`

## Phase 1: models and indicators

## Task 1: domain models and validation

**Description:** Add `Side`, `SignalIdea`, `SignalCandidate`, `ScanConfig`,
`SignalBatch`, `ProposalError`, and the price/confidence validation rules.

**Acceptance criteria:**
- [x] Stops below entry and targets above entry are enforced
- [x] Confidence outside `[0, 1]` and empty rationale are rejected
- [x] `created_at_ms` is the candle close, not the wall clock
- [x] Money fields use `Decimal`; confidence is `float`

**Verification:** `uv run pytest tests/strategy_engine/test_models.py`

**Dependencies:** market-data

**Files:** `src/nomicronix/strategy_engine/models.py`,
`src/nomicronix/strategy_engine/__init__.py`, `tests/strategy_engine/fakes.py`,
`tests/strategy_engine/test_models.py`

## Task 2: causal indicators

**Description:** Implement simple moving average, Wilder ATR and Donchian
channels over closed candles only.

**Acceptance criteria:**
- [x] Hand-computed SMA and ATR fixtures match exactly
- [x] ATR uses Wilder smoothing after the seed window
- [x] Channel helpers exclude the decision candle
- [x] Insufficient history returns `None`, not a partial value

**Verification:** `uv run pytest tests/strategy_engine/test_indicators.py`

**Dependencies:** Task 1

**Files:** `src/nomicronix/strategy_engine/indicators.py`,
`tests/strategy_engine/test_indicators.py`

## Phase 2: proposers and engine

## Task 3: built-in proposers

**Description:** Add `SmaCross` and `DonchianBreakout` with ATR stops, fixed
reward/risk targets and fresh-event detection.

**Acceptance criteria:**
- [x] SMA fires only on a cross, not while the spread persists
- [x] Donchian fires only on a fresh breakout
- [x] Zero ATR or bad geometry returns `None`
- [x] Invalid constructor params fail loudly

**Verification:** `uv run pytest tests/strategy_engine/test_proposers.py`

**Dependencies:** Tasks 1, 2

**Files:** `src/nomicronix/strategy_engine/proposers.py`,
`tests/strategy_engine/test_proposers.py`

## Task 4: causal scan engine

**Description:** Add `CandleView`, `scan_candles`, validation, warnings,
cooldown-free fresh signals and the max-signal cap.

**Acceptance criteria:**
- [x] `CandleView` never exposes a later candle (spy test)
- [x] Signals are emitted only inside `[start_ms, end_ms)`
- [x] Low-confidence and invalid ideas appear in `warnings`
- [x] Exceeding `max_signals` is an error, not truncation
- [x] Repeated scans are identical

**Verification:** `uv run pytest tests/strategy_engine/test_engine.py`

**Dependencies:** Tasks 1, 3

**Files:** `src/nomicronix/strategy_engine/engine.py`,
`tests/strategy_engine/test_engine.py`

## Task 5: AI extension point

**Description:** Define `AIProposer`, `ProposalRequest` and `AIProposal`, with
the same validation used for rule ideas.

**Acceptance criteria:**
- [x] AI proposals require entry, stop, target, confidence and rationale
- [x] Invalid geometry is rejected with `ProposalError`
- [x] No network call or provider is included in v1
- [x] The boundary is documented for a future adapter

**Verification:** `uv run pytest tests/strategy_engine/test_ai.py`

**Dependencies:** Task 1

**Files:** `src/nomicronix/strategy_engine/ai.py`,
`tests/strategy_engine/test_ai.py`

## Phase 3: data, results and CLI

## Task 6: strict data loading

**Description:** Load a gap-free range plus warm-up from `MarketDataStore`.

**Acceptance criteria:**
- [x] A gap raises `DataGapError` with the missing ranges
- [x] Too few candles raises `InsufficientDataError`
- [x] Unsorted or duplicate candles are rejected
- [x] Warm-up candles are visible but never traded

**Verification:** `uv run pytest tests/strategy_engine/test_data.py`

**Dependencies:** Task 4

**Files:** `src/nomicronix/strategy_engine/data.py`,
`tests/strategy_engine/test_data.py`

## Task 7: deterministic results and CLI

**Description:** Serialize a `SignalBatch` deterministically, write atomically,
and add the `scan` command with TOML defaults.

**Acceptance criteria:**
- [x] Repeated runs produce byte-identical JSON
- [x] Existing output is not overwritten without `--force`
- [x] Exit codes match the spec
- [x] A zero-signal scan exits `0`

**Verification:** `uv run pytest tests/strategy_engine/test_results.py tests/strategy_engine/test_cli.py`

**Dependencies:** Tasks 4, 6

**Files:** `src/nomicronix/strategy_engine/results.py`,
`src/nomicronix/strategy_engine/__main__.py`,
`src/nomicronix/strategy_engine/config.py`, `config/strategy.toml`,
`tests/strategy_engine/test_results.py`, `tests/strategy_engine/test_cli.py`

## Phase 4: repository integration

## Task 8: docs, examples and status

**Description:** Add the module guide, example proposer, README/SPEC updates and
run the full quality sweep.

**Acceptance criteria:**
- [x] `docs/strategy-engine.md` documents the contract and limitations
- [x] `examples/strategy_models.py` shows a custom proposer
- [x] README lists the module as built
- [x] Full suite, ruff, mypy and 90% module coverage pass

**Verification:** `uv run pytest -m "not network"`, `uv run ruff check src tests
examples`, `uv run mypy src/nomicronix`

**Dependencies:** Tasks 1-7

**Files:** `README.md`, `docs/strategy-engine.md`, `examples/`,
`config/strategy.toml`
