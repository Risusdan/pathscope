"""M8 task 3/4: edit-mode input routing on diagram items - drag, snap,
resize, waypoint editing on wires. Built on the same construction
pattern as test_scene.py (Engine.load -> build_scene), driving
BlockItem/WireItem/WaypointHandle/LegendItem's own event handlers
directly rather than going through Qt's event loop (offscreen
platform, no real mouse)."""
import dataclasses
import shutil

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QToolBar

from core.adapter.mock import MockAdapter
from core.engine.core import Engine
from core.target.layout_io import LayoutPatchError
from core.target.topology import Edge, load_topology
from ui.bridge import EngineBridge
from ui.demo import make_demo_engine
from ui.diagram.items import (LegendItem, WireItem, _ALIGN_THRESHOLD,
                              _compute_alignment_snap,
                              _straight_route_points, snap)
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
    """Minimal stand-in for QKeyEvent: WaypointHandle/BlockItem/
    LegendItem's keyPressEvent handlers only ever call
    .key()/.modifiers()/.accept(). modifiers defaults to NoModifier -
    the arrow-key nudge reads Shift off the event itself (not
    QApplication.keyboardModifiers(), which an offscreen test cannot
    drive), so tests pass Qt.ShiftModifier directly here to exercise
    the fine-step path."""

    def __init__(self, key, modifiers=Qt.NoModifier):
        self._key = key
        self._modifiers = modifiers
        self.accepted = False

    def key(self):
        return self._key

    def modifiers(self):
        return self._modifiers

    def accept(self):
        self.accepted = True


class _FakeWheelEvent:
    """Minimal stand-in for QWheelEvent: _DiagramView.wheelEvent only
    ever calls .pixelDelta()/.angleDelta(). Real trackpad events carry
    a QPoint pixelDelta (possibly (0, 0) when the platform does not
    report pixel deltas) and a QPoint angleDelta in eighths of a
    degree (only ever the y component matters here)."""

    def __init__(self, pixel_dy=0, angle_dy=0):
        self._pixel = QPoint(0, pixel_dy)
        self._angle = QPoint(0, angle_dy)

    def pixelDelta(self):
        return self._pixel

    def angleDelta(self):
        return self._angle


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
    # Acceptance round 5 note: this test used to assert that
    # targets/f411's SHIPPED yaml has no layout.legend line - but the
    # whole point of M8 is that the app writes that file, so the
    # assertion broke the first time a real Save layout ran against
    # the demo target. The no-legend half now forces legend=None
    # explicitly instead of assuming the file's on-disk state.
    engine = Engine.load("targets/f411", MockAdapter({}))
    topology_no_legend = dataclasses.replace(engine.topology, legend=None)
    state = DiagramState()
    scene, blocks, wires = build_scene(topology_no_legend, state)
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


def test_set_editable_on_pointless_edge_creates_endpoint_handles(qtbot):
    # Acceptance round 7 flipped this test's original expectation (no
    # handles on a pointless edge): every wire now shows its two
    # endpoint handles in edit mode, auto-routed or not.
    _, state, scene, blocks, wires = _build(qtbot)
    wire = next(w for w in wires.values() if not w.edge.points)
    wire.set_editable(True)
    assert len(wire._handles) == 2
    assert wire._handles[0].pos() == wire.pts[0]
    assert wire._handles[1].pos() == wire.pts[-1]


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
    # acceptance round 7: reverting to auto-route keeps the two
    # endpoint handles (rebuilt from the auto route), interior gone
    assert len(wire._handles) == 2


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


def test_edit_layout_button_and_dirty_label_live_in_datapath_header(qtbot):
    # M8 manual-gate finding 1: edit_layout_btn/layout_dirty_label were
    # relocated off the toolbar (where they read as a third page
    # switcher next to Data Path/Scope) into the Data Path page's own
    # header row, right alongside Halt MCU - same parent widget as
    # halt_btn, and NOT a descendant of any QToolBar.
    engine, win = _build_window(qtbot)
    assert win.edit_layout_btn.parentWidget() is win.halt_btn.parentWidget()
    assert (win.layout_dirty_label.parentWidget()
           is win.halt_btn.parentWidget())

    def _in_a_toolbar(widget) -> bool:
        w = widget
        while w is not None:
            if isinstance(w, QToolBar):
                return True
            w = w.parentWidget()
        return False

    assert not _in_a_toolbar(win.edit_layout_btn)
    assert not _in_a_toolbar(win.layout_dirty_label)


def test_header_buttons_share_the_same_minimum_height(qtbot):
    # User-acceptance finding 2 (styling): Halt MCU, Edit Layout, and
    # Run/Stop all sit in the same Data Path header row - Edit Layout
    # was left at the default (short) QToolButton height while the
    # other two were explicitly set to 36px, reading as smaller and
    # vertically misaligned next to them.
    engine, win = _build_window(qtbot)
    assert win.edit_layout_btn.minimumHeight() == win.halt_btn.minimumHeight()
    assert (win.dp_run_stop_btn.minimumHeight()
           == win.halt_btn.minimumHeight())

    # Round 2 (user re-tested, styling still off): matching height
    # alone was not enough - edit_layout_btn was still a DIFFERENT
    # WIDGET CLASS (QToolButton) than its QPushButton neighbors, which
    # carries its own default look regardless of size hints. It must
    # be the SAME class as its header neighbors.
    assert type(win.edit_layout_btn) is type(win.halt_btn)
    assert type(win.edit_layout_btn) is type(win.dp_run_stop_btn)


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


def test_undo_stack_clears_on_mode_exit_and_reentry_undo_is_noop(qtbot):
    # Spec: "Stack clears on save and on mode exit." A gesture made
    # this session must not be undo-able after the user leaves edit
    # mode and comes back later - Ctrl/Cmd+Z on re-entry has nothing to
    # pop, so the geometry from the earlier session stays exactly as
    # committed.
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    item = win.blocks["adc1"]
    _drag_block(item, 400, 500)
    moved = item.geometry()
    assert win._undo_stack   # the gesture pushed a snapshot

    win.edit_layout_btn.setChecked(False)   # exit mode: stack clears
    assert win._undo_stack == []
    assert win._layout_dirty is True        # dirty persists (untouched)
    assert win.layout_dirty_label.isVisible() is True

    win.edit_layout_btn.setChecked(True)    # re-enter later
    win._on_layout_undo()                   # empty stack: no-op, no raise

    assert item.geometry() == moved         # nothing reverted
    assert win._layout_dirty is True        # still dirty - no Save/Revert yet


def test_undo_guard_ignores_a_populated_stack_when_edit_mode_flag_is_off(
        qtbot):
    # Belt-and-suspenders on _on_layout_undo's own edit_mode guard: the
    # toggle handler is what clears the stack on a normal exit (see the
    # test above), but if diagram_state.edit_mode is ever false while
    # the stack still holds something - by some other path than the
    # toggle button - undo must still refuse to pop.
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    item = win.blocks["adc1"]
    _drag_block(item, 400, 500)
    moved = item.geometry()
    assert win._undo_stack

    win.diagram_state.edit_mode = False   # bypass the toggle handler
    win._on_layout_undo()

    assert item.geometry() == moved
    assert win._undo_stack   # untouched - the guard returned before popping


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


def _point_on_block_boundary(pt, geom) -> bool:
    """True if `pt` (a QPointF) sits on the boundary rectangle of a
    block whose geometry() is `geom` - used to check a re-routed
    wire's endpoint actually anchors to its (possibly just-moved)
    block, without depending on straight_route_points' exact side/frac
    choice (which side of the rect it lands on is a routing detail;
    landing ON the rect at all is the invariant under test)."""
    x, y, w, h = geom
    px, py = pt.x(), pt.y()
    eps = 1e-6
    on_vertical = ((abs(px - x) < eps or abs(px - (x + w)) < eps)
                  and y - eps <= py <= y + h + eps)
    on_horizontal = ((abs(py - y) < eps or abs(py - (y + h)) < eps)
                     and x - eps <= px <= x + w + eps)
    return on_vertical or on_horizontal


def test_auto_layout_moves_blocks_clears_wire_points_and_one_undo_restores(
        qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)

    original_blocks = {bid: item.geometry()
                       for bid, item in win.blocks.items()}
    original_wire_points = {eid: list(wire.edge.points)
                            for eid, wire in win.wires.items()}
    original_wire_pts = {eid: list(wire.pts)
                         for eid, wire in win.wires.items()}
    assert any(original_wire_points.values())   # sanity: some start pointed
    assert any(not p for p in original_wire_points.values())  # some don't

    from core.target.autolayout import auto_layout
    expected = auto_layout(list(engine.topology.blocks.values()),
                           engine.topology.edges)

    win._on_auto_layout()

    moved_blocks = {bid: item.geometry() for bid, item in win.blocks.items()}
    for bid, item in win.blocks.items():
        assert item.geometry()[:2] == expected[bid]
    assert all(wire.edge.points == [] for wire in win.wires.values())
    assert win._layout_dirty is True
    assert win.layout_dirty_label.isVisible() is True

    # M8 fix round 2 CRITICAL: every wire is now pointless (cleared
    # above) and must actually re-route to the MOVED blocks - not keep
    # rendering whatever it was drawing before auto-layout ran.
    for wire in win.wires.values():
        src_geom = moved_blocks[wire.edge.src]
        dst_geom = moved_blocks[wire.edge.dst]
        assert _point_on_block_boundary(wire.pts[0], src_geom)
        assert _point_on_block_boundary(wire.pts[-1], dst_geom)

    # M8 fix round 2 IMPORTANT: only a wire that ACTUALLY had explicit
    # points before the clear is dirty - an already-pointless wire's
    # yaml entry must stay untouched by Save (see the dedicated
    # byte-identical save test below).
    for eid, wire in win.wires.items():
        if original_wire_points[eid]:
            assert wire.edge_key() in win._dirty_wire_keys
        else:
            assert wire.edge_key() not in win._dirty_wire_keys

    win._on_layout_undo()

    for bid, item in win.blocks.items():
        assert item.geometry() == original_blocks[bid]
    for eid, wire in win.wires.items():
        assert wire.edge.points == original_wire_points[eid]
        # M8 fix round 2: the RENDERED path is back too, not just the
        # logical edge.points - matters for the 2 originally-pointless
        # wires, whose auto-route fallback must also have been
        # refreshed back to the restored block positions.
        assert wire.pts == original_wire_pts[eid]


