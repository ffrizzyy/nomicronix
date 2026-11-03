# risk-engine

Deterministic safety layer between a trade idea and an exchange. The AI proposes;
this module decides. It has no network code, no exchange client, no LLM and no
price feed. You give it a fully specified proposal, a portfolio snapshot, the
instrument's quantity rules and the configured limits; it returns either an
approved quantity with its risk amount or a rejection with machine-readable
reasons.

`signal-delivery` and `execution` must both call `RiskService.check`. Neither may
size a position or bypass a limit itself.

## Concepts

- **Proposal:** signal id, strategy id, symbol, side, entry, stop, target,
  confidence and creation time. Confidence is recorded but never used to bypass a
  limit.
- **Instrument:** exchange quantity rules for one symbol (`quantity_step`,
  `min_quantity`, `min_notional`). Passed by `execution` at check time. This module
  never caches exchange filters.
- **Snapshot:** equity, peak equity, UTC day-start equity, realized P&L today,
  open positions, kill switch and the snapshot timestamp.
- **Decision:** approved flag, approved quantity/notional, risk amount, reward/risk,
  a stable tuple of rejection reasons, and a rule trace explaining caps.
- **Kill switch:** sticky, persisted state. A drawdown breach engages it
  automatically; only an exact manual confirmation clears it.

Money is `Decimal`. Time is UTC epoch milliseconds (`*_ms`). `float` appears only
for signal `confidence` and for reporting ratios.

## Commands

All commands accept `--db` (default `data/risk.sqlite`), `--config` (default
`config/risk.toml`) and `--cash` (initial paper cash, default `1000`).

```bash
# Status (creates the account on first use)
uv run python -m nomicronix.risk_engine status

# Check a proposal, optionally marking prices first
uv run python -m nomicronix.risk_engine check \
    --proposal examples/proposal.json --price BTC/USDT=100

# Mark open positions to market
uv run python -m nomicronix.risk_engine mark --prices prices.json

# Manual kill and resume
uv run python -m nomicronix.risk_engine kill --reason "manual stop"
uv run python -m nomicronix.risk_engine resume --confirm "RESUME TRADING"
```

`prices.json` is a JSON object of symbol to price, for example
`{"BTC/USDT": "101.50"}`.

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Success, or the proposal was approved |
| `2` | The proposal was rejected by risk |
| `1` | Invalid input, missing/corrupt state, config error or another failure |

## Library use

```python
from decimal import Decimal

from nomicronix.risk_engine import (
    Fill,
    FillAction,
    Instrument,
    RiskLimits,
    RiskService,
    Side,
    TradeProposal,
)
from nomicronix.risk_engine.store import RiskStateStore

store = RiskStateStore("data/risk.sqlite")
service = RiskService(store, RiskLimits(), initial_cash=Decimal("1000"))

proposal = TradeProposal(
    signal_id="sig-001",
    strategy_id="sma-cross",
    symbol="BTC/USDT",
    side=Side.BUY,
    entry_price=Decimal("100"),
    stop_price=Decimal("95"),
    target_price=Decimal("115"),
    confidence=0.8,
    created_at_ms=1_735_689_600_000,
)
instrument = Instrument(
    symbol="BTC/USDT",
    quantity_step=Decimal("0.00001"),
    min_quantity=Decimal("0.00001"),
    min_notional=Decimal("10"),
)
decision = service.check(proposal, instrument, now_ms=1_735_689_600_000)
if decision.approved:
    # execution places the order, then reports each fill piece here
    service.record_fill(
        "sig-001",
        Fill(
            action=FillAction.OPEN,
            symbol="BTC/USDT",
            quantity=decision.approved_quantity,
            price=Decimal("100"),
            fee=Decimal("0"),
            filled_at_ms=1_735_689_600_001,
        ),
    )
store.close()
```

`check` evaluates and persists the decision atomically before returning. A caller
cannot receive an approval that is missing from the journal.

`record_fill` records a piece of an approval. An unknown or already-consumed
`signal_id` raises `ApprovalError`. An OPEN fill must match the approved symbol
and satisfy `0 < quantity <= approved_quantity - filled_quantity`.

## Partial fills

A spot market order can fill in pieces. v1.1 accepts that without corrupting the
books:

- Each OPEN piece accumulates into the same position. Quantity adds up, the entry
  price becomes the quantity-weighted average and fees add up.
- The approval is consumed only when the cumulative filled quantity equals the
  approved quantity. A partial piece leaves the remainder usable.
- An overfill is rejected before any state changes.
- `expire_approval(signal_id)` voids a canceled remainder. The approval keeps its
  `filled_quantity` for audit and refuses every later fill.
- A partial CLOSE reduces the position, allocates the entry fee proportionally
  and realizes the matching share of P&L. A close larger than the position is
  rejected.

The `approvals` table gained `filled_quantity` in v1.1. Opening an existing v1
database adds the column with a default of `0`; no data is rewritten.

## Rule order

Every applicable rule is checked so one decision explains every failure. The only
early return is an invalid proposal, because later arithmetic would be meaningless.

1. Kill switch engaged.
2. Stale signal: `now_ms - created_at_ms > max_signal_age_ms`.
3. Stale state: `now_ms - as_of_ms > max_signal_age_ms`.
4. Symbol not in `allowed_symbols`.
5. Instrument symbol mismatch.
6. Invalid proposal: non-positive prices, buy stop not below entry, target not
   above entry, confidence outside `0..1`, empty signal id or a future timestamp.
