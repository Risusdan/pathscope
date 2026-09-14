"""M8 task 3/4: edit-mode input routing on diagram items - drag, snap,
resize, waypoint editing on wires. Built on the same construction
pattern as test_scene.py (Engine.load -> build_scene), driving
BlockItem/WireItem/WaypointHandle/LegendItem's own event handlers
directly rather than going through Qt's event loop (offscreen
platform, no real mouse)."""
import dataclasses
import shutil

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QImage, QPainter

from core.adapter.mock import MockAdapter
from core.engine.core import Engine
from core.target.layout_io import LayoutPatchError
from core.target.topology import Edge, load_topology
from ui.bridge import EngineBridge
from ui.demo import make_demo_engine
from ui.diagram.items import LegendItem, WireItem, snap
from ui.diagram.scene import DiagramState, build_scene
from ui.main_window import MainWindow


class _FakeEvent:
    """Minimal stand-in for QGraphicsSceneMouseEvent: the handlers
    under test only ever call .accept()/.pos()/.scenePos()."""

    def __init__(self, pos=None, scene_pos=None):
        self._pos = pos if pos is not None else QPointF(0, 0)
        self._scene_pos = scene_pos if scene_pos is not None else self._pos
        self.accepted = False

    def pos(self):
        return self._pos

    def scenePos(self):
        return self._scene_pos

    def accept(self):
        self.accepted = True


class _FakeKeyEvent:
    """Minimal stand-in for QKeyEvent: WaypointHandle.keyPressEvent
    only ever calls .key()/.accept()."""

    def __init__(self, key):
        self._key = key
        self.accepted = False

    def key(self):
        return self._key

    def accept(self):
        self.accepted = True


def _build(qtbot):
    engine = Engine.load("targets/f411", MockAdapter({}))
    state = DiagramState()
    scene, blocks, wires = build_scene(engine.topology, state)
    return engine, state, scene, blocks, wires


def _make_wire(points=None, label="L"):
    """A standalone WireItem/Edge/DiagramState triple, not routed
    through build_scene - gives waypoint tests direct control over
    the initial path. `points=None` stands in for a pointless
    (auto-routed) edge: self.pts starts as a plain 2-point line, the
    same shape build_scene's _straight_route would hand WireItem."""
    state = DiagramState()
    pts_list = list(points) if points else []
    edge = Edge(id="e", src="a", dst="b", label=label, points=pts_list)
    if points:
        pts = [QPointF(x, y) for x, y in points]
    else:
        pts = [QPointF(0, 0), QPointF(100, 0)]
    wire = WireItem(edge, pts, state)
    return wire, state, edge


def _paint_wire(wire):
    """Actually renders the wire (not just exercises its data-model
    methods) by handing it a real QPainter on an offscreen QImage.
    This is the only way to reproduce the crash class a fix-round
    review found: WireItem.paint()'s arrowhead code indexes
    self.pts[-2]/self.pts[-1], which raises IndexError for a
    transient invalid (sub-2-point) state that a data-only assertion
    between deletes would never exercise."""
    img = QImage(200, 200, QImage.Format_ARGB32)
    painter = QPainter(img)
    try:
        wire.paint(painter, None)
    finally:
        painter.end()


# -- snap() -------------------------------------------------------------


def test_snap_rounds_to_10_unit_grid_by_default():
    assert snap(103, False) == 100
    assert snap(118, False) == 120
    assert snap(24, False) == 20


def test_snap_rounds_to_1_unit_when_fine():
    assert snap(103, True) == 103
    assert snap(4.6, True) == 5


# -- DiagramState defaults -----------------------------------------------


def test_diagram_state_edit_defaults():
    state = DiagramState()
    assert state.edit_mode is False
    assert state.legend_pos is None
    state.on_geometry_changed()   # must not raise - default is a no-op


# -- BlockItem drag: snap on release, block.x/y written back ------------


