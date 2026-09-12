"""DiagramState and the scene builder that turns a Topology into
BlockItem/WireItem QGraphicsItems.

Nothing here imports the engine (core.engine.*) - only the topology
dataclasses. DiagramState is the mutable holder the items read at paint
time; the main window (a later task) owns one instance, fills it in
from EngineUpdate, and calls .update() on the affected items."""
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import QGraphicsScene

from core.target.topology import Block, Edge, Topology

from .items import BlockItem, WireItem


@dataclass
class BadgeState:
    count: int = 0
    active: bool = False


def _noop(_id: str) -> None:
    pass


class DiagramState:
    """Mutable paint-time state read by BlockItem/WireItem/LegendItem."""

    def __init__(self):
        self.tinted: bool = True
        self.dash_phase: float = 0.0
        self.chsel_value: Optional[int] = None
        self.selected_block: Optional[str] = None
        self.flow_blocks: Set[str] = set()
        self.flow_edges: Set[str] = set()
        self.active_edges: Set[str] = set()
        self.badges: Dict[str, BadgeState] = {}
        self.progress_text: Dict[str, str] = {}
        self.on_block_clicked: Callable[[str], None] = _noop
        self.on_badge_clicked: Callable[[str], None] = _noop
        self.on_edge_clicked: Callable[[str], None] = _noop


def _side_point(block: Block, w: float, h: float, side: str,
                frac: float) -> Tuple[float, float]:
    if side == "l":
        return block.x, block.y + h * frac
    if side == "r":
        return block.x + w, block.y + h * frac
    if side == "t":
        return block.x + w * frac, block.y
    return block.x + w * frac, block.y + h   # "b"


def _ranges_overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 and b0 < a1


def _facing_sides(src: Block, sw: float, sh: float, dst: Block, dw: float,
                  dh: float) -> Tuple[str, str]:
    """Which side of src faces dst, and vice versa: horizontal
    preference (right-of-src -> left-of-dst, or the reverse), vertical
    fallback when the blocks' x-ranges overlap."""
    if not _ranges_overlap(src.x, src.x + sw, dst.x, dst.x + dw):
        if src.x + sw <= dst.x:
            return "r", "l"
        return "l", "r"
    if src.y + sh <= dst.y:
        return "b", "t"
    return "t", "b"


def _straight_route(topology: Topology, edge: Edge,
                    blocks: Dict[str, BlockItem]) -> List[QPointF]:
    src_b, dst_b = topology.blocks[edge.src], topology.blocks[edge.dst]
    src_item, dst_item = blocks[edge.src], blocks[edge.dst]
    side_s, side_d = _facing_sides(src_b, src_item.w, src_item.h,
                                   dst_b, dst_item.w, dst_item.h)
    frac_s = frac_d = 0.5
    port = src_b.ports.get(edge.src_port) if edge.src_port else None
    if port:
        side_s, frac_s = port
    port = dst_b.ports.get(edge.dst_port) if edge.dst_port else None
    if port:
        side_d, frac_d = port
    p0 = _side_point(src_b, src_item.w, src_item.h, side_s, frac_s)
    p1 = _side_point(dst_b, dst_item.w, dst_item.h, side_d, frac_d)
    return [QPointF(*p0), QPointF(*p1)]


def build_scene(topology: Topology, state: DiagramState
               ) -> Tuple[QGraphicsScene, Dict[str, BlockItem],
                          Dict[str, WireItem]]:
    scene = QGraphicsScene()
    scene.setBackgroundBrush(QBrush(Qt.white))

    blocks: Dict[str, BlockItem] = {}
    for index, (bid, block) in enumerate(topology.blocks.items()):
        if block.x is None or block.y is None:
            block.x = 40 + 200 * (index % 4)
            block.y = 40 + 140 * (index // 4)
        item = BlockItem(block, state)
        scene.addItem(item)
        blocks[bid] = item

    wires: Dict[str, WireItem] = {}
    for edge in topology.edges:
        if edge.points:
            pts = [QPointF(x, y) for x, y in edge.points]
        else:
            pts = _straight_route(topology, edge, blocks)
        item = WireItem(edge, pts, state)
        scene.addItem(item)
        wires[edge.id] = item

    return scene, blocks, wires
