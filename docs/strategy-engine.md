# strategy-engine

Turns stored candles into fully specified, causal trade ideas. A **signal
candidate** is an entry, a protective stop, a take-profit target, a confidence
and a written rationale for one symbol and one timeframe. It is a proposal, not
an order and not an approval.

The module answers one question: *given only the candles that had closed by time
T, what long trade idea, if any, does this strategy have?*

`signal-delivery` consumes candidates and passes each one to `risk-engine`.
Nothing downstream may re-derive prices or invent its own signals from raw
candles.

## Concepts

- **`SignalIdea`:** what a proposer returns — prices, confidence, rationale and
  an indicator snapshot. No identity and no timestamps.
- **`SignalCandidate`:** a validated idea with a deterministic id, strategy id,
  symbol, timeframe, `side = buy`, candle-close timestamps and expiry.
- **`CandleView`:** a read-only `Sequence[Candle]` whose last element is the
  decision candle. It physically cannot expose a later candle.
- **`ScanConfig`:** requested range, warm-up, confidence floor, signal TTL and a
  hard signal cap.
- **`SignalBatch`:** config, data metadata, signals and warnings.
- **Proposer:** a causal strategy implementing
  `strategy_id`, `min_history` and `propose(view) -> SignalIdea | None`.

Prices and money are `Decimal`. Times are UTC epoch milliseconds (`*_ms`) and
ranges are half-open: `start_ms <= open_time_ms < end_ms`. `float` appears only
for confidence.

## Commands

```bash
uv run python -m nomicronix.strategy_engine scan \
    --strategy examples.strategy_models:SmaCross \
    --param fast=20 --param slow=50 \
    --symbol BTC/USDT --timeframe 1h \
    --since 2025-01-01 --until 2025-02-01 \
    --min-confidence 0.5 --ttl 5m \
    --out results/btc-signals.json
```

`--db` defaults to `data/market.sqlite` and `--exchange` to `binance`.

| Option | Meaning |
|---|---|
| `--strategy` | `module:Class`; the class is constructed with `--param` values |
| `--param KEY=VALUE` | Repeatable; parsed as integer, then float, then string |
| `--since`, `--until` | UTC date or epoch milliseconds; the range is half-open |
| `--min-history` | Closed candles required before the first signal |
| `--min-confidence` | Ideas below this floor become warnings, not signals |
| `--ttl` | Signal lifetime (`30s`, `5m`, `12h`, `1d`); default `5m` |
| `--max-signals` | Hard cap; exceeding it is an error, never truncation |
| `--out`, `--force` | Write deterministic JSON atomically; refuse overwrite without force |
| `--config` | TOML scan defaults; flags override file values |

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Scan completed, including zero signals |
| `1` | Invalid config, bad data, malformed argument or signal-cap breach |
| `2` | Strategy import, constructor or proposal-contract error |
| `3` | Output exists and `--force` was not passed |

## Library use

```python
from nomicronix.market_data.store import SqliteStore
from nomicronix.strategy_engine import ScanConfig, SmaCross, scan

store = SqliteStore("data/market.sqlite", "binance")
config = ScanConfig(
    symbol="BTC/USDT",
    timeframe="1h",
    start_ms=1_735_689_600_000,
    end_ms=1_738_368_000_000,
    min_history=50,
    min_confidence=0.5,
)
batch = scan(store, SmaCross(fast=20, slow=50), config)
for signal in batch.signals:
    print(signal.signal_id, signal.entry_price, signal.stop_price)
    # hand the candidate to risk-engine; do not place an order here
store.close()
```

`scan` loads a gap-free range from the store. `scan_candles` runs the same loop
over an already-loaded `Sequence[Candle]`, which is useful in tests and
backtests.

## Proposer contract

```python
class SignalProposer(Protocol):
    strategy_id: str
    min_history: int

    def propose(self, view: CandleView) -> SignalIdea | None: ...
```

- `propose` is called once per closed candle, in ascending order, after at least
  `min_history` candles are visible.
- Returning `None` means no idea. Raising `ProposalError` records a warning and
  the scan continues. Any other exception aborts the scan.
- Proposers must be stateless or constructed fresh per scan. The same view must
  produce the same idea.

### Built-in proposers

| Strategy id | Fires when | Defaults |
|---|---|---|
| `sma-cross` | Fast SMA crosses strictly above slow SMA | `fast=20`, `slow=50`, `atr_period=14`, `stop_atr=2.0`, `target_rr=3.0`, `confidence=0.6` |
| `donchian-breakout` | Close breaks above the prior channel for the first time | `entry_period=20`, `atr_period=14`, `stop_atr=2.0`, `target_rr=2.0`, `confidence=0.55` |

Both use Wilder's ATR over true ranges:

```text
stop   = close - stop_atr * ATR
target = close + target_rr * (close - stop)
```

If ATR is zero, the stop would not be positive, or the geometry is invalid, the
proposer returns `None`. It never emits an invalid idea.

### Custom proposer

```python
from dataclasses import dataclass

from nomicronix.strategy_engine import CandleView, SignalIdea
from nomicronix.strategy_engine.indicators import atr


@dataclass(frozen=True)
class AlwaysLong:
    strategy_id = "always-long"
    min_history = 15

    def propose(self, view: CandleView) -> SignalIdea | None:
        value = atr(view, 14)
        if value is None or value <= 0:
            return None
        entry = view.candle.close
        stop = entry - 2 * value
        return SignalIdea(
            entry_price=entry,
            stop_price=stop,
            target_price=entry + 2 * (entry - stop),
            confidence=0.5,
            rationale="always long fixture",
        )
```

`examples/strategy_models.py` contains a complete `ThreeBarMomentum` example.

## AI extension point