def test_edit_mode_drag_snaps_and_updates_block(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["adc1"]
    calls = []
    state.on_geometry_changed = lambda: calls.append(
        (item.block.x, item.block.y))

    state.edit_mode = True
    item.set_editable(True)
    item.setPos(103, 118)              # programmatic move; itemChange snaps
    assert (item.pos().x(), item.pos().y()) == (100, 120)

    item.mouseReleaseEvent(_FakeEvent())

    assert (item.block.x, item.block.y) == (100, 120)
    assert calls == [(100, 120)]       # fired once, after the write-back


def test_edit_mode_drag_snaps_a_second_block(qtbot):
    # Sanity check on a different block/offset than the first drag
    # test. (The Shift-held -> 1-unit path is exercised directly
    # through the pure snap() helper above - QApplication.
    # keyboardModifiers() reports NoModifier in an offscreen test
    # session since nothing is actually pressed.)
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["dma2"]
    state.edit_mode = True
    item.set_editable(True)
    item.setPos(331, 322)
    assert (item.pos().x(), item.pos().y()) == (330, 320)


def test_set_editable_false_stops_snapping_and_writeback(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["adc1"]
    orig = (item.block.x, item.block.y)
    calls = []
    state.on_geometry_changed = lambda: calls.append(1)

    item.setPos(103, 118)              # not editable: no snap
    assert (item.pos().x(), item.pos().y()) == (103, 118)
    item.mouseReleaseEvent(_FakeEvent())   # not editable: no write-back

    assert (item.block.x, item.block.y) == orig
    assert calls == []


# -- click routing: inspector callback vs. edit mode ---------------------


def test_normal_mode_click_hits_inspector_edit_mode_does_not(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["cm4"]
    clicks = []
    state.on_block_clicked = clicks.append

    item.mousePressEvent(_FakeEvent())
    assert clicks == ["cm4"]

    state.edit_mode = True
    item.set_editable(True)
    item.mousePressEvent(_FakeEvent())
    assert clicks == ["cm4"]           # unchanged - no new call in edit mode


def test_normal_mode_wire_click_hits_callback_edit_mode_does_not(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    wire = next(iter(wires.values()))
    clicks = []
    state.on_edge_clicked = clicks.append

    wire.mousePressEvent(_FakeEvent())
    assert clicks == [wire.edge.id]

    state.edit_mode = True
    wire.mousePressEvent(_FakeEvent())
    assert clicks == [wire.edge.id]    # unchanged


# -- resize handle: visibility, min-clamp, snap --------------------------


def test_resize_handle_visible_only_when_editable(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["adc1"]
    assert item.handle.isVisible() is False

    item.set_editable(True)
    assert item.handle.isVisible() is True

    item.set_editable(False)
    assert item.handle.isVisible() is False


def test_resize_snaps_to_grid(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["adc1"]              # starts at w=140, h=80 (f411 yaml)
    calls = []
    state.on_geometry_changed = lambda: calls.append((item.block.w,
                                                       item.block.h))
    item.set_editable(True)

    item.handle.mousePressEvent(_FakeEvent(scene_pos=QPointF(0, 0)))
    item.handle.mouseMoveEvent(_FakeEvent(scene_pos=QPointF(23, 23)))
    item.handle.mouseReleaseEvent(_FakeEvent())

    assert (item.block.w, item.block.h) == (160, 100)
    assert (item.w, item.h) == (160, 100)
    assert calls == [(160, 100)]


def test_resize_clamps_to_minimum(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["adc1"]
    item.set_editable(True)

    item.handle.mousePressEvent(_FakeEvent(scene_pos=QPointF(0, 0)))
    item.handle.mouseMoveEvent(_FakeEvent(scene_pos=QPointF(-1000, -1000)))
    item.handle.mouseReleaseEvent(_FakeEvent())

    assert (item.block.w, item.block.h) == (30, 24)


# -- gesture no-op guard: a click-only press+release (no movement) must
# -- not fire on_geometry_changed; a real move still fires exactly once.


def test_block_click_only_press_release_fires_nothing(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["adc1"]
    calls = []
    state.on_geometry_changed = lambda: calls.append(1)
    item.set_editable(True)

    item.mousePressEvent(_FakeEvent())     # no setPos between press/release
    item.mouseReleaseEvent(_FakeEvent())

    assert calls == []


def test_block_real_move_via_press_and_release_fires_once(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["adc1"]
    calls = []
    state.on_geometry_changed = lambda: calls.append(1)
    item.set_editable(True)

    item.mousePressEvent(_FakeEvent())
    item.setPos(103, 118)
    item.mouseReleaseEvent(_FakeEvent())

    assert (item.block.x, item.block.y) == (100, 120)
    assert calls == [1]

    # a second click-only press/release right after must stay silent -
    # the baseline moved with the committed geometry.
    item.mousePressEvent(_FakeEvent())
    item.mouseReleaseEvent(_FakeEvent())
    assert calls == [1]


def test_resize_click_only_press_release_fires_nothing(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["adc1"]
    calls = []
    state.on_geometry_changed = lambda: calls.append(1)
    item.set_editable(True)

    item.handle.mousePressEvent(_FakeEvent(scene_pos=QPointF(0, 0)))
    # no mouseMoveEvent - handle released in place
    item.handle.mouseReleaseEvent(_FakeEvent())

    assert calls == []
    assert (item.block.w, item.block.h) == (140, 80)


def test_resize_real_move_fires_exactly_once(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["adc1"]
    calls = []
    state.on_geometry_changed = lambda: calls.append(1)
    item.set_editable(True)

    item.handle.mousePressEvent(_FakeEvent(scene_pos=QPointF(0, 0)))
    item.handle.mouseMoveEvent(_FakeEvent(scene_pos=QPointF(23, 23)))
    item.handle.mouseReleaseEvent(_FakeEvent())

    assert calls == [1]


def test_legend_click_only_press_release_fires_nothing(qtbot):
    state = DiagramState()
    legend = LegendItem(state)
    calls = []
    state.on_geometry_changed = lambda: calls.append(1)
    legend.set_editable(True)

    legend.mousePressEvent(_FakeEvent())
    legend.mouseReleaseEvent(_FakeEvent())

    assert calls == []


def test_legend_real_move_fires_exactly_once(qtbot):
    state = DiagramState()
    legend = LegendItem(state)
    calls = []
    state.on_geometry_changed = lambda: calls.append(1)
    legend.set_editable(True)

    legend.mousePressEvent(_FakeEvent())
    legend.setPos(103, 118)
    legend.mouseReleaseEvent(_FakeEvent())

    assert calls == [1]


# -- BlockItem geometry()/apply_geometry() (undo restore path) -----------


def test_block_geometry_roundtrip(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["adc1"]
    assert item.geometry() == (70, 280, 140, 80)

    item.apply_geometry(55, 66, 77, 88)

    assert item.geometry() == (55, 66, 77, 88)
    assert (item.block.x, item.block.y) == (55, 66)
    assert (item.block.w, item.block.h) == (77, 88)
    assert (item.pos().x(), item.pos().y()) == (55, 66)


def test_apply_geometry_on_editable_block_bypasses_snap(qtbot):
    # 73/118 are not multiples of the 10-unit grid; apply_geometry's
    # undo restore must land exactly there even mid-edit-mode, not get
    # intercepted by itemChange's live snap like a real drag would.
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["adc1"]
    item.set_editable(True)

    item.apply_geometry(73, 118, 77, 88)

    assert (item.block.x, item.block.y) == (73, 118)
    assert (item.pos().x(), item.pos().y()) == (73, 118)
    assert item.geometry() == (73, 118, 77, 88)


def test_apply_geometry_on_editable_legend_bypasses_snap(qtbot):
    state = DiagramState()
    legend = LegendItem(state)
    legend.set_editable(True)

    legend.apply_geometry(73, 118, 0, 0)

    assert (legend.pos().x(), legend.pos().y()) == (73, 118)
    assert legend.geometry() == (73, 118, 0, 0)


# -- LegendItem: set_editable/geometry/apply_geometry, position source ---


def test_legend_default_position_is_fallback(qtbot):
    state = DiagramState()
    legend = LegendItem(state)
    assert legend.geometry() == (700, 402, 0, 0)


def test_legend_geometry_and_apply_geometry_ignores_wh(qtbot):
    state = DiagramState()
    legend = LegendItem(state)

    legend.apply_geometry(10, 20, 999, 999)

    assert legend.geometry() == (10, 20, 0, 0)


def test_legend_drag_snaps_and_fires_callback_once(qtbot):
    state = DiagramState()
    legend = LegendItem(state)
    calls = []
    state.on_geometry_changed = lambda: calls.append(legend.geometry())

    legend.set_editable(True)
    legend.setPos(103, 118)
    assert legend.geometry() == (100, 120, 0, 0)

    legend.mouseReleaseEvent(_FakeEvent())

    assert calls == [(100, 120, 0, 0)]


def test_build_scene_reads_legend_pos_from_topology_when_present(qtbot):
    engine = Engine.load("targets/f411", MockAdapter({}))
    state = DiagramState()
    scene, blocks, wires = build_scene(engine.topology, state)
    # f411.topology.yaml has no layout.legend - fallback stays available
    assert state.legend_pos is None
    assert LegendItem(state).geometry()[:2] == (700, 402)

    topology_with_legend = dataclasses.replace(engine.topology,
                                                legend=(123, 456))
    state2 = DiagramState()
    build_scene(topology_with_legend, state2)
    assert state2.legend_pos == (123, 456)
    assert LegendItem(state2).geometry()[:2] == (123, 456)


# -- M8 task 4: WireItem.edge_key() ---------------------------------------


def test_edge_key_matches_layout_io_identity_with_label(qtbot):
    # ("adc1", "mux0", "DR") is exactly the identity layout_io's
    # _edge_identity() reads off the yaml's `from:`/`to:`/`label:`
    # fields for this f411 edge - see tests/test_layout_io.py's
    # `key = ("adc1", "mux0", "DR")` for the same fixture edge.
    _, state, scene, blocks, wires = _build(qtbot)
    wire = next(w for w in wires.values()
               if w.edge.src == "adc1" and w.edge.dst == "mux0")
    assert wire.edge_key() == ("adc1", "mux0", "DR")


def test_edge_key_labelless_edge_uses_empty_string():
    wire, state, edge = _make_wire(points=None, label=None)
    assert wire.edge_key() == ("a", "b", "")


# -- M8 task 4: set_editable creates/destroys WaypointHandle children ----


def test_set_editable_creates_handles_matching_points_and_destroys_on_disable(
        qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    wire = next(w for w in wires.values()
               if w.edge.src == "adc1" and w.edge.dst == "mux0")
    assert wire._handles == []

    wire.set_editable(True)
    assert len(wire._handles) == len(wire.edge.points) == 4
    for handle, (px, py) in zip(wire._handles, wire.edge.points):
        assert (int(handle.pos().x()), int(handle.pos().y())) == (px, py)
        assert handle.scene() is scene

    wire.set_editable(False)
    assert wire._handles == []


def test_set_editable_on_pointless_edge_creates_no_handles(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    wire = next(w for w in wires.values() if not w.edge.points)
    wire.set_editable(True)
    assert wire._handles == []


# -- M8 task 4: handle drag moves/snaps a point, writes edge.points ------


def test_handle_drag_updates_edge_points_and_snaps():
    wire, state, edge = _make_wire(points=[(10, 10), (50, 10), (90, 10)])
    calls = []
    state.on_geometry_changed = lambda: calls.append(list(edge.points))
    wire.set_editable(True)

    handle = wire._handles[1]
    handle.mousePressEvent(_FakeEvent(scene_pos=QPointF(50, 10)))
    handle.mouseMoveEvent(_FakeEvent(scene_pos=QPointF(53, 18)))
    handle.mouseReleaseEvent(_FakeEvent())

    assert edge.points == [(10, 10), (50, 20), (90, 10)]
    assert calls == [[(10, 10), (50, 20), (90, 10)]]   # fired once


def test_handle_click_only_press_release_fires_nothing():
    wire, state, edge = _make_wire(points=[(10, 10), (50, 10), (90, 10)])
    calls = []
    state.on_geometry_changed = lambda: calls.append(1)
    wire.set_editable(True)

    handle = wire._handles[0]
    handle.mousePressEvent(_FakeEvent(scene_pos=QPointF(10, 10)))
    handle.mouseReleaseEvent(_FakeEvent())   # no move in between

    assert edge.points == [(10, 10), (50, 10), (90, 10)]
    assert calls == []


# -- M8 task 4: double-click insertion ------------------------------------
#
# NOTE on the pointless-edge case: a first insert deliberately captures
# the edge's CURRENT rendered endpoints (self.pts) into edge.points
# alongside the clicked point, rather than writing a single-element
# list. WireItem's existing architecture (Task 1/2, see the f411
# fixture's hand-authored points: lists) treats edge.points, once
# non-empty, as the ENTIRE literal path - paint()'s arrowhead alone
# indexes self.pts[-2]/self.pts[-1], so a genuinely single-point path
# is unrenderable (IndexError) and would silently disconnect the wire
# from its blocks on the next repaint. Freezing the current endpoints
# on first insert keeps the invariant "edge.points, if non-empty, is a
# complete renderable path" intact and matches how every hand-authored
# edge in targets/f411/f411.topology.yaml is already shaped.


def test_double_click_pointless_edge_creates_explicit_path_and_fires_once():
    wire, state, edge = _make_wire(points=None)
    state.edit_mode = True
    calls = []
    state.on_geometry_changed = lambda: calls.append(list(edge.points))

    wire.mouseDoubleClickEvent(_FakeEvent(pos=QPointF(53, 4)))

    assert edge.points == [(0, 0), (50, 0), (100, 0)]
    assert calls == [[(0, 0), (50, 0), (100, 0)]]   # fired once


def test_double_click_pointed_edge_inserts_into_right_segment():
    # Two segments: (0,0)-(100,0) then (100,0)-(100,100) - "the second
    # segment" is the vertical one. Click on it, near its midpoint.
    wire, state, edge = _make_wire(points=[(0, 0), (100, 0), (100, 100)])
    state.edit_mode = True
    calls = []
    state.on_geometry_changed = lambda: calls.append(1)

    wire.mouseDoubleClickEvent(_FakeEvent(pos=QPointF(100, 50)))

    assert len(edge.points) == 4
    assert edge.points[2] == (100, 50)   # inserted into the 2nd segment
    assert edge.points == [(0, 0), (100, 0), (100, 50), (100, 100)]
    assert calls == [1]


def test_double_click_outside_edit_mode_does_nothing():
    wire, state, edge = _make_wire(points=None)
    calls = []
    state.on_geometry_changed = lambda: calls.append(1)

    wire.mouseDoubleClickEvent(_FakeEvent(pos=QPointF(53, 4)))

    assert edge.points == []
    assert calls == []


# -- M8 task 4: Delete on a selected handle removes its point ------------


def test_delete_removes_selected_interior_point():
    # 2 interior points ((30,10) and (50,10)) between the (10,10)/
    # (90,10) anchors - deleting one interior point still leaves the
    # path explicit (3 points remain, more than just the 2 anchors),
    # so no auto-route reversion happens here (see the dedicated
    # revert-to-auto-route test below for that boundary case).
    wire, state, edge = _make_wire(
        points=[(10, 10), (30, 10), (50, 10), (90, 10)])
    wire.set_editable(True)
    calls = []
    state.on_geometry_changed = lambda: calls.append(list(edge.points))

    handle = wire._handles[1]   # the first interior point, (30, 10)
    handle.keyPressEvent(_FakeKeyEvent(Qt.Key_Delete))

    assert edge.points == [(10, 10), (50, 10), (90, 10)]
    assert calls == [[(10, 10), (50, 10), (90, 10)]]
    assert len(wire._handles) == 3


def test_delete_on_endpoint_handle_is_a_noop():
    # Endpoints (index 0 and -1) are anchors, not waypoints, under the
    # full-polyline schema - Delete must never be able to detach the
    # wire from its block (fix-round ruling, Important 1).
    wire, state, edge = _make_wire(points=[(0, 0), (50, 0), (100, 0)])
    wire.set_editable(True)
    calls = []
    state.on_geometry_changed = lambda: calls.append(1)

    wire._handles[0].keyPressEvent(_FakeKeyEvent(Qt.Key_Delete))    # src
    wire._handles[-1].keyPressEvent(_FakeKeyEvent(Qt.Key_Delete))   # dst

    assert edge.points == [(0, 0), (50, 0), (100, 0)]
    assert calls == []
    assert len(wire._handles) == 3


def test_deleting_the_only_interior_point_reverts_to_auto_route():
    # Freezing a pointless edge via double-click always yields 2
    # anchors + >= 1 interior point; deleting that last interior point
    # down to just the 2 anchors IS "deleting the last waypoint" under
    # the corrected schema - it reverts to points == [] (auto-
    # routing), which also makes the invalid 1-point state (see
    # apply_points) unreachable by construction.
    wire, state, edge = _make_wire(points=None)
    auto_a, auto_b = wire.pts[0], wire.pts[1]
    wire.set_editable(True)
    state.edit_mode = True
    wire.mouseDoubleClickEvent(_FakeEvent(pos=QPointF(53, 4)))
    assert len(edge.points) == 3

    interior_handle = wire._handles[1]
    interior_handle.keyPressEvent(_FakeKeyEvent(Qt.Key_Delete))

    assert edge.points == []
    assert wire.pts == [auto_a, auto_b]
    assert wire._handles == []


def test_chained_interior_deletes_stay_paintable(qtbot):
    # Direct repro of the fix-round Critical finding: the reviewer
    # reproduced an IndexError in paint() mid-chain that a purely
    # data-level assertion between deletes (no actual paint() call)
    # never caught. Every step here renders the wire for real via a
    # QPainter on an offscreen QImage. The qtbot fixture is required
    # here (fix round 2): _paint_wire touches fontMetrics/
    # QFontDatabase, which needs a live QGuiApplication - without
    # qtbot this test only passed by accident, riding on a
    # QApplication left alive by earlier tests in the same file (and
    # hard-aborted with "QFontDatabase: Must construct a
    # QGuiApplication" when run standalone) - the exact execution-
    # order masking this test exists to eliminate, reintroduced
    # inside itself.
    wire, state, edge = _make_wire(
        points=[(0, 0), (20, 0), (40, 0), (60, 0), (100, 0)])
    wire.set_editable(True)
    _paint_wire(wire)   # sanity: paints fine before any delete

    while len(wire.edge.points) > 2:
        interior_handle = wire._handles[1]   # always the first interior
        interior_handle.keyPressEvent(_FakeKeyEvent(Qt.Key_Delete))
        _paint_wire(wire)   # must not raise at any point in the chain

    assert edge.points == []
    _paint_wire(wire)   # auto-routed fallback paints fine too


# -- M8 task 4: apply_points (undo restore path) --------------------------


def test_apply_points_restores_exact_unsnapped_values_without_firing():
    wire, state, edge = _make_wire(points=[(10, 10), (50, 10), (90, 10)])
    wire.set_editable(True)
    calls = []
    state.on_geometry_changed = lambda: calls.append(1)

    wire.apply_points([(13, 27), (58, 44)])   # not grid-aligned on purpose

    assert edge.points == [(13, 27), (58, 44)]
    assert [(int(p.x()), int(p.y())) for p in wire.pts] == [(13, 27),
                                                             (58, 44)]
    assert calls == []
    assert len(wire._handles) == 2
    assert (int(wire._handles[0].pos().x()),
           int(wire._handles[0].pos().y())) == (13, 27)
    assert (int(wire._handles[1].pos().x()),
           int(wire._handles[1].pos().y())) == (58, 44)


def test_apply_points_with_empty_list_reverts_to_auto_route():
    wire, state, edge = _make_wire(points=None)
    auto = list(wire.pts)
    calls = []
    state.on_geometry_changed = lambda: calls.append(1)

    wire.apply_points([(37, 53), (80, 53)])   # 2 anchors, valid
    assert edge.points == [(37, 53), (80, 53)]

    wire.apply_points([])
    assert edge.points == []
    assert wire.pts == auto
    assert calls == []


def test_apply_points_rejects_single_point_list():
    # A single point is neither a valid explicit path (needs >= 2
    # anchors) nor the empty auto-route sentinel - fix-round ruling
    # (Critical): this is the invalid state remove_point can no
    # longer produce, so apply_points must refuse it too rather than
    # silently accepting an undo snapshot that would crash paint().
    wire, state, edge = _make_wire(points=[(10, 10), (50, 10), (90, 10)])

    with pytest.raises(ValueError):
        wire.apply_points([(5, 5)])


# -- M8 task 5: MainWindow integration - mode toggle, undo, save/revert --
#
# Unlike the item-level tests above (which drive BlockItem/WireItem
# handlers directly against a bare DiagramState), these build a real
# MainWindow over Engine.load so the toggle/undo/save/revert/auto-
# layout wiring under test is exercised end to end - the toolbar
# button, the item layer, and (for save/revert) the actual target
# directory's *.topology.yaml on disk.


def _build_window(qtbot, target_dir="targets/f411"):
    engine = Engine.load(target_dir, MockAdapter({}))
    bridge = EngineBridge(engine)
    win = MainWindow(engine, bridge)
    qtbot.addWidget(win)
    win.show()
    return engine, win


def _drag_block(item, x, y):
    """Same press/setPos/release gesture the item-level tests above
    drive directly - fires DiagramState.on_geometry_changed exactly
    once via BlockItem.mouseReleaseEvent, this time through whatever
    handler MainWindow wired there."""
    item.mousePressEvent(_FakeEvent())
    item.setPos(x, y)
    item.mouseReleaseEvent(_FakeEvent())


def test_edit_toggle_sets_state_and_every_item_editable_both_ways(qtbot):
    # The CRITICAL combination (task handover note): edit_mode and
    # every item's own set_editable must always flip together, never
    # independently - drag/resize commits gate on the latter, click
    # suppression on the former.
    engine, win = _build_window(qtbot)
    assert win.diagram_state.edit_mode is False
    assert win.layout_edit_strip.isVisible() is False

    win.edit_layout_btn.setChecked(True)
    assert win.diagram_state.edit_mode is True
    assert win.blocks and all(item._editable for item in win.blocks.values())
    assert win.wires and all(w._editable for w in win.wires.values())
    assert win.legend._editable is True
    assert win.layout_edit_strip.isVisible() is True

    win.edit_layout_btn.setChecked(False)
    assert win.diagram_state.edit_mode is False
    assert all(not item._editable for item in win.blocks.values())
    assert all(not w._editable for w in win.wires.values())
    assert win.legend._editable is False
    assert win.layout_edit_strip.isVisible() is False


def test_drag_marks_dirty_and_shows_dirty_label(qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    assert win.layout_dirty_label.isVisible() is False

    item = win.blocks["adc1"]
    _drag_block(item, 400, 500)

    assert (item.block.x, item.block.y) == (400, 500)
    assert win._layout_dirty is True
    assert win.layout_dirty_label.isVisible() is True


def test_undo_restores_previous_geometry(qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    item = win.blocks["adc1"]
    original = item.geometry()

    _drag_block(item, 400, 500)
    assert item.geometry() != original

    win._on_layout_undo()

    assert item.geometry() == original
    assert (item.block.x, item.block.y) == original[:2]


def test_undo_is_a_noop_outside_edit_mode_and_when_stack_is_empty(qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    item = win.blocks["adc1"]
    _drag_block(item, 400, 500)
    moved = item.geometry()

    win.edit_layout_btn.setChecked(False)   # leaves the undo stack populated
    win._on_layout_undo()                   # must not apply outside edit mode
    assert item.geometry() == moved

    win.edit_layout_btn.setChecked(True)
    win._on_layout_undo()                   # now it applies
    assert item.geometry() != moved
    win._on_layout_undo()                   # stack now empty: no-op, no raise


def test_save_writes_yaml_and_reload_reproduces_positions(qtbot, tmp_path):
    tdir = tmp_path / "f411"
    shutil.copytree("targets/f411", tdir)
    engine, win = _build_window(qtbot, str(tdir))

    win.edit_layout_btn.setChecked(True)
    _drag_block(win.blocks["adc1"], 400, 500)
    assert win._layout_dirty is True

    win._on_save_layout()

    assert win._layout_dirty is False
    assert win._undo_stack == []
    assert win.layout_dirty_label.isVisible() is False

    reloaded = load_topology(engine.topology_path, engine.model)
    assert (reloaded.blocks["adc1"].x, reloaded.blocks["adc1"].y) == (400, 500)


def test_save_layout_patch_error_shows_status_message(qtbot, tmp_path,
                                                       monkeypatch):
    tdir = tmp_path / "f411"
    shutil.copytree("targets/f411", tdir)
    engine, win = _build_window(qtbot, str(tdir))
    win.edit_layout_btn.setChecked(True)
    _drag_block(win.blocks["adc1"], 400, 500)

    def _boom(*args, **kwargs):
        raise LayoutPatchError("boom")
    monkeypatch.setattr("ui.main_window.save_layout", _boom)

    win._on_save_layout()

    assert "boom" in win.statusBar().currentMessage()
    assert win._layout_dirty is True   # unsaved edit is not discarded on error


def test_revert_restores_original_geometry(qtbot, tmp_path):
    tdir = tmp_path / "f411"
    shutil.copytree("targets/f411", tdir)
    engine, win = _build_window(qtbot, str(tdir))
    win.edit_layout_btn.setChecked(True)
    item = win.blocks["adc1"]
    original = item.geometry()
    _drag_block(item, 400, 500)
    assert item.geometry() != original

    win._on_revert_layout()

    assert item.geometry() == original
    assert win._layout_dirty is False
    assert win._undo_stack == []
    assert win.layout_dirty_label.isVisible() is False


def test_auto_layout_moves_blocks_clears_wire_points_and_one_undo_restores(
        qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)

    original_blocks = {bid: item.geometry()
                       for bid, item in win.blocks.items()}
    original_wire_points = {eid: list(wire.edge.points)
                            for eid, wire in win.wires.items()}
    assert any(original_wire_points.values())   # sanity: some start pointed

    from core.target.autolayout import auto_layout
    expected = auto_layout(list(engine.topology.blocks.values()),
                           engine.topology.edges)

    win._on_auto_layout()

    for bid, item in win.blocks.items():
        assert item.geometry()[:2] == expected[bid]
    assert all(wire.edge.points == [] for wire in win.wires.values())
    assert win._layout_dirty is True
    assert win.layout_dirty_label.isVisible() is True
    assert all(w.edge_key() in win._dirty_wire_keys
              for w in win.wires.values())

    win._on_layout_undo()

    for bid, item in win.blocks.items():
        assert item.geometry() == original_blocks[bid]
    for eid, wire in win.wires.items():
        assert wire.edge.points == original_wire_points[eid]


def test_tab_switch_to_scope_exits_edit_mode_dirty_label_persists(qtbot):
    engine = make_demo_engine("targets/f411")
    bridge = EngineBridge(engine)
    win = MainWindow(engine, bridge)
    qtbot.addWidget(win)
    win.show()
    # M7: activating the Scope tab discovers the trace target
    # synchronously (ScopePage.__init__), which needs the poller thread
    # actually running to service the read - same requirement as
    # test_main_window.py's scope-switch tests.
    engine.start()
    try:
        win.edit_layout_btn.setChecked(True)
        _drag_block(win.blocks["adc1"], 400, 500)
        assert win._layout_dirty is True
        assert win.layout_dirty_label.isVisible() is True

        win.tabs.setCurrentIndex(1)   # switch to Scope

        assert win.edit_layout_btn.isChecked() is False
        assert win.diagram_state.edit_mode is False
        assert all(not item._editable for item in win.blocks.values())
        assert win.layout_edit_strip.isVisible() is False
        assert win.layout_dirty_label.isVisible() is True    # persists
        assert win._layout_dirty is True                     # untouched
    finally:
        engine.stop()
