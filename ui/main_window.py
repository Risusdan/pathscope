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
from collections import OrderedDict
from typing import Any, Dict, Optional, Set

from PySide6.QtCore import QEvent, QRectF, QTimer, Qt
from PySide6.QtGui import QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (QButtonGroup, QDockWidget, QGraphicsLineItem,
                               QGraphicsView, QHBoxLayout, QLabel,
                               QMainWindow, QPushButton, QSizePolicy,
                               QStackedWidget, QToolBar, QToolButton,
                               QVBoxLayout, QWidget)

from core.engine.core import Engine, EngineError
from core.engine.rules import EngineUpdate
from core.target.autolayout import auto_layout
from core.target.layout_io import LayoutPatchError, save_layout
from core.target.topology import load_topology

from .bridge import EngineBridge
from .diagram.items import MONO, LegendItem, default_wh, snap
from .diagram.scene import DiagramState, build_scene
from .panels.event_log import EventLog
from .panels.flow_page import FlowPage, build_flow_edge_map
from .panels.memory_page import MemoryPage
from .panels.register_page import RegisterPage
from .style import COL_GREY

# M8 layout edit mode: dashed border cue applied to the diagram view's
# viewport while editing (a plain stylesheet swap - cleared back to ""
# on toggle-off), so the user always has an unambiguous "you are
# editing the layout" cue independent of the toolbar button's own
# checked state.
_EDIT_VIEW_STYLE = "QGraphicsView { border: 2px dashed #999999; }"

# Manual-gate wave B, B4 (dynamic alignment guides): light grey dashed
# - reuses the existing palette's COL_GREY rather than adding a new
# color, per the "no new colors beyond ui/style.py... add if needed"
# instruction (grey already covers "light grey dashed", so nothing new
# was needed). GUIDE_MARGIN pads the block-extents-derived span the
# reference lines are drawn across, so a line reaches visibly past the
# outermost block rather than stopping exactly at its edge.
_GUIDE_PEN = QPen(COL_GREY, 1, Qt.DashLine)
_GUIDE_MARGIN = 200

# Low sweep-rate warning on the toolbar's poll label (moved here from
# the scope page's budget label, which used to repeat the same rate):
# below LOW_RATE_HZ - and above 0, a reported stall is not a budget
# problem - the label turns orange with a tooltip naming the likely
# cause. Global on purpose: a saturated read budget slows BOTH pages.
LOW_RATE_HZ = 15.0
RATE_ORANGE_STYLE = "color: #E65100;"
RATE_TOOLTIP = ("high read count is lowering the sweep rate; prefer "
                "contiguous addresses")

# Manual-gate finding 3: trackpad/wheel zoom. _ZOOM_MIN/_ZOOM_MAX bound
# the view's CUMULATIVE scale (tracked in _DiagramView._zoom, since
# extracting a scalar "current zoom" back out of a QTransform is more
# indirection than just keeping our own running total); both the wheel
# and the native pinch-gesture path share the one clamp.
_ZOOM_MIN = 0.2
_ZOOM_MAX = 5.0
_WHEEL_ZOOM_FACTOR = 1.15


def _clamped_zoom_factor(current_scale: float, requested_factor: float,
                         min_scale: float = _ZOOM_MIN,
                         max_scale: float = _ZOOM_MAX) -> float:
    """Given the view's current cumulative scale and a requested
    multiplicative zoom factor, returns the ACTUAL factor to apply so
    that current_scale * factor lands within [min_scale, max_scale] -
    clamping the requested zoom rather than rejecting it outright, so
    a large pinch/scroll right at the boundary still glides smoothly
    up to the limit instead of doing nothing. Returns exactly 1.0
    (a no-op factor) once current_scale is already sitting at (or
    would overshoot past) the boundary in the requested direction.
    Pure - no Qt dependency - so it is unit-testable without a
    QApplication."""
    target = current_scale * requested_factor
    clamped_target = max(min_scale, min(target, max_scale))
    return clamped_target / current_scale


