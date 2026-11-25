# execution

`execution` turns an approved signal into a spot market order, in stages, without
ever bypassing `risk-engine`. It is the only module allowed to hold exchange API
credentials or call a private endpoint, and it has no withdrawal path.

**Spot only, long only, market orders only.** No leverage, futures, margin,
shorts or autonomous trading.

## Status

Stages 1 and 2 are built:

| Stage | Config | Behaviour |
|---|---|---|
| 1 · shadow | `stage = "shadow"` | Builds intents, checks risk and slippage, makes no private call |
| 2 · approved | `stage = "approved"` | The owner submits each intent with the exact phrase and `--live` |
| 3 · autonomous | rejected by config | Out of scope for this spec |

Live submission is additionally disabled until `dry_run = false` and
`allow_live = true` are both set in `config/execution.toml`. The repository config
keeps `dry_run = true`, so a fresh checkout cannot place an order.

`signal-delivery readiness` is the promotion gate. It exits `0` only after at
least 30 closed paper trades over 28 days with positive expectancy after assumed
costs. `execution submit` enforces the same criteria in-process by reading the
delivery ledger (`--delivery-db`, default `data/delivery.sqlite`) before it
touches the exchange; a failing report refuses the order with exit `2`. The gate
is on by default (`require_readiness = true`) and is skipped only when the owner
deliberately sets `require_readiness = false`.

## Safety gates

Every `submit` must pass all of these before the exchange is touched:

1. The exact phrase `PLACE ORDER`.
2. `--live` on the command.
3. `dry_run = false` in the validated config.
4. `allow_live = true` and `stage = "approved"`.
5. The intent is `awaiting_approval`, not stale and not expired.
6. No open position already exists for the symbol.
7. The signal-delivery readiness report is `ready` (unless
   `require_readiness = false`). A missing checker fails closed.
8. The current best ask has not moved beyond `max_slippage_bps` from the signal
   entry.
9. `RiskService.status().trading_allowed` is true (kill switch clear, fresh mark,
   daily loss and drawdown inside limits).
10. `reconcile` is clean: no unknown orders, missing orders or unexplained
    positions.

An intent already `submitted` may always be resumed and polled, even if
readiness later drops, so an in-flight order is never abandoned. Readiness only
blocks a new order.

`flatten` is the only command that works while the kill switch is engaged,
because it can only reduce exposure. It caps each sell at the actual exchange
balance and skips positions that exist only in the paper book, reporting them as
`skipped`. An incomplete flatten exits `3`.

## Preflight

`preflight` answers "could this host place an order right now?" without touching
an order endpoint:

```bash
uv run python -m nomicronix.execution preflight
uv run python -m nomicronix.execution preflight --json
```

It checks the three config switches (`dry_run`, `allow_live`, `stage`),
credentials in the environment (masked), spot-only, key permissions when the
exchange reports them, clock skew, and the readiness report. Exit `0` means every
blocking check passed, `2` means at least one did not, and `1` means the config or
input is invalid.

A `[WARN]` check does not block: an exchange that does not expose permissions
cannot be verified in code, and server time may be unavailable. Both stay manual
dashboard checks. Preflight never places, cancels or changes an order and never
writes the risk or execution journals. It does not run `reconcile`, which
journals; run both before submitting.

## Credentials

Environment only. The CLI never accepts keys as flags and never writes them to
config, journals or output.

```bash
export NOMICRONIX_BINANCE_API_KEY=...
export NOMICRONIX_BINANCE_API_SECRET=...
```

Use a **trade-only** key with withdrawals disabled, restricted to the host's IP.
When the exchange exposes permissions, startup refuses a key that can withdraw.
When it does not, the CLI still prints a trade-only reminder on every private
command. Only the last four characters of the key are shown.

Public commands (`shadow`, `status`, `show`) need no credentials.

## Commands

```bash
# Stage 1: create intents from an approved signal file (no private call)
uv run python -m nomicronix.execution shadow \
    --signals results/btc-signals.json

# Inspect the journal
uv run python -m nomicronix.execution status
uv run python -m nomicronix.execution show --intent nx-1b2a5ef73e8f6887c56b0feaa35bddd9

# Stage 2: submit one intent (all four live gates are required)
uv run python -m nomicronix.execution submit \
    --intent nx-1b2a5ef73e8f6887c56b0feaa35bddd9 \
    --confirm "PLACE ORDER" --live

# Cancel one open order
uv run python -m nomicronix.execution cancel --intent nx-...

# Compare the journal, the exchange and the risk book
uv run python -m nomicronix.execution reconcile

# Read-only live-readiness report (no order endpoint, exit 0 ready / 2 not ready)
uv run python -m nomicronix.execution preflight

# Panic button: cancel every open order, market-sell every open position
uv run python -m nomicronix.execution flatten --confirm "FLATTEN NOW"
```

| Option | Meaning |
|---|---|
| `--signals` | Strategy-engine result (`schema_version = 1`, `kind = "signals"`) |
| `--execution-db` | Order journal; default `data/execution.sqlite` |
| `--risk-db` | Risk database; default `data/risk.sqlite` (shared with `signal-delivery` paper trades; point live execution at a dedicated file) |
| `--delivery-db` | Signal-delivery ledger used by the readiness gate; default `data/delivery.sqlite` |
| `--risk-config` | Risk limits; default `config/risk.toml` |
| `--config` | Execution config; default `config/execution.toml` |
| `--cash` | Initial cash for a fresh risk database; default `1000` |
| `--intent` | Intent id for `show`, `submit`, `cancel` |
| `--confirm` | Exact confirmation phrase |
| `--live` | Required, together with `dry_run = false`, to submit |
| `--json` | Machine-readable output |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Command completed, including zero intents |
| `1` | Invalid input, config, credentials, intent state or phrase |
| `2` | Risk refused the signal or the submission, or `preflight` found a blocking check |
| `3` | Exchange or reconciliation error; books are not proven consistent |
| `4` | Live submission is disabled by the stage, config or missing `--live` |

