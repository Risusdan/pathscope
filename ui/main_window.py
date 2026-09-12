"""Main application window: central diagram view plus chrome toolbar.

Ported from prototype/ui_proto.py's Main class - toolbar construction
(stylesheet, actions) and freeze semantics are the same pattern, wired
to the real Engine/EngineBridge/DiagramState instead of the prototype's
StubEngine and stub-dict snapshots."""
from typing import Dict, List, Optional, Set, Tuple

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QPainter
from PySide6.QtWidgets import (QGraphicsView, QLabel, QMainWindow,
                               QSizePolicy, QToolBar, QWidget)

from core.engine.core import Engine, EngineError
from core.engine.rules import EngineUpdate
from core.target.topology import Topology

from .bridge import EngineBridge
from .diagram.items import MONO, LegendItem
from .diagram.scene import DiagramState, build_scene


class _DiagramView(QGraphicsView):
    """QGraphicsView with wheel-to-zoom. The prototype overrode
    wheelEvent on the QMainWindow itself; here it lives on the view
    widget directly, which receives wheel events unconditionally
    (independent of Qt's event-bubbling path for unhandled events)."""

    def wheelEvent(self, ev) -> None:
        factor = 1.15 if ev.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)


class MainWindow(QMainWindow):
    def __init__(self, engine: Engine, bridge: EngineBridge, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Data Path Explorer")
        self.resize(1280, 800)

        self.engine = engine
        self.bridge = bridge
        self.frozen = False
        self.last_update: Optional[EngineUpdate] = None

        self.diagram_state = DiagramState()
        self.scene, self.blocks, self.wires = build_scene(
            engine.topology, self.diagram_state)
        self.legend = LegendItem(self.diagram_state)
        self.scene.addItem(self.legend)

        self.view = _DiagramView(self.scene)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setCentralWidget(self.view)

        self._flow_edges = self._build_flow_edge_map(engine.topology,
                                                      engine.flowspec)
        self._progress_edge = self._build_progress_edge_map(
            engine.flowspec, self._flow_edges, self.wires)
        self._mux_select_ref = self._find_mux_select(engine)

        self._build_toolbar()

        self.bridge.update.connect(self.apply_update)
        self.bridge.state.connect(self.on_state)

        self.anim = QTimer(self)
        self.anim.setInterval(60)
        self.anim.timeout.connect(self._advance_dash)
        self.anim.start()

        QTimer.singleShot(0, self.fit_view)

    # -- one-time topology/flowspec derived maps ----------------------------

    @staticmethod
    def _build_flow_edge_map(topology: Topology, flowspec
                             ) -> Dict[str, Set[str]]:
        """An activity's edge set = edges whose (src, dst) are
        consecutive blocks in the activity's `path` (order-sensitive
        pairs) - same rule the prototype hardcoded by hand in its FLOWS
        dict, computed here from topology + flowspec instead."""
        pair_to_edges: Dict[Tuple[str, str], List[str]] = {}
        for edge in topology.edges:
            pair_to_edges.setdefault((edge.src, edge.dst), []).append(edge.id)
        result: Dict[str, Set[str]] = {}
        for act in flowspec.activities:
            edges: Set[str] = set()
            for a, b in zip(act.path, act.path[1:]):
                edges.update(pair_to_edges.get((a, b), []))
            result[act.name] = edges
        return result

    @staticmethod
    def _build_progress_edge_map(flowspec, flow_edges, wires
                                 ) -> Dict[str, str]:
        """For each activity that carries a progress register, pick one
        representative edge to carry the "REG 0xNNNN" text: the longest
        straight segment among the activity's edges, preferring a
        segment that is more horizontal than vertical (|dy| <= |dx|)
        when one is available.

        WireItem draws the progress text centered under the segment's
        midpoint with only a fixed +16px vertical offset (no
        perpendicular offset), so a steep diagonal segment sweeps
        through the text's full horizontal span and can cross a
        character - visible on the f411 demo target's dma2->busmx edge,
        whose slope is ~50 degrees because `busmx` has no declared
        ports (Task 8's straight-route fallback). Restricting the
        search to near-horizontal segments first sidesteps that
        collision (lands on busmx->sram1 for f411) while still falling
        back to the plain longest segment - even a diagonal one - if an
        activity has no near-horizontal edge at all, so progress text
        is never simply dropped."""
        result: Dict[str, str] = {}
        for act in flowspec.activities:
            if act.progress is None:
                continue
            best_id = best_len = None
            fallback_id = fallback_len = None
            for eid in flow_edges.get(act.name, ()):
                wire = wires.get(eid)
                if wire is None:
                    continue
                for a, b in zip(wire.pts, wire.pts[1:]):
                    dx, dy = b.x() - a.x(), b.y() - a.y()
                    length = (dx * dx + dy * dy) ** 0.5
                    if fallback_len is None or length > fallback_len:
                        fallback_len, fallback_id = length, eid
                    if abs(dy) <= abs(dx) \
                            and (best_len is None or length > best_len):
                        best_len, best_id = length, eid
            chosen = best_id if best_id is not None else fallback_id
            if chosen is not None:
                result[act.name] = chosen
        return result

    @staticmethod
    def _find_mux_select(engine: Engine):
        for block in engine.topology.blocks.values():
            if block.kind == "mux" and block.select:
                return engine.model.resolve(block.select)
        return None

    # -- chrome --------------------------------------------------------------

    def _build_toolbar(self) -> None:
        tb = QToolBar("main")
        tb.setMovable(False)
        tb.setStyleSheet(
            "QToolBar { spacing: 8px; padding: 4px; }"
            "QToolButton { padding: 7px 16px; font-size: 14px; }"
            "QLabel { font-size: 14px; }")
        self.addToolBar(tb)

        cpu_blocks = [b for b in self.engine.topology.blocks.values()
                     if b.kind == "cpu"]
        target_name = cpu_blocks[0].title if cpu_blocks else "target"
        self.target_label = QLabel("  %s  " % target_name)
        tb.addWidget(self.target_label)
        tb.addSeparator()

        self.halt_act = QAction("Halt", self)
        self.halt_act.triggered.connect(self._toggle_halt)
        tb.addAction(self.halt_act)

        self.freeze_act = QAction("Freeze", self)
        self.freeze_act.setCheckable(True)
        self.freeze_act.toggled.connect(self._toggle_freeze)
        tb.addAction(self.freeze_act)

        self.tint_act = QAction("Tint", self)
        self.tint_act.setCheckable(True)
        self.tint_act.setChecked(True)
        self.tint_act.toggled.connect(self._toggle_tint)
        tb.addAction(self.tint_act)

        self.fit_act = QAction("Fit", self)
        self.fit_act.triggered.connect(self.fit_view)
        tb.addAction(self.fit_act)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        self.rate_label = QLabel("poll -- Hz  ")
        self.rate_label.setFont(MONO)
        tb.addWidget(self.rate_label)

    def fit_view(self) -> None:
        rect = self.scene.itemsBoundingRect()
        if not rect.isEmpty():
            self.view.fitInView(rect, Qt.KeepAspectRatio)

    # -- actions ---------------------------------------------------------

    def _toggle_halt(self) -> None:
        halting = self.halt_act.text() == "Halt"
        try:
            if halting:
                self.engine.halt()
            else:
                self.engine.resume()
        except EngineError as e:
            self.statusBar().showMessage("error: %s" % e, 5000)
            return
        self.halt_act.setText("Resume" if halting else "Halt")

    def _toggle_freeze(self, on: bool) -> None:
        self.frozen = on
        if not on and self.last_update is not None:
            self._apply(self.last_update)

    def _toggle_tint(self, on: bool) -> None:
        self.diagram_state.tinted = on
        for item in self.blocks.values():
            item.update()
        self.legend.update()

    # -- live data -----------------------------------------------------------

    def apply_update(self, u: EngineUpdate) -> None:
        self.last_update = u
        if self.frozen:
            return
        self._apply(u)

    def _apply(self, u: EngineUpdate) -> None:
        self.rate_label.setText("poll %4.1f Hz  " % u.snapshot.rate_hz)

        active_edges: Set[str] = set()
        for name, edges in self._flow_edges.items():
            fs = u.flows.get(name)
            if fs is not None and fs.active:
                active_edges |= edges
        self.diagram_state.active_edges = active_edges

        self.diagram_state.badges = u.badges

        progress_text: Dict[str, str] = {}
        for act in self.engine.flowspec.activities:
            edge_id = self._progress_edge.get(act.name)
            if edge_id is None:
                continue
            fs = u.flows.get(act.name)
            if fs is None or fs.progress is None:
                continue
            short = act.progress.split(".")[-1]
            progress_text[edge_id] = "%s 0x%04X" % (short, fs.progress)
        self.diagram_state.progress_text = progress_text

        chsel = None
        ref = self._mux_select_ref
        if ref is not None:
            val = u.snapshot.value(ref.reg_key)
            if val is not None:
                mask = (1 << (ref.msb - ref.lsb + 1)) - 1
                chsel = (val >> ref.lsb) & mask
        self.diagram_state.chsel_value = chsel

        for item in self.blocks.values():
            item.update()
        for item in self.wires.values():
            item.update()

    def on_state(self, state: str) -> None:
        self.statusBar().showMessage("poller: %s" % state)

    def _advance_dash(self) -> None:
        if self.frozen:
            return
        self.diagram_state.dash_phase = (self.diagram_state.dash_phase
                                         + 1.3) % 100
        for eid, item in self.wires.items():
            if eid in self.diagram_state.active_edges:
                item.update()
