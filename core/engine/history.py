"""Per-register ring history: powers stalled()/changed() and sparklines."""
import threading
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple


class History:
    def __init__(self, window_s: float = 10.0):
        self.window_s = window_s
        self._buf: Dict[str, Deque[Tuple[float, int]]] = {}
        self._last_change_t: Dict[str, float] = {}
        self._first_t: Dict[str, float] = {}
        self._count: Dict[str, int] = {}
        self._prev_value: Dict[str, int] = {}
        self._lock = threading.Lock()

    def record(self, reg_key: str, t: float, value: int) -> None:
        with self._lock:
            buf = self._buf.setdefault(reg_key, deque())
            if buf:
                self._prev_value[reg_key] = buf[-1][1]
                if buf[-1][1] != value:
                    self._last_change_t[reg_key] = t
            if reg_key not in self._first_t:
                self._first_t[reg_key] = t
            self._count[reg_key] = self._count.get(reg_key, 0) + 1
            buf.append((t, value))
            while buf and buf[0][0] < t - self.window_s:
                buf.popleft()

    def last_change_age(self, reg_key: str, now: float) -> Optional[float]:
        with self._lock:
            if self._count.get(reg_key, 0) < 2:
                return None
            anchor = self._last_change_t.get(reg_key, self._first_t[reg_key])
            return now - anchor

    def changed_on_last(self, reg_key: str) -> bool:
        with self._lock:
            if self._count.get(reg_key, 0) < 2:
                return False
            return self._buf[reg_key][-1][1] != self._prev_value[reg_key]

    def series(self, reg_key: str) -> List[Tuple[float, int]]:
        with self._lock:
            return list(self._buf.get(reg_key, []))