7. Duplicate symbol already open (no pyramiding).
8. `max_concurrent_positions`.
9. `max_daily_loss_pct`, measured against UTC day-start equity.
10. `max_drawdown_pct`, measured against peak equity; a breach also engages the
    kill switch.
11. `min_reward_risk`: `(target - entry) / (entry - stop)`.
12. Position sizing and the per-position notional cap.
13. `max_total_exposure_pct` across marked positions plus the proposal.
14. `max_open_risk_pct` across open stops plus the proposal.
15. Approve.

A stale or future signal is rejected rather than adjusted. A negative time delta is
treated as stale.

## Sizing

```text
risk_amount = equity * risk_per_trade_pct
by_risk     = risk_amount / (entry - stop)
by_notional = (equity * max_position_notional_pct) / entry
quantity    = floor_to_step(min(by_risk, by_notional), quantity_step)
notional    = quantity * entry
```

- Rounding always goes down. A size is never rounded up to meet a minimum.
- Reject when quantity is below `min_quantity`, or when `notional` is below the
  larger of `Instrument.min_notional` and `min_notional_usdt`.
- A notional cap that merely reduces size is not a rejection; it is recorded in
  `rule_trace` as `capped_by_notional=True`. If the cap pushes size below the
  minimum, the decision is rejected with `size_below_minimum`.
- A zero or negative equity, entry or stop distance returns zero size and is
  rejected.

At 1,000 USDT equity, a 100 entry with a 95 stop gives:

- risk amount: 5 USDT
- risk-based size: 1 unit
- notional: 100 USDT, inside the 250 USDT per-position cap
- reward/risk at a 115 target: 3.0

## Default limits

`config/risk.toml` is committed with these values:

| Limit | Default |
|---|---|
| Risk per trade | 0.5% of equity |
| Max daily loss | 3% of day-start equity, including unrealized P&L |
| Max drawdown | 10% from peak marked-to-market equity |
| Max concurrent positions | 2 |
| Max position notional | 25% of equity |
| Max total exposure | 50% of equity |
| Max aggregate open risk | 2% of equity |
| Minimum reward/risk | 2.0 |
| Minimum notional | 10 USDT (or the instrument's higher minimum) |
| Max signal age | 5 minutes |
| Allowed symbols | `BTC/USDT`, `ETH/USDT` |
| Starting paper capital | 1,000 USDT |

Aggregate open risk (2%) is intentionally tighter than the daily loss limit (3%),
so it binds first and prevents stacking several trades that could lose the daily
budget together.

Percentages are TOML strings parsed as `Decimal`, not floats. Validation rejects
values outside `(0, 1]`, non-positive counts/notional/ages, `min_reward_risk < 1`,
an empty allowlist and malformed TOML.

## State and persistence

- Default database: `data/risk.sqlite` (gitignored).
- SQLite runs in WAL mode. Mutations use `BEGIN IMMEDIATE`; run one writer process
  at a time. Do not hand-edit the database.
- Tables:
  - `risk_state`: singleton row with cash, peak equity, day-start equity, UTC day,
    realized P&L today, kill switch, last mark time.
  - `positions`: open positions keyed by symbol.
  - `approvals`: every approved signal id, quantity/notional, stop/target,
    cumulative filled quantity, creation time and consumption time.
  - `decisions`: append-only journal of every check, approved or rejected.
- A UTC day rollover resets `day_start_equity` and `realized_pnl_today`. It does not
  reset the kill switch.
- `mark` updates current prices, recomputes equity, raises the peak and engages the
  kill switch if drawdown reaches the limit.
- A drawdown breach is also fail-closed inside `check`; the decision and the
  kill-switch state are written in the same transaction.

## Failure behavior

- Missing database: initialized at `initial_cash` on first use.
- Corrupt or inconsistent state: surfaces as `StoreError`; the check fails, it does
  not silently approve.
- Rejected check: journaled, but no approval row is created.
- Duplicate fill or unknown approval: `ApprovalError`.
- Overfill or a fill for the wrong symbol: `ApprovalError`, no state change.
- Expiring an unknown or already-consumed approval: `ApprovalError`.
- Close fill larger than the open position: rejected; a smaller close reduces the
  position and realizes P&L proportionally.
- `resume` with any phrase other than `RESUME TRADING`: `ValueError`; the kill
  switch stays engaged.

## Decisions taken for the spec's open questions

| Question | Decision |
|---|---|
| Daily loss basis | Equity drawdown from UTC day-start, including unrealized P&L |
| Drawdown basis | Intraday marked-to-market equity against the all-time peak |
| Signal age | Flat 5 minutes, configurable; stale signals fail closed |
| Aggregate open risk | 2% of equity, tighter than the daily limit |
| Partial fills | v1.1: any positive piece of an approval; consumed only on the final piece |
| Resume safety | Exact phrase `RESUME TRADING`; no auto-reset or cooldown |
| Instrument filters | Passed by `execution` at check time, never cached |
| Kill-switch alerts | Journal plus CLI for now; `signal-delivery` will consume it later |

## Development

```bash
uv run pytest tests/risk_engine --cov=nomicronix.risk_engine --cov-fail-under=90
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/nomicronix/risk_engine
```

Tests use temporary SQLite files and injected clocks. There are no network tests in
this module; it must never contact an exchange.
