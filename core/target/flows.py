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
    guarded: List[str] = dfield(default_factory=list)


def _compile(ev: Evaluator, expr: str, where: str) -> CompiledExpr:
    try:
        return ev.compile(expr)
    except ExprError as e:
        raise FlowError("%s: %s" % (where, e))


def _required(mapping, key: str, where: str):
    try:
        return mapping[key]
    except KeyError:
        raise FlowError("%s: missing required key %r" % (where, key))


def load_flows(path: str, evaluator: Evaluator,
               topology: Topology) -> FlowSpec:
    with open(path) as f:
        doc = yaml.safe_load(f)
    activities: List[Activity] = []
    for raw in doc.get("activities", []):
        where = "activity %s" % raw.get("name", "<name?>")
        name = _required(raw, "name", where)
        act_path = _required(raw, "path", where)
        for bid in act_path:
            if bid not in topology.blocks:
                raise FlowError("%s: unknown block %r"
                                % (where, bid))
        active_when = _required(raw, "active_when", where)
        active = _compile(evaluator, active_when,
                          "%s active_when" % where)
        progress = raw.get("progress")
        if progress is not None:
            try:
                progress = evaluator.model.resolve(progress).reg_key
            except SvdError as e:
                raise FlowError("%s progress: %s" % (where, e))
        rules: List[Rule] = []
        for r in raw.get("anomalies", []):
            rule_expr = _required(r, "rule", where)
            msg = _required(r, "msg", where)
            target = _required(r, "target", where)
            if target not in topology.blocks:
                raise FlowError("%s rule target unknown: %r"
                                % (where, target))
            rules.append(Rule(
                expr=rule_expr, msg=msg, target=target,
                compiled=_compile(evaluator, rule_expr,
                                  "%s rule" % where)))
        activities.append(Activity(
            name=name, path=act_path, active_when=active_when,
            active_compiled=active, progress=progress, rules=rules))
    force = []
    for ref in (doc.get("poll", {}) or {}).get("force_poll", []) or []:
        try:
            force.append(evaluator.model.resolve(ref).reg_key)
        except SvdError as e:
            raise FlowError("force_poll: %s" % e)
    guarded = []
    for ref in (doc.get("poll", {}) or {}).get("guarded", []) or []:
        try:
            guarded.append(evaluator.model.resolve(ref).reg_key)
        except SvdError as e:
            raise FlowError("guarded: %s" % e)
    return FlowSpec(activities=activities, force_poll=force,
                    guarded=guarded)


def needed_registers(spec: FlowSpec) -> Set[str]:
    keys: Set[str] = set()
    for a in spec.activities:
        keys |= a.active_compiled.refs
        if a.progress is not None:
            keys.add(a.progress)
        for r in a.rules:
            keys |= r.compiled.refs
    return keys
