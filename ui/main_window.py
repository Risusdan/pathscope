"""Main application window: central diagram view plus chrome toolbar.

Ported from prototype/ui_proto.py's Main class - toolbar construction
(stylesheet, actions) is the same pattern, wired to the real
Engine/EngineBridge/DiagramState instead of the prototype's StubEngine
and stub-dict snapshots. The prototype's single global Freeze has
since been replaced by per-page Run/Stop (spec point 1, M6 scope-view
plan v2): each page carries its own button in the same top-right
position - dp_run_stop_btn on the Data Path page header,
ScopePage.run_stop_btn on the Scope page - and the toolbar carries
none. The toolbar holds only the target name, the Data Path/Scope
page switch (two exclusive buttons at a fixed position - the old
QTabWidget tab bar rendered as a segmented control that shifted
position with the page content) and the poll rate."""
from typing import Dict, Optional, Set

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (QButtonGroup, QDockWidget, QGraphicsView,
                               QHBoxLayout, QLabel, QMainWindow,
                               QPushButton, QSizePolicy, QStackedWidget,
                               QToolBar, QToolButton, QVBoxLayout, QWidget)

from core.engine.core import Engine, EngineError
from core.engine.rules import EngineUpdate

from .bridge import EngineBridge
from .diagram.items import MONO, LegendItem
from .diagram.scene import DiagramState, build_scene
from .panels.event_log import EventLog
from .panels.flow_page import FlowPage, build_flow_edge_map
from .panels.memory_page import MemoryPage
from .panels.register_page import RegisterPage

# Low sweep-rate warning on the toolbar's poll label (moved here from
# the scope page's budget label, which used to repeat the same rate):
# below LOW_RATE_HZ - and above 0, a reported stall is not a budget
# problem - the label turns orange with a tooltip naming the likely
# cause. Global on purpose: a saturated read budget slows BOTH pages.
LOW_RATE_HZ = 15.0
RATE_ORANGE_STYLE = "color: #E65100;"
RATE_TOOLTIP = ("high read count is lowering the sweep rate; prefer "
                "contiguous addresses")


class _DiagramView(QGraphicsView):
    """QGraphicsView with wheel-to-zoom. The prototype overrode
    wheelEvent on the QMainWindow itself; here it lives on the view
    widget directly, which receives wheel events unconditionally
    (independent of Qt's event-bubbling path for unhandled events)."""

    def wheelEvent(self, ev) -> None:
        factor = 1.15 if ev.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)


