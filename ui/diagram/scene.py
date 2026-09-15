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


def _noop_handle(_handle: object) -> None:
    pass


def _noop_status(_msg: str) -> None:
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
        # M8 wave 2 (Visio-style connector glue): fires on EVERY
        # position change of an editable block DURING a drag (before
        # release/commit) - BlockItem.itemChange on
        # ItemPositionHasChanged. MainWindow uses this to live-reroute
        # pointless wires attached to the moving block and to glue
        # explicit-path endpoints to it; unlike on_geometry_changed
        # this can fire many times per gesture, so it carries the
        # moved block's id rather than nothing.
        self.on_block_live_moved: Callable[[str], None] = _noop
        # M8 wave B3 (live coordinate readout): fired with a one-line
        # status string on every live move/resize step of a block,
        # waypoint handle, or the legend in edit mode - "" clears it
        # (on release). Kept deliberately generic (a plain string, not
        # a structured payload) since MainWindow's only job is to
        # forward it to statusBar().showMessage() - see
        # MainWindow._on_live_status.
        self.on_live_status: Callable[[str], None] = _noop_status
        # Acceptance round 8 (endpoint neighbor alignment): fired with
        # the dragged WaypointHandle on every live move step so
        # MainWindow can draw the same guide lines a block drag gets -
        # the handle carries its own _active_guides tuple, exactly
        # like a BlockItem does for wave B4.
        self.on_handle_live_moved: Callable[[object], None] = _noop_handle
        # M8 wave B4 (dynamic alignment guides): live id -> BlockItem
        # registry, populated by build_scene below - lets a dragged
        # BlockItem's itemChange check its edges/centers against every
        # OTHER block's, for alignment-snap, without its own
        # blocks-dict plumbing (the same problem WireItem's
        # refresh_auto_route(blocks) solves a different way - this one
        # is read-many/write-once-at-startup, so a plain state field
        # populated eagerly is simpler than a per-caller pass-through).
        self.blocks: Dict[str, "BlockItem"] = {}


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
    state.blocks = blocks   # M8 wave B4: see DiagramState.blocks' comment

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
