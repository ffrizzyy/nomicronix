# Tasks: execution

Plan: `tasks/plan-execution.md` · Spec: `SPEC-execution.md`

**Status: built.** Tasks 1-9 are complete. The owner approved stages 1 and 2
(not stage 3) on 2026-11-09. `submit` now enforces the readiness criteria itself
from the delivery ledger, but Task 0's operational checks remain open: the paper
record still fails readiness (exit `2`), and the trade-only key/IP restriction
cannot be verified without the owner's exchange account.

## Phase 0: gate and prerequisites

## Task 0: prove the promotion gate and prerequisites

**Description:** Run the readiness gate, archive its JSON, get the risk-engine
v1.1 partial-fill change approved, and confirm the exchange key is trade-only.

**Acceptance criteria:**
- [ ] `readiness` exits `0` and its JSON is archived (needs 30 closed paper
  trades over 28 days; cannot be fabricated)
- [x] The risk-engine v1.1 partial-fill contract is approved and implemented
- [ ] Withdrawals are disabled and the key is IP-restricted (owner/exchange-only)
- [x] The owner approves stages 1 and 2, not stage 3 (2026-11-09)
- [x] `submit` enforces readiness in-process via `require_readiness` and
  `--delivery-db`, failing closed if no checker is available

**Verification:** `uv run python -m nomicronix.signal_delivery readiness --json`

**Dependencies:** signal-delivery

**Files:** none (operational gate)

## Phase 1: models, journal and fake adapter

## Task 1: domain models and order journal

**Description:** Add `OrderIntent`, `ExchangeOrder`, `OrderEvent`,
`Reconciliation`, errors and a SQLite journal keyed by intent id.

**Acceptance criteria:**
- [x] `intent_id` is derived from `signal_id` and is the client order id
- [x] Creating a duplicate intent is a no-op, never a second order
- [x] Every event is appended and survives reopen
- [x] Corrupt or unknown states surface as `ExecutionError`

**Verification:** `uv run pytest tests/execution/test_store.py`

**Dependencies:** Task 0

**Files:** `src/nomicronix/execution/models.py`, `store.py`,
`tests/execution/fakes.py`, `test_store.py`

## Task 2: adapter protocol and fake exchange

**Description:** Define the narrow exchange protocol and a scripted fake with
fills, partial fills, errors, timeouts and duplicate submits.

**Acceptance criteria:**
- [x] The domain imports no exchange library
- [x] The fake can script every terminal status
- [x] Submit retries are idempotent by client order id
- [x] Errors map to stable `ExecutionError` codes

**Verification:** `uv run pytest tests/execution/test_adapter.py`

**Dependencies:** Task 1

**Files:** `src/nomicronix/execution/adapter.py`, `tests/execution/test_adapter.py`

## Phase 2: shadow mode

## Task 3: shadow intents

**Description:** Load approved signals, re-check risk at candle close, apply
expiry and slippage guards, and record shadow intents without private calls.

**Acceptance criteria:**
- [x] No private adapter method is called in shadow mode
- [x] An expired or stale signal is rejected and journaled
- [x] Slippage beyond the ceiling is rejected and journaled
- [x] A rejected risk decision never becomes an intent

**Verification:** `uv run pytest tests/execution/test_shadow.py`

**Dependencies:** Task 2

**Files:** `src/nomicronix/execution/shadow.py`, `service.py`,
`tests/execution/test_shadow.py`

## Phase 3: human-approved submission

## Task 4: two-switch live approval

**Description:** Require the exact phrase, `dry_run = false` and `--live`;
submit a spot market order, poll to terminal and journal each transition.

**Acceptance criteria:**
- [x] Any one missing gate refuses with exit `1` or `4`
- [x] The exact phrase is the only accepted confirmation
- [x] A duplicate submit reuses the client order id
- [x] Kill switch and unclean reconciliation refuse submission
- [x] A timeout leaves `submitted` and never auto-resubmits

**Verification:** `uv run pytest tests/execution/test_submit.py`

**Dependencies:** Task 3

**Files:** `src/nomicronix/execution/submit.py`, `__main__.py`,
`tests/execution/test_submit.py`

## Task 5: fill reporting with partial fills

**Description:** Aggregate exchange fills and report each increment once through
`RiskService.record_fill` under the risk-engine v1.1 contract.

**Acceptance criteria:**
- [x] A full fill reports the approved quantity once
- [x] A partial fill reports only the filled quantity
- [x] Fees are summed and reported exactly
- [x] A canceled remainder expires the approval, never opens a second position

**Verification:** `uv run pytest tests/execution/test_fills.py`

**Dependencies:** Task 4, risk-engine v1.1

**Files:** `src/nomicronix/execution/fills.py`, `tests/execution/test_fills.py`

## Phase 4: reconciliation, cancel and flatten

## Task 6: reconciliation

**Description:** Compare local intents, exchange open orders and balances; journal
mismatches and refuse submissions when not clean.

**Acceptance criteria:**
- [x] Matched orders produce a clean report
- [x] Unknown exchange orders and unknown positions are both reported
- [x] A missing local order is reported
- [x] Unclean state blocks new submissions with exit `3`

**Verification:** `uv run pytest tests/execution/test_reconcile.py`

**Dependencies:** Task 5

**Files:** `src/nomicronix/execution/reconcile.py`, `tests/execution/test_reconcile.py`

## Task 7: cancel and panic flatten

**Description:** Cancel a single open order and flatten all open orders and
positions behind the exact phrase.

**Acceptance criteria:**
- [x] `cancel` refuses an unknown or terminal intent
- [x] `flatten` requires `FLATTEN NOW`
- [x] `flatten` only reduces exposure and reports fills
- [x] Every action is journaled before output

**Verification:** `uv run pytest tests/execution/test_flatten.py`

**Dependencies:** Task 6

**Files:** `src/nomicronix/execution/flatten.py`, `tests/execution/test_flatten.py`

## Phase 5: CLI, docs and network contract

## Task 8: CLI, config and documentation

**Description:** Add `shadow`, `status`, `show`, `submit`, `cancel`, `reconcile`
and `flatten`, an example config, the module guide, README updates and one
opt-in read-only network test.

**Acceptance criteria:**
- [x] No CI job needs exchange credentials
- [x] Exit codes match the spec
- [x] `docs/execution.md` documents the gates and reconciliation rules
- [x] README marks the module built only after this task passes
- [x] Full offline suite, ruff, mypy and 90% module coverage pass

**Verification:** `uv run pytest -m "not network"`, `uv run ruff check src tests
examples`, `uv run mypy src/nomicronix`

**Dependencies:** Tasks 1-7

**Files:** `src/nomicronix/execution/__main__.py`, `config/execution.toml`,
`docs/execution.md`, `README.md`, `tests/execution/`

## Phase 6: read-only preflight

## Task 9: preflight report

**Description:** Report every live-readiness gate in one read-only run: config
switches, credentials, spot-only, key permissions, clock skew and the
paper-record gate.

**Acceptance criteria:**
- [x] No order, cancel or journal write happens, and no private probe runs
      without credentials
- [x] Warnings (unreported permissions, missing server time) do not block;
      failures exit `2`
- [x] The readiness report uses the same `--cash` as `submit`
- [x] JSON output is deterministic and includes the readiness payload
- [x] Offline execution coverage stays above 90%

**Verification:** `uv run pytest tests/execution/test_preflight.py`

**Dependencies:** Tasks 1-8

**Files:** `src/nomicronix/execution/preflight.py`, `adapter.py`, `__main__.py`,
`tests/execution/test_preflight.py`, `docs/execution.md`
