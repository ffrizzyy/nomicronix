# signal-delivery

Carries an approved signal candidate to a human channel and keeps an honest paper
record of what happened next. It sits between `strategy-engine` (proposes) and
`risk-engine` (decides): it never approves a signal itself and never invents
prices.

The module answers two questions:

1. *Which signal candidates passed risk, and how do I see them?*
2. *If each approved candidate had been taken on paper, what is the P&L?*

`execution` will later read the same approval path instead of paper-filling it.

## Concepts

- **`Notification`:** the stable payload a sink receives. It copies the signal
  levels, confidence, rationale, timestamps and the risk decision. Rejected
  notifications carry machine-readable `reasons`; approved ones carry size,
  notional, risk amount and reward/risk.
- **`PaperTrade`:** one approved signal tracked at a full, fee-free entry. It
  stays open until `tick` closes it at the stop or the target.
- **`DeliveryRecord`:** one outbox row per signal id with status `pending`,
  `delivered`, `rejected` or `failed`, the stored payload and any error text.
- **Sink:** a tiny object with `name` and `send(notification)`.

Prices and money are `Decimal`; times are UTC epoch milliseconds. Confidence is
the only `float`.

## Commands

```bash
# Risk-check and deliver a strategy-engine result
uv run python -m nomicronix.signal_delivery deliver \
    --signals results/btc-signals.json \
    --sink console --sink jsonl --jsonl data/deliveries.jsonl

# Optional Telegram sink (credentials from the environment)
export NOMICRONIX_TELEGRAM_BOT_TOKEN=...
export NOMICRONIX_TELEGRAM_CHAT_ID=...
uv run python -m nomicronix.signal_delivery deliver \
    --signals results/btc-signals.json --sink telegram

# Mark open paper trades against fresh prices
uv run python -m nomicronix.signal_delivery tick \
    --price BTC/USDT=94.50 --at 1735779900000

# Delivery and paper P&L report
uv run python -m nomicronix.signal_delivery report

# Check whether the paper record can promote execution (0 ready, 2 not ready)
uv run python -m nomicronix.signal_delivery readiness
```

| Option | Meaning |
|---|---|
| `--signals` | `strategy-engine` result JSON (`kind = "signals"`) |
| `--sink` | Repeatable; `console`, `jsonl` or `telegram`; overrides the config list |
| `--jsonl` | JSONL output path; defaults to the config value |
| `--delivery-db` | Outbox and paper ledger; default `data/delivery.sqlite` |
| `--risk-db` | `risk-engine` database; default `data/risk.sqlite` |
| `--risk-config` | Risk limits TOML; default `config/risk.toml` |
| `--cash` | Initial paper cash; default `1000` USDT |
| `--quantity-step`, `--min-quantity`, `--min-notional` | Instrument filters |
| `--config` | Delivery defaults TOML; default `config/delivery.toml` |
| `--prices` | JSON object of symbol to price for `tick` |
| `--price SYMBOL=PRICE` | Repeatable tick mark |
| `--at` | UTC epoch milliseconds for `tick`; default now |
| `--json` | Machine-readable output for `readiness` |

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Command completed, including zero delivered or zero closed trades |
| `1` | Invalid input, config, signal file, store or price map |
| `2` | A configured sink failed, or `readiness` criteria are not met |

A rejected signal does not make `deliver` exit nonzero. Risk did its job, and
the rejection is recorded in the outbox.

## Delivery flow

For each signal in file order:

1. If a delivery row exists for `signal_id`:
   - `delivered` or `rejected`: skip.
   - `pending` or `failed`: resend the stored payload; do not re-check risk.
2. Mark the risk account at the signal's candle close, using its entry price.
   This keeps replay state fresh without a wall clock.
3. Build a `TradeProposal` and call `RiskService.check` with
   `now_ms = signal.created_at_ms`.
4. If rejected: store a `rejected` outbox row and never send a trade alert.
5. If approved: call `RiskService.record_fill` with one full OPEN fill at the
   entry price, fee zero. Persist the `pending` outbox row and the paper trade.
6. Send to every configured sink. Mark the row `delivered`; on a sink error mark
   it `failed` and raise `DeliveryError` (CLI exit `2`).

The outbox makes local delivery idempotent. Re-running the same file never
duplicates a delivery or a position. A crash between the risk fill and the
outbox insert is the one at-most-once window; it is documented and still cannot
create a second position.

## Paper tracking

`tick` takes a price map and a time. For each open trade whose symbol has a
price:

- `price <= stop`: close at `min(stop, price)` with reason `stop`.
- `price >= target`: close at `target` with reason `target`.
- otherwise leave it open.

