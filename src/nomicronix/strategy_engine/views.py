"""Causal, read-only candle views for proposers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import overload

from nomicronix.market_data.models import Candle


class CandleView(Sequence[Candle]):
    """A read-only window over a candle list, ending at the decision candle.

    Slicing returns a tuple, so a proposer cannot reach a later candle through
    ordinary indexing. The wrapped list is private to the engine.
    """

    __slots__ = ("_candles", "_end")

    def __init__(self, candles: Sequence[Candle], end: int) -> None:
        if not 0 <= end <= len(candles):
            raise ValueError("end must be inside the candle list")
        self._candles = candles
        self._end = end

    @property
    def candle(self) -> Candle:
        """The decision candle: the last visible candle."""
        if self._end == 0:
            raise ValueError("empty candle view")
        return self._candles[self._end - 1]

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
