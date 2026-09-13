"""Scope panel: live pyqtgraph plot of History-backed channels, the
M6 scope-view plan's "Scope" dock.

Not a port of anything in prototype/ui_proto.py - the prototype had no
scope view. New widget, following the same conventions as the other
Inspector-family pages (register_page.py, memory_page.py): a plain
QWidget, EngineError caught at the boundary and rendered as text in
the panel rather than a QMessageBox (never a dialog, including for
the address-watch add path).

Channel model: a channel is either a currently-polled register key
(picked from `engine.polled`, already being read by the poller - the
scope does not itself call engine.set_watch for these) or a synthetic
address-watch key returned by `engine.add_addr_watch()` (owned by this
panel: removing the channel also calls engine.remove_addr_watch() so
the poller stops reading it, since the scope was the one that asked
for it).

X axis: seconds relative to `self._t0`, captured as time.monotonic()
at ScopePage construction. Note this is "dock-open time", not a true
"engine start" timestamp - Engine (core/engine/core.py) exposes no
such timestamp, and adding one is out of this task's scope. Samples
already older than the panel's own t0 (History's 10 s window can
predate a lazily-opened dock) simply plot at a negative x, which is
harmless since the window is always <= History.window_s wide.

Gap honesty (spec 6.7): consecutive samples more than 3x the median
sample interval apart get a NaN inserted between them, and every curve
is drawn with `connect="finite"` so pyqtgraph breaks the line there
instead of drawing a lying straight segment across the gap.

Effective rate: per channel, (samples in the last 2 s - 1) / span,
shown as a suffix on the channel's list row.

Event markers: `add_event_marker(t, msg)` (fed by MainWindow from the
same update path that feeds the event log) adds a vertical
pg.InfiniteLine with a short label; markers older than the History
window are pruned on each refresh so the marker set never outlives the
data it annotates.

Test-support accessors `channel_sample_count(key)` (raw History sample
count - independent of whether refresh_plot() has run yet) and
`curve_point_count(key)` (plotted point count, post gap-NaN-insertion)
are part of this panel's produced interface.

Bandwidth budget indicator: `budget_label`, next to the add-channel
controls, reads "sweep N reads @ R Hz" - N is `engine.read_ops` (one
block-read transaction per contiguous group build_read_plan merged the
polled registers/addr-watches into), R is the live sweep rate last
reported via `set_sweep_rate(hz)` (wired from MainWindow.apply_update,
fed from EngineUpdate.snapshot.rate_hz). Before any rate has been
received the label drops the "@ R Hz" suffix. When 0 < R < LOW_RATE_HZ
the label is styled orange with a tooltip nudging towards contiguous
addresses; otherwise it uses the default palette. `_update_budget_label`
is called from refresh_plot() (every repaint) and also right after
add_channel()/add_address_channel()/remove_channel() return, so the
count does not wait for the next repaint tick to reflect a channel
change - though since add_addr_watch()/remove_addr_watch()'s plan swap
lands on the poller thread, an immediate read of engine.read_ops right
after one of those calls can still show the pre-swap count until the
next refresh.

Cursor sync (event-log-to-scope focus): `jump_to(t)` places (or moves)
a single cursor `pg.InfiniteLine` at t, styled distinctly from event
markers (dashed blue vs. solid red) so the two are never confused, and
flashes whichever marker in `_markers` is nearest to t so the user can
see which annotation the cursor landed on. `t` is in the same
time.monotonic domain as `self._t0` and `add_event_marker`'s own t -
positioned the same way, `x = t - self._t0`. `cursor_time()` returns
the raw t last passed to jump_to(), or None before the cursor has ever
been placed - fed by MainWindow._on_log_time_focus, itself wired to
EventLog's new on_event_time callback (an event-log row click).

ELF symbol picker: "Load ELF..." opens a
QFileDialog (the one dialog this panel uses - everything else is
inline, per the class-level convention above) and hands the chosen
path to `load_elf()`, which lazily imports ui.elf_symbols (same
import-on-first-use pattern as pyqtgraph at this module's top) and
populates `symbol_list` with every symbol name, filtered live by
`symbol_filter_edit`. Picking one and clicking "Add symbol" calls
`add_symbol_channel(name)`, which is a thin wrapper over the existing
`add_address_channel()` - same synthetic-key/EngineError-through
behavior as the manual address row, not a separate code path.
load_symbols() reports every OBJECT symbol regardless of size; a
channel always reads one 32-bit word (Engine.add_addr_watch), so
symbol_list carries a tooltip noting that a symbol larger than 4
bytes is only sampled at its first word - this is documented here
rather than filtered at load time, per the brief.
"""
import time
from typing import Dict, List, Optional, Tuple

import pyqtgraph as pg
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox,
                               QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from core.engine.core import Engine, EngineError

