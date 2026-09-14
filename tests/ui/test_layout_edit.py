"""M8 task 3: edit-mode input routing on diagram items - drag, snap,
resize. Built on the same construction pattern as test_scene.py
(Engine.load -> build_scene), driving BlockItem/WireItem/LegendItem's
own event handlers directly rather than going through Qt's event loop
(offscreen platform, no real mouse)."""
import dataclasses

from PySide6.QtCore import QPointF

from core.adapter.mock import MockAdapter
from core.engine.core import Engine
from ui.diagram.items import LegendItem, snap
from ui.diagram.scene import DiagramState, build_scene


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


def _build(qtbot):
    engine = Engine.load("targets/f411", MockAdapter({}))
    state = DiagramState()
    scene, blocks, wires = build_scene(engine.topology, state)
    return engine, state, scene, blocks, wires


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
