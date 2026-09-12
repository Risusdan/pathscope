"""Loads <target>.flows.yaml: activities (data paths with liveness
conditions) and anomaly rules. Everything compiles at load time so a
typo dies at startup, not mid-debug-session."""
from dataclasses import dataclass, field as dfield
from typing import List, Optional, Set

import yaml

from ..engine.evaluator import CompiledExpr, Evaluator, ExprError
from .registers import SvdError
from .topology import Topology


class FlowError(Exception):
    pass


@dataclass
class Rule:
    expr: str
    msg: str
    target: str
    compiled: CompiledExpr


@dataclass
class Activity:
    name: str
    path: List[str]
    active_when: str
    active_compiled: CompiledExpr
    progress: Optional[str]
    rules: List[Rule] = dfield(default_factory=list)


@dataclass
class FlowSpec:
    activities: List[Activity]
    force_poll: List[str] = dfield(default_factory=list)


def _compile(ev: Evaluator, expr: str, where: str) -> CompiledExpr:
    try:
        return ev.compile(expr)
    except ExprError as e:
        raise FlowError("%s: %s" % (where, e))


def load_flows(path: str, evaluator: Evaluator,
               topology: Topology) -> FlowSpec:
    with open(path) as f:
        doc = yaml.safe_load(f)
    activities: List[Activity] = []
    for raw in doc.get("activities", []):
        name = raw["name"]
        act_path = raw["path"]
        for bid in act_path:
            if bid not in topology.blocks:
                raise FlowError("activity %s: unknown block %r"
                                % (name, bid))
        active = _compile(evaluator, raw["active_when"],
                          "activity %s active_when" % name)
        progress = raw.get("progress")
        if progress is not None:
            try:
                progress = evaluator.model.resolve(progress).reg_key
            except SvdError as e:
                raise FlowError("activity %s progress: %s" % (name, e))
        rules: List[Rule] = []
        for r in raw.get("anomalies", []):
            target = r["target"]
            if target not in topology.blocks:
                raise FlowError("activity %s rule target unknown: %r"
                                % (name, target))
            rules.append(Rule(
                expr=r["rule"], msg=r["msg"], target=target,
                compiled=_compile(evaluator, r["rule"],
                                  "activity %s rule" % name)))
        activities.append(Activity(
            name=name, path=act_path, active_when=raw["active_when"],
            active_compiled=active, progress=progress, rules=rules))
    force = []
    for ref in (doc.get("poll", {}) or {}).get("force_poll", []) or []:
        try:
            force.append(evaluator.model.resolve(ref).reg_key)
        except SvdError as e:
            raise FlowError("force_poll: %s" % e)
    return FlowSpec(activities=activities, force_poll=force)


def needed_registers(spec: FlowSpec) -> Set[str]:
    keys: Set[str] = set()
    for a in spec.activities:
        keys |= a.active_compiled.refs
        if a.progress is not None:
            keys.add(a.progress)
        for r in a.rules:
            keys |= r.compiled.refs
    return keys
