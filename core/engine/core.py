"""Engine facade: loads a target directory, wires poller -> history ->
rules -> callbacks. The only class UI or CLI code needs to touch."""
import glob
import os
import queue
from typing import Any, Callable, List, Set

from ..adapter.base import TargetAdapter
from ..target.flows import FlowSpec, load_flows, needed_registers
from ..target.registers import RegisterModel
from ..target.topology import Topology, load_topology
from .evaluator import Evaluator
from .history import History
from .poller import Poller
from .readplan import build_read_plan
from .rules import EngineUpdate, RuleEngine
from .snapshot import Snapshot


class EngineError(Exception):
    pass


def _one(target_dir: str, pattern: str) -> str:
    hits = sorted(glob.glob(os.path.join(target_dir, pattern)))
    if len(hits) != 1:
        raise EngineError("expected exactly one %s in %s, found %d"
                          % (pattern, target_dir, len(hits)))
    return hits[0]


class Engine:
    def __init__(self, model: RegisterModel, topology: Topology,
                 flowspec: FlowSpec, history: History, poller: Poller,
                 rules: RuleEngine, excluded: List[str],
                 guarded_addrs: Set[int], base_polled: Set[str]):
        self.model = model
        self.topology = topology
        self.flowspec = flowspec
        self.history = history
        self.excluded = excluded
        self.guarded_addrs = guarded_addrs
        self._guarded_addrs = guarded_addrs
        self._base_polled = set(base_polled)
        self._extra: Set[str] = set()
        self.polled: Set[str] = set(self._base_polled)
        self._poller = poller
        self._rules = rules
        self._update_cbs: List[Callable[[EngineUpdate], None]] = []
        poller.on_snapshot(self._handle_snapshot)

    @classmethod
    def load(cls, target_dir: str, adapter: TargetAdapter,
             interval_s: float = 0.02) -> "Engine":
        model = RegisterModel.from_svd(_one(target_dir, "*.svd"))
        history = History()
        evaluator = Evaluator(model, history)
        topology = load_topology(_one(target_dir, "*.topology.yaml"), model)
        flowspec = load_flows(_one(target_dir, "*.flows.yaml"), evaluator,
                              topology)
        needed = needed_registers(flowspec)
        for b in topology.blocks.values():
            if b.select is not None:
                needed.add(model.resolve(b.select).reg_key)
        overlay = set(flowspec.guarded)
        guarded_addrs = set()
        for periph in model.peripherals.values():
            for reg in periph.registers.values():
                key = "%s.%s" % (periph.name, reg.name)
                if reg.read_action is not None or key in overlay:
                    guarded_addrs.add(reg.address)
        excluded = []
        base_polled = set()
        for key in needed:
            rr = model.resolve(key)
            is_guarded = rr.read_action is not None or key in overlay
            if is_guarded and key not in flowspec.force_poll:
                excluded.append(key)
            else:
                base_polled.add(key)
        plan = build_read_plan(base_polled, model,
                               forbidden_addrs=frozenset(guarded_addrs))
        poller = Poller(adapter, plan, interval_s=interval_s)
        rules = RuleEngine(flowspec)
        return cls(model, topology, flowspec, history, poller, rules,
                   sorted(excluded), guarded_addrs, base_polled)

    def start(self) -> None:
        self._poller.start()

    def stop(self) -> None:
        self._poller.stop()

    def on_update(self, cb: Callable[[EngineUpdate], None]) -> None:
        self._update_cbs.append(cb)

    def on_state(self, cb: Callable[[str], None]) -> None:
        self._poller.on_state(cb)

    def clear_badge(self, block_id: str) -> None:
        self._rules.clear_badge(block_id)

    def submit(self, fn: Callable[[TargetAdapter], Any]) -> Any:
        return self._poller.submit(fn)

    def set_watch(self, extra: Set[str]) -> List[str]:
        refused = []
        accepted = set()
        for key in extra:
            rr = self.model.resolve(key)
            is_guarded = rr.read_action is not None \
                or key in set(self.flowspec.guarded)
            if is_guarded and key not in self.flowspec.force_poll:
                refused.append(key)
            else:
                accepted.add(key)
        self._extra = accepted
        self.polled = set(self._base_polled) | accepted
        plan = build_read_plan(self.polled, self.model,
                               forbidden_addrs=frozenset(self._guarded_addrs))
        poller = self._poller

        def swap(_adapter):
            poller.plan = plan
        self._poller.submit(swap)
        return sorted(refused)

    def read_words(self, addr: int, count: int,
                   timeout_s: float = 2.0) -> List[int]:
        return self._exec(lambda ad: ad.read_block32(addr, count),
                          timeout_s)

    def halt(self) -> None:
        self._exec(lambda ad: ad.halt())

    def resume(self) -> None:
        self._exec(lambda ad: ad.resume())

    def _exec(self, fn, timeout_s: float = 2.0):
        q = self._poller.submit(fn)
        try:
            ok, result = q.get(timeout=timeout_s)
        except queue.Empty:
            raise EngineError("command timed out")
        if not ok:
            raise EngineError("command failed: %r" % (result,))
        return result

    def _handle_snapshot(self, snap: Snapshot) -> None:
        for key, sample in snap.values.items():
            self.history.record(key, sample.t, sample.value)
        update = self._rules.process(snap)
        for cb in self._update_cbs:
            cb(update)