def test_wire_auto_route_fallback_is_correct_immediately_after_construction(
        qtbot):
    # M8 fix round 2 CRITICAL, isolating the construction-time half of
    # the fix from the gesture-triggered half: apply_points([]) is
    # callback-silent (never fires on_geometry_changed, so none of
    # MainWindow's gesture-driven refresh_auto_route calls can be
    # responsible) - if this already renders a fresh straight route
    # instead of the frozen original dog-leg, WireItem's auto-route
    # fallback must have been corrected by the time MainWindow's
    # __init__ finished, before any gesture ever ran.
    engine, win = _build_window(qtbot)
    wire = next(w for w in win.wires.values()
               if w.edge.src == "adc1" and w.edge.dst == "mux0")
    original_dogleg = list(wire.pts)
    assert len(wire.edge.points) == 4   # f411's adc1->mux0 dog-leg

    wire.apply_points([])

    from ui.diagram.items import _straight_route_points
    expected = _straight_route_points(win.blocks["adc1"], win.blocks["mux0"],
                                      wire.edge)
    assert wire.pts == expected
    assert wire.pts != original_dogleg


def test_born_pointed_edge_delete_to_auto_route_renders_fresh_straight_line(
        qtbot):
    # M8 fix round 2 CRITICAL, end to end via the real interactive
    # delete path (handles included): deleting a born-pointed edge's
    # waypoints down to auto-route must render a fresh 2-point
    # straight line between the CURRENT block anchors, not the frozen
    # original dog-leg - and the handles must be gone.
    engine, win = _build_window(qtbot)
    wire = next(w for w in win.wires.values()
               if w.edge.src == "adc1" and w.edge.dst == "mux0")
    original_dogleg = list(wire.pts)
    win.edit_layout_btn.setChecked(True)
    assert len(wire._handles) == 4

    # 4 points (2 anchors + 2 interior) -> one interior delete leaves 3
    # (still explicit) -> a second interior delete collapses to [].
    wire._handles[1].keyPressEvent(_FakeKeyEvent(Qt.Key_Delete))
    wire._handles[1].keyPressEvent(_FakeKeyEvent(Qt.Key_Delete))

    assert wire.edge.points == []
    # acceptance round 7: the two endpoint handles remain, riding the
    # fresh auto route; only the interior ones are gone
    assert len(wire._handles) == 2
    from ui.diagram.items import _straight_route_points
    expected = _straight_route_points(win.blocks["adc1"], win.blocks["mux0"],
                                      wire.edge)
    assert wire.pts == expected
    assert wire.pts != original_dogleg


# -- M8 manual-gate finding 2d: endpoint re-anchor magnet ------------------


def test_nearest_rect_boundary_point_outside_and_inside():
    from ui.diagram.items import _nearest_rect_boundary_point
    rect = (0, 0, 100, 50)
    # outside the rect: nearest point is a plain clamp onto the edge.
    assert _nearest_rect_boundary_point(50, -10, *rect) == (50, 0)
    assert _nearest_rect_boundary_point(-10, 25, *rect) == (0, 25)
    assert _nearest_rect_boundary_point(50, 60, *rect) == (50, 50)
    assert _nearest_rect_boundary_point(110, 25, *rect) == (100, 25)
    # inside the rect: pushed out to whichever of the 4 edges is nearest.
    assert _nearest_rect_boundary_point(10, 25, *rect) == (0, 25)    # left
    assert _nearest_rect_boundary_point(90, 25, *rect) == (100, 25)  # right
    assert _nearest_rect_boundary_point(50, 5, *rect) == (50, 0)     # top
    assert _nearest_rect_boundary_point(50, 45, *rect) == (50, 50)   # bottom


def test_endpoint_magnet_snaps_onto_block_boundary_within_radius(qtbot):
    # adc1: x=70, y=280, w=140, h=80 -> right edge at x=210 (grid-
    # aligned already). Releasing the src-anchor handle (index 0) 5px
    # inside that edge must pull it back onto the boundary - NOT the
    # plain grid-snap of the raw drop point (205 -> 200, banker's
    # rounding), which would land 10 units short of the block.
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    wire = next(w for w in win.wires.values()
               if w.edge.src == "adc1" and w.edge.dst == "mux0")
    handle = wire._handles[0]

    handle.mousePressEvent(_FakeEvent(scene_pos=QPointF(210, 320)))
    handle.mouseMoveEvent(_FakeEvent(scene_pos=QPointF(205, 320)))
    handle.mouseReleaseEvent(_FakeEvent())

    assert wire.edge.points[0] == (210, 320)   # magnetized, not (200, 320)


