# backend/tradebook/source.py
from collections.abc import Iterable
from typing import Protocol, runtime_checkable
from .domain import ClosedTradePacket


@runtime_checkable
class TradeSource(Protocol):
    source_key: str

    def iter_closes(self) -> Iterable[ClosedTradePacket]: ...
