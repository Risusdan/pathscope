"""Main application window: central diagram view plus chrome toolbar.

Ported from prototype/ui_proto.py's Main class - toolbar construction
(stylesheet, actions) and freeze semantics are the same pattern, wired
to the real Engine/EngineBridge/DiagramState instead of the prototype's
StubEngine and stub-dict snapshots."""
from typing import Dict, Optional, Set

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QPainter
from PySide6.QtWidgets import (QDockWidget, QGraphicsView, QLabel,
                               QMainWindow, QSizePolicy, QStackedWidget,
                               QToolBar, QWidget)

from core.engine.core import Engine, EngineError
from core.engine.rules import EngineUpdate

from .bridge import EngineBridge
from .diagram.items import MONO, LegendItem
from .diagram.scene import DiagramState, build_scene
from .panels.event_log import EventLog
from .panels.flow_page import FlowPage, build_flow_edge_map
from .panels.register_page import RegisterPage


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

        self._flow_edges = build_flow_edge_map(engine.topology,
                                               engine.flowspec)
        self._progress_edge = self._build_progress_edge_map(
            engine.flowspec, self._flow_edges, self.wires)
        self._mux_select_ref = self._find_mux_select(engine)

        self._build_toolbar()
        self._build_docks()
        self.diagram_state.on_block_clicked = self._select_block
        self.diagram_state.on_edge_clicked = self._on_edge_clicked
        self.diagram_state.on_badge_clicked = self._on_badge_clicked
        self.flow_page.on_pick = self._highlight_flow
        self.event_log.on_focus = self._on_log_focus

        self.bridge.update.connect(self.apply_update)
        self.bridge.state.connect(self.on_state)

        self.anim = QTimer(self)
        self.anim.setInterval(60)
        self.anim.timeout.connect(self._advance_dash)
        self.anim.start()

        QTimer.singleShot(0, self.fit_view)

    # -- one-time topology/flowspec derived maps ----------------------------

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

    def _build_docks(self) -> None:
        """Right "Inspector" dock: a QStackedWidget holding the register
        page (block clicks, Task 10) and the flow page (edge clicks,
        Task 11). Bottom "Event log" dock: same size (140px) as the
        prototype's `dock2`."""
        self.reg_page = RegisterPage(self.engine)
        self.flow_page = FlowPage(self.engine)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.reg_page)
        self.stack.addWidget(self.flow_page)
        dock = QDockWidget("Inspector", self)
        dock.setWidget(self.stack)
        dock.setMinimumWidth(300)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)

        self.event_log = EventLog()
        log_dock = QDockWidget("Event log", self)
        log_dock.setWidget(self.event_log)
        log_dock.setMinimumHeight(100)
        self.addDockWidget(Qt.BottomDockWidgetArea, log_dock)
        self.resizeDocks([log_dock], [140], Qt.Vertical)

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

    # -- selection ---------------------------------------------------------

    def _select_block(self, block_id: str) -> None:
        """Wired to diagram_state.on_block_clicked (ui/diagram/items.py's
        BlockItem.mousePressEvent). Ported from the prototype's
        Main.select_block: marks the block selected, clears any flow
        highlight, and routes the Inspector dock to the register page
        for this block."""
        self.diagram_state.selected_block = block_id
        self.diagram_state.flow_blocks = set()
        self.diagram_state.flow_edges = set()
        for item in self.blocks.values():
            item.update()
        for item in self.wires.values():
            item.update()
        self.reg_page.show_block(block_id)
        self.stack.setCurrentWidget(self.reg_page)

    def _on_edge_clicked(self, edge_id: str) -> None:
        """Wired to diagram_state.on_edge_clicked (ui/diagram/items.py's
        WireItem.mousePressEvent). Ported from the prototype's
        Main.select_edge: an edge that belongs to no activity is a
        no-op (reuses self._flow_edges, T9, for that membership check);
        otherwise clears block selection, routes the Inspector dock to
        the flow page for this edge, and highlights the flow the page
        auto-picked."""
        flow_ids = [name for name, edges in self._flow_edges.items()
                   if edge_id in edges]
        if not flow_ids:
            return
        self.diagram_state.selected_block = None
        for item in self.blocks.values():
            item.update()
        self.flow_page.show_edge(edge_id, self.last_update)
        self._highlight_flow(self.flow_page.flow_id)
        self.stack.setCurrentWidget(self.flow_page)

    def _highlight_flow(self, flow_id: Optional[str]) -> None:
        """Wired to flow_page.on_pick as well as called directly from
        _on_edge_clicked. Ported from the prototype's
        Main.highlight_flow: sets diagram_state.flow_edges/flow_blocks
        from a flow_id - edges via self._flow_edges (T9), blocks via
        the activity's own `path` (flow_page.activities)."""
        edges: Set[str] = (self._flow_edges.get(flow_id, set())
                           if flow_id else set())
        blocks: Set[str] = set()
        if flow_id is not None:
            act = self.flow_page.activities.get(flow_id)
            if act is not None:
                blocks = set(act.path)
        self.diagram_state.flow_edges = edges
        self.diagram_state.flow_blocks = blocks
        for item in self.blocks.values():
            item.update()
        for item in self.wires.values():
            item.update()

    def _on_badge_clicked(self, block_id: str) -> None:
        """Wired to diagram_state.on_badge_clicked (BlockItem's
        mousePressEvent badge-rect hit). Ported from the prototype's
        BlockItem.mousePressEvent badge branch: clears the badge and
        logs an info row. No explicit repaint here - same as the
        prototype - since the next EngineUpdate's badges dict (the
        RuleEngine, cleared by engine.clear_badge(), is the source of
        truth diagram_state.badges is refreshed from in _apply) repaints
        the block within one poll tick."""
        self.engine.clear_badge(block_id)
        self.event_log.add_info("badge cleared on %s" % block_id, block_id)

    def _on_log_focus(self, block_id: str) -> None:
        """Wired to event_log.on_focus (EventLog row click). Ported
        from the prototype's Main.log_clicked: centers the view on the
        block and routes the Inspector dock to its register page."""
        if block_id in self.blocks:
            self._select_block(block_id)
            self.view.centerOn(self.blocks[block_id])

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

        self.reg_page.refresh(u)
        self.flow_page.refresh(u)
        self.event_log.add_events(u.events)

    def on_state(self, state: str) -> None:
        self.statusBar().showMessage("poller: %s" % state)
        self.event_log.add_info("poller: %s" % state)

    def _advance_dash(self) -> None:
        if self.frozen:
            return
        self.diagram_state.dash_phase = (self.diagram_state.dash_phase
                                         + 1.3) % 100
        for eid, item in self.wires.items():
            if eid in self.diagram_state.active_edges:
                item.update()
