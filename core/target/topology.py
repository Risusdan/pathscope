"""Loads <target>.topology.yaml: the blocks and directed edges of the
data-path diagram. Pure structure - no live semantics here."""
from dataclasses import dataclass, field as dfield
from typing import Dict, List, Optional, Tuple

import yaml

from .registers import RegisterModel, SvdError

KINDS = {"peripheral", "dma", "memory", "cpu", "interconnect", "mux", "pin"}


class TopologyError(Exception):
    pass


@dataclass
class Block:
    id: str
    kind: str
    title: str
    svd: Optional[str] = None
    select: Optional[str] = None
    base: Optional[int] = None
    size: Optional[int] = None
    ports: Dict[str, Tuple[str, float]] = dfield(default_factory=dict)
    x: Optional[int] = None
    y: Optional[int] = None
    w: Optional[int] = None
    h: Optional[int] = None


@dataclass
class Edge:
    id: str
    src: str
    dst: str
    via: Optional[str] = None
    label: Optional[str] = None
    when_select: Optional[int] = None
    src_port: Optional[str] = None
    dst_port: Optional[str] = None
    points: List[Tuple[int, int]] = dfield(default_factory=list)


@dataclass
class Topology:
    """blocks/edges plus the optional layout.legend position (M8:
    the GUI's draggable legend marker - UI geometry, not a block)."""
    blocks: Dict[str, Block]
    edges: List[Edge]
    legend: Optional[Tuple[int, int]] = None


def load_topology(path: str, model: RegisterModel) -> Topology:
    with open(path) as f:
        doc = yaml.safe_load(f)
    blocks: Dict[str, Block] = {}
    for raw in doc.get("blocks", []):
        bid = raw["id"]
        if bid in blocks:
            raise TopologyError("duplicate block id: %s" % bid)
        kind = raw["kind"]
        if kind not in KINDS:
            raise TopologyError("block %s: unknown kind %r" % (bid, kind))
        svd = raw.get("svd")
        if svd is not None and svd not in model.peripherals:
            raise TopologyError("block %s: unknown svd peripheral %r"
                                % (bid, svd))
        select = raw.get("select")
        if select is not None:
            try:
                model.resolve(select)
            except SvdError as e:
                raise TopologyError("block %s: bad select: %s" % (bid, e))
        ports = {name: (side_frac[0], float(side_frac[1]))
                 for name, side_frac in (raw.get("ports") or {}).items()}
        blocks[bid] = Block(
            id=bid, kind=kind, title=raw.get("title", bid.upper()),
            svd=svd, select=select, base=raw.get("base"),
            size=raw.get("size"), ports=ports)
    edges: List[Edge] = []
    for i, raw in enumerate(doc.get("edges", [])):
        src, dst = raw["from"], raw["to"]
        for end in (src, dst):
            if end not in blocks:
                raise TopologyError("edge %d: unknown block %r" % (i, end))
        via = raw.get("via")
        if via is not None and via not in blocks:
            raise TopologyError("edge %d: unknown via %r" % (i, via))
        points = [(int(px), int(py)) for px, py in raw.get("points", [])]
        edges.append(Edge(
            id="%s->%s#%d" % (src, dst, i), src=src, dst=dst, via=via,
            label=raw.get("label"), when_select=raw.get("when_select"),
            src_port=raw.get("from_port"), dst_port=raw.get("to_port"),
            points=points))
    layout = doc.get("layout") or {}
    legend_pos = layout.get("legend")
    legend = ((int(legend_pos["x"]), int(legend_pos["y"]))
             if legend_pos is not None else None)
    for bid, pos in layout.items():
        if bid == "legend":
            continue
        if bid not in blocks:
            raise TopologyError("layout: unknown block %r" % bid)
        b = blocks[bid]
        b.x, b.y = int(pos["x"]), int(pos["y"])
        if "w" in pos:
            b.w = int(pos["w"])
        if "h" in pos:
            b.h = int(pos["h"])
    return Topology(blocks=blocks, edges=edges, legend=legend)
