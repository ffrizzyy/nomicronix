"""Write a fresh proposal for a manual risk check.

The committed `examples/proposal.json` is a schema sample. Its `created_at_ms` is
fixed, so the default 5-minute signal-age rule will reject it. This helper stamps
the current time so the example can be checked against a live clock.

Usage:
    uv run python examples/make_proposal.py [output.json]
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

from nomicronix.risk_engine.models import utc_now_ms

DEFAULT_OUTPUT = Path("data/proposal.json")


def build_proposal(now_ms: int) -> dict[str, object]:
    return {
        "signal_id": f"sig-{now_ms}",
        "strategy_id": "sma-cross",
        "symbol": "BTC/USDT",
        "side": "buy",
        "entry_price": str(Decimal("100")),
        "stop_price": str(Decimal("95")),
        "target_price": str(Decimal("115")),
        "confidence": 0.8,
        "created_at_ms": now_ms,
        "instrument": {
            "symbol": "BTC/USDT",
            "quantity_step": "0.00001",
            "min_quantity": "0.00001",
            "min_notional": "10",
        },
    }


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    destination = Path(arguments[0]) if arguments else DEFAULT_OUTPUT
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(build_proposal(utc_now_ms()), indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
