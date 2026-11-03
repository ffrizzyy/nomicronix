"""Causal signal generation: the AI proposes, risk-engine decides."""

from nomicronix.strategy_engine.ai import (
    AIProposal,
    AIProposer,
    ProposalRequest,
    request_from_view,
)
from nomicronix.strategy_engine.data import (
    DataError,
    DataGapError,
    InsufficientDataError,
    load_candles,
)
from nomicronix.strategy_engine.engine import (
    ScanError,
    SignalLimitError,
    scan,
    scan_candles,
)
from nomicronix.strategy_engine.models import (
    ProposalError,
    ScanConfig,
    Side,
    SignalBatch,
    SignalCandidate,
    SignalIdea,
)
from nomicronix.strategy_engine.proposers import (
    DonchianBreakout,
    SignalProposer,
    SmaCross,
)
from nomicronix.strategy_engine.views import CandleView

__all__ = [
    "AIProposal",
    "AIProposer",
    "CandleView",
    "DataError",
    "DataGapError",
    "DonchianBreakout",
    "InsufficientDataError",
    "ProposalError",
    "ProposalRequest",
    "ScanConfig",
    "ScanError",
    "Side",
    "SignalBatch",
    "SignalCandidate",
    "SignalIdea",
    "SignalLimitError",
    "SignalProposer",
    "SmaCross",
    "load_candles",
    "request_from_view",
    "scan",
    "scan_candles",
]