Stops are checked before targets, so a candle that crosses both is treated
conservatively. A gap below the stop fills at the worse price. Closing a trade
is a full `RiskService.record_fill` CLOSE with zero fee. v1 ignores partial
fills and slippage; those belong to `execution`.

## Execution promotion gate

The execution module is built, but live submission is not enabled until the
paper record is credible. `readiness` makes that rule executable: it scores the
ledger against the `[promotion]` table and exits `0` only when every criterion
passes, `2` when the record is not ready, and `1` on bad input.

| Criterion | Default | Why it exists |
|---|---|---|
| Closed paper trades | `>= 30` | A handful of trades is luck, not evidence |
| Track-record days | `>= 28` | Must span more than one market mood |
| Expectancy after costs | `> 0` USDT/trade | Fee-free paper fills are charged an assumed 20 bps round trip |
| Paper max drawdown | `<= 5%` | Half the live kill-switch limit |
| Delivery health | 0 failed, 0 pending | An unreachable alert channel invalidates the record |
| Ledger integrity | delivered == trades | Every approved alert must have a paper trade |

`--json` emits the same report machine-readably:

```json
{
  "schema_version": 1,
  "kind": "execution-readiness",
  "ready": false,
  "closed_trades": 12,
  "track_record_days": "9.5",
  "gross_pnl": "40",
  "net_pnl": "35.6",
  "expectancy": "2.966666666666666666666666667",
  "max_drawdown_pct": "0.012",
  "criteria": [
    {"key": "closed-trades", "label": "closed paper trades",
     "passed": false, "actual": "12", "required": ">= 30"}
  ]
}
```

The gate is a minimum bar, not a profit promise. Passing it means the process
has been exercised and did not lose money after conservative costs; it does not
mean live trading will work. Raising a threshold is always allowed; lowering one
is a deliberate owner decision that should be written down.

### Running the cycle

The 28-day clock advances on wall time, so the paper loop needs a schedule.
[`../scripts/paper_cycle.py`](../scripts/paper_cycle.py) runs one complete cycle
with the documented CLIs: backfill, mark open trades at the latest closed candle,
scan and deliver each symbol, then print readiness. Marking comes before
delivery, so a trade opened this cycle is not stopped on the same candle that
created it.

```bash
# Print every command without running it
uv run python scripts/paper_cycle.py --dry-run

# One live cycle (defaults: BTC/USDT and ETH/USDT on 1h). It backfills 120 days
# of warm-up but scans only the candle that just closed.
uv run python scripts/paper_cycle.py

# Same, where api.binance.com is blocked but the spot mirror is reachable
uv run python scripts/paper_cycle.py --api-url https://data-api.binance.vision/api/v3

# Replay a historical window into the paper ledger (research only). The store
# needs warm-up candles before this date; backfill them first if it does not.
uv run python scripts/paper_cycle.py --since 2026-07-30
```

**A replay is not live evidence.** `deliver` evaluates a signal at its own candle
close, so a wide `--since` writes weeks or months of paper trades in a single run.
That is useful for research, but it does not show that the system ran for 28 days.
Only live cycles, each scanning the just-closed candle, count toward the
promotion gate; do not hand a replayed ledger to `readiness`.

Cron on a UTC host:

```cron
1 * * * * cd /srv/nomicronix && uv run python scripts/paper_cycle.py >> data/paper-cycle.log 2>&1
```

Windows Task Scheduler:

```powershell
schtasks /Create /SC HOURLY /TN "Nomicronix paper cycle" /TR "cmd /c cd /d D:\path\to\nomicronix && uv run python scripts\paper_cycle.py" /ST 00:01
```

A readiness exit `2` is reported but does not fail the cycle, because the gate is
expected to stay red while evidence accumulates. A failure in any earlier step
stops the cycle with that exit code, so a broken feed, config or sink is loud.

## Sinks

```python
class Sink(Protocol):
    name: str

    def send(self, notification: Notification) -> None: ...
```

| Sink | Behaviour |
|---|---|
| `console` | Prints a stable multi-line message to stdout |
| `jsonl` | Appends one compact, sorted-key JSON object per line |
| `telegram` | Sends the rendered text through the Bot API |

Telegram credentials come only from `NOMICRONIX_TELEGRAM_BOT_TOKEN` and
`NOMICRONIX_TELEGRAM_CHAT_ID`. Missing credentials are a configuration error
before any delivery. Tokens are never logged, stored or included in an error;
network failures report a redacted message. Tests inject a fake transport, so
no test touches the network.

Every notification includes the signal id, so a consumer can de-duplicate even
if a retry happens after a process crash.

