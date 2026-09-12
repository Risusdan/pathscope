"""In-memory adapter for tests: word-addressed dict, scripted failures."""
from typing import Dict, List, Tuple

from .base import TargetAdapter, TargetInfo, TargetLostError


class MockAdapter(TargetAdapter):
    def __init__(self, mem: Dict[int, int]):
        self.mem = dict(mem)
        self.read_log: List[Tuple[int, int]] = []
        self._fail = 0
        self._running = True

    def connect(self) -> TargetInfo:
        return TargetInfo(name="mock", idcode=0x0)

    def disconnect(self) -> None:
        pass

    def read_block32(self, addr: int, count: int) -> List[int]:
        if self._fail > 0:
            self._fail -= 1
            raise TargetLostError("scripted failure")
        self.read_log.append((addr, count))
        return [self.mem.get(addr + 4 * i, 0) for i in range(count)]

    def write32(self, addr: int, value: int) -> None:
        self.mem[addr] = value

    def halt(self) -> None:
        self._running = False

    def resume(self) -> None:
        self._running = True

    def is_running(self) -> bool:
        return self._running

    def set_word(self, addr: int, value: int) -> None:
        self.mem[addr] = value

    def fail_next(self, n: int) -> None:
        self._fail = n
