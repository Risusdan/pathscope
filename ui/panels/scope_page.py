"""Scope panel: M7 replaces polling (a History-fed channel table) with
a firmware trace-buffer scope. This task (task 7, "trace plumbing")
gets this page as far as: discover the trace target, show discovery/
error state, and render the channel table's fixed MAX_CH-row
skeleton. Channel occupancy (watching an address on a trace slot),
the watch-table protocol, and the plotted curves themselves are
Task 8's job - this page only gets the plumbing to that point working,
reviewable on its own.

Page states (spec point 1), all rendered inline in `error_label` -
never a dialog, the same convention as every other Inspector-family
page (register_page.py, memory_page.py):

  NO_SOURCE - neither `engine.trace_desc_addr` (demo mode) nor a
    loaded ELF's `ps_trace_desc` symbol exists yet. error_label reads
    NO_SOURCE_TEXT.
  TraceError - TraceReader.discover() raised (bad magic, unsupported
    version, bad geometry - core/trace/contract.py's ContractError,
    wrapped by reader.py) - error_label renders str(e) verbatim.
  READY - discover() succeeded: `self.desc` is set, error_label is
    clear, and `rate_label` shows the firmware's own sample rate,
    "%d Hz (firmware)" % round(1e6 / desc.period_us) - a property of
    the trace buffer's period_us, not a measurement of how fast this
    page happens to be redrawing.

Discovery (`self.reader`, a `core.trace.reader.TraceReader`) runs at
construction if `engine.trace_desc_addr` exists (demo mode - see
ui/demo.py), and again, at whatever address the loaded ELF's
`ps_trace_desc` symbol resolves to, every time `load_elf()` finds one
- a real target engine has no `trace_desc_addr` attribute at all, so
an ELF built with the ps_trace module linked in is its only path to
READY. A missing symbol after a real ELF load leaves the page in
NO_SOURCE rather than silently keeping whatever unrelated state was
already showing.

Not a port of anything in prototype/ui_proto.py - the prototype had no
scope view. New widget, following the same conventions as the other
Inspector-family pages: a plain QWidget, errors caught at the boundary
and rendered as text in the panel rather than a QMessageBox.

X axis: ROLL MODE, standard-scope style. Every sample plots at
sample_t - now, where now = time.monotonic() is captured once per
refresh_plot() call and cached as `self._last_now`. The newest data
always sits at x=0 (the view's right edge) and scrolls left as it
ages, and the viewport itself never slides: x auto-range is disabled
entirely (`self.plot.vb.enableAutoRange(x=False)`, set once in
__init__) and every refresh pins it to exactly
(-History.window_s, 0) via `setXRange(..., padding=0.01)` - the same
two numbers every tick, so the axis never re-labels and the "data
block marching across the view" artifact of an absolute time axis
cannot occur. Y auto-range stays on (`enableAutoRange(y=True)`),
adapting only to the currently-visible data. Event markers and the
jump cursor (see "Cursor sync" below) store their ABSOLUTE
time.monotonic t and are repositioned every refresh (t - now) so they
scroll left with their moment in history, exactly like the data
curves. `self._t0` (dock-open time, still captured at construction)
is no longer used for axis positioning - only `self._last_now` is;
it survives only as a convenience anchor for a few tests.

Gap honesty (spec 6.7): consecutive samples more than 3x the median
sample interval apart get a NaN inserted between them, and every curve
is drawn with `connect="finite"` so pyqtgraph breaks the line there
instead of drawing a lying straight segment across the gap.

Event markers: `add_event_marker(t, msg)` (fed by MainWindow from the
same update path that feeds the event log) adds a vertical
pg.InfiniteLine with a short label; markers older than the History
window are pruned on each refresh so the marker set never outlives the
data it annotates.

Test-support accessors `channel_sample_count(key)` (raw History sample
count - independent of whether refresh_plot() has run yet) and
`curve_point_count(key)` (plotted point count, post gap-NaN-insertion)
are part of this panel's produced interface.

Cursor sync (event-log-to-scope focus): `jump_to(t)` places (or moves)
a single cursor `pg.InfiniteLine` at t, styled distinctly from event
markers (dashed blue vs. solid red) so the two are never confused, and
flashes whichever marker in `_markers` is nearest to t so the user can
see which annotation the cursor landed on. `t` is an ABSOLUTE
time.monotonic value, stored as-is - `cursor_time()` returns exactly
this t back, never a relative one - and positioned the same way
`add_event_marker`'s own t is, `x = t - self._last_now`, repositioned
on every refresh_plot() tick the same way so it scrolls left with its
moment in history, exactly like a real scope annotation. Before the
cursor has ever been placed, `cursor_time()` returns None - fed by
MainWindow._on_log_time_focus, itself wired to EventLog's new
on_event_time callback (an event-log row click). This is purely
visual (spec point 5) - no PIN, no value-locking: an event-log click
only ever moves this line, and never touches the Value column or its
"Value"/"Value @ -X.Xs" header (see `_value_text_for`'s two-state
readout below), which stays governed solely by the mouse's own
on/off-plot state. To read the value at an event's moment: stop and
hover the cursor's x by eye.

ELF symbol picker: "Load ELF..." opens a
QFileDialog (the one dialog this panel uses - everything else is
inline, per the class-level convention above) and hands the chosen
path to `load_elf()`, which lazily imports ui.elf_symbols (same
import-on-first-use pattern as pyqtgraph at this module's top) and
populates `symbol_list` with every symbol name, filtered live by
`symbol_filter_edit`. load_elf() also checks the loaded symbol table
for `ps_trace_desc` (TRACE_DESC_SYMBOL below) and, if present, drives
discovery at that address - see "Discovery" above. Picking a symbol
and clicking "Add symbol" is currently a no-op
(`_on_add_symbol_clicked`) - wiring a selected symbol to an actual
trace watch channel is Task 8. load_symbols() reports every OBJECT
symbol regardless of size; symbol_list carries a tooltip noting that a
channel only ever reads one 32-bit word once Task 8 wires it up - this
is documented here rather than filtered at load time, per the brief.
"""
import struct
import time
from typing import Dict, List, Optional, Tuple

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtGui import QColor, QDoubleValidator, QFont
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QSplitter, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from core.engine.core import Engine
from core.trace.contract import MAX_CH
from core.trace.reader import TraceError, TraceReader