class _DiagramView(QGraphicsView):
    """QGraphicsView with wheel-to-zoom and trackpad pinch-to-zoom. The
    prototype overrode wheelEvent on the QMainWindow itself; here it
    lives on the view widget directly, which receives wheel events
    unconditionally (independent of Qt's event-bubbling path for
    unhandled events).

    Manual-gate finding 3 (trackpad zoomed out but never in): macOS
    trackpad wheel events typically carry angleDelta().y() == 0 (the
    trackpad reports pixel-based scrolling, not the discrete "clicks"
    angleDelta measures) - the old `angleDelta().y() > 0 else
    zoom-out` logic fell into the zoom-out branch on EVERY trackpad
    scroll, so the view could zoom out but never in. pixelDelta() is
    now preferred whenever it is non-zero; angleDelta() is the
    fallback for a real (non-trackpad) wheel; a genuinely zero delta
    from either is a no-op rather than the old default zoom-out.

    A trackpad PINCH is an entirely separate Qt event -
    QEvent.NativeGesture, NativeGestureType.ZoomNativeGesture subtype
    - which never reaches wheelEvent at all; handled here via an
    event() override. Both paths share one cumulative-scale clamp
    (_clamped_zoom_factor, module-level, pure, unit-tested directly).

    M8 wave B fix round 3 (user-acceptance finding 1 root cause): both
    paths also share this method as the one choke point for a second
    guard - self.scale() rescales the view's transform in place, and
    if a block/legend/waypoint drag is in flight (the scene has a
    mouseGrabberItem) when that happens, Qt's own default
    ItemIsMovable drag tracking - which recomputes each step's
    proposed position by remapping the CURRENT mouse position through
    the item's local coordinate frame and subtracting the button-down
    local position captured at press time - remaps through the NEW
    transform on the very next move while still subtracting an offset
    captured under the OLD one. That mismatch is entirely independent
    of any real mouse movement and can be tens of scene-pixels on a
    single step; BlockItem.itemChange's grid/alignment snap then only
    ever bounds its correction relative to THAT ALREADY-CORRUPTED
    proposed position (see _compute_alignment_snap's own clamp), not
    relative to the gesture's true trajectory, so the dragged item can
    "teleport" across the canvas - and land on top of an unrelated
    block if one happens to be near the corrupted landing spot. A
    trackpad pinch or scroll firing mid-drag (very plausible - it is
    the same input device the drag itself came from) is a real trigger
    for this, not just this window's one-shot startup fit_view() call.
    Ignoring a zoom request outright while a gesture is in flight is
    strictly safer than trying to compensate for the transform change
    after the fact."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._zoom = 1.0
        # Acceptance round 6: zoom anchors under the mouse cursor
        # (Qt's default AnchorViewCenter kept re-centering the view,
        # so zooming in always drifted away from what the user was
        # pointing at). Applies to both wheel and trackpad pinch -
        # scale() honors this anchor whenever the cursor is over the
        # viewport, falling back to center otherwise. Safe w.r.t. the
        # fly-away guards: _apply_zoom already refuses to rescale
        # while any drag gesture is in flight.
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)

    def _apply_zoom(self, factor: float) -> None:
        scene = self.scene()
        if scene is not None and scene.mouseGrabberItem() is not None:
            return   # a drag/resize/waypoint gesture is in flight - ignore
        factor = _clamped_zoom_factor(self._zoom, factor)
        if factor == 1.0:
            return   # already at (or would overshoot) a clamp boundary
        self._zoom *= factor
        self.scale(factor, factor)

    def wheelEvent(self, ev) -> None:
        dy = ev.pixelDelta().y()
        if dy == 0:
            dy = ev.angleDelta().y()
        if dy == 0:
            return
        factor = _WHEEL_ZOOM_FACTOR if dy > 0 else 1 / _WHEEL_ZOOM_FACTOR
        self._apply_zoom(factor)

    def event(self, ev) -> bool:
        if ev.type() == QEvent.NativeGesture:
            if ev.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                self._apply_zoom(1 + ev.value())
                return True
        return super().event(ev)


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
        # M8 task 5 fix round 2: a wire that started with an explicit
        # path seeds its auto-route fallback wrong (WireItem.__init__'s
        # comment) - correct every wire's fallback right away, before
        # the window is ever shown, so even a first-interaction
        # delete-back-to-auto-route already renders a fresh straight
        # route instead of the frozen original path. Harmless/no-op
        # rendering-wise for wires that still have explicit points
        # (refresh_auto_route only touches the fallback cache for
        # those, never self.pts).
        for wire in self.wires.values():
            wire.refresh_auto_route(self.blocks)
        self.legend = LegendItem(self.diagram_state)
        self.scene.addItem(self.legend)

        self.view = _DiagramView(self.scene)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setDragMode(QGraphicsView.ScrollHandDrag)
        # Acceptance round 6: the live coordinate readout (wave B3)
        # lives INSIDE the drawing area - reading the window-bottom
        # status bar mid-drag meant taking eyes off the diagram. An
        # overlay label pinned to the view's top-left corner, shown
        # only while a gesture is feeding coordinates.
        self.live_coord_label = QLabel(self.view)
        self.live_coord_label.setFont(MONO)
        self.live_coord_label.setStyleSheet(
            "background: rgba(255, 255, 255, 220); color: #1565C0;"
            " border: 1px solid #90A4AE; padding: 2px 6px;")
        self.live_coord_label.move(10, 10)
        self.live_coord_label.hide()
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
        self._init_layout_edit_state()
        self.diagram_state.on_block_clicked = self._select_block
        self.diagram_state.on_edge_clicked = self._on_edge_clicked
        self.diagram_state.on_badge_clicked = self._on_badge_clicked
        self.diagram_state.on_geometry_changed = (
            self._on_layout_geometry_changed)
        self.diagram_state.on_block_live_moved = self._on_block_live_moved
        self.diagram_state.on_live_status = self._on_live_status
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

        # M8 layout edit mode: edit_layout_btn and layout_dirty_label
        # used to live here (toolbar), but a manual-gate finding (1)
        # was that they read as a third page switcher next to Data
        # Path/Scope - both moved into the Data Path page's own header
        # row instead (_build_central_tabs, right after Halt MCU); see
        # that method for their construction.

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
        lives with the machine picture) on the left, this page's
        Run/Stop on the right - the SAME top-right position as the
        Scope page's own big button, deliberately (user requirement:
        Run/Stop sits in one consistent place on both pages) - and,
        in between, M8's edit_layout_btn/layout_dirty_label (manual-
        gate finding 1: these used to sit on the toolbar next to the
        Data Path/Scope page switch and read as a third page there;
        moved here, right after Halt MCU, with the dirty label right
        beside the button so it stays visible whether or not edit mode
        is on).

        Below that header, M8 (task 5) adds a second row -
        layout_edit_strip - carrying the Save layout/Revert/Auto-layout
        buttons; it is hidden until edit_layout_btn
        (_on_edit_layout_toggled) turns edit mode on.

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

        # M8 layout edit mode (task 5, manual-gate finding 1): moved
        # off the toolbar into this header row, right after Halt MCU.
        # Toggle behavior (drives DiagramState.edit_mode + every item's
        # set_editable together - see _on_edit_layout_toggled's
        # docstring for why those two must never be set independently)
        # is unchanged; only the widgets' PARENT changed.
        # User-acceptance finding 2, round 2: a QToolButton here (even
        # with matching setMinimumHeight/Width - round 1's fix) still
        # rendered with the toolbar-era look (smaller font, darker
        # fill, different height/baseline) because it is a DIFFERENT
        # WIDGET CLASS from its neighbors, with its own default style.
        # halt_btn/dp_run_stop_btn are plain QPushButtons; matching
        # the class (not just the size hints) is what actually gets
        # the same flat, tall look - including its CHECKED state,
        # which now renders as the same standard pressed-pushbutton
        # look dp_run_stop_btn's own checked state already uses.
        self.edit_layout_btn = QPushButton("Edit Layout")
        self.edit_layout_btn.setCheckable(True)
        self.edit_layout_btn.setMinimumHeight(36)
        self.edit_layout_btn.setMinimumWidth(110)
        self.edit_layout_btn.toggled.connect(self._on_edit_layout_toggled)
        header.addWidget(self.edit_layout_btn)

        self.layout_dirty_label = QLabel("unsaved layout changes")
        self.layout_dirty_label.setStyleSheet("color: #B71C1C;")
        self.layout_dirty_label.setVisible(False)
        # User-acceptance finding 2: explicitly vertically centered in
        # the row - the row's height is driven by the 36px buttons on
        # either side of it, and the label should not default to
        # whatever top/stretch behavior QHBoxLayout gives an
        # un-flagged widget in a taller row.
        header.addWidget(self.layout_dirty_label, 0, Qt.AlignVCenter)

        header.addStretch(1)
        self.dp_run_stop_btn = QPushButton("Stop")
        self.dp_run_stop_btn.setCheckable(True)
        self.dp_run_stop_btn.setMinimumHeight(36)
        self.dp_run_stop_btn.setMinimumWidth(100)
        self.dp_run_stop_btn.clicked.connect(self._on_dp_run_stop_clicked)
        header.addWidget(self.dp_run_stop_btn)
        dp_layout.addLayout(header)

        self.layout_edit_strip = QWidget()
        strip = QHBoxLayout(self.layout_edit_strip)
        strip.setContentsMargins(8, 0, 8, 0)
        self.layout_save_btn = QPushButton("Save layout")
        self.layout_save_btn.clicked.connect(self._on_save_layout)
        self.layout_revert_btn = QPushButton("Revert")
        self.layout_revert_btn.clicked.connect(self._on_revert_layout)
        self.layout_auto_btn = QPushButton("Seed layout")
        self.layout_auto_btn.setToolTip(
            "Generate a first-draft layout from scratch: replaces ALL "
            "block positions and clears explicit wire paths. One "
            "Cmd/Ctrl+Z restores the previous layout. Meant for a new "
            "target with no layout yet, not for tuning an existing one.")
        self.layout_auto_btn.clicked.connect(self._on_auto_layout)
        strip.addWidget(self.layout_save_btn)
        strip.addWidget(self.layout_revert_btn)
        strip.addWidget(self.layout_auto_btn)
        strip.addStretch(1)
        self.layout_edit_strip.setVisible(False)
        dp_layout.addWidget(self.layout_edit_strip)

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

    # -- M8 layout edit mode (task 5): mode toggle, undo, save/revert -------

    def _init_layout_edit_state(self) -> None:
        """Undo/dirty bookkeeping for the layout editor. self._layout_world
        is a whole-scene snapshot - {"blocks": {id: geometry()},
        "legend": geometry(), "wires": {edge id: points-list}} -
        refreshed after every completed gesture; the undo stack holds
        the PREVIOUS snapshot per gesture (simpler than per-item deltas,
        and cheap at this diagram's scale - see _capture_layout_world/
        _apply_layout_world). Dirty tracking is a single bool for
        blocks/legend (Save always regenerates every block's geometry
        and the legend line, so nothing needs a which-block-changed
        set) but per-edge for wires (Save only rewrites TOUCHED edges'
        points: lines, so self._dirty_wire_keys - edge_key() tuples -
        tracks which edges changed this session). Undo does NOT clear
        the dirty flag: landing back on the saved geometry via Z still
        leaves layout_dirty_label showing until an explicit Save or
        Revert (v1 simplification, not worth a byte-for-byte compare)."""
        self._undo_stack = []
        self._dirty_wire_keys = set()
        self._layout_dirty = False
        self._legend_moved = self.engine.topology.legend is not None
        self._layout_world = self._capture_layout_world()
        # M8 wave 2 (Visio-style connector glue): (block_id, x, y) of
        # the block's position as of the LAST on_block_live_moved call
        # this gesture, or None between gestures - see
        # _on_block_live_moved's docstring. Reset to None any time a
        # block's geometry changes through a path OTHER than a live
        # drag (a commit, undo, revert, or auto-layout), so a stale
        # value from an earlier gesture can never be diffed against a
        # position that block reached some other way.
        self._live_move_tracking = None

        # M8 wave B4b (dynamic alignment guides - rendering half of
        # wave B4a's detection): one vertical + one horizontal
        # QGraphicsLineItem, owned by MainWindow (the scene is
        # MainWindow's own - items.py's BlockItem only computes WHICH
        # lines are active, in _active_guides, it never touches the
        # scene itself). Both start hidden; _update_alignment_guides
        # repositions and shows/hides them on every live block move.
        self._guide_v = QGraphicsLineItem()
        self._guide_h = QGraphicsLineItem()
        for guide in (self._guide_v, self._guide_h):
            guide.setPen(_GUIDE_PEN)
            guide.setZValue(20)
            guide.setVisible(False)
            self.scene.addItem(guide)

        self._undo_shortcut = QShortcut(QKeySequence.Undo, self)
        self._undo_shortcut.activated.connect(self._on_layout_undo)

    def _on_edit_layout_toggled(self, on: bool) -> None:
        """The ONE toggle for edit mode. diagram_state.edit_mode (gates
        click-vs-drag routing in items.py) and every item's own
        set_editable (gates whether a drag/resize/waypoint-drag
        actually commits) are two independent switches upstream - see
        the task handover note - and MUST always be flipped together
        here so the two can never drift apart.

        Toggling off clears the undo stack (spec: "Stack clears on
        save and on mode exit") - a stale pre-session snapshot must
        never be poppable after a later re-entry into edit mode, since
        the user would have no way to tell it apart from one made this
        session. This is also the path _on_tab_changed's Scope-switch
        exit uses (it flips this same button via setChecked), so both
        exits are covered by clearing here. The DIRTY flag (and
        layout_dirty_label) is untouched - spec: "unsaved edits stay in
        the items/topology objects", so the label must keep showing
        after a toggle-off/tab-switch exit until an explicit Save or
        Revert. Only editability, the dashed viewport cue and the
        strip's visibility otherwise change.

        M8 wave B fix round 1: toggle-off cancels any in-flight
        gesture's VISUALS (_cancel_gesture_visuals) BEFORE the
        set_editable(False) loop below runs - items lose their
        gesture-tracking state (WireItem._clear_handles destroys any
        mid-drag WaypointHandle) as part of that loop, so the sweep
        must see the pre-loop world, not react to it having already
        happened."""
        if not on:
            self._cancel_gesture_visuals()
        self.diagram_state.edit_mode = on
        for item in self.blocks.values():
            item.set_editable(on)
        for wire in self.wires.values():
            wire.set_editable(on)
        self.legend.set_editable(on)
        self.view.setStyleSheet(_EDIT_VIEW_STYLE if on else "")
        self.layout_edit_strip.setVisible(on)
        if not on:
            self._undo_stack = []
            self._layout_world = self._capture_layout_world()

    def _capture_layout_world(self) -> Dict[str, Any]:
        return {
            "blocks": {bid: item.geometry()
                      for bid, item in self.blocks.items()},
            "legend": self.legend.geometry(),
            "wires": {eid: list(wire.edge.points)
                     for eid, wire in self.wires.items()},
        }

    def _apply_layout_world(self, snapshot: Dict[str, Any]) -> None:
        for bid, geom in snapshot["blocks"].items():
            item = self.blocks.get(bid)
            if item is not None:
                item.apply_geometry(*geom)
        lx, ly, _, _ = snapshot["legend"]
        self.legend.apply_geometry(lx, ly, 0, 0)
        for eid, points in snapshot["wires"].items():
            wire = self.wires.get(eid)
            if wire is not None:
                wire.apply_points(points)
        # M8 task 5 fix round 2: blocks above may have just moved (this
        # is undo's only call site) - re-route every currently-pointless
        # wire from their RESTORED geometry, and refresh every wire's
        # auto-route fallback to match, same as every other site that
        # changes block geometry (see refresh_auto_route's docstring).
        for wire in self.wires.values():
            wire.refresh_auto_route(self.blocks)
        # M8 wave 2: this bypassed the live-drag path entirely - any
        # in-progress live-move tracking baseline is now meaningless.
        self._live_move_tracking = None
        # M8 wave B fix round 1: undo (this method's only call site)
        # can land mid-gesture - cancel any in-flight gesture visual
        # the popped snapshot did not itself already account for.
        self._cancel_gesture_visuals()

    def _on_layout_geometry_changed(self) -> None:
        """Wired to diagram_state.on_geometry_changed - fires once per
        completed drag/resize/waypoint gesture, AFTER the item layer
        has already applied it. Diffs the new world snapshot against
        the previous one only to learn WHICH wire(s) moved (for the
        per-edge dirty set) and whether the legend moved at all (Save's
        "was the legend ever moved this session" condition) - blocks
        need no such diff since Save regenerates all of them
        unconditionally."""
        old = self._layout_world
        new = self._capture_layout_world()
        if new["legend"] != old["legend"]:
            self._legend_moved = True
        for eid, points in new["wires"].items():
            if points != old["wires"].get(eid):
                wire = self.wires.get(eid)
                if wire is not None:
                    self._dirty_wire_keys.add(wire.edge_key())
        self._undo_stack.append(old)
        self._layout_world = new
        self._layout_dirty = True
        self._update_layout_dirty_label()
        # M8 task 5 fix round 2: this fires after EVERY gesture, block
        # drag/resize commits included - re-route every currently-
        # pointless wire from the blocks' now-current geometry (a no-op
        # for a wire whose endpoints did not move) and refresh every
        # wire's auto-route fallback, so a pointless edge's line keeps
        # following a dragged block (spec 3) and a later delete-to-
        # auto-route on an explicit-path wire never falls back to a
        # stale route.
        for wire in self.wires.values():
            wire.refresh_auto_route(self.blocks)
        # M8 wave 2: this gesture (whatever kind) is now fully
        # committed - a block's live-move tracking baseline, if any,
        # is stale from here on (the NEXT drag on that block starts a
        # fresh gesture with its own baseline).
        self._live_move_tracking = None
        # M8 wave B fix round 1: a normal commit already had its own
        # item-level release handler clear magnet-highlight/readout,
        # so this is a redundant (idempotent, cheap) pass in the
        # common case - but routing every gesture-terminating path
        # through the SAME _cancel_gesture_visuals, rather than only
        # calling _clear_alignment_guides here and the fuller sweep
        # elsewhere, is the one-unified-path the review asked for.
        self._cancel_gesture_visuals()

    def _on_block_live_moved(self, block_id: str) -> None:
        """Wired to diagram_state.on_block_live_moved - fires on EVERY
        snapped step of a block drag, well before release/commit (M8
        wave 2, Visio-style connector glue). Unlike
        _on_layout_geometry_changed this only touches the wires
        actually attached to `block_id` (cheap - there are only ever a
        few), and does not touch the undo stack or dirty bookkeeping
        at all: dirty-marking and the undo snapshot are both already
        handled for free at commit, because translate_endpoint (below)
        mutates wire.edge.points directly, and
        _on_layout_geometry_changed's own before/after world-snapshot
        diff (unchanged) already catches that mutation exactly like
        any other points change - self._layout_world/`old` was
        captured at the END of the PREVIOUS commit, i.e. before this
        gesture (and all of its live-move calls) ever started.

        Computes this block's delta since the LAST call this gesture -
        or, on the FIRST call, since the gesture's own start
        (BlockItem.gesture_origin(), not "wherever we happened to
        start tracking"), so the very first snapped step of a drag is
        not silently dropped. Applies that delta two ways per attached
        wire: translate_endpoint (a no-op for a pointless wire) glues
        an explicit path's own endpoint to the block; refresh_auto_route
        (a no-op on RENDERING for a pointed wire) re-derives a
        pointless wire's straight route from the block's now-current
        position, so it visibly stays attached through the whole drag
        instead of only snapping back at release."""
        item = self.blocks.get(block_id)
        if item is None:
            return
        x, y, _, _ = item.geometry()
        if (self._live_move_tracking is not None
                and self._live_move_tracking[0] == block_id):
            _, lx, ly = self._live_move_tracking
        else:
            lx, ly = item.gesture_origin()
        dx, dy = x - lx, y - ly
        attached = [w for w in self.wires.values()
                   if w.edge.src == block_id or w.edge.dst == block_id]
        if dx or dy:
            for wire in attached:
                wire.translate_endpoint(block_id, dx, dy)
        self._live_move_tracking = (block_id, x, y)
        for wire in attached:
            wire.refresh_auto_route(self.blocks)
        self._update_alignment_guides(item)   # M8 wave B4b

    def _diagram_bounds(self) -> QRectF:
        """Bounding rect of every block's CURRENT geometry, padded by
        _GUIDE_MARGIN - the span an alignment reference line is drawn
        across. Deliberately computed from self.blocks (not
        self.scene.itemsBoundingRect(), which would also include the
        guide lines themselves once shown - a growing span each move,
        feeding back into itself) so it stays stable regardless of how
        long a previous guide line was."""
        xs0 = [item.geometry()[0] for item in self.blocks.values()]
        ys0 = [item.geometry()[1] for item in self.blocks.values()]
        xs1 = [g[0] + g[2] for g in
              (item.geometry() for item in self.blocks.values())]
        ys1 = [g[1] + g[3] for g in
              (item.geometry() for item in self.blocks.values())]
        if not xs0:
            return QRectF(0, 0, 0, 0)
        return QRectF(min(xs0) - _GUIDE_MARGIN, min(ys0) - _GUIDE_MARGIN,
                      max(xs1) - min(xs0) + 2 * _GUIDE_MARGIN,
                      max(ys1) - min(ys0) + 2 * _GUIDE_MARGIN)

    def _update_alignment_guides(self, item) -> None:
        """M8 wave B4b: reads the dragged BlockItem's _active_guides
        (set by its own itemChange during the snap decision - wave
        B4a) and shows/positions the matching QGraphicsLineItem(s)
        across the current diagram bounds, or hides whichever axis has
        no active alignment."""
        gx, gy = item._active_guides
        bounds = self._diagram_bounds()
        if gx is not None:
            self._guide_v.setLine(gx, bounds.top(), gx, bounds.bottom())
            self._guide_v.setVisible(True)
        else:
            self._guide_v.setVisible(False)
        if gy is not None:
            self._guide_h.setLine(bounds.left(), gy, bounds.right(), gy)
            self._guide_h.setVisible(True)
        else:
            self._guide_h.setVisible(False)

    def _clear_alignment_guides(self) -> None:
        self._guide_v.setVisible(False)
        self._guide_h.setVisible(False)

    def _cancel_gesture_visuals(self) -> None:
        """M8 wave B fix round 1 (findings 1-3, one design gap): a
        single, unified sweep of every in-flight-gesture VISUAL that
        has no other guaranteed terminator - alignment guides (B4),
        any block's magnet-highlight outline (B2), and the status-bar
        live readout (B3) - all three are driven by mouse-move events
        that a real drag's own mouseReleaseEvent normally clears, but
        nothing FORCES a release to ever happen: edit-mode toggle-off,
        undo, revert, and auto-layout can all land mid-gesture (the
        user releases the mouse only after, if at all) and none of
        those four previously reset this trio. Call this from all four
        BEFORE the state it would otherwise be reacting to changes
        (toggle-off: before items lose editability, so this method's
        own reads/writes see the same item states a live gesture would
        have). Magnet highlight is swept unconditionally across every
        block (set_magnet_highlight(False) is idempotent/cheap) rather
        than tracked centrally - WireItem._clear_handles independently
        clears its own tracked target too (finding 2), so the two
        never depend on each other; this is a second, complete
        backstop, not a partial one relying on that item-level fix
        having already run first."""
        self._clear_alignment_guides()
        for item in self.blocks.values():
            item.set_magnet_highlight(False)
        self._on_live_status("")

    def _on_live_status(self, msg: str) -> None:
        """Wired to diagram_state.on_live_status (M8 wave B3): a block
        drag/resize (BlockItem/_ResizeHandle), a waypoint drag
        (WaypointHandle), or a legend drag (LegendItem) all forward
        their own formatted "id: x, y (w x h)" / "waypoint: x, y" /
        "legend: x, y" string here on every live move step, and an
        empty string on release. Acceptance round 6 moved the readout
        from the window status bar into live_coord_label, an overlay
        inside the drawing area itself (see __init__) - visible only
        while a gesture feeds it."""
        if msg:
            self.live_coord_label.setText(msg)
            self.live_coord_label.adjustSize()
            self.live_coord_label.show()
        else:
            self.live_coord_label.hide()

    def _on_layout_undo(self) -> None:
        if not self.diagram_state.edit_mode or not self._undo_stack:
            return
        snapshot = self._undo_stack.pop()
        self._apply_layout_world(snapshot)
        self._layout_world = snapshot

    def _update_layout_dirty_label(self) -> None:
        self.layout_dirty_label.setVisible(self._layout_dirty)

    def _on_save_layout(self) -> None:
        """Collects every block's current geometry (ordered as in
        topology.blocks), only the TOUCHED wires' points, and the
        legend position if it was ever moved this session (or the file
        already had one) - then hands them to layout_io.save_layout,
        which patches <target>.topology.yaml in place and validates the
        patched file by reloading it before committing. A
        LayoutPatchError (bad patch, or the reload validation itself
        failing) is reported the same way _toggle_halt reports an
        EngineError: an inline statusBar message, no dialog."""
        blocks = OrderedDict(
            (bid, self.blocks[bid].geometry())
            for bid in self.engine.topology.blocks if bid in self.blocks)
        edge_points = {
            wire.edge_key(): list(wire.edge.points)
            for wire in self.wires.values()
            if wire.edge_key() in self._dirty_wire_keys}
        legend = None
        if self._legend_moved or self.engine.topology.legend is not None:
            lx, ly, _, _ = self.legend.geometry()
            legend = (lx, ly)

        try:
            save_layout(
                self.engine.topology_path, blocks, edge_points, legend,
                lambda p: load_topology(p, self.engine.model))
        except LayoutPatchError as e:
            self.statusBar().showMessage("error: %s" % e, 5000)
            return

        if legend is not None:
            self.engine.topology.legend = legend
        self._undo_stack = []
        self._dirty_wire_keys = set()
        self._layout_dirty = False
        self._layout_world = self._capture_layout_world()
        self._update_layout_dirty_label()

    def _on_revert_layout(self) -> None:
        """Reloads engine.topology_path from disk and re-applies its
        block/edge/legend geometry over the live items via
        apply_geometry/apply_points (callback-silent - this does not
        re-fire on_geometry_changed or grow the undo stack), discarding
        every unsaved edit. Block/legend/wire dataclass instances stay
        the SAME objects the items already reference (apply_* mutates
        their fields in place) - only their x/y/w/h/points values
        change, so nothing elsewhere holding a Block/Edge reference
        goes stale."""
        fresh = load_topology(self.engine.topology_path, self.engine.model)
        for index, (bid, item) in enumerate(self.blocks.items()):
            block = fresh.blocks.get(bid)
            if block is None:
                continue
            dw, dh = default_wh(block.kind)
            x = block.x if block.x is not None else 40 + 200 * (index % 4)
            y = block.y if block.y is not None else 40 + 140 * (index // 4)
            w = block.w if block.w is not None else dw
            h = block.h if block.h is not None else dh
            item.apply_geometry(x, y, w, h)

        fresh_edges_by_id = {e.id: e for e in fresh.edges}
        for eid, wire in self.wires.items():
            edge = fresh_edges_by_id.get(eid)
            if edge is not None:
                wire.apply_points(list(edge.points))

        lx, ly = fresh.legend if fresh.legend is not None else (700, 402)
        self.legend.apply_geometry(lx, ly, 0, 0)
        self.engine.topology.legend = fresh.legend

        # M8 task 5 fix round 2: blocks above were just reverted to
        # their on-disk geometry - re-route every currently-pointless
        # wire from THAT geometry (and refresh every wire's fallback),
        # same as every other block-geometry-changing site.
        for wire in self.wires.values():
            wire.refresh_auto_route(self.blocks)

        self._undo_stack = []
        self._dirty_wire_keys = set()
        self._layout_dirty = False
        self._legend_moved = fresh.legend is not None
        self._layout_world = self._capture_layout_world()
        self._update_layout_dirty_label()
        self._live_move_tracking = None   # M8 wave 2: bypassed the drag path
        # M8 wave B fix round 1: Revert can land mid-gesture too.
        self._cancel_gesture_visuals()

    def _on_auto_layout(self) -> None:
        """auto_layout() only returns (x, y) - width/height are kept as
        they currently are on each item. Explicit edge points are
        cleared on EVERY wire (spec 6: auto-layout re-routes everything
        straight, not just the blocks it moved) and every wire is
        re-routed from the NEW block positions (fix round 2:
        refresh_auto_route, so a pointless wire's line actually follows
        its moved blocks instead of rendering its pre-auto-layout
        path) - but only a wire that ACTUALLY HAD explicit points
        before the clear is added to the dirty set (fix round 2: a
        wire that was already pointless gains nothing to save, and
        marking it dirty would stamp a needless `points: []` onto an
        edge entry that never had one, breaking the clean-diff
        promise - spec section 5). One undo snapshot is pushed up
        front (apply_geometry/apply_points are callback-silent, so
        this method owns pushing it) so a single Z restores the
        pre-auto-layout picture whole."""
        self._undo_stack.append(self._layout_world)

        positions = auto_layout(list(self.engine.topology.blocks.values()),
                                self.engine.topology.edges)
        for bid, item in self.blocks.items():
            if bid not in positions:
                continue
            x, y = positions[bid]
            _, _, w, h = item.geometry()
            item.apply_geometry(x, y, w, h)

        # Acceptance round 5: the legend moves too. Left at its stale
        # pre-layout coordinates it routinely lands on top of a
        # relocated block (seen on f411: legend over the bus matrix).
        # Below the whole picture is the one spot no block placement
        # can collide with; x aligns with the leftmost column.
        if positions:
            max_bottom = 0
            for bid, item in self.blocks.items():
                if bid not in positions:
                    continue
                x, y, w, h = item.geometry()
                max_bottom = max(max_bottom, y + h)
            lx = snap(min(x for x, _ in positions.values()), False)
            ly = snap(max_bottom + 60, False)
            self.legend.apply_geometry(lx, ly, 0, 0)
            self._legend_moved = True

        for wire in self.wires.values():
            had_points = bool(wire.edge.points)
            wire.apply_points([])
            wire.refresh_auto_route(self.blocks)
            if had_points:
                self._dirty_wire_keys.add(wire.edge_key())

        self._layout_dirty = True
        self._layout_world = self._capture_layout_world()
        self._update_layout_dirty_label()
        self._live_move_tracking = None   # M8 wave 2: bypassed the drag path
        # M8 wave B fix round 1: Auto-layout can land mid-gesture too.
        self._cancel_gesture_visuals()

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
        time the Scope page is activated.

        M8 (task 5): switching to Scope while the layout editor is on
        turns it off - unlike the page buttons above, setChecked here
        is meant to recurse into _on_edit_layout_toggled (via the
        toggled signal, not clicked) so editability/the dashed cue/the
        strip all unwind exactly as a manual click would; dirty state
        is untouched (layout_dirty_label keeps showing on the
        toolbar)."""
        on_scope = index == 1
        self.inspector_dock.setVisible(not on_scope)
        self.datapath_page_btn.setChecked(not on_scope)
        self.scope_page_btn.setChecked(on_scope)
        if on_scope and self.edit_layout_btn.isChecked():
            self.edit_layout_btn.setChecked(False)
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
        # T11 hardware gate: lets ScopePage's own target-reboot
        # recovery (_recover_after_reboot) post one info line to the
        # SAME event log every other info/anomaly row goes through,
        # without ScopePage needing to own (or import) EventLog
        # itself - the same "push a callback down, not a reference up"
        # shape event_log.py's own on_focus/on_event_time already use
        # in the other direction.
        self.scope_page.on_info = self.event_log.add_info
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