`AIProposer`, `ProposalRequest` and `AIProposal` define the boundary for a future
model-backed proposer. An AI proposal is validated exactly like a rule idea:
positive prices, stop below entry, target above entry, confidence in `[0, 1]`,
non-empty rationale and model id. `AIProposal.to_idea()` then enters the same
scan loop.

v1 ships **no provider and makes no network call**. An AI proposal is still only
a candidate; `risk-engine` remains the decider.

## Data and causality rules

- A scan may read candles before `start_ms` for warm-up, but signals are emitted
  only for candles with `start_ms <= open_time_ms < end_ms`.
- Candles must be strictly ascending and de-duplicated.
- Gaps inside `[start_ms, end_ms)` raise `DataGapError` with the missing ranges.
  The warm-up prefix is best-effort, because an exchange may not have listed the
  symbol early enough.
- A range with fewer than `min_history` candles raises `InsufficientDataError`.
- `created_at_ms` is the decision candle's close, never the wall clock, so reruns
  are reproducible.
- The scan stops at the first candle that would exceed `max_signals` and raises
  `SignalLimitError`; it never returns a truncated batch.

## Validation and rejection

A `SignalIdea` is rejected with a stable warning when:

- entry, stop or target is non-positive;
- `stop >= entry` or `target <= entry`;
- confidence is outside `[0, 1]`;
- the rationale is empty;
- confidence is below `min_confidence`.

Rejected ideas never become candidates. Accepted candidates always satisfy the
long-only geometry above.

## Output

```json
{
  "schema_version": 1,
  "kind": "signals",
  "config": {
    "symbol": "BTC/USDT",
    "timeframe": "1h",
    "start_ms": 1735689600000,
    "end_ms": 1738368000000,
    "min_history": 50,
    "min_confidence": 0.5,
    "ttl_ms": 300000,
    "max_signals": 100
  },
  "data": {
    "symbol": "BTC/USDT",
    "timeframe": "1h",
    "first_candle_ms": 1735689600000,
    "last_candle_ms": 1738364400000,
    "candle_count": 744
  },
  "signals": [
    {
      "signal_id": "sma-cross:BTC/USDT:1h:1735776000000",
      "strategy_id": "sma-cross",
      "symbol": "BTC/USDT",
      "timeframe": "1h",
      "side": "buy",
      "entry_price": "100",
      "stop_price": "95",
      "target_price": "115",
      "confidence": 0.6,
      "rationale": "fast SMA crossed above slow SMA",
      "features": {"atr": "2.5", "fast_sma": "99.5", "slow_sma": "98.1"},
      "created_at_ms": 1735779600000,
      "expires_at_ms": 1735779900000,
      "candle_open_time_ms": 1735776000000
    }
  ],
  "warnings": []
}
```

Prices are JSON strings. Confidence is a JSON number. JSON is written with
`sort_keys=True`, a trailing newline and a temporary file plus atomic replace, so
two identical scans produce byte-identical output.

`signal_id` is `f"{strategy_id}:{symbol}:{timeframe}:{candle_open_time_ms}"`.
`expires_at_ms` is `created_at_ms + ttl_ms` (default 5 minutes), matching the
maximum signal age enforced by `risk-engine`.

## Configuration

`config/strategy.toml` holds scan defaults, not strategy parameters:

```toml
[scan]
min_confidence = "0.5"
ttl_ms = 300000
min_history = 50
max_signals = 100
```

CLI flags override file values. A missing default file falls back to these
defaults; a missing explicit `--config` file is an error. Confidence is parsed as
`Decimal` and converted to `float` only at the model boundary. Malformed TOML,
confidence outside `[0, 1]` and non-positive counts are rejected.

## Guarantees

- **Causal.** `CandleView` slices at the decision index; a spy test proves no
  later candle is reachable.
- **Proposal only.** The module cannot size, approve or place anything.
- **Deterministic.** Same candles, strategy and config produce identical ids,
  ordering and bytes.
- **Explicit failure.** Bad data, bad params and overflowing signal counts fail
  loudly; dropped ideas appear in `warnings`.
- **Reproducible provenance.** Every signal names its strategy, its trigger
  candle and its exact indicator values.

## Known limitations

- No portfolio state: a signal may fire while a position is already open. Risk
  and duplicate rules decide downstream.
- Rule-based confidences are fixed constants, not calibrated probabilities.
- No AI/LLM provider is shipped; only the validated extension point.
- No multi-timeframe confluence, order-book or funding inputs.
- No shorting, leverage or futures.
- Signals are research inputs, never orders and never profit guarantees.

## Development

```bash
uv run pytest tests/strategy_engine \
    --cov=nomicronix.strategy_engine --cov-fail-under=90
uv run ruff check src tests examples
uv run ruff format --check src tests examples
uv run mypy src/nomicronix/strategy_engine
```

Tests use in-memory SQLite stores and synthetic candles. The module has no
network tests and must never contact an exchange.

## Decisions taken for the spec's open questions

| Question | Decision |
|---|---|
| Signal interface | `SignalIdea` from a proposer; the engine adds ids, times and validation |
| Built-in strategies | SMA crossover and Donchian breakout, both ATR-stopped |
| Confidence semantics | Bounded score in `[0, 1]`; never bypasses risk |
| AI integration | Protocol plus validated model only; no provider or network in v1 |
| Signal identity | Deterministic `strategy:symbol:timeframe:candle_open_ms` |
| Signal age | `expires_at_ms = close + ttl`, default 5 minutes |
| Warm-up | `min_history` closed candles before the first signal |
| Multiple signals per candle | Allowed by the contract; built-ins fire on fresh events only |
| Portfolio awareness | None in v1; delegated to `signal-delivery` and `risk-engine` |
| Output | Deterministic JSON plus a human-readable CLI summary |