## Library use

```python
from decimal import Decimal

from nomicronix.risk_engine.config import load_limits
from nomicronix.risk_engine.service import RiskService
from nomicronix.risk_engine.store import RiskStateStore
from nomicronix.signal_delivery import (
    ConsoleSink,
    DeliveryService,
    DeliveryStore,
    InstrumentRules,
    build_report,
    load_signals,
    render_report,
)

risk = RiskService(RiskStateStore("data/risk.sqlite"), load_limits())
delivery = DeliveryStore("data/delivery.sqlite")
service = DeliveryService(
    risk,
    delivery,
    [ConsoleSink()],
    InstrumentRules(),
)
outcomes = service.deliver_file("results/btc-signals.json")
for outcome in outcomes:
    print(outcome.action, outcome.record.signal_id)

print(render_report(build_report(delivery, risk)))
delivery.close()
risk.close()
```

`service.deliver(candidate)` handles one candidate; use it when another process
already loaded the strategies. `service.tick({...})` returns closed and still
open paper trades.

## Persistence

One SQLite database at `data/delivery.sqlite` (WAL):

- `deliveries`: signal id (primary key), status, payload JSON, recorded time and
  error. One row per consumed signal, including rejected ones.
- `paper_trades`: signal id (primary key), symbol, quantity, levels, open/close
  times, exit price/reason and realized P&L.

The risk database remains the source of truth for cash, equity, positions and
the decision journal. Delivery never writes those tables directly; it calls
`RiskService`.

## Configuration

`config/delivery.toml` holds defaults:

```toml
[delivery]
sinks = ["console"]
jsonl_path = "data/deliveries.jsonl"
quantity_step = "0.00001"
min_quantity = "0.00001"
min_notional = "10"

[promotion]
min_closed_trades = 30
min_days = 28
assumed_round_trip_cost_pct = "0.002"
max_paper_drawdown_pct = "0.05"
```

CLI flags override file values. Unknown sinks, repeated sinks, non-positive
instrument values and malformed TOML are rejected. A missing default file falls
back to these defaults; a missing explicit `--config` file is an error. A file
without a `[promotion]` table uses the readiness defaults above.

## Guarantees

- **Risk decides.** No tradeable alert exists without an approved
  `RiskDecision`; no position opens except through `record_fill`.
- **Deterministic replay.** Signal time is the candle close, not the wall clock.
  Re-running the same file with the same databases changes nothing.
- **Auditable.** Every consumed signal has an outbox row, and every paper trade
  names its signal id.
- **No secrets in git.** Telegram credentials are environment-only and never
  serialized.
- **Explicit failure.** Bad files, bad prices and sink errors fail loudly.

## Known limitations

- Paper fills are full, fee-free and at the planned price. Real execution adds
  fees, slippage and partial fills.
- One position per symbol, inherited from `risk-engine`; no pyramiding.
- No built-in scheduler, retry daemon or rate-limit handling. `tick` is
  owner-invoked; [`../scripts/paper_cycle.py`](../scripts/paper_cycle.py) runs one
  cycle and the scheduler stays external.
- Telegram is the only remote sink. Email and webhooks are future work.
- The promotion gate is a minimum bar, not proof of edge. Passing it only means
  the paper process was exercised without losing money after assumed costs.
- Signals are research inputs; paper P&L is not evidence of future profit.

## Development

```bash
uv run pytest tests/signal_delivery \
    --cov=nomicronix.signal_delivery --cov-fail-under=90
uv run ruff check src tests examples
uv run ruff format --check src tests examples
uv run mypy src/nomicronix/signal_delivery
```

Tests use in-memory SQLite databases, fake sinks and a fake Telegram transport.
The module has no network tests and must never contact an exchange.

## Decisions taken for the spec's open questions

| Question | Decision |
|---|---|
| Delivery input | A `strategy-engine` JSON result, plus a library API for candidates |
| Risk timing | Signal candle close, so replay is deterministic |
| Rejected signals | Recorded in the outbox, never sent as trade alerts |
| Duplicate handling | Outbox keyed by signal id; delivered and rejected rows are skipped |
| Retry behaviour | Pending and failed rows resend the stored payload without re-checking |
| Paper fills | Full, fee-free, at entry; stops before targets |
| Gap handling on a stop | Close at the worse of stop and mark price |
| Credentials | Environment variables only, never persisted or printed |
| Remote sink | Telegram Bot API; console and JSONL work offline |
| Scheduling | Owner-invoked CLI; no daemon in v1 |
| Execution gate | Measurable readiness criteria in `[promotion]`; `readiness` exits 2 until met |