SYMBOL_LIST_TOOLTIP = (
    "Symbols from the loaded ELF. A channel added from one of these "
    "(Task 8) always reads the symbol's first 32-bit word, regardless "
    "of its declared size.")

# The ELF symbol this page auto-discovers the trace target at, once
# load_elf() finds it (see the module docstring's "Discovery"
# paragraph) - the ps_trace module names its descriptor global
# exactly this, so any firmware built with it exposes this symbol.
TRACE_DESC_SYMBOL = "ps_trace_desc"

# NO_SOURCE state text (spec point 1): neither engine.trace_desc_addr
# (demo mode) nor a loaded ELF's ps_trace_desc symbol exists yet.
NO_SOURCE_TEXT = "Load an ELF built with the ps_trace module to use the scope"

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
GAP_FACTOR = 3.0
MARKER_PEN = "#C62828"
CURSOR_PEN = "#1565C0"
CROSSHAIR_PEN = "#9E9E9E"
MARKER_FLASH_PEN = "#FFB300"
MARKER_FLASH_MS = 400
MARKER_HARD_CAP = 200
PLACEHOLDER_FG = "#9E9E9E"

# Channel table columns (spec point 2). The per-channel Hz column is
# retired with the old per-channel poll rate - the firmware period is
# now the one rate that matters, and it's shown once, in rate_label,
# rather than once per row.
COL_REMOVE, COL_SWATCH, COL_NAME, COL_ADDR, COL_TYPE, COL_VALUE, \
    COL_SCALE, COL_OFFSET = range(8)
COLUMN_LABELS = ["", "", "Name", "Address", "Type", "Value",
                 "Scale", "Offset"]

# Type decode set (spec point 3): display-side only, core untouched.
# Each spec is (bit width, signed?, shift-from-bit-0); f32 is handled
# separately (IEEE-754 reinterpretation of the raw 32-bit word).
TYPE_SPECS = {
    "u32":    {"width": 32, "signed": False, "shift": 0},
    "i32":    {"width": 32, "signed": True,  "shift": 0},
    "u16.lo": {"width": 16, "signed": False, "shift": 0},
    "u16.hi": {"width": 16, "signed": False, "shift": 16},
    "i16.lo": {"width": 16, "signed": True,  "shift": 0},
    "i16.hi": {"width": 16, "signed": True,  "shift": 16},
    "u8.0":   {"width": 8,  "signed": False, "shift": 0},
    "u8.1":   {"width": 8,  "signed": False, "shift": 8},
    "u8.2":   {"width": 8,  "signed": False, "shift": 16},
    "u8.3":   {"width": 8,  "signed": False, "shift": 24},
    "f32":    {"width": 32, "signed": None,  "shift": 0, "float": True},
}
TYPES = list(TYPE_SPECS)
DEFAULT_TYPE = "u32"

# Scale/offset cells (spec point 2): plain QLineEdit text fields, not
# spinboxes - accept scientific notation, Enter commits, an invalid
# entry reverts to the last-good value and flashes this background
# briefly so the user sees why nothing changed.
INVALID_EDIT_STYLE = "background-color: #FFCDD2;"
INVALID_EDIT_FLASH_MS = 400

CURVE_COLORS = [
    "#1976D2",  # blue
    "#B71C1C",  # red
    "#2E7D32",  # green
    "#EF6C00",  # orange
    "#6A1B9A",  # purple
    "#00838F",  # teal
    "#D81B60",  # pink
    "#5D4037",  # brown
    "#212121",  # near-black
    "#827717",  # olive
]
# Neither entry above may collide with MARKER_PEN ("#C62828", event
# markers) or CURSOR_PEN ("#1565C0", the jump_to() cursor line) -
# that's why red is #B71C1C rather than MARKER_PEN's own shade and
# blue stays #1976D2 rather than CURSOR_PEN's: a curve must never be
# the same color as either of the plot's other line kinds. Exactly
# MAX_CH entries - the channel table has one permanently-colored
# swatch row per trace slot, CURVE_COLORS[i] for row i.


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


def decode_value(raw: int, type_name: str):
    """Display-side type decode (spec point 3) of a raw 32-bit register
    sample - core is untouched, this is purely how the value is
    interpreted for the table's Value column and the plotted curve.
    f32 reinterprets the raw word's bit pattern as IEEE-754 (returns a
    float); every other type extracts a sub-field by shift/width and,
    if signed, applies two's-complement sign extension (returns an
    int)."""
    spec = TYPE_SPECS[type_name]
    if spec.get("float"):
        raw32 = raw & 0xFFFFFFFF
        return struct.unpack("<f", raw32.to_bytes(4, "little"))[0]
    width = spec["width"]
    field = (raw >> spec["shift"]) & ((1 << width) - 1)
    if spec["signed"] and field & (1 << (width - 1)):
        field -= 1 << width
    return field


def format_value(decoded, type_name: str) -> str:
    """The Value column's fixed dual-radix format (spec point 6):
    "55 (0x37)" for an integer type, sized to that type's own bit
    width (e.g. a u8 field shows 2 hex digits, not 8); f32 shows the
    float alone - a hex reading of a float's bits would not mean
    anything to the person reading it, so no radix pair is offered
    for that type. No user-facing radix toggle exists for either
    case."""
    spec = TYPE_SPECS[type_name]
    if spec.get("float"):
        return "%.6g" % decoded
    hex_digits = spec["width"] // 4
    raw_bits = decoded & ((1 << spec["width"]) - 1)
    return "%d (0x%0*X)" % (decoded, hex_digits, raw_bits)