def test_endpoint_magnet_does_not_apply_beyond_radius(qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    wire = next(w for w in win.wires.values()
               if w.edge.src == "adc1" and w.edge.dst == "mux0")
    handle = wire._handles[0]

    handle.mousePressEvent(_FakeEvent(scene_pos=QPointF(210, 320)))
    handle.mouseMoveEvent(_FakeEvent(scene_pos=QPointF(150, 320)))  # 60px in
    handle.mouseReleaseEvent(_FakeEvent())

    assert wire.edge.points[0] == (150, 320)   # plain grid-snap, no magnet


def test_endpoint_magnet_does_not_apply_to_interior_handles(qtbot):
    # An interior waypoint (index 1 of this 4-point path) dropped 15px
    # from mux0's own (off-grid, x=255) boundary must NOT be
    # magnetized - only index 0/-1 are anchors. If the index gate were
    # missing, this would land on (260, 350) instead.
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    wire = next(w for w in win.wires.values()
               if w.edge.src == "adc1" and w.edge.dst == "mux0")
    handle = wire._handles[1]
    orig = wire.edge.points[1]

    handle.mousePressEvent(_FakeEvent(scene_pos=QPointF(*orig)))
    handle.mouseMoveEvent(_FakeEvent(scene_pos=QPointF(240, 350)))
    handle.mouseReleaseEvent(_FakeEvent())

    assert wire.edge.points[1] == (240, 350)   # plain grid-snap, unaffected


def test_endpoint_magnet_is_inert_on_a_standalone_wire_without_blocks(qtbot):
    # _make_wire-built wires never had refresh_auto_route(blocks)
    # called, so _src_item/_dst_item stay None - the magnet must
    # degrade to plain grid-snap rather than raise.
    wire, state, edge = _make_wire(points=[(10, 10), (50, 10), (90, 10)])
    wire.set_editable(True)
    handle = wire._handles[0]

    handle.mousePressEvent(_FakeEvent(scene_pos=QPointF(10, 10)))
    handle.mouseMoveEvent(_FakeEvent(scene_pos=QPointF(13, 17)))
    handle.mouseReleaseEvent(_FakeEvent())

    assert edge.points[0] == (10, 20)   # plain snap(13)->10, snap(17)->20


def test_block_drag_commit_reroutes_attached_pointless_wires(qtbot):
    # M8 fix round 2 CRITICAL: spec 3's "anchors follow their block"
    # for a pointless (auto-routed) edge, live during a plain drag -
    # not just after auto-layout/undo/revert.
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    wire = next(w for w in win.wires.values()
               if w.edge.src == "mux0" and w.edge.dst == "dma2")
    assert wire.edge.points == []          # f411's born-pointless edge
    original_pts = list(wire.pts)

    _drag_block(win.blocks["dma2"], 700, 700)

    from ui.diagram.items import _straight_route_points
    expected = _straight_route_points(win.blocks["mux0"], win.blocks["dma2"],
                                      wire.edge)
    assert wire.pts == expected
    assert wire.pts != original_pts
    assert wire.edge.points == []          # still pointless - only the
                                            # RENDERED path moved


def test_auto_layout_save_leaves_never_pointed_edge_lines_byte_identical(
        qtbot, tmp_path):
    # M8 fix round 2 IMPORTANT: auto-layout used to mark EVERY wire
    # dirty, so Save stamped `points: []` onto edges that never had a
    # points: entry - breaking the clean-diff promise (spec section
    # 5). Only a wire that ACTUALLY had explicit points before the
    # clear may end up in the saved patch.
    tdir = tmp_path / "f411"
    shutil.copytree("targets/f411", tdir)
    yaml_path = tdir / "f411.topology.yaml"
    original_lines = yaml_path.read_text().splitlines()
    orig_pointless_line = next(l for l in original_lines
                               if "from: mux0" in l and "to: dma2" in l)
    assert "points" not in orig_pointless_line   # sanity: born pointless

    engine, win = _build_window(qtbot, str(tdir))
    win.edit_layout_btn.setChecked(True)
    win._on_auto_layout()
    win._on_save_layout()

    new_lines = yaml_path.read_text().splitlines()
    new_pointless_line = next(l for l in new_lines
                              if "from: mux0" in l and "to: dma2" in l)
    assert new_pointless_line == orig_pointless_line   # byte-identical

    # Contrast: an edge that DID start with explicit points is cleared
    # and DOES get saved.
    reloaded = load_topology(str(yaml_path), engine.model)
    trgo_edge = next(e for e in reloaded.edges
                     if e.src == "tim1" and e.dst == "mux0")
    assert trgo_edge.points == []


def test_tab_switch_to_scope_exits_edit_mode_dirty_flag_persists(qtbot):
    # M8 manual-gate finding 1: edit_layout_btn/layout_dirty_label now
    # live in the Data Path page's own header row (moved off the
    # toolbar, which used to make the button read as a third page
    # switcher) - a consequence of that move is that layout_dirty_label
    # is itself hidden whenever the Data Path page is (a QStackedWidget
    # page switch hides the whole page and everything under it, this
    # label included), so it is NOT visible while on the Scope tab.
    # The underlying dirty STATE (self._layout_dirty) is what actually
    # matters and stays true regardless - the label simply reappears,
    # already showing, the moment the user switches back.
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
        assert win._undo_stack

        win.tabs.setCurrentIndex(1)   # switch to Scope

        assert win.edit_layout_btn.isChecked() is False
        assert win.diagram_state.edit_mode is False
        assert all(not item._editable for item in win.blocks.values())
        assert win.layout_edit_strip.isVisible() is False
        assert win._layout_dirty is True   # untouched - flag persists
        assert win._undo_stack == []   # spec: stack clears on mode exit too

        win.tabs.setCurrentIndex(0)   # back to Data Path

        assert win.layout_dirty_label.isVisible() is True   # reappears
        assert win._layout_dirty is True
    finally:
        engine.stop()


# -- M8 wave 2: Visio-style connector glue (findings 4/5) -----------------
#
# mux0 is the drag target throughout: it is both the dst of two
# EXPLICIT-path edges (adc1->mux0, tim1->mux0 - real f411 dog-legs)
# and the src of one POINTLESS edge (mux0->dma2) - one drag exercises
# live re-route (finding 4) and endpoint glue (finding 5) together,
# matching how they actually interact in the real diagram.


def test_live_move_reroutes_attached_pointless_wire_mid_gesture(qtbot):
    # finding 4: a pointless wire's line must stay attached DURING a
    # drag, not only snap into place at release - setPos (like every
    # other drag test in this file) drives BlockItem.itemChange
    # directly, and the assertion runs BEFORE mouseReleaseEvent.
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    wire = next(w for w in win.wires.values()
               if w.edge.src == "mux0" and w.edge.dst == "dma2")
    assert wire.edge.points == []   # f411's born-pointless edge
    original_pts = list(wire.pts)
    # sanity: an UNattached pointless-if-any wire is not touched by
    # this block's drag - adc1->mux0 is not pointless, skip; instead
    # confirm the filter by checking a wire on the other side of the
    # diagram is untouched.
    unrelated = next(w for w in win.wires.values()
                     if w.edge.src == "flash" and w.edge.dst == "busmx")
    unrelated_pts = list(unrelated.pts)

    item = win.blocks["mux0"]
    item.mousePressEvent(_FakeEvent())
    item.setPos(item.pos().x() + 100, item.pos().y())   # live, pre-release

    expected = _straight_route_points(win.blocks["mux0"], win.blocks["dma2"],
                                      wire.edge)
    assert wire.pts == expected
    assert wire.pts != original_pts
    assert unrelated.pts == unrelated_pts   # untouched

    item.mouseReleaseEvent(_FakeEvent())
    assert wire.pts == _straight_route_points(win.blocks["mux0"],
                                              win.blocks["dma2"], wire.edge)


def test_live_move_glues_explicit_endpoints_interior_points_unchanged(qtbot):
    # finding 5: an explicit path's endpoint anchored to the dragged
    # block translates by the block's own exact delta, live, while
    # everything else in edge.points (the other anchor, any interior
    # waypoint) stays put.
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    wire_adc = next(w for w in win.wires.values()
                   if w.edge.src == "adc1" and w.edge.dst == "mux0")
    wire_tim = next(w for w in win.wires.values()
                   if w.edge.src == "tim1" and w.edge.dst == "mux0")
    orig_adc = list(wire_adc.edge.points)
    orig_tim = list(wire_tim.edge.points)
    orig_x, orig_y, _, _ = win.blocks["mux0"].geometry()

    item = win.blocks["mux0"]
    item.mousePressEvent(_FakeEvent())
    item.setPos(item.pos().x() + 100, item.pos().y() + 20)   # live
    new_x, new_y, _, _ = item.geometry()
    dx, dy = new_x - orig_x, new_y - orig_y
    assert (dx, dy) != (0, 0)

    # glued mid-gesture, BEFORE release:
    assert wire_adc.edge.points[-1] == (orig_adc[-1][0] + dx,
                                        orig_adc[-1][1] + dy)
    assert wire_tim.edge.points[-1] == (orig_tim[-1][0] + dx,
                                        orig_tim[-1][1] + dy)
    assert wire_adc.edge.points[:-1] == orig_adc[:-1]   # src anchor + interior
    assert wire_tim.edge.points[:-1] == orig_tim[:-1]

    item.mouseReleaseEvent(_FakeEvent())   # commit - unchanged by release

    assert wire_adc.edge.points[-1] == (orig_adc[-1][0] + dx,
                                        orig_adc[-1][1] + dy)
    assert wire_tim.edge.points[-1] == (orig_tim[-1][0] + dx,
                                        orig_tim[-1][1] + dy)
    assert win._layout_dirty is True
    assert wire_adc.edge_key() in win._dirty_wire_keys
    assert wire_tim.edge_key() in win._dirty_wire_keys


def test_live_move_glue_undo_restores_block_and_both_glued_wires(qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    wire_adc = next(w for w in win.wires.values()
                   if w.edge.src == "adc1" and w.edge.dst == "mux0")
    wire_tim = next(w for w in win.wires.values()
                   if w.edge.src == "tim1" and w.edge.dst == "mux0")
    orig_adc_points = list(wire_adc.edge.points)
    orig_tim_points = list(wire_tim.edge.points)
    item = win.blocks["mux0"]
    orig_geom = item.geometry()

    item.mousePressEvent(_FakeEvent())
    item.setPos(item.pos().x() + 100, item.pos().y())
    item.mouseReleaseEvent(_FakeEvent())
    assert wire_adc.edge.points != orig_adc_points   # sanity: glue happened

    win._on_layout_undo()

    assert wire_adc.edge.points == orig_adc_points
    assert wire_tim.edge.points == orig_tim_points
    assert item.geometry() == orig_geom


def test_live_move_glue_save_round_trip_reproduces(qtbot, tmp_path):
    tdir = tmp_path / "f411"
    shutil.copytree("targets/f411", tdir)
    engine, win = _build_window(qtbot, str(tdir))
    win.edit_layout_btn.setChecked(True)
    wire_adc = next(w for w in win.wires.values()
                   if w.edge.src == "adc1" and w.edge.dst == "mux0")

    item = win.blocks["mux0"]
    item.mousePressEvent(_FakeEvent())
    item.setPos(item.pos().x() + 100, item.pos().y())
    item.mouseReleaseEvent(_FakeEvent())
    expected_points = list(wire_adc.edge.points)
    assert expected_points  # sanity: still explicit

    win._on_save_layout()

    reloaded = load_topology(engine.topology_path, engine.model)
    reloaded_edge = next(e for e in reloaded.edges
                         if e.src == "adc1" and e.dst == "mux0")
    assert reloaded_edge.points == expected_points


def test_live_move_tracking_resets_on_non_drag_geometry_changes(qtbot):
    # Correctness guard (not directly asked for by the finding, but
    # load-bearing for it): if a block's position changes via undo/
    # revert/auto-layout rather than a live drag, any in-progress
    # live-move tracking baseline must be invalidated - otherwise the
    # NEXT drag's first live-move event would diff the block's new
    # position against a STALE remembered one and apply a wildly wrong
    # delta to any glued wire endpoint.
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    item = win.blocks["mux0"]

    item.mousePressEvent(_FakeEvent())
    item.setPos(item.pos().x() + 50, item.pos().y())
    assert win._live_move_tracking is not None
    item.mouseReleaseEvent(_FakeEvent())
    assert win._live_move_tracking is None   # commit resets it

    item.mousePressEvent(_FakeEvent())
    item.setPos(item.pos().x() + 20, item.pos().y())
    assert win._live_move_tracking is not None
    win._on_auto_layout()   # bypasses the drag path entirely
    assert win._live_move_tracking is None


# -- M8 manual-gate finding 3: trackpad/wheel zoom -------------------------
#
# Root cause (verified): macOS trackpad wheel events typically carry
# angleDelta().y() == 0 (pixel-based scrolling), so the old
# `angleDelta().y() > 0 else zoom-out` logic fell into the zoom-out
# branch on every trackpad scroll. A real pinch gesture is a separate
# QEvent.NativeGesture entirely (hardware-only, not simulatable
# offscreen - noted for the user's manual re-test); these tests cover
# the wheelEvent path and the pure clamp helper.


def test_wheel_zero_delta_leaves_transform_unchanged(qtbot):
    from ui.main_window import _DiagramView
    view = _DiagramView()
    qtbot.addWidget(view)
    before = view.transform()

    view.wheelEvent(_FakeWheelEvent(pixel_dy=0, angle_dy=0))

    assert view.transform() == before
    assert view._zoom == 1.0


def test_wheel_prefers_pixel_delta_over_angle_delta(qtbot):
    from ui.main_window import _DiagramView
    view = _DiagramView()
    qtbot.addWidget(view)

    # pixelDelta says zoom IN; angleDelta (consulted only as a
    # fallback) says zoom OUT - pixelDelta must win since it is
    # non-zero, or the view would zoom the wrong way.
    view.wheelEvent(_FakeWheelEvent(pixel_dy=5, angle_dy=-120))

    assert view._zoom > 1.0


def test_wheel_falls_back_to_angle_delta_when_pixel_delta_is_zero(qtbot):
    from ui.main_window import _DiagramView
    view = _DiagramView()
    qtbot.addWidget(view)

    view.wheelEvent(_FakeWheelEvent(pixel_dy=0, angle_dy=120))
    assert view._zoom > 1.0
    grew = view._zoom

    view.wheelEvent(_FakeWheelEvent(pixel_dy=0, angle_dy=-120))
    assert view._zoom < grew


def test_clamped_zoom_factor_stays_within_bounds():
    from ui.main_window import _clamped_zoom_factor, _ZOOM_MAX, _ZOOM_MIN

    # in-range zoom: the requested factor passes through unchanged.
    assert _clamped_zoom_factor(1.0, 1.15) == pytest.approx(1.15)

    # already at the max: any further zoom-in is a no-op factor.
    assert _clamped_zoom_factor(_ZOOM_MAX, 1.15) == pytest.approx(1.0)

    # near the max: a big zoom-in factor is clamped to land EXACTLY at
    # the max rather than overshoot it.
    near_max = _ZOOM_MAX / 1.05
    result = _clamped_zoom_factor(near_max, 1.15)
    assert near_max * result == pytest.approx(_ZOOM_MAX)

    # already at the min: any further zoom-out is a no-op factor.
    assert _clamped_zoom_factor(_ZOOM_MIN, 1 / 1.15) == pytest.approx(1.0)


def test_repeated_wheel_zoom_clamps_at_min_and_max(qtbot):
    from ui.main_window import _DiagramView, _ZOOM_MAX, _ZOOM_MIN
    view = _DiagramView()
    qtbot.addWidget(view)

    for _ in range(200):
        view.wheelEvent(_FakeWheelEvent(pixel_dy=5))
    assert view._zoom == pytest.approx(_ZOOM_MAX)

    for _ in range(400):
        view.wheelEvent(_FakeWheelEvent(pixel_dy=-5))
    assert view._zoom == pytest.approx(_ZOOM_MIN)


def test_zoom_anchors_under_the_mouse_cursor(qtbot):
    # Acceptance round 6: Qt's default AnchorViewCenter made every
    # zoom-in drift away from what the user pointed at.
    from PySide6.QtWidgets import QGraphicsView
    from ui.main_window import _DiagramView
    view = _DiagramView()
    qtbot.addWidget(view)
    assert view.transformationAnchor() == QGraphicsView.AnchorUnderMouse


# -- M8 wave B1: arrow-key nudge -------------------------------------------


def test_block_click_in_edit_mode_selects(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["adc1"]
    item.set_editable(True)

    item.mousePressEvent(_FakeEvent())

    assert item.isSelected() is True


def test_nudge_block_moves_by_grid_step_and_fires_once(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["adc1"]
    calls = []
    state.on_geometry_changed = lambda: calls.append(item.geometry())
    item.set_editable(True)
    orig = item.geometry()

    item.keyPressEvent(_FakeKeyEvent(Qt.Key_Right))

    expected = (orig[0] + 10, orig[1], orig[2], orig[3])
    assert item.geometry() == expected
    assert (item.block.x, item.block.y) == expected[:2]
    assert calls == [expected]


def test_nudge_block_with_shift_moves_by_one_unit(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["adc1"]
    item.set_editable(True)
    orig = item.geometry()

    item.keyPressEvent(_FakeKeyEvent(Qt.Key_Down, modifiers=Qt.ShiftModifier))

    assert item.geometry() == (orig[0], orig[1] + 1, orig[2], orig[3])


def test_nudge_block_inert_outside_edit_mode(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["adc1"]
    calls = []
    state.on_geometry_changed = lambda: calls.append(1)
    orig = item.geometry()

    item.keyPressEvent(_FakeKeyEvent(Qt.Key_Right))   # never set_editable

    assert item.geometry() == orig
    assert calls == []


def test_nudge_block_glues_explicit_wire_endpoints_like_a_drag(qtbot):
    # M8 wave B post-review fix: a nudge is a move, and Visio-style
    # connector glue is input-agnostic - nudging mux0 (dst of two
    # explicit-path edges, src of one pointless edge) must glue the
    # explicit endpoints by the exact nudge delta, leave interior
    # points untouched, mark both wires dirty, and let one undo
    # restore both the block and both wires.
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    wire_adc = next(w for w in win.wires.values()
                   if w.edge.src == "adc1" and w.edge.dst == "mux0")
    wire_tim = next(w for w in win.wires.values()
                   if w.edge.src == "tim1" and w.edge.dst == "mux0")
    orig_adc = list(wire_adc.edge.points)
    orig_tim = list(wire_tim.edge.points)
    item = win.blocks["mux0"]
    orig_geom = item.geometry()

    item.keyPressEvent(_FakeKeyEvent(Qt.Key_Right))

    assert item.geometry()[:2] == (orig_geom[0] + 10, orig_geom[1])
    assert wire_adc.edge.points[-1] == (orig_adc[-1][0] + 10,
                                        orig_adc[-1][1])
    assert wire_tim.edge.points[-1] == (orig_tim[-1][0] + 10,
                                        orig_tim[-1][1])
    assert wire_adc.edge.points[:-1] == orig_adc[:-1]   # src anchor + interior
    assert wire_tim.edge.points[:-1] == orig_tim[:-1]
    assert win._layout_dirty is True
    assert wire_adc.edge_key() in win._dirty_wire_keys
    assert wire_tim.edge_key() in win._dirty_wire_keys

    win._on_layout_undo()

    assert item.geometry() == orig_geom
    assert wire_adc.edge.points == orig_adc
    assert wire_tim.edge.points == orig_tim


def test_shift_nudge_block_glues_explicit_wire_endpoints_by_one_unit(qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    wire_adc = next(w for w in win.wires.values()
                   if w.edge.src == "adc1" and w.edge.dst == "mux0")
    orig_adc = list(wire_adc.edge.points)
    item = win.blocks["mux0"]
    orig_geom = item.geometry()

    item.keyPressEvent(_FakeKeyEvent(Qt.Key_Down, modifiers=Qt.ShiftModifier))

    assert item.geometry()[:2] == (orig_geom[0], orig_geom[1] + 1)
    assert wire_adc.edge.points[-1] == (orig_adc[-1][0], orig_adc[-1][1] + 1)
    assert wire_adc.edge.points[:-1] == orig_adc[:-1]


def test_nudge_waypoint_handle_moves_by_grid_step_and_fires_once():
    wire, state, edge = _make_wire(points=[(10, 10), (50, 10), (90, 10)])
    wire.set_editable(True)
    calls = []
    state.on_geometry_changed = lambda: calls.append(list(edge.points))
    handle = wire._handles[1]   # interior point

    handle.keyPressEvent(_FakeKeyEvent(Qt.Key_Up))

    assert edge.points == [(10, 10), (50, 0), (90, 10)]
    assert [int(p.x()) for p in wire.pts] == [10, 50, 90]
    assert [int(p.y()) for p in wire.pts] == [10, 0, 10]
    assert calls == [[(10, 10), (50, 0), (90, 10)]]


def test_nudge_waypoint_handle_with_shift_moves_by_one_unit():
    wire, state, edge = _make_wire(points=[(10, 10), (50, 10), (90, 10)])
    wire.set_editable(True)
    handle = wire._handles[0]

    handle.keyPressEvent(_FakeKeyEvent(Qt.Key_Left, modifiers=Qt.ShiftModifier))

    assert edge.points[0] == (9, 10)


def test_nudge_legend_moves_by_grid_step_and_fires_once():
    state = DiagramState()
    legend = LegendItem(state)
    calls = []
    state.on_geometry_changed = lambda: calls.append(legend.geometry())
    legend.set_editable(True)
    orig = legend.geometry()

    legend.keyPressEvent(_FakeKeyEvent(Qt.Key_Down))

    expected = (orig[0], orig[1] + 10, 0, 0)
    assert legend.geometry() == expected
    assert calls == [expected]


def test_nudge_legend_inert_outside_edit_mode():
    state = DiagramState()
    legend = LegendItem(state)
    calls = []
    state.on_geometry_changed = lambda: calls.append(1)
    orig = legend.geometry()

    legend.keyPressEvent(_FakeKeyEvent(Qt.Key_Down))   # never set_editable

    assert legend.geometry() == orig
    assert calls == []


# -- M8 wave B2: endpoint-magnet visual feedback ---------------------------


def test_magnet_highlight_toggles_as_endpoint_handle_enters_and_leaves_range(
        qtbot):
    # adc1: x=70, y=280, w=140, h=80 -> right edge at x=210.
    _, state, scene, blocks, wires = _build(qtbot)
    for wire in wires.values():
        wire.refresh_auto_route(blocks)   # populates _src_item/_dst_item
    wire = next(w for w in wires.values()
               if w.edge.src == "adc1" and w.edge.dst == "mux0")
    wire.set_editable(True)
    handle = wire._handles[0]   # src anchor -> adc1
    adc1 = blocks["adc1"]

    handle.mousePressEvent(_FakeEvent(scene_pos=QPointF(210, 320)))
    assert adc1._magnet_highlighted is False   # no move yet

    handle.mouseMoveEvent(_FakeEvent(scene_pos=QPointF(205, 320)))  # in range
    assert adc1._magnet_highlighted is True

    handle.mouseMoveEvent(_FakeEvent(scene_pos=QPointF(100, 320)))  # far away
    assert adc1._magnet_highlighted is False

    handle.mouseMoveEvent(_FakeEvent(scene_pos=QPointF(205, 320)))  # back in
    assert adc1._magnet_highlighted is True

    handle.mouseReleaseEvent(_FakeEvent())
    assert adc1._magnet_highlighted is False   # clears on release


def test_magnet_highlight_never_set_for_interior_handles(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    for wire in wires.values():
        wire.refresh_auto_route(blocks)
    wire = next(w for w in wires.values()
               if w.edge.src == "adc1" and w.edge.dst == "mux0")
    wire.set_editable(True)
    handle = wire._handles[1]   # interior point
    adc1, mux0 = blocks["adc1"], blocks["mux0"]

    handle.mousePressEvent(_FakeEvent(scene_pos=QPointF(232, 320)))
    handle.mouseMoveEvent(_FakeEvent(scene_pos=QPointF(205, 320)))  # near adc1

    assert adc1._magnet_highlighted is False
    assert mux0._magnet_highlighted is False
    assert handle._magnet_target is None


def test_magnet_highlight_inert_on_a_standalone_wire_without_blocks():
    # _make_wire-built wires never had refresh_auto_route(blocks)
    # called, so _src_item/_dst_item are None - the highlight update
    # must degrade to a no-op rather than raise.
    wire, state, edge = _make_wire(points=[(10, 10), (50, 10), (90, 10)])
    wire.set_editable(True)
    handle = wire._handles[0]

    handle.mousePressEvent(_FakeEvent(scene_pos=QPointF(10, 10)))
    handle.mouseMoveEvent(_FakeEvent(scene_pos=QPointF(12, 12)))
    handle.mouseReleaseEvent(_FakeEvent())

    assert handle._magnet_target is None


# -- M8 wave B3: live coordinate readout -----------------------------------


def test_block_drag_emits_live_status_and_clears_on_release(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["adc1"]
    item.set_editable(True)
    calls = []
    state.on_live_status = calls.append

    item.mousePressEvent(_FakeEvent())
    item.setPos(item.pos().x() + 10, item.pos().y())

    assert calls[-1] == "adc1: %d, %d (%d x %d)" % item.geometry()

    item.mouseReleaseEvent(_FakeEvent())
    assert calls[-1] == ""


def test_block_resize_emits_live_status_and_clears_on_release(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["adc1"]
    item.set_editable(True)
    calls = []
    state.on_live_status = calls.append

    item.handle.mousePressEvent(_FakeEvent(scene_pos=QPointF(0, 0)))
    item.handle.mouseMoveEvent(_FakeEvent(scene_pos=QPointF(23, 23)))

    assert calls[-1] == "adc1: %d, %d (%d x %d)" % item.geometry()

    item.handle.mouseReleaseEvent(_FakeEvent())
    assert calls[-1] == ""


def test_waypoint_drag_emits_live_status_and_clears_on_release():
    wire, state, edge = _make_wire(points=[(10, 10), (50, 10), (90, 10)])
    wire.set_editable(True)
    calls = []
    state.on_live_status = calls.append
    handle = wire._handles[1]

    handle.mousePressEvent(_FakeEvent(scene_pos=QPointF(50, 10)))
    handle.mouseMoveEvent(_FakeEvent(scene_pos=QPointF(53, 18)))

    assert calls[-1] == "waypoint: 53, 18"

    handle.mouseReleaseEvent(_FakeEvent())
    assert calls[-1] == ""


def test_legend_drag_emits_live_status_and_clears_on_release():
    state = DiagramState()
    legend = LegendItem(state)
    legend.set_editable(True)
    calls = []
    state.on_live_status = calls.append

    legend.mousePressEvent(_FakeEvent())
    legend.setPos(legend.pos().x() + 10, legend.pos().y())

    assert calls[-1] == "legend: %d, %d" % (int(legend.pos().x()),
                                            int(legend.pos().y()))

    legend.mouseReleaseEvent(_FakeEvent())
    assert calls[-1] == ""


def test_main_window_shows_live_status_as_overlay_in_the_drawing_area(qtbot):
    # Acceptance round 6: the readout moved from the window-bottom
    # status bar to an overlay label inside the view - reading the
    # status bar mid-drag meant taking eyes off the diagram.
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    item = win.blocks["adc1"]

    item.mousePressEvent(_FakeEvent())
    item.setPos(item.pos().x() + 10, item.pos().y())

    assert win.live_coord_label.isVisible() is True
    assert (win.live_coord_label.text()
           == "adc1: %d, %d (%d x %d)" % item.geometry())
    assert win.statusBar().currentMessage() == ""   # bar stays free

    item.mouseReleaseEvent(_FakeEvent())
    assert win.live_coord_label.isVisible() is False


# -- M8 wave B4a: dynamic alignment guides - detection (snap) --------------


def test_compute_alignment_snap_matches_closest_same_type_line():
    from ui.diagram.items import _compute_alignment_snap
    others = [(500, 500, 160, 100)]

    sx, sy, gx, gy = _compute_alignment_snap(497, 900, 45, 90, others)

    assert (sx, gx) == (500, 500)   # left(497) -> left(500), within 6px
    assert (sy, gy) == (900, None)   # no y-axis line anywhere near 900


def test_compute_alignment_snap_no_match_outside_threshold():
    from ui.diagram.items import _compute_alignment_snap
    others = [(500, 500, 160, 100)]

    sx, sy, gx, gy = _compute_alignment_snap(480, 900, 45, 90, others)

    assert (sx, gx) == (480, None)   # 20px away - beyond the 6px threshold


def test_compute_alignment_snap_picks_closest_among_multiple_others():
    from ui.diagram.items import _compute_alignment_snap
    # left lines at 505 and 498; candidate's own left is 500.
    others = [(505, 0, 10, 10), (498, 0, 10, 10)]

    sx, sy, gx, gy = _compute_alignment_snap(500, 0, 10, 10, others)

    assert gx == 498   # |498-500|=2 is closer than |505-500|=5
    assert sx == 498


def test_block_drag_snaps_to_alignment_with_another_block(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    dma2 = blocks["dma2"]
    other = blocks["mux0"]
    # Controlled coordinates (apply_geometry bypasses snap) so only
    # the ONE alignment under test can possibly match.
    dma2.apply_geometry(500, 500, 160, 100)
    other.apply_geometry(200, 900, 45, 90)
    other.set_editable(True)

    other.setPos(497, 900)   # 3px short of dma2's left edge (500)

    assert (other.pos().x(), other.pos().y()) == (500, 900)
    assert other._active_guides == (500, None)


def test_block_drag_no_alignment_when_out_of_range(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    dma2 = blocks["dma2"]
    other = blocks["mux0"]
    dma2.apply_geometry(500, 500, 160, 100)
    other.apply_geometry(200, 900, 45, 90)
    other.set_editable(True)

    other.setPos(480, 900)   # 20px short - beyond the 6px threshold

    assert (other.pos().x(), other.pos().y()) == (480, 900)   # grid-snap only
    assert other._active_guides == (None, None)


def test_block_drag_shift_bypasses_alignment_snap(qtbot, monkeypatch):
    _, state, scene, blocks, wires = _build(qtbot)
    dma2 = blocks["dma2"]
    other = blocks["mux0"]
    dma2.apply_geometry(500, 500, 160, 100)
    other.apply_geometry(200, 900, 45, 90)
    other.set_editable(True)
    monkeypatch.setattr("ui.diagram.items._fine_snap", lambda: True)

    other.setPos(497, 900)   # within alignment range, but Shift held

    assert (other.pos().x(), other.pos().y()) == (497, 900)   # unsnapped
    assert other._active_guides == (None, None)


# -- M8 wave B4b: dynamic alignment guides - rendering ----------------------


def test_alignment_guide_visible_with_correct_coords_when_in_range(qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    dma2 = win.blocks["dma2"]
    other = win.blocks["mux0"]
    dma2.apply_geometry(500, 500, 160, 100)
    other.apply_geometry(200, 900, 45, 90)
    assert win._guide_v.isVisible() is False

    other.mousePressEvent(_FakeEvent())
    other.setPos(497, 900)   # 3px short of dma2's left edge (500)

    assert win._guide_v.isVisible() is True
    line = win._guide_v.line()
    assert (line.x1(), line.x2()) == (500, 500)
    bounds = win._diagram_bounds()
    assert (line.y1(), line.y2()) == (bounds.top(), bounds.bottom())
    assert win._guide_h.isVisible() is False   # no y-axis match here


def test_alignment_guide_hidden_when_out_of_range(qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    dma2 = win.blocks["dma2"]
    other = win.blocks["mux0"]
    dma2.apply_geometry(500, 500, 160, 100)
    other.apply_geometry(200, 900, 45, 90)

    other.mousePressEvent(_FakeEvent())
    other.setPos(480, 900)   # 20px short - beyond the 6px threshold

    assert win._guide_v.isVisible() is False
    assert win._guide_h.isVisible() is False


def test_alignment_guide_clears_on_release_and_on_commit(qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    dma2 = win.blocks["dma2"]
    other = win.blocks["mux0"]
    dma2.apply_geometry(500, 500, 160, 100)
    other.apply_geometry(200, 900, 45, 90)

    other.mousePressEvent(_FakeEvent())
    other.setPos(497, 900)
    assert win._guide_v.isVisible() is True

    other.mouseReleaseEvent(_FakeEvent())   # commits -> _on_layout_geometry_changed
    assert win._guide_v.isVisible() is False
    assert win._guide_h.isVisible() is False


def test_alignment_guide_clears_on_mode_exit(qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    dma2 = win.blocks["dma2"]
    other = win.blocks["mux0"]
    dma2.apply_geometry(500, 500, 160, 100)
    other.apply_geometry(200, 900, 45, 90)

    other.mousePressEvent(_FakeEvent())
    other.setPos(497, 900)
    assert win._guide_v.isVisible() is True

    win.edit_layout_btn.setChecked(False)   # exit edit mode mid-drag

    assert win._guide_v.isVisible() is False
    assert win._guide_h.isVisible() is False


# -- M8 wave B fix round 1: unified gesture-visual cancellation ------------
#
# The review's core finding: alignment guides, magnet highlight, and
# the status-bar readout are all driven by mouse-MOVE events, which a
# normal mouseReleaseEvent clears - but nothing forces a release to
# ever happen. Every test here drives a gesture HALFWAY (press + move,
# deliberately no release) and then triggers a terminator that is NOT
# a release, asserting the visual state comes back clean anyway.


def test_mode_exit_mid_block_drag_clears_readout(qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    item = win.blocks["adc1"]

    item.mousePressEvent(_FakeEvent())
    item.setPos(item.pos().x() + 10, item.pos().y())
    assert win.live_coord_label.isVisible() is True

    win.edit_layout_btn.setChecked(False)   # terminator: mode exit, no release

    assert win.live_coord_label.isVisible() is False


def test_mode_exit_mid_waypoint_drag_clears_magnet_highlight(qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    wire = next(w for w in win.wires.values()
               if w.edge.src == "adc1" and w.edge.dst == "mux0")
    handle = wire._handles[0]   # src anchor -> adc1
    adc1 = win.blocks["adc1"]

    handle.mousePressEvent(_FakeEvent(scene_pos=QPointF(210, 320)))
    handle.mouseMoveEvent(_FakeEvent(scene_pos=QPointF(205, 320)))  # in range
    assert adc1._magnet_highlighted is True

    win.edit_layout_btn.setChecked(False)   # terminator: mode exit, no release

    assert adc1._magnet_highlighted is False


def test_wire_clear_handles_clears_magnet_highlight_even_without_release(
        qtbot):
    # Item-level half of the same fix (finding 2), tested directly
    # against WireItem rather than through MainWindow's toggle.
    _, state, scene, blocks, wires = _build(qtbot)
    for wire in wires.values():
        wire.refresh_auto_route(blocks)
    wire = next(w for w in wires.values()
               if w.edge.src == "adc1" and w.edge.dst == "mux0")
    wire.set_editable(True)
    handle = wire._handles[0]
    adc1 = blocks["adc1"]

    handle.mousePressEvent(_FakeEvent(scene_pos=QPointF(210, 320)))
    handle.mouseMoveEvent(_FakeEvent(scene_pos=QPointF(205, 320)))
    assert adc1._magnet_highlighted is True

    wire.set_editable(False)   # destroys the handle mid-drag, no release

    assert adc1._magnet_highlighted is False


def test_undo_mid_drag_clears_alignment_guides(qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    dma2 = win.blocks["dma2"]
    other = win.blocks["mux0"]
    dma2.apply_geometry(500, 500, 160, 100)
    other.apply_geometry(200, 900, 45, 90)

    # a completed gesture first, so the undo stack has something to pop.
    other.mousePressEvent(_FakeEvent())
    other.setPos(other.pos().x() + 20, other.pos().y())
    other.mouseReleaseEvent(_FakeEvent())
    assert win._undo_stack

    # a SECOND drag, mid-gesture (no release), that raises a guide.
    other.mousePressEvent(_FakeEvent())
    other.setPos(497, 900)
    assert win._guide_v.isVisible() is True

    win._on_layout_undo()   # terminator: undo, no release

    assert win._guide_v.isVisible() is False
    assert win._guide_h.isVisible() is False


def test_revert_mid_drag_cancels_gesture_visuals(qtbot, tmp_path):
    tdir = tmp_path / "f411"
    shutil.copytree("targets/f411", tdir)
    engine, win = _build_window(qtbot, str(tdir))
    win.edit_layout_btn.setChecked(True)
    item = win.blocks["adc1"]

    item.mousePressEvent(_FakeEvent())
    item.setPos(item.pos().x() + 10, item.pos().y())
    assert win.live_coord_label.isVisible() is True

    win._on_revert_layout()   # terminator: revert, no release

    assert win.live_coord_label.isVisible() is False


def test_auto_layout_mid_drag_cancels_gesture_visuals(qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    item = win.blocks["adc1"]

    item.mousePressEvent(_FakeEvent())
    item.setPos(item.pos().x() + 10, item.pos().y())
    assert win.live_coord_label.isVisible() is True

    win._on_auto_layout()   # terminator: auto-layout, no release

    assert win.live_coord_label.isVisible() is False


# -- M8 wave B fix round 1 (finding 4): edit-mode selection indicator ------


def _paint_block(item):
    """Same offscreen-render precedent as _paint_wire, above - proves
    the new selection-indicator branch does not crash, not just that
    the flags it reads are set correctly."""
    img = QImage(300, 300, QImage.Format_ARGB32)
    painter = QPainter(img)
    try:
        item.paint(painter, None)
    finally:
        painter.end()


def test_block_click_in_edit_mode_sets_the_selection_indicator_condition(
        qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["adc1"]
    item.set_editable(True)
    state.edit_mode = True

    item.mousePressEvent(_FakeEvent())

    assert item.isSelected() is True
    assert state.edit_mode is True   # exactly paint()'s branch condition
    _paint_block(item)   # renders without crashing


def test_block_selection_indicator_condition_false_outside_edit_mode(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    item = blocks["adc1"]
    state.edit_mode = False

    _paint_block(item)   # never selected, never edit_mode - renders fine

    assert item.isSelected() is False
    assert state.edit_mode is False


def test_legend_click_in_edit_mode_sets_the_selection_indicator_condition():
    state = DiagramState()
    legend = LegendItem(state)
    legend.set_editable(True)
    state.edit_mode = True

    legend.mousePressEvent(_FakeEvent())

    assert legend.isSelected() is True
    assert state.edit_mode is True
    _paint_block(legend)   # renders without crashing


def test_legend_selection_indicator_condition_false_outside_edit_mode():
    state = DiagramState()
    legend = LegendItem(state)
    state.edit_mode = False

    _paint_block(legend)

    assert legend.isSelected() is False
    assert state.edit_mode is False


# -- M8 wave B fix round 2 (regression): normal-mode click must not --------
# clear the persistent status line
#
# BlockItem/LegendItem.mouseReleaseEvent's on_live_status("") backstop was
# ungated (finding 3, above) so it fired on every release - including an
# ordinary NORMAL-mode click, since both items accept presses outside edit
# mode too (they drive inspector clicks). That clobbered
# MainWindow.on_state's persistent "poller: ..." connection line on every
# block/legend click. The mode-exit-mid-drag scenario the ungated clear
# was guarding against is already covered synchronously by
# MainWindow._cancel_gesture_visuals (toggle-off calls it BEFORE the
# set_editable(False) loop runs), so gating the clear back onto
# self._editable loses nothing.


def test_normal_mode_click_leaves_persistent_status_message_alone(qtbot):
    engine, win = _build_window(qtbot)
    win.statusBar().showMessage("poller: running")
    item = win.blocks["adc1"]

    # NORMAL mode: edit_layout_btn never toggled, item never made
    # editable - an ordinary inspector click, not a drag.
    item.mousePressEvent(_FakeEvent())
    item.mouseReleaseEvent(_FakeEvent())

    assert win.statusBar().currentMessage() == "poller: running"
    assert win.live_coord_label.isVisible() is False

    # The readout lives in the overlay now (acceptance round 6): an
    # edit-mode drag never touches the status bar at all, and the
    # overlay clears on release.
    win.edit_layout_btn.setChecked(True)
    _drag_block(item, item.pos().x() + 10, item.pos().y())

    assert win.statusBar().currentMessage() == "poller: running"
    assert win.live_coord_label.isVisible() is False


# -- M8 wave B fix round 3: user-acceptance finding 1 - "ADC1 flies -------
# far away on a slight drag"
#
# Controller hypothesis (a broken/inverted threshold, or the wrong pair
# of alignment lines, in _compute_alignment_snap) did NOT reproduce
# empirically: both a 200k-sample fuzz of _compute_alignment_snap
# against the real f411 block set and a full Qt-event-driven drag
# simulation stayed within the threshold on every call - the search
# loop already only ever accepts a candidate with abs(d) <= threshold.
# The two tests below codify that bound as a permanent guarantee
# anyway (round 3 also makes it hold BY CONSTRUCTION - max/min-clamped
# in _compute_alignment_snap - rather than only as a consequence of
# the search loop's own gating, so a future change to the search can
# never silently regress it).
#
# The ACTUAL root cause: _DiagramView._apply_zoom rescaled the view's
# transform unconditionally, including while a drag was in flight
# (scroll-wheel or trackpad-pinch zoom firing mid-drag - very
# plausible, since it is the same input device the drag itself came
# from, and this app's own startup fit_view() call demonstrates the
# same failure mode from a completely different trigger). Qt's default
# ItemIsMovable drag tracking remaps each step's proposed position
# through the item's CURRENT local coordinate frame while still
# subtracting the button-down offset captured under the OLD transform
# - a mismatch unrelated to any real mouse movement, which
# BlockItem.itemChange's snap then only ever bounds relative to
# (i.e. the now-corrupted "raw" position it was handed), not relative
# to the gesture's true trajectory. See test_zoom_is_ignored_while_a_
# gesture_is_in_flight for that fix's regression test.


def test_alignment_snap_stays_within_threshold_for_a_distant_shared_edge(
        qtbot):
    # The controller's prescribed repro: two blocks far apart in y but
    # sharing dma2's left edge exactly (mirrors ADC1/CM4's real,
    # coincidental shared left edge at x=70) - dragging dma2 a few px
    # along the OTHER axis (y) must never let that distant shared edge
    # pull either axis more than _ALIGN_THRESHOLD from the raw drag
    # target.
    _, state, scene, blocks, wires = _build(qtbot)
    dma2 = blocks["dma2"]
    far = blocks["mux0"]
    dma2.apply_geometry(500, 500, 160, 100)
    far.apply_geometry(500, 9000, 45, 90)   # shares dma2's left edge, far in y
    dma2.set_editable(True)

    dma2.setPos(500, 520)   # a "few px" drag, y only

    x, y = dma2.pos().x(), dma2.pos().y()
    assert abs(x - 500) <= _ALIGN_THRESHOLD
    assert abs(y - 520) <= _ALIGN_THRESHOLD


def test_alignment_snap_bound_holds_across_the_f411_scene(qtbot):
    # Property-style sweep: every block in the real f411 scene, as the
    # dragged candidate, against every OTHER real block's geometry as
    # alignment candidates, for a spread of raw drag offsets covering
    # near-misses, exact edge matches, threshold-boundary values, and
    # far-away positions - the structural bound must hold everywhere.
    _, state, scene, blocks, wires = _build(qtbot)
    geoms = {bid: item.geometry() for bid, item in blocks.items()}

    offsets = [-2000, -500, -100, -6.5, -3, 0, 3, 6.5, 100, 500, 2000]
    for bid, (bx, by, bw, bh) in geoms.items():
        others = [g for oid, g in geoms.items() if oid != bid]
        for dx in offsets:
            for dy in offsets:
                x, y = bx + dx, by + dy
                sx, sy, _, _ = _compute_alignment_snap(x, y, bw, bh, others)
                assert abs(sx - x) <= _ALIGN_THRESHOLD + 1e-9
                assert abs(sy - y) <= _ALIGN_THRESHOLD + 1e-9


def test_zoom_is_ignored_while_a_gesture_is_in_flight(qtbot):
    # Root cause fix: _apply_zoom (the one choke point both wheelEvent
    # and the trackpad-pinch event() handler funnel through) must
    # no-op while the scene has an active mouseGrabberItem - grabbing
    # the mouse is exactly what Qt's own event dispatch does at the
    # start of any block/legend/waypoint drag, resize, or
    # waypoint-handle gesture.
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    item = win.blocks["adc1"]
    before = win.view._zoom

    item.grabMouse()
    win.view._apply_zoom(0.85)
    assert win.view._zoom == before   # ignored: a gesture is in flight

    item.ungrabMouse()
    win.view._apply_zoom(0.85)
    assert win.view._zoom != before   # works again once the grab ends


# -- M8 wave B fix round 4: exclusive selection in edit mode --------------
#
# User-acceptance finding: clicking three items one after another left
# ALL THREE showing the teal selection outline - setSelected(True)
# alone is ADDITIVE (matches multiple items' own selection state). A
# plain click's normal exclusivity comes from QGraphicsItem's
# base-class mousePressEvent, which clears the scene's previous
# selection before selecting the clicked item - but every edit-mode
# press site here fully consumes its own event (ev.accept(), no
# super().mousePressEvent(ev) call), so that base-class clearing never
# ran. Fixed via _select_exclusively (items.py): scene().clearSelection()
# before setSelected(True), guarded for items under test construction
# that were never added to a scene. Multi-select (Ctrl-click) is an
# explicit backlog exclusion - single, exclusive selection is the
# spec'd model.


def test_edit_mode_click_selection_is_exclusive(qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    a = win.blocks["adc1"]
    b = win.blocks["cm4"]
    legend = win.legend

    a.mousePressEvent(_FakeEvent())
    assert a.isSelected() is True

    b.mousePressEvent(_FakeEvent())
    assert a.isSelected() is False
    assert b.isSelected() is True

    # a THIRD item, and a different item TYPE too - the same scene-wide
    # selection group spans BlockItem and LegendItem alike.
    legend.mousePressEvent(_FakeEvent())
    assert a.isSelected() is False
    assert b.isSelected() is False
    assert legend.isSelected() is True


def test_empty_diagram_space_click_clears_selection(qtbot):
    # Unlike every other test in this file, this one goes through a
    # REAL Qt mouse click (qtbot.mouseClick on the actual viewport)
    # rather than calling an item's own handler directly - there is no
    # item-level handler to call here, since a click on truly empty
    # scene space never reaches any BlockItem/LegendItem/WaypointHandle
    # at all. This is QGraphicsScene's own default behavior (clear
    # selection on an unhandled plain click, no modifier held) - it
    # already held before this fix (verified empirically) and keeps
    # holding now that every edit-mode press site stopped fully
    # consuming its own event; this locks that in.
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    QApplication.processEvents()   # flush the deferred startup fit_view()
    QApplication.processEvents()
    item = win.blocks["adc1"]
    item.mousePressEvent(_FakeEvent())
    assert item.isSelected() is True

    empty_pt = QPoint(3, 3)   # viewport corner - empty once fit_view has run
    assert (win.view.scene().itemAt(win.view.mapToScene(empty_pt),
                                    win.view.transform())
           is None)
    qtbot.mouseClick(win.view.viewport(), Qt.LeftButton, pos=empty_pt)

    assert item.isSelected() is False


def test_nudge_after_reselect_moves_only_the_newly_selected_block(qtbot):
    _, state, scene, blocks, wires = _build(qtbot)
    a = blocks["adc1"]
    b = blocks["cm4"]
    a.set_editable(True)
    b.set_editable(True)
    a_orig = a.geometry()
    b_orig = b.geometry()

    a.mousePressEvent(_FakeEvent())
    a.keyPressEvent(_FakeKeyEvent(Qt.Key_Right))
    assert a.geometry() == (a_orig[0] + 10, a_orig[1], a_orig[2], a_orig[3])
    assert b.geometry() == b_orig   # unaffected by a's nudge

    b.mousePressEvent(_FakeEvent())
    assert a.isSelected() is False   # b's press deselected a
    b.keyPressEvent(_FakeKeyEvent(Qt.Key_Right))
    assert b.geometry() == (b_orig[0] + 10, b_orig[1], b_orig[2], b_orig[3])
    # a stays exactly where its own nudge left it - the second nudge,
    # aimed at b, must not also move a.
    assert a.geometry() == (a_orig[0] + 10, a_orig[1], a_orig[2], a_orig[3])


# -- M8 wave B fix round 5: alignment snap bounded on the LIVE drag ---------
# path (user-acceptance finding, round 2)
#
# Round 1 added a threshold clamp inside _compute_alignment_snap and
# fixed ONE trigger (gating _apply_zoom while a gesture is in flight).
# The bug survived: dragging Flash "slightly" teleported it so its
# left edge landed on PA1's x (8) - a ~770px jump. Round 1's own
# property test called BlockItem.itemChange via a direct .setPos(...),
# which never exercises the actual corruption - that lives entirely
# inside Qt's OWN default ItemIsMovable mouse-move handling
# (event->pos() minus buttonDownPos(), both item-local), which only
# runs when Qt's real per-item drag tracking is driven by REAL mouse
# events (QTest.mousePress/mouseMove below), not when test code calls
# .setPos() directly with a value it chose itself.
#
# Root cause (round 2): NOT the pure function (still clamped, still
# correct in isolation - the sweep below keeps proving that) and NOT
# only _apply_zoom - a plain window/dock RESIZE mid-drag reproduces
# the identical corruption with no zoom call anywhere involved
# (resizing the viewport shifts its scrollbar range/position even
# with the transform's scale left untouched, which is enough by
# itself to desync event->pos() from the stale buttonDownPos() Qt
# captured at press). Fixed generically via _GestureMappingGuard
# (items.py): rather than gate each individual triggering call
# (zoom today, resize now, whatever else tomorrow), BlockItem and
# LegendItem now detect directly whether the view's widget-to-scene
# mapping has moved since the gesture started, for ANY reason, and if
# so discard every remaining itemChange proposal for that gesture
# outright (freezing the item) rather than trust a value that cannot
# be distinguished from real mouse movement once corrupted - a
# helper's own clamp can only ever bound a correction RELATIVE TO its
# input, and an already-corrupted input stays corrupted no matter how
# tightly that correction is bounded. BlockItem.itemChange also now
# re-applies the alignment/grid-snap threshold budget a second time,
# directly at the application site, as explicit defense in depth.


def test_window_resize_mid_drag_does_not_teleport_the_block(qtbot):
    # THE repro: real mousePressEvent + real Qt-driven ItemPositionChange
    # (QTest-simulated mouse events, not a direct .setPos() call) on a
    # scene-mounted, edit-mode BlockItem, with another block (pa1) far
    # away sharing flash's... no, actually pa1 shares NOTHING with
    # flash's real geometry going in - the whole point is that the
    # pre-fix bug did not need a legitimate nearby alignment candidate
    # at all: the corrupted value it fed into snap could coincidentally
    # land near ANY other block's line. flash/pa1 is exactly the pair
    # from the user's screenshot.
    engine, win = _build_window(qtbot)
    QApplication.processEvents()   # flush the deferred startup fit_view()
    QApplication.processEvents()
    win.edit_layout_btn.setChecked(True)
    view = win.view
    flash = win.blocks["flash"]
    pa1 = win.blocks["pa1"]
    before = flash.geometry()
    assert before[0] != pa1.geometry()[0]   # not already aligned

    scene_pos = flash.mapToScene(flash.boundingRect().center())
    start = view.mapFromScene(scene_pos)
    QTest.mousePress(view.viewport(), Qt.LeftButton, Qt.NoModifier, start)
    QApplication.processEvents()

    x, y = start.x() + 2, start.y() + 2
    QTest.mouseMove(view.viewport(), QPoint(int(x), int(y)))
    QApplication.processEvents()

    # the disruption: an ordinary window resize mid-drag - no zoom
    # call anywhere in this test.
    win.resize(1000, 700)
    QApplication.processEvents()

    x += 2
    y += 2
    QTest.mouseMove(view.viewport(), QPoint(int(x), int(y)))
    QApplication.processEvents()

    QTest.mouseRelease(view.viewport(), Qt.LeftButton, Qt.NoModifier,
                       QPoint(int(x), int(y)))
    QApplication.processEvents()

    after = flash.geometry()
    # bounded to a "slight drag" worth of movement, not a teleport
    # anywhere near pa1's x=8 (roughly 770 scene units away).
    assert abs(after[0] - before[0]) <= 20
    assert abs(after[1] - before[1]) <= 20


def test_alignment_snap_still_works_near_a_legit_match_after_the_fix(qtbot):
    # The bound must not kill the feature: an ordinary, undisrupted
    # drag that comes within _ALIGN_THRESHOLD of another block's edge
    # must still snap onto it exactly, through the SAME real-view path
    # (_build_window, not the view-less _build) the guard above now
    # runs on for every gesture.
    engine, win = _build_window(qtbot)
    QApplication.processEvents()
    QApplication.processEvents()
    win.edit_layout_btn.setChecked(True)
    dma2 = win.blocks["dma2"]
    other = win.blocks["mux0"]
    dma2.apply_geometry(500, 500, 160, 100)
    other.apply_geometry(200, 900, 45, 90)

    other.mousePressEvent(_FakeEvent())   # arms this gesture's guard
    other.setPos(497, 900)   # 3px short of dma2's left edge (500)

    assert (other.pos().x(), other.pos().y()) == (500, 900)
    assert other._active_guides == (500, None)


# -- M8 wave B fix round 6: stale movingItemsInitialPositions ---------------
# (user-acceptance finding, round 3 - the drag-teleport that survived
# rounds 1 AND 2)
#
# The user's repro needed NO zoom and NO resize: grab a block, move it
# slightly, and it lands tucked into the view's top-left corner (its
# left edge on PA1's x=8 alignment line - the same landing spot as the
# round-1 screenshot). The missing ingredient in every previous repro
# attempt: a PRIOR COMPLETED drag in the same session. Qt's default
# ItemIsMovable mouse-move handler computes every step as
#
#     item->setPos(initialPositions.value(item)
#                  + currentParentPos - buttonDownParentPos)
#
# where initialPositions is QGraphicsScenePrivate::
# movingItemsInitialPositions - populated on the FIRST move of a drag
# ONLY IF EMPTY, and cleared in exactly ONE place in all of Qt: the
# base QGraphicsItem::mouseReleaseEvent. BlockItem/LegendItem's
# overridden release handlers consumed the event without ever calling
# super(), so the very first completed drag left the map permanently
# holding {that item: its pre-drag pos}. Every later drag of a
# DIFFERENT item then finds the map non-empty (so the refill is
# skipped), reads .value(item) == default-constructed QPointF(0, 0)
# for itself, and teleports to (0, 0) + the slight drag delta - the
# origin corner, where the alignment snap then happily parks its left
# edge on pa1's x=8. A second drag of the SAME item instead jumps
# back to its stale pre-first-drag position. buttonDownScenePos is
# NOT the culprit - the scene stores that on press-accept regardless
# of super() (verified by instrumentation: it was correct in every
# corrupted gesture).
#
# Why rounds 1-2 could not catch it: their real-event repros all drove
# ONE drag per fresh window - and the first drag of a session finds
# the map empty, populates it correctly, and works perfectly. The
# corruption only exists from the second gesture onward.
#
# Fix: BlockItem/LegendItem now route real mouse events through the
# base-class press/release handlers (items.py _run_base_mouse_handler)
# so Qt's own bookkeeping - movingItemsInitialPositions above all -
# is armed and torn down exactly as QGraphicsItem requires.


def _real_drag(view, item, dx, dy):
    """One complete QTest-driven drag gesture - real press, one real
    move, real release, all through the view's viewport - grabbing
    `item` at its center and dragging by (dx, dy) viewport pixels."""
    scene_pos = item.mapToScene(item.boundingRect().center())
    start = view.mapFromScene(scene_pos)
    QTest.mousePress(view.viewport(), Qt.LeftButton, Qt.NoModifier, start)
    QApplication.processEvents()
    end = QPoint(start.x() + dx, start.y() + dy)
    QTest.mouseMove(view.viewport(), end)
    QApplication.processEvents()
    QTest.mouseRelease(view.viewport(), Qt.LeftButton, Qt.NoModifier, end)
    QApplication.processEvents()


def test_second_drag_of_another_block_does_not_teleport_to_origin(qtbot):
    # THE round-3 user repro: one completed drag (dma2), then a slight
    # drag of a different block (flash). Pre-fix, flash lands at
    # (8, 10) - a 772px teleport into the view's top-left corner.
    engine, win = _build_window(qtbot)
    QApplication.processEvents()   # flush the deferred startup fit_view()
    QApplication.processEvents()
    win.edit_layout_btn.setChecked(True)

    _real_drag(win.view, win.blocks["dma2"], 12, 12)   # completes fine

    flash = win.blocks["flash"]
    before = flash.geometry()
    _real_drag(win.view, flash, 6, 6)
    after = flash.geometry()
    # bounded to a "slight drag" worth of movement, not a teleport
    assert abs(after[0] - before[0]) <= 20
    assert abs(after[1] - before[1]) <= 20


def test_second_drag_of_the_same_block_does_not_jump_back(qtbot):
    # The same stale map, other signature: the map still holds THIS
    # block's pre-first-drag position, so a second slight drag
    # teleports it back to where the first drag started from.
    engine, win = _build_window(qtbot)
    QApplication.processEvents()
    QApplication.processEvents()
    win.edit_layout_btn.setChecked(True)
    flash = win.blocks["flash"]

    _real_drag(win.view, flash, 60, 60)   # a LARGE first drag

    before = flash.geometry()
    _real_drag(win.view, flash, 6, 6)
    after = flash.geometry()
    assert abs(after[0] - before[0]) <= 20
    assert abs(after[1] - before[1]) <= 20


def test_block_drag_after_a_legend_drag_does_not_teleport(qtbot):
    # LegendItem is the other ItemIsMovable item riding Qt's default
    # drag handler - a completed legend drag poisons the same map, and
    # the next block drag (adc1, the user's second screenshot) flies.
    engine, win = _build_window(qtbot)
    QApplication.processEvents()
    QApplication.processEvents()
    win.edit_layout_btn.setChecked(True)

    _real_drag(win.view, win.legend, 12, 12)   # completes fine

    adc1 = win.blocks["adc1"]
    before = adc1.geometry()
    _real_drag(win.view, adc1, 6, 6)
    after = adc1.geometry()
    assert abs(after[0] - before[0]) <= 20
    assert abs(after[1] - before[1]) <= 20


def test_auto_layout_parks_legend_below_blocks_and_undo_restores(qtbot):
    # Acceptance round 5: auto-layout repositioned every block but
    # left the legend at its stale pre-layout coordinates - on the
    # f411 diagram that parked the legend on top of the relocated bus
    # matrix. Auto-layout must move the legend too: below the whole
    # picture, the one region no block placement can collide with.
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)

    legend_before = win.legend.geometry()

    win._on_auto_layout()

    lx, ly, _, _ = win.legend.geometry()
    for item in win.blocks.values():
        x, y, w, h = item.geometry()
        assert ly >= y + h, "legend overlaps a block vertically"
    assert lx % 10 == 0 and ly % 10 == 0
    assert win._legend_moved is True   # Save must write the legend line

    win._on_layout_undo()
    assert win.legend.geometry() == legend_before


def test_real_click_reaches_an_endpoint_handle_on_a_block_boundary(qtbot):
    # Acceptance round 6: the DR wire's src endpoint handle sits ON
    # adc1's right edge. WireItem carries z=-1 (lines run under blocks
    # in normal view) and a child's stacking is resolved by its
    # top-level parent's z, so the handle subtree sat BELOW the block
    # - a real click there always hit the block and the endpoint was
    # undraggable ("the endpoint is stuck to the block"). Fake-event
    # tests call the handle's methods directly and skip hit-testing
    # entirely, which is how every magnet test stayed green over it.
    engine, win = _build_window(qtbot)
    QApplication.processEvents()
    QApplication.processEvents()
    win.edit_layout_btn.setChecked(True)

    wire = next(w for w in win.wires.values()
               if w.edge.src == "adc1" and w.edge.dst == "mux0")
    before = wire.edge.points[0]
    adc1_before = win.blocks["adc1"].geometry()

    # Press 4px INSIDE adc1's right edge - still well within the
    # 12px handle square centered on the boundary at (210, 320).
    # This is where a real fingertip lands; pressing the exact
    # boundary pixel dodges the bug because QRectF.contains()
    # excludes the right edge, so the block never contests it.
    start = win.view.mapFromScene(QPointF(206, 320))
    QTest.mousePress(win.view.viewport(), Qt.LeftButton,
                     Qt.NoModifier, start)
    QApplication.processEvents()
    end = QPoint(start.x(), start.y() - 40)
    QTest.mouseMove(win.view.viewport(), end)
    QApplication.processEvents()
    QTest.mouseRelease(win.view.viewport(), Qt.LeftButton,
                       Qt.NoModifier, end)
    QApplication.processEvents()

    after = wire.edge.points[0]
    assert after != before, "real drag never reached the handle"
    assert abs(after[1] - before[1]) >= 20
    # and the block itself must NOT have been the thing dragged
    assert win.blocks["adc1"].geometry() == adc1_before


# -- Acceptance round 7: auto-routed wires get endpoint handles too --------
# (user: two visually identical straight wires behaved differently
# depending on whether the yaml happened to carry a points: entry -
# every wire now shows draggable endpoints in edit mode; dragging one
# converts the wire to an explicit 2-point path, same conversion the
# double-click insert has always done)


def test_auto_routed_wire_shows_two_endpoint_handles_in_edit_mode(qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    wire = next(w for w in win.wires.values()
               if w.edge.src == "mux0" and w.edge.dst == "dma2")
    assert wire.edge.points == []          # sanity: auto-routed
    assert len(wire._handles) == 2
    assert wire._handles[0].pos() == wire.pts[0]
    assert wire._handles[1].pos() == wire.pts[-1]


def test_dragging_an_auto_wire_endpoint_converts_to_explicit_path(qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    wire = next(w for w in win.wires.values()
               if w.edge.src == "mux0" and w.edge.dst == "dma2")
    handle = wire._handles[0]
    start = wire.pts[0]

    handle.mousePressEvent(_FakeEvent(scene_pos=start))
    handle.mouseMoveEvent(_FakeEvent(
        scene_pos=QPointF(start.x(), start.y() - 30)))
    handle.mouseReleaseEvent(_FakeEvent())

    assert len(wire.edge.points) == 2      # converted, endpoint moved
    # release grid-snaps, and the auto route's endpoints are float
    # side-midpoints - assert within one grid step of the drag target
    assert abs(wire.edge.points[0][1] - (start.y() - 30)) <= 5
    assert abs(wire.edge.points[0][0] - start.x()) <= 5
    assert wire.edge.points[1] == (int(wire.pts[1].x()),
                                   int(wire.pts[1].y()))


def test_click_only_press_on_auto_wire_endpoint_stays_auto_routed(qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    wire = next(w for w in win.wires.values()
               if w.edge.src == "mux0" and w.edge.dst == "dma2")
    handle = wire._handles[0]

    handle.mousePressEvent(_FakeEvent(scene_pos=wire.pts[0]))
    handle.mouseReleaseEvent(_FakeEvent())

    assert wire.edge.points == []          # no silent pin-down


def test_auto_wire_endpoint_handles_follow_a_block_drag(qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    wire = next(w for w in win.wires.values()
               if w.edge.src == "mux0" and w.edge.dst == "dma2")

    _drag_block(win.blocks["dma2"], 400, 320)   # commit re-routes

    assert wire.edge.points == []          # still auto
    assert wire._handles[0].pos() == wire.pts[0]
    assert wire._handles[1].pos() == wire.pts[-1]


def test_delete_on_auto_wire_endpoint_handle_is_a_noop(qtbot):
    engine, win = _build_window(qtbot)
    win.edit_layout_btn.setChecked(True)
    wire = next(w for w in win.wires.values()
               if w.edge.src == "mux0" and w.edge.dst == "dma2")

    for handle in wire._handles:
        handle.keyPressEvent(_FakeKeyEvent(Qt.Key_Delete))

    assert wire.edge.points == []
    assert len(wire._handles) == 2
