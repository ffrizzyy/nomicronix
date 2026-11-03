# Tasks: execution

Plan: `tasks/plan-execution.md` · Spec: `SPEC-execution.md`

**Status: blocked.** Do not start these tasks until the entry checklist in the
plan is complete and the owner approves the spec for implementation.

## Phase 0: gate and prerequisites

## Task 0: prove the promotion gate and prerequisites

**Description:** Run the readiness gate, archive its JSON, get the risk-engine
v1.1 partial-fill change approved, and confirm the exchange key is trade-only.

**Acceptance criteria:**
- [ ] `readiness` exits `0` and its JSON is archived
- [x] The risk-engine v1.1 partial-fill contract is approved and implemented
- [ ] Withdrawals are disabled and the key is IP-restricted
- [ ] The owner approves stages 1 and 2, not stage 3

**Verification:** `uv run python -m nomicronix.signal_delivery readiness --json`

**Dependencies:** signal-delivery

**Files:** none (operational gate)

## Phase 1: models, journal and fake adapter

## Task 1: domain models and order journal

**Description:** Add `OrderIntent`, `ExchangeOrder`, `OrderEvent`,
`Reconciliation`, errors and a SQLite journal keyed by intent id.

**Acceptance criteria:**
- [ ] `intent_id` is derived from `signal_id` and is the client order id
- [ ] Creating a duplicate intent is a no-op, never a second order
- [ ] Every event is appended and survives reopen
- [ ] Corrupt or unknown states surface as `ExecutionError`

**Verification:** `uv run pytest tests/execution/test_store.py`

**Dependencies:** Task 0

**Files:** `src/nomicronix/execution/models.py`, `store.py`,
`tests/execution/fakes.py`, `test_store.py`

## Task 2: adapter protocol and fake exchange

**Description:** Define the narrow exchange protocol and a scripted fake with
fills, partial fills, errors, timeouts and duplicate submits.

**Acceptance criteria:**
- [ ] The domain imports no exchange library
- [ ] The fake can script every terminal status
- [ ] Submit retries are idempotent by client order id
- [ ] Errors map to stable `ExecutionError` codes

**Verification:** `uv run pytest tests/execution/test_adapter.py`

**Dependencies:** Task 1

**Files:** `src/nomicronix/execution/adapter.py`, `tests/execution/test_adapter.py`

## Phase 2: shadow mode

## Task 3: shadow intents

**Description:** Load approved signals, re-check risk at candle close, apply
expiry and slippage guards, and record shadow intents without private calls.

**Acceptance criteria:**
- [ ] No private adapter method is called in shadow mode
- [ ] An expired or stale signal is rejected and journaled
- [ ] Slippage beyond the ceiling is rejected and journaled
- [ ] A rejected risk decision never becomes an intent

**Verification:** `uv run pytest tests/execution/test_shadow.py`

**Dependencies:** Task 2

**Files:** `src/nomicronix/execution/shadow.py`, `service.py`,
`tests/execution/test_shadow.py`

## Phase 3: human-approved submission

## Task 4: two-switch live approval

**Description:** Require the exact phrase, `dry_run = false` and `--live`;
submit a spot market order, poll to terminal and journal each transition.

**Acceptance criteria:**
- [ ] Any one missing gate refuses with exit `1` or `4`
- [ ] The exact phrase is the only accepted confirmation
- [ ] A duplicate submit reuses the client order id
- [ ] Kill switch and unclean reconciliation refuse submission
- [ ] A timeout leaves `submitted` and never auto-resubmits

**Verification:** `uv run pytest tests/execution/test_submit.py`

**Dependencies:** Task 3

**Files:** `src/nomicronix/execution/submit.py`, `__main__.py`,
`tests/execution/test_submit.py`

## Task 5: fill reporting with partial fills

**Description:** Aggregate exchange fills and report once through
`RiskService.record_fill` under the risk-engine v1.1 contract.

**Acceptance criteria:**
- [ ] A full fill reports the approved quantity once
- [ ] A partial fill reports only the filled quantity
- [ ] Fees are summed and reported exactly
- [ ] A canceled remainder expires the approval, never opens a second position

**Verification:** `uv run pytest tests/execution/test_fills.py`

**Dependencies:** Task 4, risk-engine v1.1

**Files:** `src/nomicronix/execution/fills.py`, `tests/execution/test_fills.py`

## Phase 4: reconciliation, cancel and flatten

## Task 6: reconciliation

**Description:** Compare local intents, exchange open orders and balances; journal
mismatches and refuse submissions when not clean.

**Acceptance criteria:**
- [ ] Matched orders produce a clean report
- [ ] Unknown exchange orders and unknown positions are both reported
- [ ] A missing local order is reported
- [ ] Unclean state blocks new submissions with exit `3`

**Verification:** `uv run pytest tests/execution/test_reconcile.py`

**Dependencies:** Task 5

**Files:** `src/nomicronix/execution/reconcile.py`, `tests/execution/test_reconcile.py`

## Task 7: cancel and panic flatten

**Description:** Cancel a single open order and flatten all open orders and
positions behind the exact phrase.

**Acceptance criteria:**
- [ ] `cancel` refuses an unknown or terminal intent
- [ ] `flatten` requires `FLATTEN NOW`
- [ ] `flatten` only reduces exposure and reports fills
- [ ] Every action is journaled before output

**Verification:** `uv run pytest tests/execution/test_flatten.py`

**Dependencies:** Task 6

**Files:** `src/nomicronix/execution/flatten.py`, `tests/execution/test_flatten.py`

## Phase 5: CLI, docs and network contract

## Task 8: CLI, config and documentation

**Description:** Add `shadow`, `status`, `show`, `submit`, `cancel`, `reconcile`
and `flatten`, an example config, the module guide, README updates and one
opt-in read-only network test.

**Acceptance criteria:**
- [ ] No CI job needs exchange credentials
- [ ] Exit codes match the spec
- [ ] `docs/execution.md` documents the gates and reconciliation rules
- [ ] README marks the module built only after this task passes
- [ ] Full offline suite, ruff, mypy and 90% module coverage pass

**Verification:** `uv run pytest -m "not network"`, `uv run ruff check src tests
examples`, `uv run mypy src/nomicronix`

**Dependencies:** Tasks 1-7

**Files:** `src/nomicronix/execution/__main__.py`, `config/execution.toml`,
`docs/execution.md`, `README.md`, `tests/execution/`
