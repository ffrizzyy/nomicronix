"""The causal strategy protocol and immutable history view."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Protocol, overload

from nomicronix.backtesting.models import Signal
from nomicronix.market_data.models import Candle


class Strategy(Protocol):
    def on_candle(self, candle: Candle, history: Sequence[Candle]) -> Signal | None:
        """Return a signal at this candle's close, or None.

        `history` ends at `candle`; it never contains a later candle. Keep state
        inside the instance and construct a fresh instance for each run.
        """
        ...


StrategyFactory = Callable[[Mapping[str, object]], Strategy]


class HistoryView(Sequence[Candle]):
    """A read-only window over the full candle list, ending at the current index."""

    __slots__ = ("_candles", "_end")

    def __init__(self, candles: Sequence[Candle], end: int) -> None:
        if not 0 <= end <= len(candles):
            raise ValueError("end must be inside the candle list")
        self._candles = candles
        self._end = end

    def __len__(self) -> int:
        return self._end

    @overload
    def __getitem__(self, index: int) -> Candle: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[Candle, ...]: ...

    def __getitem__(self, index: int | slice) -> Candle | tuple[Candle, ...]:
        if isinstance(index, slice):
            return tuple(self._candles[: self._end][index])
        if index < 0:
            index += self._end
        if not 0 <= index < self._end:
            raise IndexError(index)
        return self._candles[index]