def _default_type_for_size(size: int) -> str:
    """ELF preselect (spec point 3): choose a type from the symbol's
    declared byte size, unsigned default - a 1-byte symbol starts as
    u8.0, a 2-byte symbol as u16.lo, anything else (4 bytes, or larger
    - a channel only ever samples the first word regardless) as u32."""
    if size <= 1:
        return "u8.0"
    if size == 2:
        return "u16.lo"
    return "u32"


def _fmt_num(v: float) -> str:
    """Compact numeric text for a scale/offset cell - "%g" keeps
    scientific notation for very small/large magnitudes instead of a
    long fixed-point expansion."""
    return "%g" % v


def _fit_scale_offset(lo: float, hi: float, band_lo: float,
                      band_hi: float) -> Tuple[float, float]:
    """Auto-lane's core computation (spec point 4): the scale/offset
    pair such that a channel whose raw decoded window spans [lo, hi]
    displays inside [band_lo, band_hi] through the existing display
    transform y' = (y - offset) * scale - the same fields a manual
    edit could set by hand, just computed. A flat window (hi <= lo,
    including no data at all: lo == hi == 0.0) can't be mapped to a
    span without dividing by zero, so it centers on the band's
    midpoint instead - analogous to old Normalize's flat-series 0.5
    guard, generalized to an arbitrary band."""
    if hi <= lo:
        return 1.0, lo - (band_lo + band_hi) / 2.0
    scale = (band_hi - band_lo) / (hi - lo)
    offset = lo - band_lo / scale
    return scale, offset


def _apply_transform(ys: List[float], transform: dict) -> List[float]:
    """Display-only transform applied to an already-gapped y array
    (see _gapped_xy): y' = (y - offset) * scale. NaN gap placeholders
    pass through untouched, since a gap's midpoint carries no real
    sample to scale. Pure python lists throughout - no numpy
    dependency."""
    scale = transform["scale"]
    offset = transform["offset"]
    return [(v - offset) * scale if v == v else v for v in ys]


