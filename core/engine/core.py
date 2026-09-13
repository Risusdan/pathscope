"""Engine facade: loads a target directory, wires poller -> history ->
rules -> callbacks. The only class UI or CLI code needs to touch."""
import glob
import os
import queue
from typing import Any, Callable, Dict, List, Set, Union

from ..adapter.base import TargetAdapter
from ..target.flows import FlowSpec, load_flows, needed_registers
from ..target.registers import RegisterModel
from ..target.topology import Topology, load_topology
from .evaluator import Evaluator
from .history import History
from .poller import Poller, PollerState
from .readplan import ReadOp, build_read_plan
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
        self._addr_watches: Dict[str, int] = {}
        self.addr_watch_labels: Dict[str, str] = {}
        self.polled: Set[str] = set(self._base_polled)
        self._poller = poller
        self._rules = rules
        self._update_cbs: List[Callable[[EngineUpdate], None]] = []
        poller.on_snapshot(self._handle_snapshot)
        poller.on_state(self._on_poller_state)

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
        entries = {k: model.resolve(k).address for k in base_polled}
        plan = build_read_plan(entries,
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

    @property
    def read_ops(self) -> int:
        """Number of block-read transactions the current sweep issues
        - len(poller.plan), one entry per contiguous block
        build_read_plan (readplan.py) merged the polled registers/
        addr-watches into. A scattered address far from everything
        else in the plan costs one whole read op by itself; addresses
        that pack into the same struct (or otherwise sit within
        merge_gap_words of each other) share one.

        Reading a list reference's len() here is GIL-safe without a
        lock: the poller thread only ever rebinds self._poller.plan
        whole (_swap_plan's inner swap() does `poller.plan = plan`, a
        single assignment - see also Poller.__init__'s `self.plan =
        plan`) and never mutates the existing list object in place
        (poller.py's _sweep() only iterates it with `for op in
        self.plan`; grepping the codebase turns up no .append or
        item-assignment against poller.plan anywhere). So a concurrent
        _swap_plan can only ever be observed as either the old list
        object or the new one, never a half-built one - this property
        may be read from any thread. If that ever changes (e.g. the
        plan becomes mutated in place instead of rebound), this
        property would need its own lock."""
        return len(self._poller.plan)

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
        self._recompute_polled()
        self._swap_plan()
        return sorted(refused)

    def add_addr_watch(self, addr: int, label: str) -> str:
        """Register a 32-bit word watch at a fixed address, keyed by
        the synthetic "@%08X" % addr key used everywhere else (snapshot
        values, History). Idempotent for the same addr: calling it
        again just re-sets the label. Refuses out-of-range, guarded,
        or misaligned addresses.

        Builds the post-add dict first and only then rebinds
        self._addr_watches and self.polled - same build-then-rebind
        pattern remove_addr_watch uses below, for the same reason:
        each is a single, whole-object assignment (atomic under the
        GIL) rather than a mutation of the existing dict/set in place,
        so a concurrent _swap_plan -> _build_plan never sees the two
        fall out of sync."""
        if not (0 <= addr <= 0xFFFFFFFC):
            raise EngineError("address out of 32-bit range: %#x" % addr)
        if addr % 4 != 0:
            raise EngineError(
                "address 0x%08X is not word-aligned" % addr)
        if addr in self.guarded_addrs:
            raise EngineError("address 0x%08X is guarded" % addr)
        key = "@%08X" % addr
        new_watches = dict(self._addr_watches)
        new_watches[key] = addr
        new_polled = self._polled_union(new_watches)
        self._addr_watches = new_watches
        self.polled = new_polled
        self.addr_watch_labels[key] = label
        self._swap_plan()
        return key

    def remove_addr_watch(self, addr_or_key: Union[int, str]) -> None:
        """Popping the key from self._addr_watches and then recomputing
        self.polled as two separate steps leaves a window, visible to
        the poller thread's _on_poller_state -> _swap_plan ->
        _build_plan, where self.polled (not yet reassigned) still
        carries this "@" key after self._addr_watches has already
        lost it - _build_plan then falls through to
        model.resolve("@XXXXXXXX"), which raises SvdError. Avoid that
        by computing the post-removal dict/set first and only then
        rebinding self._addr_watches and self.polled (each a single,
        whole-object assignment - atomic under the GIL, unlike
        mutating the existing dict/set in place) before touching the
        plan."""
        if isinstance(addr_or_key, int):
            key = "@%08X" % addr_or_key
        else:
            key = addr_or_key
        new_watches = dict(self._addr_watches)
        new_watches.pop(key, None)
        new_polled = self._polled_union(new_watches)
        self._addr_watches = new_watches
        self.polled = new_polled
        self.addr_watch_labels.pop(key, None)
        self._swap_plan()

    def _polled_union(self, addr_watches: Dict[str, int]) -> Set[str]:
        """The one place the "what should be polled" union formula is
        written - base registers, set_watch()'s extras, and the given
        addr-watch keys - shared by _recompute_polled() and both
        add_addr_watch()/remove_addr_watch() so the three call sites
        can never drift apart."""
        return (set(self._base_polled) | self._extra
               | set(addr_watches.keys()))

    def _recompute_polled(self) -> None:
        self.polled = self._polled_union(self._addr_watches)

    def _build_plan(self) -> List[ReadOp]:
        entries: Dict[str, int] = {}
        for key in self.polled:
            addr = self._addr_watches.get(key)
            if addr is None:
                if key.startswith("@"):
                    continue  # defensive: see remove_addr_watch
                addr = self.model.resolve(key).address
            entries[key] = addr
        return build_read_plan(
            entries, forbidden_addrs=frozenset(self._guarded_addrs))

    def _swap_plan(self) -> None:
        """Rebuild the read plan from the current self.polled and
        submit it as a poller command. Used both by set_watch() (and
        add_addr_watch()/remove_addr_watch()) and by
        _on_poller_state() below - always rebuilds from self.polled
        (the engine's source of truth for what should be watched)
        rather than closing over a plan computed earlier, so a
        re-swap is idempotent: replaying it against an already-current
        plan is harmless."""
        plan = self._build_plan()
        poller = self._poller

        def swap(_adapter):
            poller.plan = plan
        self._poller.submit(swap)

    def _on_poller_state(self, state: str) -> None:
        """A set_watch() swap command can be silently discarded: if
        the adapter drops between the command being queued and the
        poller draining it, the reconnect path's _fail_pending()
        drains-and-fails every queued command (poller.py) without
        ever applying it, leaving poller.plan stale while self.polled
        (and set_watch()'s return value) claim the swap took effect.
        Rebuilding and re-submitting the plan on every transition back
        to RUNNING - including the very first one, at startup - closes
        that window regardless of whether the original swap survived."""
        if state == PollerState.RUNNING:
            self._swap_plan()

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