SYMBOL_LIST_TOOLTIP = (
    "Symbols from the loaded ELF. Adding a channel always reads the "
    "symbol's first 32-bit word, regardless of its declared size.")

# pathscope is light-theme only by design (see ui/app.py's
# app.styleHints().setColorScheme(Qt.ColorScheme.Light)) - pyqtgraph's
# own defaults violate that twice over: a black plot background, and a
# foreground of 'd' (a grey tuned to read on black, which axis/tick/
# grid/legend text all key off). Both must be set here, at this
# module's import site - the only place pyqtgraph is imported at all
# (MainWindow imports this module lazily, from inside its "Scope"
# toolbar toggle handler, specifically so pyqtgraph's import cost is
# paid only on first open) - and before ScopePage.__init__ constructs
# any PlotWidget below, since pyqtgraph bakes these options into each
# new plot item at construction time.
pg.setConfigOption("background", "w")
pg.setConfigOption("foreground", "k")

MONO = QFont()
MONO.setFamilies(["Menlo", "Consolas", "Courier New"])
MONO.setPointSize(10)

REFRESH_MS = 200
RATE_WINDOW_S = 2.0
GAP_FACTOR = 3.0
MARKER_PEN = "#C62828"
CURSOR_PEN = "#1565C0"
CROSSHAIR_PEN = "#9E9E9E"
MARKER_FLASH_PEN = "#FFB300"
MARKER_FLASH_MS = 400
MARKER_HARD_CAP = 200

# Side panel width (also used by side_widget.setMaximumWidth() below) -
# named here so the transform strip's Normalize-label fit check has a
# stable budget to measure against, rather than a second copy of the
# literal.
SIDE_MAX_WIDTH = 260
SPIN_MIN_WIDTH = 90
NORMALIZE_LABEL_FULL = "Normalize (map window to 0..1)"
NORMALIZE_LABEL_SHORT = "Normalize"

# Bandwidth budget indicator: below this live sweep rate, the label
# calls out that the read count is the likely cause (spec: "R < 15.0
# and R > 0").
LOW_RATE_HZ = 15.0
BUDGET_ORANGE = "#E65100"
BUDGET_ORANGE_STYLE = "color: %s;" % BUDGET_ORANGE
BUDGET_TOOLTIP = ("high read count is lowering the sweep rate; prefer "
                  "contiguous addresses")

CURVE_COLORS = [
    "#1976D2", "#2E7D32", "#EF6C00", "#6A1B9A",
    "#00838F", "#AD1457", "#5D4037", "#455A64",
]


def _gapped_xy(series: List[Tuple[float, int]], t0: float
              ) -> Tuple[List[float], List[float]]:
    """series -> (x, y) with a NaN inserted wherever the gap to the
    previous sample exceeds GAP_FACTOR times the median sample
    interval, so a pyqtgraph curve drawn with connect="finite" breaks
    the line instead of interpolating across a stall."""
    if not series:
        return [], []
    xs = [t - t0 for t, _v in series]
    ys = [float(v) for _t, v in series]
    if len(xs) < 3:
        return xs, ys
    intervals = sorted(xs[i + 1] - xs[i] for i in range(len(xs) - 1))
    mid = len(intervals) // 2
    if len(intervals) % 2 == 1:
        median = intervals[mid]
    else:
        # even count: true median is the average of the two middle
        # values, not the upper one - intervals[mid] alone skews high,
        # raising GAP_FACTOR * median enough to under-detect real
        # gaps in a short series (see the _gapped_xy test cases).
        median = (intervals[mid - 1] + intervals[mid]) / 2.0
    out_x = [xs[0]]
    out_y = [ys[0]]
    for i in range(1, len(xs)):
        dt = xs[i] - xs[i - 1]
        if median > 0 and dt > GAP_FACTOR * median:
            out_x.append((xs[i - 1] + xs[i]) / 2.0)
            out_y.append(float("nan"))
        out_x.append(xs[i])
        out_y.append(ys[i])
    return out_x, out_y


def _effective_rate_hz(series: List[Tuple[float, int]], now: float) -> float:
    recent = [t for t, _v in series if now - t <= RATE_WINDOW_S]
    if len(recent) < 2:
        return 0.0
    span = recent[-1] - recent[0]
    if span <= 0:
        return 0.0
    return (len(recent) - 1) / span


def value_at(series: List[Tuple[float, int]], t: float) -> Optional[int]:
    """The newest sample with sample_t <= t, or None if series is
    empty or every sample postdates t. series must be sorted
    ascending by t - History.series()'s own guarantee - so a linear
    scan from the end finds it without a bisect import; this is the
    crosshair readout's pure, unit-testable lookup."""
    for i in range(len(series) - 1, -1, -1):
        if series[i][0] <= t:
            return series[i][1]
    return None