Every refusal and rejection is journaled, never silently dropped.

## Configuration

[`../config/execution.toml`](../config/execution.toml) is safe by default:

```toml
[execution]
exchange = "binance"
market_type = "spot"
dry_run = true
allow_live = false
stage = "shadow"
max_slippage_bps = 50
max_intent_age_ms = 300000
order_timeout_ms = 60000
poll_interval_ms = 1000
max_clock_skew_ms = 2000
require_readiness = true
quantity_step = "0.00001"
min_quantity = "0.00001"
min_notional = "10"

[risk]
require_kill_switch_clear = true
```

The loader rejects unknown keys or tables, a non-spot `market_type`,
`stage = "autonomous"`, negative numbers, a non-positive timeout or step, and
`dry_run = false` without `allow_live = true`.

The three instrument keys are execution defaults; the live exchange filters are
not fetched yet, so keep them conservative or pass symbol-specific values in a
private config.

## Order flow

1. `shadow` loads each signal and re-runs `RiskService.check` at the candle
   close. A rejected check is journaled and never becomes an order.
2. For an approval it fetches the public ticker, applies the expiry, staleness
   and slippage guards, and writes the intent with the exact approved quantity.
3. `submit` first checks readiness, then re-fetches the ticker, re-marks risk,
   reconciles, and sends a **spot market** order whose client order id is a
   deterministic `nx-` plus a truncated SHA-256 digest of the signal id (at most
   36 characters, the Binance limit).
4. It polls until terminal or `order_timeout_ms`. A timeout leaves the intent
   `submitted`; `reconcile` then matches it by client id, attaches the exchange
   order id when it can, and otherwise reports it as missing for the owner to
   resolve. Nothing is ever auto-resubmitted.
5. Exchange fills are aggregated and reported to `RiskService.record_fill` as
   one increment per exchange update. Each increment carries a deterministic
   fill key, so replaying the same exchange update after a crash is a no-op. A
   full fill consumes the approval; a canceled remainder calls
   `RiskService.expire_approval`.

## Partial fills

`risk-engine` v1.1 accepts one positive piece at a time:

- `0 < quantity <= approved_quantity - filled_quantity`;
- the approval is consumed only when the pieces reach the approved quantity;
- a canceled remainder is expired, after which no later fill can use it.

Execution stores the cumulative `filled_quantity` and
`average_fill_price` on the intent. The incremental price is derived from the
change in cumulative notional, so several pieces do not distort the position's
entry price. Fees are reported as the change in cumulative fee.

Fill keys are durable in the risk database (`fill_keys`), so a crash between the
risk commit and the execution journal commit can be replayed safely: the second
report finds the key and returns the already-committed state instead of opening
the position twice. `flatten` uses the same mechanism for its close fills.

## Reconciliation

`reconcile` is mandatory before every submission and is safe to re-run:

1. Fetch every open order and all balances.
2. Match each local `submitted` intent to its exchange order by exchange id, or
   by client id when a submit outcome is unknown. A match attaches the exchange
   order id and journals a `reconciled` event.
3. Report:
   - `missing_orders`: a local submitted intent with no exchange order;
   - `unknown_orders`: an open exchange order with no local submitted intent;
   - `unknown_positions`: a non-quote balance with no risk position, or a risk
     position larger than the exchange balance.
4. Return clean only when all three sets are empty.

An unclean report exits `3` and blocks submissions. Mismatches are journaled as
`reconciled` events. There is no "ignore" flag.

## Persistence

One SQLite database at `data/execution.sqlite` (WAL):

- `intents`: intent id, signal id, symbol, side, quantity, entry/stop/target,
  approved notional, timestamps, state, cumulative filled quantity and average.
- `orders`: the latest exchange snapshot per client order id.
- `events`: append-only journal (`intent_created`, `shadowed`, `approved`,
  `submitted`, `partially_filled`, `filled`, `canceled`, `rejected`, `failed`,
  `refused`, `reconciled`, `flattened`).

Execution writes risk state only through `RiskService`. It never writes the risk
tables directly.

## Testing

```bash
uv run pytest tests/execution -m "not network" --cov=nomicronix.execution --cov-fail-under=90
uv run pytest tests/execution -m network   # opt-in public ticker only
```

Every offline test uses `FakeAdapter`; no test needs credentials or the network.
The opt-in contract test reads a public ticker and cannot place an order.

## Known limitations

- Binance through `ccxt`, spot, long only, market orders only.
- One symbol per signal; no OCO brackets, exchange-side stops or portfolio
  rebalancing.
- The exchange's live symbol filters are not fetched yet; the config's
  `quantity_step`, `min_quantity` and `min_notional` are authoritative.
- `reconcile` reports and attaches known orders, but does not apply fills or
  terminalize intents by itself. An unknown non-open outcome stays manual and is
  never auto-resubmitted.
- Fill reporting to `risk-engine` and the execution journal use separate SQLite
  transactions. The durable fill key makes a replay safe, but the two writes
  remain separate commits.
- `preflight` cannot prove the key is trade-only or IP-restricted when the
  exchange does not expose permissions; that remains a dashboard check.
- Stage 3 autonomy is out of scope.
- Live trading is never risk-free. Nothing here is a profit guarantee.