class MainWindow(QMainWindow):
    def __init__(self, engine: Engine, bridge: EngineBridge, parent=None,
                 target_label: Optional[str] = None):
        super().__init__(parent)
        self.setWindowTitle("PathScope")
        self.resize(1280, 800)

        self.engine = engine
        self.bridge = bridge
        # Data Path's own stop flag (spec point 1: per-page run/stop
        # replaces the old global Freeze) - holds the diagram +
        # Inspector display exactly like the old self.frozen did,
        # minus the scope side effect: Scope's stop state
        # (scope_page._stopped) is now completely independent, set
        # only via ScopePage.set_stopped().
        self._datapath_stopped = False
        self.last_update: Optional[EngineUpdate] = None
        self._target_label_text = target_label

        self.diagram_state = DiagramState()
        self.scene, self.blocks, self.wires = build_scene(
            engine.topology, self.diagram_state)
        self.legend = LegendItem(self.diagram_state)
        self.scene.addItem(self.legend)

        self.view = _DiagramView(self.scene)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setDragMode(QGraphicsView.ScrollHandDrag)
        # (central widget is set below, by _build_central_tabs() - the
        # view sits inside the Data Path page of a Data Path/Scope
        # QStackedWidget rather than being the window's sole central
        # widget.)

        self._flow_edges = build_flow_edge_map(engine.topology,
                                               engine.flowspec)
        self._progress_edge = self._build_progress_edge_map(
            engine.flowspec, self._flow_edges, self.wires)
        self._mux_select_ref = self._find_mux_select(engine)

        self.scope_page = None

        self._build_toolbar()
        self._build_docks()
        self._build_central_tabs()
        self.diagram_state.on_block_clicked = self._select_block
        self.diagram_state.on_edge_clicked = self._on_edge_clicked
        self.diagram_state.on_badge_clicked = self._on_badge_clicked
        self.flow_page.on_pick = self._highlight_flow
        self.event_log.on_focus = self._on_log_focus
        self.event_log.on_event_time = self._on_log_time_focus

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

        target_name = self._target_label_text
        if target_name is None:
            cpu_blocks = [b for b in self.engine.topology.blocks.values()
                         if b.kind == "cpu"]
            target_name = cpu_blocks[0].title if cpu_blocks else "target"
        self.target_label = QLabel("  %s  " % target_name)
        tb.addWidget(self.target_label)
        tb.addSeparator()

        # The ONLY Data Path/Scope switch: two exclusive checkable
        # buttons at a fixed toolbar position. (The QTabWidget tab bar
        # this replaces rendered as a platform segmented control whose
        # on-screen position shifted with the page content - a UX
        # finding.) setChecked() below in _on_tab_changed never emits
        # clicked, so programmatic tab switches cannot recurse here.
        self.datapath_page_btn = QToolButton()
        self.datapath_page_btn.setText("Data Path")
        self.datapath_page_btn.setCheckable(True)
        self.datapath_page_btn.setChecked(True)
        self.scope_page_btn = QToolButton()
        self.scope_page_btn.setText("Scope")
        self.scope_page_btn.setCheckable(True)
        self._page_group = QButtonGroup(self)
        self._page_group.setExclusive(True)
        self._page_group.addButton(self.datapath_page_btn, 0)
        self._page_group.addButton(self.scope_page_btn, 1)
        self._page_group.idClicked.connect(
            lambda index: self.tabs.setCurrentIndex(index))
        tb.addWidget(self.datapath_page_btn)
        tb.addWidget(self.scope_page_btn)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        self.rate_label = QLabel("poll -- Hz  ")
        self.rate_label.setFont(MONO)
        tb.addWidget(self.rate_label)

    def _build_docks(self) -> None:
        """Right "Inspector" dock: a QStackedWidget holding the register
        page (block clicks, Task 10), the flow page (edge clicks,
        Task 11), and the memory page (memory-kind block clicks, Task
        12). Bottom "Event log" dock: same size (140px) as the
        prototype's `dock2`.

        Inspector's visibility is now tied to which central tab is
        active (_on_tab_changed, below - visible only on Data Path; a
        full-page Scope has no room for it and it is irrelevant
        there). Event log stays visible on both tabs - events matter
        to both, and scope markers come from the same update stream -
        so nothing here toggles log_dock."""
        self.reg_page = RegisterPage(self.engine)
        self.flow_page = FlowPage(self.engine)
        self.mem_page = MemoryPage(self.engine)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.reg_page)
        self.stack.addWidget(self.flow_page)
        self.stack.addWidget(self.mem_page)
        self.inspector_dock = QDockWidget("Inspector", self)
        self.inspector_dock.setWidget(self.stack)
        self.inspector_dock.setMinimumWidth(300)
        self.addDockWidget(Qt.RightDockWidgetArea, self.inspector_dock)

        self.event_log = EventLog()
        self.log_dock = QDockWidget("Event log", self)
        self.log_dock.setWidget(self.event_log)
        self.log_dock.setMinimumHeight(100)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.log_dock)
        self.resizeDocks([self.log_dock], [140], Qt.Vertical)

    def _build_central_tabs(self) -> None:
        """Central widget: two full-page tabs, "Data Path" (the
        existing diagram QGraphicsView) and "Scope" - replacing the
        old diagram-plus-docked-scope stack per a UX finding: when
        watching the scope the diagram is irrelevant, and both were
        cramped sharing the window.

        The pages live in a plain QStackedWidget (still named
        self.tabs - QStackedWidget shares QTabWidget's
        currentIndex/setCurrentIndex/currentChanged surface, so every
        caller and test kept working across the switch); the visible
        switch is the pair of toolbar page buttons, not a tab bar.

        The Data Path page wraps the diagram view under a header row
        carrying the page's own controls: Halt MCU (target control
        lives with the machine picture) on the left and this page's
        Run/Stop on the right - the SAME top-right position as the
        Scope page's own big button, deliberately (user requirement:
        Run/Stop sits in one consistent place on both pages).

        The Scope page starts as a plain placeholder label; the real
        ScopePage (and its pyqtgraph import) is constructed lazily on
        first activation, in _activate_scope_tab() - pay the import
        cost only if the user ever visits it. currentChanged is
        connected only after both pages are added, so building them
        never fires _on_tab_changed (which reaches into
        self.inspector_dock - it must already exist; _build_docks()
        runs before this in __init__)."""
        datapath_page = QWidget()
        dp_layout = QVBoxLayout(datapath_page)
        dp_layout.setContentsMargins(0, 4, 0, 0)
        dp_layout.setSpacing(4)
        header = QHBoxLayout()
        header.setContentsMargins(8, 0, 8, 0)
        self.halt_btn = QPushButton("Halt MCU")
        self.halt_btn.setMinimumHeight(36)
        self.halt_btn.setMinimumWidth(110)
        self.halt_btn.clicked.connect(self._toggle_halt)
        header.addWidget(self.halt_btn)
        header.addStretch(1)
        self.dp_run_stop_btn = QPushButton("Stop")
        self.dp_run_stop_btn.setCheckable(True)
        self.dp_run_stop_btn.setMinimumHeight(36)
        self.dp_run_stop_btn.setMinimumWidth(100)
        self.dp_run_stop_btn.clicked.connect(self._on_dp_run_stop_clicked)
        header.addWidget(self.dp_run_stop_btn)
        dp_layout.addLayout(header)
        dp_layout.addWidget(self.view)

        self.tabs = QStackedWidget()
        self.tabs.addWidget(datapath_page)
        self._scope_placeholder = QLabel("Loading Scope...")
        self._scope_placeholder.setAlignment(Qt.AlignCenter)
        self.tabs.addWidget(self._scope_placeholder)
        self.setCentralWidget(self.tabs)
        self.tabs.currentChanged.connect(self._on_tab_changed)

    def fit_view(self) -> None:
        rect = self.scene.itemsBoundingRect()
        if not rect.isEmpty():
            self.view.fitInView(rect, Qt.KeepAspectRatio)

    # -- actions ---------------------------------------------------------

    def _toggle_halt(self) -> None:
        halting = self.halt_btn.text() == "Halt MCU"
        try:
            if halting:
                self.engine.halt()
            else:
                self.engine.resume()
        except EngineError as e:
            self.statusBar().showMessage("error: %s" % e, 5000)
            return
        self.halt_btn.setText("Resume MCU" if halting else "Halt MCU")

    def set_datapath_stopped(self, on: bool) -> None:
        """The Data Path page's own independent stop flag (spec
        point 1: diagram+Inspector hold, the display only - the
        target and the poller keep running, and the Scope page's own
        flag is untouched). Mirror of ScopePage.set_stopped: syncs
        the page button's label/checked state whichever entry point
        (the button, or this method directly) made the change."""
        self._datapath_stopped = on
        if not on and self.last_update is not None:
            self._apply(self.last_update)
        self.dp_run_stop_btn.setText("Run" if on else "Stop")
        self.dp_run_stop_btn.setChecked(on)

    def _on_dp_run_stop_clicked(self) -> None:
        self.set_datapath_stopped(not self._datapath_stopped)

    def _on_tab_changed(self, index: int) -> None:
        """Wired to self.tabs.currentChanged. Keeps the Inspector dock
        visible only on the Data Path page (index 0) - a full-page
        Scope has no room for it and it is irrelevant there - and the
        toolbar page buttons' checked state mirroring the current
        page (setChecked never emits clicked, so no recursion into
        the page switch). Event log dock is untouched here - it stays
        visible on both pages. Each page carries its own Run/Stop
        button holding its own flag, so a page switch has nothing to
        resync there. Lazily constructs the real ScopePage the first
        time the Scope page is activated."""
        on_scope = index == 1
        self.inspector_dock.setVisible(not on_scope)
        self.datapath_page_btn.setChecked(not on_scope)
        self.scope_page_btn.setChecked(on_scope)
        if on_scope and self.scope_page is None:
            self._activate_scope_tab()

    def _activate_scope_tab(self) -> None:
        """Lazy construction (per the M6 scope-view plan): the
        pyqtgraph-importing module is only imported here, on first
        activation of the Scope tab, not at MainWindow import time -
        pyqtgraph's import cost is paid only if the user ever visits
        it. Swaps the placeholder widget _build_central_tabs() inserted
        for a real ScopePage at the same page index (1).

        removeWidget() on the then-current placeholder makes the
        stack switch currentIndex away and back as this runs (to 0,
        then back to 1 via the explicit setCurrentIndex below),
        re-entering _on_tab_changed along the way. The re-entries are
        harmless: the dock-visible/button-checked state they set is
        overwritten by the next step, and the `scope_page is None`
        guard above (set non-None before removeWidget runs) prevents
        a second construction - the method converges on Scope,
        active, with the real page in place. The page starts running
        regardless of Data Path's own _datapath_stopped (independent
        flags, spec point 1)."""
        from .panels.scope_page import ScopePage
        self.scope_page = ScopePage(self.engine)
        placeholder = self._scope_placeholder
        self.tabs.removeWidget(placeholder)
        placeholder.deleteLater()
        self.tabs.insertWidget(1, self.scope_page)
        self.tabs.setCurrentIndex(1)
        self._scope_placeholder = None

    # -- selection ---------------------------------------------------------

    def _select_block(self, block_id: str) -> None:
        """Wired to diagram_state.on_block_clicked (ui/diagram/items.py's
        BlockItem.mousePressEvent). Ported from the prototype's
        Main.select_block: marks the block selected, clears any flow
        highlight, and routes the Inspector dock to the register page
        for this block - except a memory-kind block (SRAM/Flash, no SVD
        peripheral registers to show), which routes to the memory page
        instead."""
        self.diagram_state.selected_block = block_id
        self.diagram_state.flow_blocks = set()
        self.diagram_state.flow_edges = set()
        for item in self.blocks.values():
            item.update()
        for item in self.wires.values():
            item.update()
        block = self.engine.topology.blocks.get(block_id)
        if block is not None and block.kind == "memory":
            self.mem_page.show_block(block_id)
            self.stack.setCurrentWidget(self.mem_page)
        else:
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

    def _on_log_time_focus(self, t: float) -> None:
        """Wired to event_log.on_event_time (EventLog row click, for a
        row that carries an event time - the event-to-scope cursor
        sync). Routes to the scope page's cursor; a no-op if the Scope
        tab has never been activated, same "page may not exist yet"
        guard apply_update() already uses for add_event_marker."""
        if self.scope_page is not None:
            self.scope_page.jump_to(t)

    # -- live data -----------------------------------------------------------

    def apply_update(self, u: EngineUpdate) -> None:
        self.last_update = u
        # spec point 1 "event log always live": the log must not
        # depend on either tab's own stop flag, so it is fed here,
        # unconditionally, before the Data Path stop check below - not
        # from inside _apply(), which that check can skip entirely.
        # This also means resuming Data Path (which replays
        # self.last_update through _apply() to restore the display)
        # can no longer double-log that update's events, since
        # _apply() itself never touches the log. Scope's own event
        # markers are likewise unconditional here - Scope's stop flag
        # only pauses ITS repaint timer (ScopePage.set_stopped), not
        # this feed.
        self.event_log.add_events(u.events)
        if self.scope_page is not None:
            for ev in u.events:
                self.scope_page.add_event_marker(
                    ev.t, "%s: %s" % (ev.flow, ev.msg))
        if self._datapath_stopped:
            return
        self._apply(u)

    def _update_rate_label(self, rate_hz: float) -> None:
        self.rate_label.setText("poll %4.1f Hz  " % rate_hz)
        if 0 < rate_hz < LOW_RATE_HZ:
            self.rate_label.setStyleSheet(RATE_ORANGE_STYLE)
            self.rate_label.setToolTip(RATE_TOOLTIP)
        else:
            self.rate_label.setStyleSheet("")
            self.rate_label.setToolTip("")

    def _apply(self, u: EngineUpdate) -> None:
        self._update_rate_label(u.snapshot.rate_hz)

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

    def on_state(self, state: str) -> None:
        self.statusBar().showMessage("poller: %s" % state)
        self.event_log.add_info("poller: %s" % state)
        if state != "running":
            # a lost target must not keep animating a live data path -
            # clear whatever the diagram was showing so dashes/progress
            # text freeze-then-vanish instead of implying the (now
            # stale) flow is still moving. The dash timer
            # (_advance_dash) keeps running, but with active_edges
            # empty it has nothing to animate. The next apply_update()
            # after recovery repopulates both sets from the fresh
            # EngineUpdate, same as any other poll tick.
            affected = (self.diagram_state.active_edges
                       | set(self.diagram_state.progress_text))
            self.diagram_state.active_edges = set()
            self.diagram_state.progress_text = {}
            for eid in affected:
                item = self.wires.get(eid)
                if item is not None:
                    item.update()
            self.rate_label.setText("poll -- Hz  ")

    def _advance_dash(self) -> None:
        if self._datapath_stopped:
            return
        self.diagram_state.dash_phase = (self.diagram_state.dash_phase
                                         + 1.3) % 100
        for eid, item in self.wires.items():
            if eid in self.diagram_state.active_edges:
                item.update()
