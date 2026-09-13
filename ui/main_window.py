"""Main application window: central diagram view plus chrome toolbar.

Ported from prototype/ui_proto.py's Main class - toolbar construction
(stylesheet, actions) is the same pattern, wired to the real
Engine/EngineBridge/DiagramState instead of the prototype's StubEngine
and stub-dict snapshots. The prototype's single global Freeze has
since been replaced by per-page Run/Stop (spec point 1, M6 scope-view
plan v2) - see run_stop_act/_toggle_run_stop below."""
from typing import Dict, Optional, Set

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QPainter
from PySide6.QtWidgets import (QDockWidget, QGraphicsView, QLabel,
                               QMainWindow, QSizePolicy, QStackedWidget,
                               QTabWidget, QToolBar, QWidget)

from core.engine.core import Engine, EngineError
from core.engine.rules import EngineUpdate

from .bridge import EngineBridge
from .diagram.items import MONO, LegendItem
from .diagram.scene import DiagramState, build_scene
from .panels.event_log import EventLog
from .panels.flow_page import FlowPage, build_flow_edge_map
from .panels.memory_page import MemoryPage
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
        # view becomes tab 0 of a Data Path/Scope QTabWidget rather
        # than the window's sole central widget.)

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

        self.halt_act = QAction("Halt", self)
        self.halt_act.triggered.connect(self._toggle_halt)
        tb.addAction(self.halt_act)

        # Per-page Run/Stop (spec point 1) - replaces the old global
        # Freeze action. Acts on whichever tab is current (Data Path
        # or Scope, each with its own independent stop flag - see
        # _toggle_run_stop) and its own checked/text state is kept in
        # sync with that tab's own flag on every tab switch (see
        # _on_tab_changed's _sync_run_stop_action call).
        self.run_stop_act = QAction("Stop", self)
        self.run_stop_act.setCheckable(True)
        self.run_stop_act.toggled.connect(self._toggle_run_stop)
        tb.addAction(self.run_stop_act)

        self.tint_act = QAction("Tint", self)
        self.tint_act.setCheckable(True)
        self.tint_act.setChecked(True)
        self.tint_act.toggled.connect(self._toggle_tint)
        tb.addAction(self.tint_act)

        self.fit_act = QAction("Fit", self)
        self.fit_act.triggered.connect(self.fit_view)
        tb.addAction(self.fit_act)

        self.scope_act = QAction("Scope", self)
        self.scope_act.setCheckable(True)
        self.scope_act.toggled.connect(self._toggle_scope)
        tb.addAction(self.scope_act)

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

        The Scope tab starts as a plain placeholder label; the real
        ScopePage (and its pyqtgraph import) is constructed lazily on
        first activation, in _activate_scope_tab() - same "pay the
        import cost only if the user ever visits it" rule the old
        dock-based _toggle_scope used. currentChanged is connected
        only after both tabs are added, so building this method's own
        two initial tabs never fires _on_tab_changed (which reaches
        into self.inspector_dock/self.scope_act - both must already
        exist; _build_docks() and _build_toolbar() run before this in
        __init__)."""
        self.tabs = QTabWidget()
        self.tabs.addTab(self.view, "Data Path")
        self._scope_placeholder = QLabel("Loading Scope...")
        self._scope_placeholder.setAlignment(Qt.AlignCenter)
        self.tabs.addTab(self._scope_placeholder, "Scope")
        self.setCentralWidget(self.tabs)
        self.tabs.currentChanged.connect(self._on_tab_changed)

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

    def _toggle_run_stop(self, on: bool) -> None:
        """Wired to the toolbar's run_stop_act - acts on whichever tab
        is CURRENT (spec point 1), not both: on the Scope tab this
        only sets scope_page's own independent stop flag (the
        waveform hold), and on the Data Path tab this only sets
        self._datapath_stopped (the diagram+Inspector hold) - unlike
        the old global Freeze, neither path touches the other tab's
        state at all. The action's own text ("Run"/"Stop") is kept in
        sync here since this is the one place both entry points (the
        toolbar action itself, and _on_scope_stopped_changed relaying
        the scope page's own big button/spacebar) ultimately update
        the tab-independent flags from."""
        if self.tabs.currentIndex() == 1 and self.scope_page is not None:
            self.scope_page.set_stopped(on)
        else:
            self._datapath_stopped = on
            if not on and self.last_update is not None:
                self._apply(self.last_update)
        self.run_stop_act.setText("Run" if on else "Stop")

    def _toggle_scope(self, on: bool) -> None:
        """Wired to the toolbar's "Scope" action - now a tab shortcut
        rather than a dock-visibility toggle (the old dock-stack
        layout was replaced by full-page Data Path/Scope tabs).
        Checking it switches to the Scope tab; unchecking switches
        back to Data Path. _on_tab_changed mirrors the action's
        checked state back the other way too, for a plain tab-bar
        click, so the two stay in sync regardless of entry point."""
        self.tabs.setCurrentIndex(1 if on else 0)

    def _on_tab_changed(self, index: int) -> None:
        """Wired to self.tabs.currentChanged. Keeps the Inspector dock
        visible only on the Data Path tab (index 0) - a full-page
        Scope has no room for it and it is irrelevant there - and the
        toolbar's Scope action's checked state mirroring whichever tab
        is active, including a plain tab-bar click (not just the
        toolbar action). blockSignals guards against recursion: an
        unblocked setChecked() would re-fire scope_act.toggled ->
        _toggle_scope -> tabs.setCurrentIndex(), re-entering this slot.
        Event log dock is untouched here - it stays visible on both
        tabs. Lazily constructs the real ScopePage the first time the
        Scope tab is activated."""
        on_scope = index == 1
        self.inspector_dock.setVisible(not on_scope)
        self.scope_act.blockSignals(True)
        self.scope_act.setChecked(on_scope)
        self.scope_act.blockSignals(False)
        if on_scope and self.scope_page is None:
            self._activate_scope_tab()
        # Per-page run/stop (spec point 1): the toolbar action reflects
        # whichever tab is now current's OWN stop flag, independent of
        # the other tab's.
        self._sync_run_stop_action()

    def _sync_run_stop_action(self) -> None:
        if self.tabs.currentIndex() == 1 and self.scope_page is not None:
            stopped = self.scope_page.is_stopped()
        else:
            stopped = self._datapath_stopped
        self.run_stop_act.blockSignals(True)
        self.run_stop_act.setChecked(stopped)
        self.run_stop_act.blockSignals(False)
        self.run_stop_act.setText("Run" if stopped else "Stop")

    def _on_scope_stopped_changed(self, stopped: bool) -> None:
        """Wired to scope_page.on_stopped_changed - relays a state
        change made through the scope page's OWN entry points (its big
        Run/Stop button, or spacebar) back to the toolbar action, but
        only while Scope is the active tab (acting on the toolbar
        action while looking at Data Path would be confusing - the
        Scope tab already stays in sync with its own state next time
        it becomes current, via _sync_run_stop_action above)."""
        if self.tabs.currentIndex() == 1:
            self.run_stop_act.blockSignals(True)
            self.run_stop_act.setChecked(stopped)
            self.run_stop_act.blockSignals(False)
            self.run_stop_act.setText("Run" if stopped else "Stop")

    def _activate_scope_tab(self) -> None:
        """Lazy construction (per the M6 scope-view plan): the
        pyqtgraph-importing module is only imported here, on first
        activation of the Scope tab, not at MainWindow import time -
        pyqtgraph's import cost is paid only if the user ever visits
        it. Swaps the placeholder widget _build_central_tabs() inserted
        for a real ScopePage at the same tab index (1).

        removeTab() on the then-current placeholder tab makes Qt
        switch currentIndex away and back as this runs (to index 0,
        since index 1 is being removed, then back to 1 via the
        explicit setCurrentIndex below), re-entering _on_tab_changed
        twice more along the way. Both re-entries are harmless: the
        dock-visible/action-checked state they set is overwritten by
        the next step, and the `scope_page is None` guard above (set
        non-None before either removeTab or insertTab run) prevents a
        second construction - the method converges on Scope, active,
        with the real page in place."""
        from .panels.scope_page import ScopePage
        self.scope_page = ScopePage(self.engine)
        # Independent stop flag (spec point 1) - the new page starts
        # running regardless of Data Path's own _datapath_stopped;
        # on_stopped_changed relays the page's own button/spacebar
        # back to the toolbar action while Scope is the active tab.
        self.scope_page.on_stopped_changed = self._on_scope_stopped_changed
        self.tabs.removeTab(1)
        self.tabs.insertTab(1, self.scope_page, "Scope")
        self.tabs.setCurrentIndex(1)
        self._scope_placeholder = None

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
        # _apply() itself never touches the log. Scope's own markers/
        # sweep rate are likewise unconditional here - Scope's stop
        # flag only pauses ITS repaint timer (ScopePage.set_stopped),
        # not this feed.
        self.event_log.add_events(u.events)
        if self.scope_page is not None:
            for ev in u.events:
                self.scope_page.add_event_marker(
                    ev.t, "%s: %s" % (ev.flow, ev.msg))
            self.scope_page.set_sweep_rate(u.snapshot.rate_hz)
        if self._datapath_stopped:
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
