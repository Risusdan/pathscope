"""Probe/transport abstraction. Everything above this layer sees only
addresses and 32-bit words."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List


class AdapterError(Exception):
    """Any adapter-level failure."""


class TargetLostError(AdapterError):
    """Target unreachable (unplugged, reset, powered off)."""


@dataclass
class TargetInfo:
    name: str
    idcode: int


class TargetAdapter(ABC):
    @abstractmethod
    def connect(self) -> TargetInfo: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def read_block32(self, addr: int, count: int) -> List[int]: ...

    @abstractmethod
    def write32(self, addr: int, value: int) -> None: ...

    @abstractmethod
    def halt(self) -> None: ...

    @abstractmethod
    def resume(self) -> None: ...

    @abstractmethod
    def is_running(self) -> bool: ...
