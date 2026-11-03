# Tasks: risk-engine and backtesting

Plan: `tasks/plan.md` · Specs: `SPEC-risk-engine.md`, `SPEC-backtesting.md`

## Phase 1: risk-engine core

## Task 1: risk-engine models and limits validation

**Description:** Add the typed domain model: side, instrument, proposal, position,
snapshot, limits, decision, status, kill switch, reject reasons.

**Acceptance criteria:**
- [ ] `RiskLimits` validates percentages, counts and non-empty symbol allowlist
- [ ] `RiskSnapshot` exposes drawdown, daily loss, exposure and open risk
- [ ] All money fields use `Decimal`; all times are `*_ms`

**Verification:** `uv run pytest tests/risk_engine/test_models.py`

**Dependencies:** None

**Files:** `src/nomicronix/risk_engine/models.py`, `src/nomicronix/risk_engine/__init__.py`,
`tests/risk_engine/test_models.py`

## Task 2: deterministic position sizing

**Description:** Implement risk-based sizing with the per-position notional cap and
step rounding, plus the aggregate exposure/open-risk helpers.

**Acceptance criteria:**
- [ ] 1,000 equity / 100 entry / 95 stop approves 1 unit at 5 USDT risk
- [ ] Step rounding always rounds down and never risks more than the limit
- [ ] The 25% notional cap reduces size without rejecting when still above minimum
- [ ] Size below min quantity/notional is a rejection reason

**Verification:** `uv run pytest tests/risk_engine/test_sizing.py`

**Dependencies:** Task 1

**Files:** `src/nomicronix/risk_engine/sizing.py`, `tests/risk_engine/test_sizing.py`

## Task 3: pure rule engine

**Description:** Implement `evaluate` with the stable rule order and one test per
rejection reason.

**Acceptance criteria:**
- [ ] Kill switch, stale signal/state, symbol, instrument, invalid proposal,
      duplicate, max positions, daily loss, drawdown, reward/risk, sizing,
      exposure and open risk are all enforced
- [ ] All applicable reasons are collected in a stable order
- [ ] Drawdown rejection also signals that the kill switch must engage
- [ ] Identical inputs produce identical decisions

**Verification:** `uv run pytest tests/risk_engine/test_engine.py`

**Dependencies:** Tasks 1, 2

**Files:** `src/nomicronix/risk_engine/engine.py`, `tests/risk_engine/fakes.py`,
`tests/risk_engine/test_engine.py`

## Task 4: portfolio state transitions

**Description:** Implement mutable state: mark-to-market, peak tracking, UTC day
rollover, open/close fills, drawdown auto-kill and manual resume.

**Acceptance criteria:**
- [ ] Marking updates equity, peak and last mark time
- [ ] A 10% drawdown engages the kill switch exactly once
- [ ] A UTC day rollover resets the daily loss budget but not the kill switch
- [ ] Open/close fills update cash, positions and realized P&L correctly
- [ ] `resume` requires the exact confirmation phrase

**Verification:** `uv run pytest tests/risk_engine/test_state.py`

**Dependencies:** Tasks 1, 2

**Files:** `src/nomicronix/risk_engine/state.py`, `tests/risk_engine/test_state.py`

## Phase 2: risk-engine persistence and CLI

## Task 5: SQLite store and atomic service

**Description:** Persist state, positions, approvals and the decision journal; make
`RiskService` evaluate and persist in one transaction.

**Acceptance criteria:**
- [ ] Restart preserves cash, positions, peak, day state and kill switch
- [ ] Every check writes one decision row, approved or rejected
- [ ] Approvals are single-use; unknown/duplicate fills are rejected
- [ ] A drawdown check persists the kill switch and decision atomically

**Verification:** `uv run pytest tests/risk_engine/test_store.py`

**Dependencies:** Tasks 3, 4

**Files:** `src/nomicronix/risk_engine/store.py`, `src/nomicronix/risk_engine/service.py`,
`tests/risk_engine/test_store.py`

## Task 6: TOML configuration

**Description:** Load and validate `config/risk.toml` with the conservative defaults.

**Acceptance criteria:**
- [ ] Missing file falls back to defaults
- [ ] Invalid percentages, counts and empty allowlists fail with actionable errors
- [ ] Decimal strings stay exact

**Verification:** `uv run pytest tests/risk_engine/test_config.py`

**Dependencies:** Task 1

**Files:** `config/risk.toml`, `src/nomicronix/risk_engine/config.py`,
`tests/risk_engine/test_config.py`

## Task 7: risk-engine CLI

**Description:** Add `status`, `check`, `mark`, `kill` and `resume` commands.

