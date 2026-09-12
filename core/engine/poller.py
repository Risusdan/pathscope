# core/engine/poller.py
"""The single thread that owns the adapter. Everything hardware goes
through here: periodic sweeps, user commands, reconnection."""
import queue
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, List

from ..adapter.base import AdapterError, TargetAdapter
from .readplan import ReadOp
from .snapshot import Sample, Snapshot


class PollerState:
    RUNNING = "running"
    TARGET_LOST = "target_lost"
    STOPPED = "stopped"


class Poller(threading.Thread):
    def __init__(self, adapter: TargetAdapter, plan: List[ReadOp],
                 interval_s: float = 0.02, reconnect_s: float = 0.5):
        super().__init__(daemon=True)
        self.adapter = adapter
        self.plan = plan
        self.interval_s = interval_s
        self.reconnect_s = reconnect_s
        self._commands: "queue.Queue" = queue.Queue()
        self._snapshot_cbs: List[Callable[[Snapshot], None]] = []
        self._state_cbs: List[Callable[[str], None]] = []
        self._stop_evt = threading.Event()
        self._times: Deque[float] = deque(maxlen=600)

    def on_snapshot(self, cb: Callable[[Snapshot], None]) -> None:
        self._snapshot_cbs.append(cb)

    def on_state(self, cb: Callable[[str], None]) -> None:
        self._state_cbs.append(cb)

    def submit(self, fn: Callable[[TargetAdapter], Any]) -> "queue.Queue":
        result: "queue.Queue" = queue.Queue(maxsize=1)
        self._commands.put((fn, result))
        return result

    def stop(self) -> None:
        self._stop_evt.set()
        self.join(timeout=5.0)

    def _emit_state(self, state: str) -> None:
        for cb in self._state_cbs:
            cb(state)

    def _drain_commands(self) -> None:
        while True:
            try:
                fn, result = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                result.put((True, fn(self.adapter)))
            except AdapterError:
                result.put((False, None))
                raise
            except Exception as e:  # command bug: report, keep polling
                result.put((False, e))

    def _fail_pending(self) -> None:
        while True:
            try:
                _fn, result = self._commands.get_nowait()
            except queue.Empty:
                return
            result.put((False, None))

    def _sweep(self) -> Snapshot:
        values = {}
        for op in self.plan:
            words = self.adapter.read_block32(op.addr, op.count)
            t = time.monotonic()
            for key, idx in op.targets:
                values[key] = Sample(words[idx], t)
        now = time.monotonic()
        self._times.append(now)
        cutoff = now - 2.0
        recent = [t for t in self._times if t >= cutoff]
        rate = len(recent) / 2.0 if len(recent) > 1 else 0.0
        return Snapshot(values=values, t=now, rate_hz=rate)

    def run(self) -> None:
        self._emit_state(PollerState.RUNNING)
        while not self._stop_evt.is_set():
            try:
                self._drain_commands()
                snap = self._sweep()
                for cb in self._snapshot_cbs:
                    cb(snap)
            except AdapterError:
                self._emit_state(PollerState.TARGET_LOST)
                while not self._stop_evt.is_set():
                    self._fail_pending()
                    self._stop_evt.wait(self.reconnect_s)
                    try:
                        self.adapter.connect()
                        self._emit_state(PollerState.RUNNING)
                        break
                    except AdapterError:
                        continue
                continue
            self._stop_evt.wait(self.interval_s)
        self._fail_pending()
        self._emit_state(PollerState.STOPPED)
