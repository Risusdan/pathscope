"""DiagramState and the scene builder that turns a Topology into
BlockItem/WireItem QGraphicsItems.

Nothing here imports engine machinery (core.engine.poller/evaluator/
etc.) - only plain dataclasses (topology, and BadgeState below). This
is a Qt-free-of-engine boundary, not a no-import-from-core-engine-at-all
one: BadgeState is imported from core.engine.rules to avoid a second,
drifting copy (see its own definition there) rather than redefined
here, since it carries no polling/threading coupling. DiagramState is
the mutable holder the items read at paint time; the main window (a
later task) owns one instance, fills it in from EngineUpdate, and calls
.update() on the affected items."""
from typing import Callable, Dict, Optional, Set, Tuple

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import QGraphicsScene

from core.engine.rules import BadgeState
from core.target.topology import Topology

from .items import BlockItem, WireItem, _straight_route_points


def _noop(_id: str) -> None:
    pass


def _noop_geometry() -> None:
    pass


class DiagramState:
    """Mutable paint-time state read by BlockItem/WireItem/LegendItem."""

    def __init__(self):
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
        # M8 layout edit mode (task 3): edit_mode gates click routing
        # (inspector/flow-select clicks are suppressed while editing);
        # on_geometry_changed fires once per completed drag/resize
        # gesture (mouse release), after the geometry has already been
        # applied - MainWindow (task 5) hooks this for undo snapshots
        # and the unsaved marker. legend_pos is the position build_scene
        # read from Topology.legend (None -> LegendItem's own fallback).
        self.edit_mode: bool = False
        self.on_geometry_changed: Callable[[], None] = _noop_geometry
        self.legend_pos: Optional[Tuple[int, int]] = None


def build_scene(topology: Topology, state: DiagramState
               ) -> Tuple[QGraphicsScene, Dict[str, BlockItem],
                          Dict[str, WireItem]]:
    scene = QGraphicsScene()
    scene.setBackgroundBrush(QBrush(Qt.white))
    state.legend_pos = topology.legend

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
            pts = _straight_route_points(blocks[edge.src], blocks[edge.dst],
                                         edge)
        item = WireItem(edge, pts, state)
        scene.addItem(item)
        wires[edge.id] = item

    return scene, blocks, wires