**Acceptance criteria:**
- [ ] `check` exits 0 on approval and 2 on rejection
- [ ] Proposal JSON includes instrument rules
- [ ] `kill` and `resume` persist across invocations
- [ ] Output is deterministic JSON

**Verification:** `uv run pytest tests/risk_engine/test_cli.py`

**Dependencies:** Tasks 5, 6

**Files:** `src/nomicronix/risk_engine/__main__.py`, `tests/risk_engine/test_cli.py`

## Phase 3: backtesting core

## Task 8: backtesting models, strategy protocol and data loader

**Description:** Add result/config/trade models, the causal strategy protocol and
strict data loading from `MarketDataStore`.

**Acceptance criteria:**
- [ ] Gap in the requested range raises `DataGapError` with exact ranges
- [ ] Fewer than `min_candles` raises `InsufficientDataError`
- [ ] Warm-up candles are loaded before `start_ms` without trading before start

**Verification:** `uv run pytest tests/backtesting/test_data.py`

**Dependencies:** market-data

**Files:** `src/nomicronix/backtesting/models.py`, `strategy.py`, `data.py`,
`tests/backtesting/test_data.py`

## Task 9: simulated broker and engine loop

**Description:** Implement next-open entries/exits, intrabar stops/targets, fees,
slippage, fixed-fraction sizing and forced end-of-data close.

**Acceptance criteria:**
- [ ] Signals fill at the next open, never the current close
- [ ] Stop-first when both levels are touched; gap through stop fills at open
- [ ] Fees and slippage are charged on both sides
- [ ] `HistoryView` never exposes a later candle
- [ ] End-of-data close updates the final equity point

**Verification:** `uv run pytest tests/backtesting/test_broker.py tests/backtesting/test_engine.py`

**Dependencies:** Task 8

**Files:** `src/nomicronix/backtesting/sizing.py`, `broker.py`, `engine.py`,
`tests/backtesting/fakes.py`, `strategies.py`, `test_broker.py`, `test_engine.py`

## Task 10: metrics

**Description:** Compute return, CAGR, drawdown, Sharpe, Sortino, Calmar, win rate,
profit factor, expectancy, fees, turnover and exposure.

**Acceptance criteria:**
- [ ] Hand-computed fixture matches within `1e-9`
- [ ] Zero variance and no-loss cases return `None`, not NaN/Infinity
- [ ] Max drawdown matches a manual equity curve

**Verification:** `uv run pytest tests/backtesting/test_metrics.py`

**Dependencies:** Task 9

**Files:** `src/nomicronix/backtesting/metrics.py`, `tests/backtesting/test_metrics.py`

## Phase 4: backtesting walk-forward and CLI

## Task 11: walk-forward validation

**Description:** Split rolling train/test windows, select parameters on train only,
run frozen parameters on test with warm-up, and stitch OOS equity.

**Acceptance criteria:**
- [ ] Windows never overlap
- [ ] Test parameters come only from the train window
- [ ] OOS stitching matches manual compounding
- [ ] Insufficient/partial windows are reported, not silently dropped

**Verification:** `uv run pytest tests/backtesting/test_walk_forward.py`

**Dependencies:** Tasks 9, 10

**Files:** `src/nomicronix/backtesting/walk_forward.py`, `tests/backtesting/test_walk_forward.py`

## Task 12: deterministic results and CLI

**Description:** Serialize result JSON deterministically, write atomically, guard
overwrites, and add `run` / `walk-forward` commands.

**Acceptance criteria:**
- [ ] Repeated identical runs produce byte-identical JSON
- [ ] Existing output is not overwritten without `--force`
- [ ] Parameters and grid values parse deterministically
- [ ] Exit codes match the spec

**Verification:** `uv run pytest tests/backtesting/test_results.py tests/backtesting/test_cli.py`

**Dependencies:** Tasks 10, 11

**Files:** `src/nomicronix/backtesting/results.py`, `__main__.py`,
`tests/backtesting/test_results.py`, `test_cli.py`

## Phase 5: repository completion

## Task 13: documentation, examples and CI

**Description:** Add a root README, module guides, example config/proposal, and a
GitHub Actions workflow for Python 3.12/3.13.

**Acceptance criteria:**
- [ ] README explains setup, commands and safety model
- [ ] `docs/risk-engine.md` and `docs/backtesting.md` document contracts and limits
- [ ] CI runs tests, coverage, ruff and mypy
- [ ] No secrets or local data committed

**Verification:** `uv run pytest -m "not network"`, `uv run ruff check src tests`,
`uv run mypy src/nomicronix`

**Dependencies:** Tasks 7, 12

**Files:** `README.md`, `docs/risk-engine.md`, `docs/backtesting.md`,
`examples/`, `.github/workflows/ci.yml`