def _apply_transform(ys: List[float], transform: dict) -> List[float]:
    """Display-only transform applied to an already-gapped y array
    (see _gapped_xy) - NaN gap placeholders pass through untouched,
    since a gap's midpoint carries no real sample to scale or
    normalize. Normalize ignores scale/offset entirely and maps this
    window's min..max to 0..1, with a flat-series guard (max == min)
    mapping every finite value to 0.5 instead of dividing by a zero
    span. Pure python lists throughout - no numpy dependency."""
    if transform["normalize"]:
        finite = [v for v in ys if v == v]        # v == v excludes NaN
        if not finite:
            return list(ys)
        lo = min(finite)
        hi = max(finite)
        if hi == lo:
            return [0.5 if v == v else v for v in ys]
        span = hi - lo
        return [(v - lo) / span if v == v else v for v in ys]
    scale = transform["scale"]
    offset = transform["offset"]
    return [(v - offset) * scale if v == v else v for v in ys]


class ScopePage(QWidget):
    def __init__(self, engine: Engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.frozen = False
        self._t0 = time.monotonic()
        self._channels: Dict[str, dict] = {}
        self._markers: List[Tuple[float, pg.InfiniteLine]] = []
        self._cursor_t: Optional[float] = None
        self._cursor_line: Optional[pg.InfiniteLine] = None
        # per-channel series cached by the last refresh_plot() - the
        # crosshair handler reads this instead of calling
        # engine.history.series() again, so a mouse-move event costs
        # no extra History copy beyond the refresh that already ran.
        self._last_series: Dict[str, List[Tuple[float, int]]] = {}
        self._crosshair_t: Optional[float] = None
        self._crosshair_line: Optional[pg.InfiniteLine] = None
        # name -> ui.elf_symbols.Symbol (a namedtuple, hence `tuple`
        # here) - populated by load_elf(); ui.elf_symbols is imported
        # lazily there, not at this module's top, so this attribute is
        # typed structurally rather than by importing the class.
        self.elf_symbols: Dict[str, tuple] = {}
        # live sweep rate last reported via set_sweep_rate(), or None
        # before MainWindow has ever fed one in - the budget label's
        # "no rate yet" state.
        self._sweep_rate: Optional[float] = None

        outer = QHBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)

        side = QVBoxLayout()
        side.addWidget(QLabel("Channels"))
        self.channel_list = QListWidget()
        side.addWidget(self.channel_list, 1)

        # Removal is a channel-list operation, so it belongs directly
        # under the list - both for locality and for reachability: on
        # a short dock (see the QScrollArea wrap below), this keeps
        # "Remove channel" visible without scrolling even when the ELF
        # section further down is scrolled out of view.
        self.remove_btn = QPushButton("Remove channel")
        self.remove_btn.clicked.connect(self._on_remove_clicked)
        side.addWidget(self.remove_btn)

        # per-channel scale/offset/normalize edit strip (feature 2) -
        # hidden until a channel row is selected, populated from that
        # channel's stored transform, and hidden again on deselection
        # (currentItemChanged fires with current=None when the list
        # goes empty of a selection, e.g. after removing the selected
        # row).
        #
        # Two rows, not one - the ~260px side panel (SIDE_MAX_WIDTH)
        # is too narrow to fit "scale" + spinbox + "offset" + spinbox
        # + "Normalize" on a single hbox row without truncating (a
        # hardware-session screenshot showed "Normalize" clipped to
        # "Norr" and the offset spinbox's value clipped): row 1 is
        # scale+offset, row 2 is Normalize alone with the full width
        # to itself.
        transform_row1 = QHBoxLayout()
        transform_row1.addWidget(QLabel("scale"))
        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(1e-6, 1e9)
        self.scale_spin.setDecimals(6)
        self.scale_spin.setValue(1.0)
        self.scale_spin.setMinimumWidth(SPIN_MIN_WIDTH)
        transform_row1.addWidget(self.scale_spin, 1)

        transform_row1.addWidget(QLabel("offset"))
        self.offset_spin = QDoubleSpinBox()
        self.offset_spin.setRange(-1e9, 1e9)
        self.offset_spin.setDecimals(6)
        self.offset_spin.setMinimumWidth(SPIN_MIN_WIDTH)
        transform_row1.addWidget(self.offset_spin, 1)

        transform_row2 = QHBoxLayout()
        # Prefer the fuller hint text, but only if it plausibly fits
        # the side panel's width - falls back to the bare word rather
        # than risk the exact truncation this strip exists to fix.
        self.normalize_check = QCheckBox(NORMALIZE_LABEL_FULL)
        fm = self.normalize_check.fontMetrics()
        checkbox_overhead = 40          # indicator box + spacing/margins
        if fm.horizontalAdvance(NORMALIZE_LABEL_FULL) > (
                SIDE_MAX_WIDTH - checkbox_overhead):
            self.normalize_check.setText(NORMALIZE_LABEL_SHORT)
        transform_row2.addWidget(self.normalize_check)
        transform_row2.addStretch(1)

        transform_layout = QVBoxLayout()
        transform_layout.setContentsMargins(0, 0, 0, 0)
        transform_layout.addLayout(transform_row1)
        transform_layout.addLayout(transform_row2)

        self.transform_strip = QWidget()
        self.transform_strip.setLayout(transform_layout)
        self.transform_strip.setVisible(False)
        side.addWidget(self.transform_strip)

        self.scale_spin.valueChanged.connect(self._on_transform_edited)
        self.offset_spin.valueChanged.connect(self._on_transform_edited)
        self.normalize_check.toggled.connect(self._on_transform_edited)
        # Small honest-UI touch: while Normalize is checked, scale and
        # offset are ignored by refresh_plot()'s transform, so grey
        # them out rather than leave them editable-but-inert.
        self.normalize_check.toggled.connect(self._set_scale_offset_enabled)
        self.channel_list.currentItemChanged.connect(
            self._on_channel_selected)

        reg_row = QHBoxLayout()
        self.reg_combo = QComboBox()
        reg_row.addWidget(self.reg_combo, 1)
        add_reg_btn = QPushButton("Add register")
        add_reg_btn.clicked.connect(self._on_add_register_clicked)
        reg_row.addWidget(add_reg_btn)
        side.addLayout(reg_row)

        addr_row = QHBoxLayout()
        self.addr_edit = QLineEdit()
        self.addr_edit.setPlaceholderText("0x20000000")
        self.addr_edit.setFont(MONO)
        addr_row.addWidget(self.addr_edit, 1)
        self.addr_label_edit = QLineEdit()
        self.addr_label_edit.setPlaceholderText("label")
        addr_row.addWidget(self.addr_label_edit, 1)
        add_addr_btn = QPushButton("Add address")
        add_addr_btn.clicked.connect(self._on_add_address_clicked)
        addr_row.addWidget(add_addr_btn)
        side.addLayout(addr_row)

        self.budget_label = QLabel("")
        side.addWidget(self.budget_label)

        side.addWidget(QLabel("ELF symbols"))
        elf_row = QHBoxLayout()
        load_elf_btn = QPushButton("Load ELF...")
        load_elf_btn.clicked.connect(self._on_load_elf_clicked)
        elf_row.addWidget(load_elf_btn)
        side.addLayout(elf_row)

        self.symbol_filter_edit = QLineEdit()
        self.symbol_filter_edit.setPlaceholderText("filter symbols")
        self.symbol_filter_edit.textChanged.connect(
            self._refresh_symbol_list)
        side.addWidget(self.symbol_filter_edit)

        self.symbol_list = QListWidget()
        self.symbol_list.setToolTip(SYMBOL_LIST_TOOLTIP)
        self.symbol_list.setMaximumHeight(120)
        side.addWidget(self.symbol_list)

        add_symbol_btn = QPushButton("Add symbol")
        add_symbol_btn.setToolTip(SYMBOL_LIST_TOOLTIP)
        add_symbol_btn.clicked.connect(self._on_add_symbol_clicked)
        side.addWidget(add_symbol_btn)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #C62828;")
        self.error_label.setWordWrap(True)
        side.addWidget(self.error_label)

        self.window_label = QLabel(
            "window: %.0f s (History)" % engine.history.window_s)
        side.addWidget(self.window_label)

        side_widget = QWidget()
        side_widget.setLayout(side)

        # The side panel's content (channel list, transform strip, add
        # rows, budget label, ELF section, Remove channel, window
        # label) can exceed the dock's height at typical sizes -
        # without a scroll area, the bottom controls are pushed
        # off-screen with no way to reach them (a hardware-session
        # screenshot showed the panel cut off at "Add symbol").
        # setWidgetResizable(True) lets side_widget track the
        # viewport's width (so its own child layouts still fill it
        # horizontally) while its height is free to exceed the
        # viewport and scroll.
        self.side_scroll = QScrollArea()
        self.side_scroll.setWidgetResizable(True)
        self.side_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff)
        self.side_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.side_scroll.setWidget(side_widget)
        self.side_scroll.setMaximumWidth(SIDE_MAX_WIDTH)
        outer.addWidget(self.side_scroll)

        plot_side = QVBoxLayout()
        self.time_label = QLabel("t=-- s")
        plot_side.addWidget(self.time_label)

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.plot_widget.setLabel("bottom", "t", units="s")
        self.plot = self.plot_widget.getPlotItem()
        self.plot.addLegend()
        plot_side.addWidget(self.plot_widget, 1)

        plot_container = QWidget()
        plot_container.setLayout(plot_side)
        outer.addWidget(plot_container, 1)

        # Crosshair (feature 1): a light-grey dashed vertical line,
        # deliberately distinct from the cursor's dashed blue
        # (CURSOR_PEN, jump_to()) and a marker's solid red
        # (MARKER_PEN, add_event_marker()) so none of the three are
        # ever confused. SignalProxy rate-limits sigMouseMoved so a
        # fast mouse doesn't flood _update_crosshair with more work
        # than the display can use; self.plot is already this page's
        # PlotItem (aliased above from self.plot_widget.getPlotItem()),
        # so its ViewBox is reached as self.plot.vb rather than a
        # second .plotItem hop off a bare PlotWidget.
        self._crosshair_proxy = pg.SignalProxy(
            self.plot.scene().sigMouseMoved, rateLimit=30,
            slot=self._on_mouse_moved)

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh_plot)
        self._timer.start()

        self._update_budget_label()

    # -- freeze --------------------------------------------------------------

    def set_frozen(self, on: bool) -> None:
        """Called by MainWindow from its existing freeze toggle. Pauses
        (or resumes) the 200 ms repaint timer; refresh_plot() itself
        stays callable regardless (tests call it directly). Resuming
        only starts the timer if the page is currently visible -
        unfreezing while the dock is hidden (tabbed away) must not
        wake a timer that hideEvent() below deliberately paused;
        showEvent() re-checks self.frozen when the dock is next shown,
        so the timer still resumes correctly at that point."""
        self.frozen = on
        if on:
            self._timer.stop()
        elif self.isVisible():
            self._timer.start()

    def hideEvent(self, event) -> None:
        """Stop the repaint timer while the page isn't shown - tabbed
        away behind Event log, same bug class as memory_page.py's
        auto-refresh: an invisible ScopePage has no reason to keep
        redrawing a plot nobody sees every 200 ms forever."""
        self._timer.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        """Resume the timer on redisplay, unless the user has frozen
        the global display - set_frozen(True) wins over a plain
        show/hide cycle, mirroring memory_page.py's
        auto_check.isChecked() guard (there, the user's own
        preference; here, MainWindow's freeze state)."""
        if not self.frozen:
            self._timer.start()
        super().showEvent(event)

    # -- channel management ----------------------------------------------

    def add_channel(self, key: str, label: Optional[str] = None) -> None:
        """Add a channel for an already-polled register key (picked
        from the "Add register" dropdown, which only lists
        engine.polled). Idempotent - re-adding an existing key is a
        no-op."""
        if key in self._channels:
            return
        if label is None:
            label = self.engine.addr_watch_labels.get(key, key)
        self._add_channel_common(key, label)
        self._update_budget_label()

    def add_address_channel(self, addr: int, label: str) -> str:
        """Add a fixed-address channel via engine.add_addr_watch().
        Raises EngineError straight through (guarded/misaligned
        address) - the button handler below is what catches it and
        renders the message inline, never a dialog.

        add_addr_watch() is idempotent on addr but always re-sets the
        engine's label; re-adding an address that already has a
        channel here must follow suit rather than silently keeping
        the first label, so the displayed legend/list entry stays in
        sync with what the engine now reports for this key."""
        key = self.engine.add_addr_watch(addr, label)
        if key in self._channels:
            self._relabel_channel(key, label)
        else:
            self._add_channel_common(key, label)
        self._update_budget_label()
        return key

    def _relabel_channel(self, key: str, label: str) -> None:
        entry = self._channels[key]
        entry["label"] = label
        entry["curve"].opts["name"] = label
        legend_label = self.plot.legend.getLabel(entry["curve"])
        if legend_label is not None:
            legend_label.setText(label)
        entry["item"].setText(label)

    def remove_channel(self, key: str) -> None:
        entry = self._channels.pop(key, None)
        if entry is None:
            return
        self._last_series.pop(key, None)
        self.plot.removeItem(entry["curve"])
        row = self.channel_list.row(entry["item"])
        if row >= 0:
            self.channel_list.takeItem(row)
        if key.startswith("@"):
            # the scope itself asked for this address watch - clean it
            # up so the poller stops reading it once nothing displays
            # it any more.
            self.engine.remove_addr_watch(key)
        self._update_budget_label()

    def _add_channel_common(self, key: str, label: str) -> None:
        color = CURVE_COLORS[len(self._channels) % len(CURVE_COLORS)]
        curve = self.plot.plot([], [], pen=pg.mkPen(color=color, width=2),
                               name=label, connect="finite")
        item = QListWidgetItem(label)
        item.setData(Qt.UserRole, key)
        self.channel_list.addItem(item)
        # transform: display-only scale/offset/normalize (feature 2),
        # identity by default; rate: last-computed effective Hz,
        # cached here so _row_text() can rebuild the row's rate
        # suffix from a crosshair move without waiting on the next
        # refresh_plot().
        self._channels[key] = {
            "label": label, "curve": curve, "item": item,
            "transform": {"scale": 1.0, "offset": 0.0, "normalize": False},
            "rate": 0.0,
        }

    # -- add-channel UI handlers -------------------------------------------

    def _refresh_reg_combo(self) -> None:
        current = self.reg_combo.currentText()
        keys = sorted(k for k in self.engine.polled if not k.startswith("@"))
        if keys == [self.reg_combo.itemText(i)
                    for i in range(self.reg_combo.count())]:
            return
        self.reg_combo.clear()
        self.reg_combo.addItems(keys)
        idx = self.reg_combo.findText(current)
        if idx >= 0:
            self.reg_combo.setCurrentIndex(idx)

    def _on_add_register_clicked(self) -> None:
        key = self.reg_combo.currentText().strip()
        if not key:
            return
        self.error_label.setText("")
        self.add_channel(key)

    def _on_add_address_clicked(self) -> None:
        text = self.addr_edit.text().strip()
        label = self.addr_label_edit.text().strip() or text
        try:
            addr = int(text, 0)
        except ValueError:
            self.error_label.setText("invalid address: %r" % text)
            return
        try:
            self.add_address_channel(addr, label)
        except EngineError as e:
            self.error_label.setText("error: %s" % e)
            return
        self.error_label.setText("")
        self.addr_edit.clear()
        self.addr_label_edit.clear()

    def _on_remove_clicked(self) -> None:
        item = self.channel_list.currentItem()
        if item is None:
            return
        self.remove_channel(item.data(Qt.UserRole))

    # -- ELF symbol picker -------------------------------------------------

    def load_elf(self, path: str) -> None:
        """Load every OBJECT symbol from path's ELF (via
        ui.elf_symbols.load_symbols, imported lazily here - same
        import-on-first-use pattern as pyqtgraph at this module's top,
        just deferred one step further since not every scope session
        loads an ELF at all) into `elf_symbols` and repopulate
        `symbol_list`. Any failure (bad path, unparsable ELF) is
        rendered in `error_label` exactly like an EngineError from the
        address row - never a dialog; the QFileDialog in
        _on_load_elf_clicked is this panel's one and only dialog."""
        try:
            from ui.elf_symbols import load_symbols
            symbols = load_symbols(path)
        except Exception as e:
            self.error_label.setText("ELF load failed: %s" % e)
            return
        self.elf_symbols = {s.name: s for s in symbols}
        self.symbol_filter_edit.clear()
        self._refresh_symbol_list()
        self.error_label.setText("")

    def add_symbol_channel(self, name: str) -> str:
        """Add a channel for a symbol already loaded by load_elf(), by
        its first word - a thin wrapper over add_address_channel (same
        synthetic key, same EngineError-through behavior), not a
        separate code path. Raises KeyError for an unknown name."""
        symbol = self.elf_symbols[name]
        return self.add_address_channel(symbol.addr, symbol.name)

    def _refresh_symbol_list(self) -> None:
        needle = self.symbol_filter_edit.text().strip().lower()
        self.symbol_list.clear()
        for name in self.elf_symbols:        # already sorted by load_symbols
            if needle in name.lower():
                self.symbol_list.addItem(QListWidgetItem(name))

    def _on_load_elf_clicked(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Load ELF...", "", "ELF files (*.elf);;All files (*)")
        if not path:
            return
        self.load_elf(path)

    def _on_add_symbol_clicked(self) -> None:
        item = self.symbol_list.currentItem()
        if item is None:
            return
        try:
            self.add_symbol_channel(item.text())
        except EngineError as e:
            self.error_label.setText("error: %s" % e)
            return
        self.error_label.setText("")

    # -- event markers ---------------------------------------------------

    def add_event_marker(self, t: float, msg: str) -> None:
        x = t - self._t0
        short = msg if len(msg) <= 24 else msg[:21] + "..."
        line = pg.InfiniteLine(
            pos=x, angle=90, pen=pg.mkPen(color=MARKER_PEN, width=1),
            label=short,
            labelOpts={"color": MARKER_PEN, "position": 0.95})
        self.plot.addItem(line)
        self._markers.append((t, line))
        # cap guards hidden/frozen accumulation; window pruning
        # (_prune_markers, run from refresh_plot) remains the primary
        # mechanism.
        while len(self._markers) > MARKER_HARD_CAP:
            _oldest_t, oldest_line = self._markers.pop(0)
            self.plot.removeItem(oldest_line)

    def _prune_markers(self, now: float) -> None:
        window = self.engine.history.window_s
        kept = []
        for t, line in self._markers:
            if now - t > window:
                self.plot.removeItem(line)
            else:
                kept.append((t, line))
        self._markers = kept

    # -- cursor sync (event-log click -> scope cursor, task 4) -------------

    def jump_to(self, t: float) -> None:
        """Place (or move) the scope cursor at t and flash the nearest
        event marker. t is in the same time.monotonic domain as
        self._t0 - positioned the same way add_event_marker positions
        a marker, x = t - self._t0. The cursor's pen (dashed blue) is
        deliberately distinct from a marker's (solid red) so the two
        are never confused on the plot."""
        self._cursor_t = t
        x = t - self._t0
        if self._cursor_line is None:
            self._cursor_line = pg.InfiniteLine(
                pos=x, angle=90,
                pen=pg.mkPen(color=CURSOR_PEN, width=2, style=Qt.DashLine))
            # ignoreBounds=True: a cursor parked at an old event time
            # must not itself stretch the plot's auto-range - without
            # this, ViewBox.childrenBounds() (which drives auto-range)
            # includes every added item by default, pinning one edge
            # of the view to a stale x forever while the live data
            # scrolls past it.
            self.plot.addItem(self._cursor_line, ignoreBounds=True)
        else:
            self._cursor_line.setPos(x)
        self._flash_nearest_marker(t)

    def cursor_time(self) -> Optional[float]:
        """The raw t last passed to jump_to(), or None before the
        cursor has ever been placed."""
        return self._cursor_t

    def _flash_nearest_marker(self, t: float) -> None:
        if not self._markers:
            return
        _nearest_t, line = min(self._markers,
                               key=lambda entry: abs(entry[0] - t))
        line.setPen(pg.mkPen(color=MARKER_FLASH_PEN, width=3))
        QTimer.singleShot(MARKER_FLASH_MS,
                          lambda: self._unflash_marker(line))

    def _unflash_marker(self, line: pg.InfiniteLine) -> None:
        # the marker may already have been pruned (History window
        # rolled past it) by the time this fires - only restore the
        # normal pen if it is still one of ours.
        if any(existing is line for _t, existing in self._markers):
            line.setPen(pg.mkPen(color=MARKER_PEN, width=1))

    # -- repaint -----------------------------------------------------------

    def refresh_plot(self) -> None:
        self._refresh_reg_combo()
        now = time.monotonic()
        for key, entry in self._channels.items():
            series = self.engine.history.series(key)
            self._last_series[key] = series
            x, y = _gapped_xy(series, self._t0)
            y = _apply_transform(y, entry["transform"])
            entry["curve"].setData(x, y, connect="finite")
            entry["rate"] = _effective_rate_hz(series, now)
            entry["item"].setText(self._row_text(key))
        self._prune_markers(now)
        self._update_budget_label()

    # -- bandwidth budget indicator -----------------------------------------

    def set_sweep_rate(self, hz: float) -> None:
        """Store the live sweep rate (fed by MainWindow.apply_update
        from EngineUpdate.snapshot.rate_hz) and refresh the budget
        label immediately - callers don't need to wait for the next
        refresh_plot() tick to see a rate-triggered orange warning."""
        self._sweep_rate = hz
        self._update_budget_label()

    def _update_budget_label(self) -> None:
        """Rebuild budget_label's text and style from engine.read_ops
        (N) and the last-reported sweep rate (R, self._sweep_rate).
        Before any rate has been received (self._sweep_rate is None)
        the "@ R Hz" suffix is dropped entirely. Orange + tooltip only
        while 0 < R < LOW_RATE_HZ - R == 0 (a real, reported stall) or
        R >= LOW_RATE_HZ both use the default palette."""
        n = self.engine.read_ops
        rate = self._sweep_rate
        if rate is None:
            text = "sweep %d reads" % n
        else:
            text = "sweep %d reads @ %.1f Hz" % (n, rate)
        if rate is not None and 0 < rate < LOW_RATE_HZ:
            self.budget_label.setStyleSheet(BUDGET_ORANGE_STYLE)
            self.budget_label.setToolTip(BUDGET_TOOLTIP)
        else:
            self.budget_label.setStyleSheet("")
            self.budget_label.setToolTip("")
        self.budget_label.setText(text)

    # -- crosshair readout (feature 1) --------------------------------------

    def _row_text(self, key: str) -> str:
        """The channel-list row text: the existing "name (rate Hz)"
        suffix, plus - once the crosshair has moved at least once - a
        "= value (0xhex)" suffix showing the RAW sample at the
        crosshair's time (never the scaled/normalized display value,
        which is the whole point of a raw readout)."""
        entry = self._channels[key]
        text = "%s  (%.1f Hz)" % (entry["label"], entry["rate"])
        if self._crosshair_t is not None:
            raw_t = self._crosshair_t + self._t0
            value = value_at(self._last_series.get(key, []), raw_t)
            if value is None:
                value_text = "--"
            else:
                value_text = "%d (0x%X)" % (value, value)
            text += "  = %s" % value_text
        return text

    def _on_mouse_moved(self, evt) -> None:
        pos = evt[0]
        if not self.plot.sceneBoundingRect().contains(pos):
            # mouse left the plot viewport - hide the crosshair rather
            # than leaving it frozen at its last x (readout rows keep
            # their last values, which is fine; only the line itself
            # needs to disappear).
            if self._crosshair_line is not None:
                self._crosshair_line.hide()
            return
        view_point = self.plot.vb.mapSceneToView(pos)
        self._update_crosshair(view_point.x())

    def _update_crosshair(self, view_t: float) -> None:
        """Move the crosshair to view_t - plot-relative seconds, the
        same domain mapSceneToView's x is in (t - self._t0) - and
        refresh every channel row's raw-value suffix plus the time
        label. Reads only self._last_series, populated by the most
        recent refresh_plot(): no engine.history.series() call here,
        so a mouse-move event costs no History copy of its own."""
        self._crosshair_t = view_t
        if self._crosshair_line is None:
            self._crosshair_line = pg.InfiniteLine(
                pos=view_t, angle=90, movable=False,
                pen=pg.mkPen(color=CROSSHAIR_PEN, width=1,
                             style=Qt.DashLine))
            # ignoreBounds=True: a crosshair left parked at whatever x
            # the mouse last visited must not itself stretch the
            # plot's auto-range - see jump_to()'s cursor line for the
            # identical reasoning (both are added the same way here).
            self.plot.addItem(self._crosshair_line, ignoreBounds=True)
        else:
            self._crosshair_line.setPos(view_t)
        self._crosshair_line.show()
        self.time_label.setText("t=%.3f s" % view_t)
        for key in self._channels:
            self._channels[key]["item"].setText(self._row_text(key))

    # -- per-channel scale/offset/normalize (feature 2) ---------------------

    def set_channel_transform(self, key: str, scale: float, offset: float,
                              normalize: bool = False) -> None:
        """The programmatic surface the scale/offset spinboxes and
        Normalize checkbox drive - also the direct entry point for
        tests. A display-only transform: refresh_plot() applies it
        (y' = (y - offset) * scale, or the normalize mapping) when
        building each curve's y array; the crosshair readout above
        always shows raw values regardless of this setting."""
        entry = self._channels.get(key)
        if entry is None:
            return
        entry["transform"] = {
            "scale": scale, "offset": offset, "normalize": normalize}

    def _on_channel_selected(self, current, _previous) -> None:
        if current is None:
            self.transform_strip.setVisible(False)
            return
        key = current.data(Qt.UserRole)
        entry = self._channels.get(key)
        if entry is None:
            self.transform_strip.setVisible(False)
            return
        transform = entry["transform"]
        spins = (self.scale_spin, self.offset_spin, self.normalize_check)
        for w in spins:
            w.blockSignals(True)
        self.scale_spin.setValue(transform["scale"])
        self.offset_spin.setValue(transform["offset"])
        self.normalize_check.setChecked(transform["normalize"])
        for w in spins:
            w.blockSignals(False)
        # setChecked() above was signal-blocked (it must not re-fire
        # _on_transform_edited and re-store the channel's own values
        # back at itself), so the enabled/disabled state it would
        # normally drive via toggled needs setting explicitly here too.
        self._set_scale_offset_enabled(transform["normalize"])
        self.transform_strip.setVisible(True)

    def _set_scale_offset_enabled(self, normalize_checked: bool) -> None:
        """Grey out scale/offset while Normalize is checked - they are
        ignored by _apply_transform() in that mode, so leaving them
        editable would be dishonest UI."""
        self.scale_spin.setEnabled(not normalize_checked)
        self.offset_spin.setEnabled(not normalize_checked)

    def _on_transform_edited(self, _value=None) -> None:
        item = self.channel_list.currentItem()
        if item is None:
            return
        key = item.data(Qt.UserRole)
        self.set_channel_transform(
            key, self.scale_spin.value(), self.offset_spin.value(),
            self.normalize_check.isChecked())

    # -- test-support accessors ---------------------------------------------

    def channel_sample_count(self, key: str) -> int:
        return len(self.engine.history.series(key))

    def curve_point_count(self, key: str) -> int:
        entry = self._channels.get(key)
        if entry is None:
            return 0
        xdata, _ydata = entry["curve"].getData()
        return 0 if xdata is None else len(xdata)

    def curve_y(self, key: str) -> List[float]:
        """The curve's currently plotted y data - post gap-NaN
        insertion and post display transform (scale/offset/
        normalize)."""
        entry = self._channels.get(key)
        if entry is None:
            return []
        _xdata, ydata = entry["curve"].getData()
        return [] if ydata is None else list(ydata)
