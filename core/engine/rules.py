"""Turns snapshots into flow states and latched anomaly events."""
from dataclasses import dataclass, field as dfield
from typing import Dict, List, Optional

from ..target.flows import FlowSpec
from .snapshot import Snapshot


@dataclass
class RuleState:
    expr: str
    msg: str
    target: str
    firing: bool


@dataclass
class FlowState:
    name: str
    active: bool
    progress: Optional[int]
    rules: List[RuleState] = dfield(default_factory=list)


@dataclass
class AnomalyEvent:
    t: float
    flow: str
    msg: str
    target: str


@dataclass
class BadgeState:
    count: int = 0
    active: bool = False


@dataclass
class EngineUpdate:
    snapshot: Snapshot
    flows: Dict[str, FlowState]
    events: List[AnomalyEvent]
    badges: Dict[str, BadgeState]


class RuleEngine:
    def __init__(self, spec: FlowSpec):
        self.spec = spec
        self._prev: Dict[str, bool] = {}
        self._badges: Dict[str, BadgeState] = {}

    def clear_badge(self, block_id: str) -> None:
        if block_id in self._badges:
            self._badges[block_id].count = 0

    def process(self, snap: Snapshot) -> EngineUpdate:
        flows: Dict[str, FlowState] = {}
        events: List[AnomalyEvent] = []
        for b in self._badges.values():
            b.active = False
        for act in self.spec.activities:
            active = act.active_compiled.eval(snap) is True
            progress = None
            if act.progress is not None:
                progress = snap.value(act.progress)
            states: List[RuleState] = []
            for i, rule in enumerate(act.rules):
                firing = rule.compiled.eval(snap) is True
                key = "%s#%d" % (act.name, i)
                if firing and not self._prev.get(key, False):
                    badge = self._badges.setdefault(rule.target,
                                                    BadgeState())
                    badge.count += 1
                    events.append(AnomalyEvent(
                        t=snap.t, flow=act.name, msg=rule.msg,
                        target=rule.target))
                if firing:
                    self._badges.setdefault(rule.target,
                                            BadgeState()).active = True
                self._prev[key] = firing
                states.append(RuleState(rule.expr, rule.msg, rule.target,
                                        firing))
            flows[act.name] = FlowState(act.name, active, progress, states)
        return EngineUpdate(snapshot=snap, flows=flows, events=events,
                            badges={k: BadgeState(v.count, v.active)
                                    for k, v in self._badges.items()})