class ScopePage(QWidget):
    def __init__(self, engine: Engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        # This page's own independent run/stop flag (spec point 1) -
        # unrelated to MainWindow's Data Path stop flag.
        self._stopped = False
        # _t0 (dock-open time) no longer drives axis positioning - see
        # the module docstring's "X axis" paragraph for roll mode.
        # Kept only as a construction-time anchor a few tests use for
        # convenience; refresh_plot()/add_event_marker()/jump_to() all
        # key off _last_now instead.
        self._t0 = time.monotonic()
        self._last_now = self._t0
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
        # Y axis follows the selected row (spec point 7) - None means
        # no selection, which hides the axis's tick numbers entirely
        # rather than showing a meaningless shared scale.
        self._selected_key: Optional[str] = None
        # Auto-lane (spec point 4): while on, every refresh_plot()
        # tick stacks ALL channels into equal horizontal bands (one
        # lane each, in add order - see _apply_auto_lane), writing
        # the computed scale/offset into the editable cells; while
        # off, the cells are whatever was last computed or typed.
        self._auto_lane = False
        # Trace discovery (spec point 1/2 of the M7 rework): the
        # reader is always constructed, even in NO_SOURCE - discover()
        # only ever runs once an address is known, at the bottom of
        # this method (demo mode) or from load_elf() (a real ELF's
        # ps_trace_desc symbol). self.desc is None until a discover()
        # call succeeds.
        self.reader = TraceReader(engine)
        self.desc = None  # type: Optional[object]

        # Top-bottom layout (a T5 hardware finding replacing the
        # original left-right split): the channel table needs the
        # window's full width to show its columns comfortably, so
        # the controls block sits ON TOP of the plot, the two joined
        # by a draggable vertical splitter. The page's first row is
        # the time label + big Run/Stop - keeping Run/Stop at the
        # page's top-right, the same spot as the Data Path page's.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)

        # ONE header row for the whole page: time readout, inline
        # error surface, the firmware rate readout, Auto-lane, and the
        # big Run/Stop, which keeps the page's top-right - same spot
        # as the Data Path page's.
        top_row = QHBoxLayout()
        self.time_label = QLabel("t-now = -- s")
        top_row.addWidget(self.time_label)
        # Inline error surface (never a dialog, per the class-level
        # convention) - lives on the always-visible top row so an
        # error (or the NO_SOURCE state) can never be hidden by the
        # splitter. Doubles as this page's page-state indicator (spec
        # point 1): NO_SOURCE / TraceError text lives here, cleared
        # once READY.
        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #C62828;")
        top_row.addWidget(self.error_label, 1)
        # Firmware sample rate (spec point: "Rate header") - a
        # property of the trace buffer's own period_us, set once
        # discovery succeeds; blank in NO_SOURCE/TraceError.
        self.rate_label = QLabel("")
        top_row.addWidget(self.rate_label)
        self.auto_lane_check = QCheckBox("Auto-lane")
        self.auto_lane_check.toggled.connect(self._on_auto_lane_toggled)
        top_row.addWidget(self.auto_lane_check)
        # The scope page's own big Run/Stop button (spec point 1),
        # this page's only run/stop control besides spacebar
        # (keyPressEvent below) - checkable so its own pressed-look
        # tracks state too.
        self.run_stop_btn = QPushButton("Stop")
        self.run_stop_btn.setCheckable(True)
        self.run_stop_btn.setMinimumHeight(36)
        self.run_stop_btn.setMinimumWidth(100)
        self.run_stop_btn.clicked.connect(self._on_run_stop_clicked)
        top_row.addWidget(self.run_stop_btn)
        outer.addLayout(top_row)

        side = QVBoxLayout()
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(3)

        # The channel table is this panel's hero (spec point 2): ALWAYS
        # exactly MAX_CH rows, row i permanently representing trace
        # slot i (see _build_channel_table_skeleton below) - occupying
        # a slot (Task 8) rewrites an existing row's cells in place,
        # this page never inserts or removes a row again after this
        # constructor runs.
        self.channel_table = QTableWidget(MAX_CH, len(COLUMN_LABELS))
        self.channel_table.setHorizontalHeaderLabels(COLUMN_LABELS)
        self.channel_table.verticalHeader().setVisible(False)
        self.channel_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.channel_table.setSelectionMode(QTableWidget.SingleSelection)
        self.channel_table.setEditTriggers(
            QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed)
        # No Stretch column (a T5 finding: Name in Stretch mode ate
        # the whole window width and squeezed Value/Scale/Offset into
        # truncation) - every column gets a fixed sensible width and
        # the leftover space simply stays blank on the right.
        self.channel_table.setColumnWidth(COL_REMOVE, 28)
        self.channel_table.setColumnWidth(COL_SWATCH, 18)
        self.channel_table.setColumnWidth(COL_NAME, 220)
        self.channel_table.setColumnWidth(COL_ADDR, 100)
        self.channel_table.setColumnWidth(COL_TYPE, 70)
        self.channel_table.setColumnWidth(COL_VALUE, 170)
        self.channel_table.setColumnWidth(COL_SCALE, 90)
        self.channel_table.setColumnWidth(COL_OFFSET, 90)
        self.channel_table.verticalHeader().setDefaultSectionSize(26)
        self.channel_table.itemChanged.connect(self._on_item_changed)
        self.channel_table.itemSelectionChanged.connect(
            self._on_table_selection_changed)
        # The table scrolls its own rows internally, so all MAX_CH
        # rows never push the add-controls below out of sight; the
        # minimum keeps a couple of rows visible even with the
        # splitter dragged up.
        self.channel_table.setMinimumHeight(60)
        self._build_channel_table_skeleton()
        side.addWidget(self.channel_table, 1)

        # Add-channel area: the ELF group is now the ONLY source
        # control (spec point: register/address rows are gone) - Load
        # ELF plus the collapsible symbol picker.
        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        load_elf_btn = QPushButton("Load ELF...")
        load_elf_btn.clicked.connect(self._on_load_elf_clicked)
        add_row.addWidget(load_elf_btn)
        # Collapsible symbol picker (spec point 8): collapsed by
        # default so the add row doesn't cost vertical space for a
        # symbol list nobody has loaded yet - auto-expands the first
        # time load_elf() actually populates one.
        self.elf_toggle_btn = QPushButton("> ELF symbols")
        self.elf_toggle_btn.setCheckable(True)
        self.elf_toggle_btn.clicked.connect(self._on_elf_toggle_clicked)
        add_row.addWidget(self.elf_toggle_btn)
        add_row.addStretch(1)
        side.addLayout(add_row)

        elf_content_layout = QVBoxLayout()
        elf_content_layout.setContentsMargins(0, 0, 0, 0)
        self.symbol_filter_edit = QLineEdit()
        self.symbol_filter_edit.setPlaceholderText("filter symbols")
        self.symbol_filter_edit.textChanged.connect(
            self._refresh_symbol_list)
        elf_content_layout.addWidget(self.symbol_filter_edit)

        self.symbol_list = QListWidget()
        self.symbol_list.setToolTip(SYMBOL_LIST_TOOLTIP)
        self.symbol_list.setMaximumHeight(120)
        elf_content_layout.addWidget(self.symbol_list)

        add_symbol_btn = QPushButton("Add symbol")
        add_symbol_btn.setToolTip(SYMBOL_LIST_TOOLTIP)
        add_symbol_btn.clicked.connect(self._on_add_symbol_clicked)
        elf_content_layout.addWidget(add_symbol_btn)

        self.elf_content = QWidget()
        self.elf_content.setLayout(elf_content_layout)
        self.elf_content.setVisible(False)
        side.addWidget(self.elf_content)

        # No outer scroll area (a T5 finding: the controls block
        # inside one let the splitter squeeze the add rows out of
        # sight behind a subtle scrollbar - "where did Load ELF
        # go?"). The block's only variable-height child is the
        # channel table, and a QTableWidget scrolls its own rows, so
        # the controls' minimum height is bounded and the splitter
        # can never hide the add rows.
        self.side_widget = QWidget()
        self.side_widget.setLayout(side)

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.plot_widget.setLabel("bottom", "t - now", units="s")
        self.plot = self.plot_widget.getPlotItem()
        self.plot.addLegend()
        # Roll mode (standard-scope style): x auto-range is disabled
        # entirely - refresh_plot() pins the viewport to a fixed
        # (-window_s, 0) every tick instead, so the axis never
        # re-labels and the view never "marches" as data ages. Y
        # auto-range stays on, adapting only to the currently-visible
        # data.
        self.plot.vb.enableAutoRange(x=False, y=True)

        # Controls over plot, user-draggable split; the plot pane gets
        # every extra pixel when the window grows.
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(self.side_widget)
        self.splitter.addWidget(self.plot_widget)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([150, 570])
        outer.addWidget(self.splitter, 1)

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
        # Belt-and-braces "mouse off plot" detection (spec point 5):
        # sigMouseMoved only fires on an in-scene move, so a mouse
        # that exits plot_widget without one last move inside the
        # scene needs this Leave event instead - see eventFilter().
        self.plot_widget.installEventFilter(self)

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh_plot)
        self._timer.start()

        # No selection yet - hide the y-axis tick numbers (spec point
        # 7) rather than show a shared scale that means nothing until
        # a channel is picked.
        self._update_y_axis()

        # Discovery (module docstring's "Discovery" paragraph): demo
        # mode exposes engine.trace_desc_addr (ui/demo.py); a real
        # engine has no such attribute at all, so it starts in
        # NO_SOURCE until load_elf() finds a ps_trace_desc symbol.
        if hasattr(engine, "trace_desc_addr"):
            self._discover_at(engine.trace_desc_addr)
        else:
            self.error_label.setText(NO_SOURCE_TEXT)

    # -- run/stop (spec point 1) ---------------------------------------------

    def set_stopped(self, on: bool) -> None:
        """This page's own independent stop flag (spec point 1: "Scope
        stop = waveform hold" - unlike the old global Freeze, this has
        no effect on the Data Path tab or vice versa). Pauses (or
        resumes) the 200 ms repaint timer; refresh_plot() itself stays
        callable regardless (tests call it directly). Resuming only
        starts the timer if the page is currently visible - resuming
        while the dock is hidden (tabbed away) must not wake a timer
        that hideEvent() below deliberately paused; showEvent()
        re-checks self._stopped when the dock is next shown, so the
        timer still resumes correctly at that point. Syncs the big
        button's own label/checked state either way this was
        triggered - this method, the big button, or spacebar."""
        self._stopped = on
        if on:
            self._timer.stop()
        elif self.isVisible():
            self._timer.start()
        self._update_run_stop_button()

    def is_stopped(self) -> bool:
        return self._stopped

    def _update_run_stop_button(self) -> None:
        self.run_stop_btn.setText("Run" if self._stopped else "Stop")
        self.run_stop_btn.setChecked(self._stopped)

    def _on_run_stop_clicked(self) -> None:
        self.set_stopped(not self._stopped)

    def keyPressEvent(self, event) -> None:
        """Spacebar toggles run/stop, but only while this page (or a
        non-text-input descendant of it) actually has keyboard focus -
        "scope-focus only" (spec point 1), not a global app-wide
        shortcut. A focused QLineEdit/QTableWidget cell editor
        consumes Space as ordinary text input before it ever reaches
        here, which is exactly the exclusion the spec calls for -
        nothing extra to intercept for that case."""
        if event.key() == Qt.Key_Space:
            self._on_run_stop_clicked()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event) -> None:
        """Stop the repaint timer while the page isn't shown - tabbed
        away behind Event log, same bug class as memory_page.py's
        auto-refresh: an invisible ScopePage has no reason to keep
        redrawing a plot nobody sees every 200 ms forever."""
        self._timer.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        """Resume the timer on redisplay, unless the user has stopped
        this page - set_stopped(True) wins over a plain show/hide
        cycle, mirroring memory_page.py's auto_check.isChecked() guard
        (there, the user's own preference; here, this page's own stop
        state)."""
        if not self._stopped:
            self._timer.start()
        super().showEvent(event)

    # -- trace discovery & page state (spec point 1) -------------------------

    def _discover_at(self, addr: int) -> bool:
        """Attempt trace discovery at addr and update this page's
        state: READY (self.desc set, rate_label shows the firmware's
        own period, error_label cleared) on success, or the TraceError
        state (str(e) rendered verbatim in error_label, self.desc left
        None) on failure - covers bad magic, unsupported version, and
        bad geometry alike, since TraceReader.discover()/parse_desc()
        report all three as one TraceError. Returns whether discovery
        succeeded, for callers (load_elf(), __init__) that branch on
        it."""
        try:
            self.desc = self.reader.discover(addr)
        except TraceError as e:
            self.desc = None
            self.rate_label.setText("")
            self.error_label.setText(str(e))
            return False
        self.rate_label.setText(
            "%d Hz (firmware)" % round(1e6 / self.desc.period_us))
        self.error_label.setText("")
        return True

    def rate_label_text(self) -> str:
        """Test-support accessor for the firmware rate readout (spec
        point: "Rate header") - "" before READY."""
        return self.rate_label.text()

    # -- channel table skeleton (spec point 2) --------------------------------

    def _build_channel_table_skeleton(self) -> None:
        """The table ALWAYS has exactly MAX_CH rows, row i permanently
        representing trace slot i - built once, here, and never grown
        or shrunk by insertRow/removeRow again (occupying a slot in
        Task 8 rewrites an existing row's cells in place). Every row
        starts as an empty-slot placeholder; CURVE_COLORS[i] binds to
        row i's swatch regardless of occupancy, so a slot's eventual
        curve color is visible even before anything watches it."""
        for row in range(MAX_CH):
            self._build_placeholder_row(row)

    def _build_placeholder_row(self, row: int) -> None:
        swatch = QLabel()
        swatch.setFixedSize(12, 12)
        swatch.setStyleSheet(
            "background-color: %s; border-radius: 2px;"
            % CURVE_COLORS[row])
        swatch_box = QWidget()
        swatch_layout = QHBoxLayout(swatch_box)
        swatch_layout.setContentsMargins(0, 0, 0, 0)
        swatch_layout.setAlignment(Qt.AlignCenter)
        swatch_layout.addWidget(swatch)
        self.channel_table.setCellWidget(row, COL_SWATCH, swatch_box)
        # COL_REMOVE deliberately carries no widget here - the "-"
        # remove button is only ever created for an occupied slot
        # (Task 8); every slot is empty in this task.

        self.channel_table.setItem(row, COL_NAME, self._placeholder_item("-"))
        for col in (COL_ADDR, COL_TYPE, COL_VALUE, COL_SCALE, COL_OFFSET):
            self.channel_table.setItem(row, col, self._placeholder_item(""))

    @staticmethod
    def _placeholder_item(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        item.setForeground(QColor(PLACEHOLDER_FG))
        return item

    # -- inline table editing (spec point 2) --------------------------------

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """Handles a committed edit of the Name cell (double-click to
        rename) - the table's other QTableWidgetItem-backed columns
        (Address, Type, Value, Scale, Offset) are marked non-editable
        but still route through this same signal on every
        programmatic setText(), hence the early-out on column."""
        if item.column() != COL_NAME:
            return
        key = item.data(Qt.UserRole)
        entry = self._channels.get(key)
        if entry is None:
            return
        new_label = item.text().strip()
        if not new_label:
            item.setText(entry["label"])
            return
        if new_label == entry["label"]:
            return
        entry["label"] = new_label
        entry["curve"].opts["name"] = new_label
        legend_label = self.plot.legend.getLabel(entry["curve"])
        if legend_label is not None:
            legend_label.setText(new_label)
        if key == self._selected_key:
            # the y-axis title (spec point 7) is this channel's name -
            # keep it in sync with a live rename.
            self._update_y_axis()

    def _on_type_changed(self, key: str, type_name: str) -> None:
        entry = self._channels.get(key)
        if entry is None:
            return
        entry["type"] = type_name
        self.refresh_plot()

    def _make_number_edit(self, key: str, field: str,
                          initial: float) -> QLineEdit:
        """A scale/offset cell (spec point 2): a plain text field, not
        a spinbox - a QDoubleValidator in scientific-notation mode
        keeps out non-numeric keystrokes, Enter commits via
        _commit_number_edit, and losing focus with an uncommitted edit
        simply leaves the field as typed (no silent commit or
        silent-revert-on-blur - only Enter commits, per spec)."""
        edit = QLineEdit(_fmt_num(initial))
        validator = QDoubleValidator()
        validator.setNotation(QDoubleValidator.ScientificNotation)
        edit.setValidator(validator)
        edit.returnPressed.connect(
            lambda k=key, f=field, e=edit: self._commit_number_edit(k, f, e))
        return edit

    def _commit_number_edit(self, key: str, field: str,
                            edit: QLineEdit) -> None:
        entry = self._channels.get(key)
        if entry is None:
            return
        text = edit.text().strip()
        try:
            value = float(text)
        except ValueError:
            self._flash_invalid(edit)
            edit.setText(_fmt_num(entry["transform"][field]))
            return
        entry["transform"][field] = value
        edit.setText(_fmt_num(value))

    def _flash_invalid(self, edit: QLineEdit) -> None:
        edit.setStyleSheet(INVALID_EDIT_STYLE)
        QTimer.singleShot(INVALID_EDIT_FLASH_MS, lambda: edit.setStyleSheet(""))

    # -- Auto-lane (spec point 4) --------------------------------------------

    def _on_auto_lane_toggled(self, on: bool) -> None:
        """While on, every refresh_plot() tick restacks every channel
        into its own lane (see _apply_auto_lane) - turning it off
        just stops the recompute, leaving whatever scale/offset was
        last computed in the (still-editable) fields for
        hand-tuning."""
        self._auto_lane = on
        if on:
            self.refresh_plot()

    def _apply_auto_lane(self) -> None:
        """Stack ALL channels into equal horizontal bands of the
        [0, 1] view (one lane per channel, in add order), mapping
        each channel's current-window min..max into its own band.
        Reads self._last_series (this tick's already-fetched samples
        - refresh_plot() runs this before decoding/plotting, see its
        own comment) rather than calling engine.history.series()
        again."""
        keys = list(self._channels)
        n = len(keys)
        for i, key in enumerate(keys):
            band_lo, band_hi = i / n, (i + 1) / n
            lo, hi = self._window_min_max(key)
            scale, offset = _fit_scale_offset(lo, hi, band_lo, band_hi)
            self.set_channel_transform(key, scale, offset)

    def _window_min_max(self, key: str) -> Tuple[float, float]:
        """The raw-decoded (spec point 4: "readouts always decoded raw
        domain" - Auto-lane fits the same domain, pre scale/offset)
        min/max of a channel's currently cached window series. (0.0,
        0.0) - a flat window, per _fit_scale_offset's guard - for a
        channel with no samples yet."""
        entry = self._channels[key]
        series = self._last_series.get(key, [])
        if not series:
            return 0.0, 0.0
        decoded = [decode_value(v, entry["type"]) for _t, v in series]
        return float(min(decoded)), float(max(decoded))

    # -- y axis follows the selected channel (spec point 7) ------------------

    def _on_table_selection_changed(self) -> None:
        # selectionModel().selectedRows(), not currentRow(): Qt keeps
        # a "current" cell independent of the actual selection (e.g.
        # clearSelection() alone does not move it), so currentRow()
        # can still report a stale row after the selection is cleared.
        rows = self.channel_table.selectionModel().selectedRows()
        if not rows:
            self._selected_key = None
        else:
            item = self.channel_table.item(rows[0].row(), COL_NAME)
            self._selected_key = item.data(Qt.UserRole) \
                if item is not None else None
        self._update_y_axis()

    def _update_y_axis(self) -> None:
        """No selection -> hide the tick numbers entirely (a shared
        y-axis scale means nothing until one channel's own domain is
        picked); a selection -> the axis title becomes that channel's
        name in its own curve color, and the tick numbers are that
        channel's raw decoded domain (the inverse of its scale/offset
        transform), not the 0..1-ish display range the curve itself is
        drawn in."""
        axis = self.plot.getAxis("left")
        entry = self._channels.get(self._selected_key) \
            if self._selected_key else None
        if entry is None:
            axis.setStyle(showValues=False)
            self.plot.setLabel("left", "")
            return
        axis.setStyle(showValues=True)
        self.plot.setLabel(
            "left", '<span style="color:%s">%s</span>'
            % (entry["color"], entry["label"]))
        axis.tickStrings = self._make_tick_strings(self._selected_key)

    def _make_tick_strings(self, key: str):
        """A pyqtgraph AxisItem.tickStrings override bound to one
        channel by key (not by a captured transform dict, which
        set_channel_transform replaces wholesale rather than mutating
        - looking the channel back up by key on every call always
        sees its current transform, however it last changed)."""
        def tick_strings(values, _scale, _spacing):
            entry = self._channels.get(key)
            if entry is None:
                return ["" for _ in values]
            transform = entry["transform"]
            scale = transform["scale"] or 1.0
            offset = transform["offset"]
            return [_fmt_num(v / scale + offset) for v in values]
        return tick_strings

    # -- ELF symbol picker -------------------------------------------------

    def load_elf(self, path: str) -> None:
        """Load every OBJECT symbol from path's ELF (via
        ui.elf_symbols.load_symbols, imported lazily here - same
        import-on-first-use pattern as pyqtgraph at this module's top,
        just deferred one step further since not every scope session
        loads an ELF at all) into `elf_symbols` and repopulate
        `symbol_list`. Any failure (bad path, unparsable ELF) is
        rendered in `error_label` - never a dialog; the QFileDialog in
        _on_load_elf_clicked is this panel's one and only dialog. On
        success, auto-expands the collapsible symbol picker (spec
        point 8) so the newly loaded list is immediately visible, then
        looks for TRACE_DESC_SYMBOL ("ps_trace_desc") in the loaded
        table and, if present, drives discovery at its address (module
        docstring's "Discovery" paragraph) - overriding whatever
        state was showing before. A missing symbol leaves the NO_SOURCE
        text showing unless a trace target is already discovered from
        elsewhere (e.g. demo mode's engine.trace_desc_addr)."""
        try:
            from ui.elf_symbols import load_symbols
            symbols = load_symbols(path)
        except Exception as e:
            self.error_label.setText("ELF load failed: %s" % e)
            return
        self.elf_symbols = {s.name: s for s in symbols}
        self.symbol_filter_edit.clear()
        self._refresh_symbol_list()
        self._set_elf_expanded(True)
        desc_symbol = self.elf_symbols.get(TRACE_DESC_SYMBOL)
        if desc_symbol is not None:
            self._discover_at(desc_symbol.addr)
        elif self.desc is None:
            self.error_label.setText(NO_SOURCE_TEXT)

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
        # Wiring a selected ELF symbol to an actual trace watch
        # channel is Task 8 (channels/protocol/plot wiring) - this
        # task only discovers the trace target and renders the fixed
        # slot table skeleton, so the button is inert for now.
        pass

    def _on_elf_toggle_clicked(self) -> None:
        self._set_elf_expanded(self.elf_toggle_btn.isChecked())

    def _set_elf_expanded(self, expanded: bool) -> None:
        self.elf_content.setVisible(expanded)
        self.elf_toggle_btn.setChecked(expanded)
        self.elf_toggle_btn.setText(
            "v ELF symbols" if expanded else "> ELF symbols")

    # -- event markers ---------------------------------------------------

    def add_event_marker(self, t: float, msg: str) -> None:
        # Roll mode: positioned in the same reference frame the
        # currently-painted curves use (self._last_now, set by the
        # most recent refresh_plot()) rather than a fresh
        # time.monotonic() call - _prune_markers() below repositions
        # every marker, this one included, on the very next refresh
        # tick regardless, so this is only the placement seen for the
        # brief window until that tick.
        x = t - self._last_now
        short = msg if len(msg) <= 24 else msg[:21] + "..."
        line = pg.InfiniteLine(
            pos=x, angle=90, pen=pg.mkPen(color=MARKER_PEN, width=1),
            label=short,
            labelOpts={"color": MARKER_PEN, "position": 0.95})
        self.plot.addItem(line)
        self._markers.append((t, line))
        # cap guards hidden/stopped accumulation; window pruning
        # (_prune_markers, run from refresh_plot) remains the primary
        # mechanism.
        while len(self._markers) > MARKER_HARD_CAP:
            _oldest_t, oldest_line = self._markers.pop(0)
            self.plot.removeItem(oldest_line)

    def _prune_markers(self, now: float) -> None:
        """Drop markers older than the History window, and - roll
        mode - reposition every surviving one to t - now so it scrolls
        left with its moment in history, exactly like the data curves
        (markers store the ABSOLUTE t they were given; only their
        on-screen x is relative-to-now)."""
        window = self.engine.history.window_s
        kept = []
        for t, line in self._markers:
            if now - t > window:
                self.plot.removeItem(line)
            else:
                line.setPos(t - now)
                kept.append((t, line))
        self._markers = kept

    # -- cursor sync (event-log click -> scope cursor, task 4) -------------

    def jump_to(self, t: float) -> None:
        """Place (or move) the scope cursor at t and flash the nearest
        event marker. t is an ABSOLUTE time.monotonic value, stored
        as-is (cursor_time() returns exactly this t back) - positioned
        the same way add_event_marker positions a marker, x = t -
        self._last_now, and repositioned every refresh_plot() tick
        the same way (t - now) so it scrolls left with its moment in
        history, exactly like a real scope annotation. The cursor's
        pen (dashed blue) is deliberately distinct from a marker's
        (solid red) so the two are never confused on the plot."""
        self._cursor_t = t
        x = t - self._last_now
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
        now = time.monotonic()
        self._last_now = now
        # series first, transforms second: Auto-lane (spec point 4)
        # needs every channel's freshly-fetched series (via
        # _window_min_max, which reads self._last_series) to compute
        # this tick's scale/offset before the second loop applies it.
        for key in self._channels:
            self._last_series[key] = self.engine.history.series(key)
        if self._auto_lane:
            self._apply_auto_lane()
        for key, entry in self._channels.items():
            series = self._last_series[key]
            # decode display-side (spec point 3) before the gap-NaN
            # pass and the scale/offset display transform - the raw
            # ints stay in self._last_series for the Value column's
            # own (unscaled) readout below.
            decoded_series = [(t, decode_value(v, entry["type"]))
                              for t, v in series]
            x, y = _gapped_xy(decoded_series, now)
            y = _apply_transform(y, entry["transform"])
            entry["curve"].setData(x, y, connect="finite")
            entry["value_item"].setText(self._value_text_for(key))
        self._prune_markers(now)
        cursor_t = self._cursor_t
        if self._cursor_line is not None and cursor_t is not None:
            self._cursor_line.setPos(cursor_t - now)
        # Roll mode: the viewport is pinned to a fixed window every
        # tick rather than left to auto-range - x auto-range was
        # disabled once, in __init__, so this setXRange is the only
        # thing moving the x axis at all, and it moves the SAME two
        # numbers every time (the axis never re-labels).
        self.plot.setXRange(-self.engine.history.window_s, 0, padding=0.01)

    # -- crosshair / Value column readout (feature 1, spec point 6) ---------

    def _value_text_for(self, key: str) -> str:
        """The Value column's text for one channel - two states, no
        PIN, no third "locked" state (spec point 5): mouse off the
        plot (self._crosshair_t is None) shows the newest sample;
        mouse on the plot shows the value at the crosshair's time.
        Either way the reading is the type-decoded, dual-radix value
        (spec point 6), RAW (decode()'d, but never scale/offset'd -
        "Readouts always decoded raw domain", spec point 4) rather
        than the scaled/fit display value on the curve, which is the
        whole point of this readout existing alongside the curve.
        jump_to()'s cursor line never reaches this method at all - an
        event-log click only moves that line, per spec point 5; to
        read the value at an event's moment, stop and hover."""
        entry = self._channels[key]
        series = self._last_series.get(key, [])
        if self._crosshair_t is None:
            raw = series[-1][1] if series else None
        else:
            # roll mode: the crosshair's x is relative to the last
            # refresh's now, not the obsolete dock-open self._t0 - see
            # the module docstring's "X axis" paragraph.
            raw_t = self._crosshair_t + self._last_now
            raw = value_at(series, raw_t)
        if raw is None:
            return "--"
        return format_value(decode_value(raw, entry["type"]), entry["type"])

    def _on_mouse_moved(self, evt) -> None:
        pos = evt[0]
        if not self.plot.sceneBoundingRect().contains(pos):
            # mouse left the plot viewport via a move that's still
            # technically within the scene - eventFilter's Leave
            # handler below is the other, more reliable path for this
            # (the mouse can also leave without a trailing move event
            # inside the scene at all).
            self._clear_crosshair()
            return
        view_point = self.plot.vb.mapSceneToView(pos)
        self._update_crosshair(view_point.x())

    def eventFilter(self, obj, event) -> bool:
        """Installed on plot_widget only, to catch the mouse leaving
        the plot widget entirely (spec point 5's "mouse off plot"
        state) - sigMouseMoved (the SignalProxy above) only fires on
        an actual move *within* the graphics scene, so a mouse that
        exits the widget without a trailing in-scene move never
        reaches _on_mouse_moved's own out-of-bounds check."""
        if obj is self.plot_widget and event.type() == QEvent.Type.Leave:
            self._clear_crosshair()
        return super().eventFilter(obj, event)

    def _clear_crosshair(self) -> None:
        """Two-state cursor readout (spec point 5): "mouse off plot" -
        hides the crosshair line, and the Value column reverts to
        showing each channel's newest sample (via _value_text_for,
        which already falls back to that when self._crosshair_t is
        None) under the plain "Value" header."""
        self._crosshair_t = None
        if self._crosshair_line is not None:
            self._crosshair_line.hide()
        self._refresh_value_column()

    def _update_crosshair(self, view_t: float) -> None:
        """Move the crosshair to view_t - plot-relative seconds, the
        same domain mapSceneToView's x is in (roll mode: sample_t -
        self._last_now, the last refresh's now; see the module
        docstring's "X axis" paragraph) - and refresh every channel
        row's Value cell plus the time label and Value column header
        (spec point 5's "mouse on plot" state: "Value @ -X.Xs").
        Reads only self._last_series, populated by the most recent
        refresh_plot(): no engine.history.series() call here, so a
        mouse-move event costs no History copy of its own."""
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
        self.time_label.setText("t-now = %.2f s" % view_t)
        self._refresh_value_column()

    def _refresh_value_column(self) -> None:
        """Rebuilds the Value cell text for every channel and the
        column header text, from the current two-state cursor
        (self._crosshair_t: None -> newest sample, "Value" header; set
        -> value at that plot-relative time, "Value @ -X.Xs" header) -
        the single place both _clear_crosshair and _update_crosshair
        delegate to so the two states can never drift apart."""
        if self._crosshair_t is None:
            header_text = "Value"
        else:
            header_text = "Value @ %.1fs" % self._crosshair_t
        header_item = self.channel_table.horizontalHeaderItem(COL_VALUE)
        if header_item is not None:
            header_item.setText(header_text)
        for key in self._channels:
            self._channels[key]["value_item"].setText(
                self._value_text_for(key))

    # -- per-channel scale/offset (feature 2) --------------------------------

    def set_channel_transform(self, key: str, scale: float,
                              offset: float) -> None:
        """The programmatic surface the table's scale/offset text
        fields drive - also the direct entry point for tests and for
        Auto-lane's own recompute (_apply_auto_lane). A display-only
        transform: refresh_plot() applies it (y' = (y - offset) *
        scale) when building each curve's y array; the Value column
        readout always shows the raw decoded value regardless of this
        setting. Also mirrors scale/offset into the row's own text
        fields so a programmatic change is visible inline rather than
        only affecting the plotted curve."""
        entry = self._channels.get(key)
        if entry is None:
            return
        entry["transform"] = {"scale": scale, "offset": offset}
        entry["scale_edit"].setText(_fmt_num(scale))
        entry["offset_edit"].setText(_fmt_num(offset))

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
        insertion and post display transform (scale/offset)."""
        entry = self._channels.get(key)
        if entry is None:
            return []
        _xdata, ydata = entry["curve"].getData()
        return [] if ydata is None else list(ydata)
