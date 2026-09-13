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
import struct
import time
from typing import Dict, List, Optional, Tuple

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtGui import QDoubleValidator, QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton,
                               QScrollArea, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

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

# Side panel width (also used by side_widget.setMaximumWidth() below).
# v2's channel table is the hero of this panel (spec point 2: 8
# columns - swatch/name/type/Value/Hz/scale/offset/Fit) and needs
# meaningfully more width than the old list+strip design's 260px to
# stay readable; the plot area still gets the rest of the window via
# outer's stretch factor.
SIDE_MAX_WIDTH = 520

# Channel table columns (spec point 2).
COL_SWATCH, COL_NAME, COL_TYPE, COL_VALUE, COL_HZ, COL_SCALE, \
    COL_OFFSET, COL_FIT = range(8)
COLUMN_LABELS = ["", "Name", "Type", "Value", "Hz", "Scale", "Offset", "Fit"]

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

# Bandwidth budget indicator: below this live sweep rate, the label
# calls out that the read count is the likely cause (spec: "R < 15.0
# and R > 0").
LOW_RATE_HZ = 15.0
BUDGET_ORANGE = "#E65100"
BUDGET_ORANGE_STYLE = "color: %s;" % BUDGET_ORANGE
BUDGET_TOOLTIP = ("high read count is lowering the sweep rate; prefer "
                  "contiguous addresses")

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
# the same color as either of the plot's other line kinds.


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
    sample to scale. Normalize (the old single-channel-only 0..1
    mapping) is gone (spec point 4) - Fit/Auto-lane replace it by
    computing this same scale/offset pair instead of a separate
    transform mode (see _fit_scale_offset). Pure python lists
    throughout - no numpy dependency."""
    scale = transform["scale"]
    offset = transform["offset"]
    return [(v - offset) * scale if v == v else v for v in ys]


class ScopePage(QWidget):
    def __init__(self, engine: Engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.frozen = False
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
        # live sweep rate last reported via set_sweep_rate(), or None
        # before MainWindow has ever fed one in - the budget label's
        # "no rate yet" state.
        self._sweep_rate: Optional[float] = None
        # Y axis follows the selected row (spec point 7) - None means
        # no selection, which hides the axis's tick numbers entirely
        # rather than showing a meaningless shared scale.
        self._selected_key: Optional[str] = None
        # Auto-lane (spec point 4): while on, every refresh_plot() tick
        # recomputes scale/offset for all channels from their per-row
        # Fit setting (see _apply_auto_lane) instead of leaving
        # whatever was last typed into the scale/offset cells.
        self._auto_lane = False

        outer = QHBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)

        side = QVBoxLayout()
        channels_header = QHBoxLayout()
        channels_header.addWidget(QLabel("Channels"))
        channels_header.addStretch(1)
        self.auto_lane_check = QCheckBox("Auto-lane")
        self.auto_lane_check.toggled.connect(self._on_auto_lane_toggled)
        channels_header.addWidget(self.auto_lane_check)
        side.addLayout(channels_header)

        # The channel table is this panel's hero (spec point 2):
        # replaces the old list+strip pair - name/type/value/rate and
        # the per-channel scale/offset that used to live in a
        # selection-driven side strip are now all inline, one row per
        # channel, always visible regardless of selection.
        self.channel_table = QTableWidget(0, len(COLUMN_LABELS))
        self.channel_table.setHorizontalHeaderLabels(COLUMN_LABELS)
        self.channel_table.verticalHeader().setVisible(False)
        self.channel_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.channel_table.setSelectionMode(QTableWidget.SingleSelection)
        self.channel_table.setEditTriggers(
            QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed)
        header = self.channel_table.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        self.channel_table.setColumnWidth(COL_SWATCH, 18)
        self.channel_table.setColumnWidth(COL_TYPE, 70)
        self.channel_table.setColumnWidth(COL_VALUE, 100)
        self.channel_table.setColumnWidth(COL_HZ, 48)
        self.channel_table.setColumnWidth(COL_SCALE, 64)
        self.channel_table.setColumnWidth(COL_OFFSET, 64)
        self.channel_table.setColumnWidth(COL_FIT, 44)
        self.channel_table.itemChanged.connect(self._on_item_changed)
        self.channel_table.itemSelectionChanged.connect(
            self._on_table_selection_changed)
        side.addWidget(self.channel_table, 1)

        # Removal is a channel-table operation, so it belongs directly
        # under the table - both for locality and for reachability: on
        # a short dock (see the QScrollArea wrap below), this keeps
        # "Remove channel" visible without scrolling even when the ELF
        # section further down is scrolled out of view.
        self.remove_btn = QPushButton("Remove channel")
        self.remove_btn.clicked.connect(self._on_remove_clicked)
        side.addWidget(self.remove_btn)

        # Add-channel area (spec point 8): compact, 3 rows - register,
        # address, and the ELF header row (Load + the collapsible
        # symbol-picker toggle below it).
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

        elf_header_row = QHBoxLayout()
        load_elf_btn = QPushButton("Load ELF...")
        load_elf_btn.clicked.connect(self._on_load_elf_clicked)
        elf_header_row.addWidget(load_elf_btn, 1)
        # Collapsible symbol picker (spec point 8): collapsed by
        # default so the compact 3-row add area doesn't cost vertical
        # space for a symbol list nobody has loaded yet - auto-expands
        # the first time load_elf() actually populates one.
        self.elf_toggle_btn = QPushButton("> ELF symbols")
        self.elf_toggle_btn.setCheckable(True)
        self.elf_toggle_btn.clicked.connect(self._on_elf_toggle_clicked)
        elf_header_row.addWidget(self.elf_toggle_btn, 1)
        side.addLayout(elf_header_row)

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

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #C62828;")
        self.error_label.setWordWrap(True)
        side.addWidget(self.error_label)

        self.window_label = QLabel(
            "window: %.0f s (History)" % engine.history.window_s)
        side.addWidget(self.window_label)

        # Bandwidth budget indicator (spec point 8: "budget label
        # bottom") - last widget in the side column.
        self.budget_label = QLabel("")
        side.addWidget(self.budget_label)

        side_widget = QWidget()
        side_widget.setLayout(side)

        # The side panel's content (channel table, add rows, ELF
        # section, Remove channel, window/budget labels) can exceed
        # the dock's height at typical sizes - without a scroll area,
        # the bottom controls are pushed off-screen with no way to
        # reach them (a hardware-session screenshot showed the panel
        # cut off at "Add symbol"). setWidgetResizable(True) lets
        # side_widget track the viewport's width (so its own child
        # layouts still fill it horizontally) while its height is free
        # to exceed the viewport and scroll.
        self.side_scroll = QScrollArea()
        self.side_scroll.setWidgetResizable(True)
        self.side_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff)
        self.side_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.side_scroll.setWidget(side_widget)
        self.side_scroll.setMaximumWidth(SIDE_MAX_WIDTH)
        outer.addWidget(self.side_scroll)

        plot_side = QVBoxLayout()
        self.time_label = QLabel("t-now = -- s")
        plot_side.addWidget(self.time_label)

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
        # Belt-and-braces "mouse off plot" detection (spec point 5):
        # sigMouseMoved only fires on an in-scene move, so a mouse
        # that exits plot_widget without one last move inside the
        # scene needs this Leave event instead - see eventFilter().
        self.plot_widget.installEventFilter(self)

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh_plot)
        self._timer.start()

        self._update_budget_label()
        # No selection yet - hide the y-axis tick numbers (spec point
        # 7) rather than show a shared scale that means nothing until
        # a channel is picked.
        self._update_y_axis()

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
        no-op. Always starts at the default type (u32) - a plain
        register/address add has no size hint to preselect from (that
        is the ELF path's job, see add_symbol_channel)."""
        if key in self._channels:
            return
        if label is None:
            label = self.engine.addr_watch_labels.get(key, key)
        self._add_channel_common(key, label, DEFAULT_TYPE)
        self._update_budget_label()

    def add_address_channel(self, addr: int, label: str,
                            type_name: Optional[str] = None) -> str:
        """Add a fixed-address channel via engine.add_addr_watch().
        Raises EngineError straight through (guarded/misaligned
        address) - the button handler below is what catches it and
        renders the message inline, never a dialog. type_name lets
        add_symbol_channel preselect a type from the ELF symbol's
        size (spec point 3); the manual address row always leaves it
        at the default (u32).

        add_addr_watch() is idempotent on addr but always re-sets the
        engine's label; re-adding an address that already has a
        channel here must follow suit rather than silently keeping
        the first label, so the displayed legend/table entry stays in
        sync with what the engine now reports for this key (the type
        of an existing channel is left alone on a re-add)."""
        key = self.engine.add_addr_watch(addr, label)
        if key in self._channels:
            self._relabel_channel(key, label)
        else:
            self._add_channel_common(key, label, type_name or DEFAULT_TYPE)
        self._update_budget_label()
        return key

    def _relabel_channel(self, key: str, label: str) -> None:
        entry = self._channels[key]
        entry["label"] = label
        entry["curve"].opts["name"] = label
        legend_label = self.plot.legend.getLabel(entry["curve"])
        if legend_label is not None:
            legend_label.setText(label)
        entry["name_item"].setText(label)
        if key == self._selected_key:
            self._update_y_axis()

    def remove_channel(self, key: str) -> None:
        entry = self._channels.pop(key, None)
        if entry is None:
            return
        self._last_series.pop(key, None)
        self.plot.removeItem(entry["curve"])
        row = self._row_for_key(key)
        if row is not None:
            self.channel_table.removeRow(row)
        if key.startswith("@"):
            # the scope itself asked for this address watch - clean it
            # up so the poller stops reading it once nothing displays
            # it any more.
            self.engine.remove_addr_watch(key)
        if self._selected_key == key:
            # belt-and-braces: removeRow() firing itemSelectionChanged
            # on its own already clears this in the common case, but
            # don't rely on exactly when/whether Qt does so - a stale
            # _selected_key would point the y axis at a channel that
            # no longer exists.
            self._selected_key = None
            self._update_y_axis()
        self._update_budget_label()

    def _row_for_key(self, key: str) -> Optional[int]:
        for row in range(self.channel_table.rowCount()):
            item = self.channel_table.item(row, COL_NAME)
            if item is not None and item.data(Qt.UserRole) == key:
                return row
        return None

    def _add_channel_common(self, key: str, label: str,
                            type_name: str) -> None:
        color = CURVE_COLORS[len(self._channels) % len(CURVE_COLORS)]
        curve = self.plot.plot([], [], pen=pg.mkPen(color=color, width=2),
                               name=label, connect="finite")

        row = self.channel_table.rowCount()
        self.channel_table.insertRow(row)

        swatch = QLabel()
        swatch.setFixedSize(12, 12)
        swatch.setStyleSheet(
            "background-color: %s; border-radius: 2px;" % color)
        swatch_box = QWidget()
        swatch_layout = QHBoxLayout(swatch_box)
        swatch_layout.setContentsMargins(0, 0, 0, 0)
        swatch_layout.setAlignment(Qt.AlignCenter)
        swatch_layout.addWidget(swatch)
        self.channel_table.setCellWidget(row, COL_SWATCH, swatch_box)

        name_item = QTableWidgetItem(label)
        name_item.setData(Qt.UserRole, key)
        self.channel_table.setItem(row, COL_NAME, name_item)

        type_combo = QComboBox()
        type_combo.addItems(TYPES)
        type_combo.setCurrentText(type_name)
        type_combo.currentTextChanged.connect(
            lambda text, k=key: self._on_type_changed(k, text))
        self.channel_table.setCellWidget(row, COL_TYPE, type_combo)

        value_item = QTableWidgetItem("--")
        value_item.setFlags(value_item.flags() & ~Qt.ItemIsEditable)
        self.channel_table.setItem(row, COL_VALUE, value_item)

        hz_item = QTableWidgetItem("0.0")
        hz_item.setFlags(hz_item.flags() & ~Qt.ItemIsEditable)
        self.channel_table.setItem(row, COL_HZ, hz_item)

        scale_edit = self._make_number_edit(key, "scale", 1.0)
        self.channel_table.setCellWidget(row, COL_SCALE, scale_edit)
        offset_edit = self._make_number_edit(key, "offset", 0.0)
        self.channel_table.setCellWidget(row, COL_OFFSET, offset_edit)

        fit_btn = QPushButton("Fill")
        fit_btn.clicked.connect(lambda _checked=False, k=key:
                                self._on_fit_clicked(k))
        self.channel_table.setCellWidget(row, COL_FIT, fit_btn)

        # transform: display-only scale/offset, identity by default;
        # rate: last-computed effective Hz. The widgets are kept on
        # the entry too so later methods (type change, transform
        # commit, fit toggle, removal) don't have to re-locate the row
        # by scanning the table every time.
        self._channels[key] = {
            "label": label, "curve": curve, "color": color,
            "type": type_name,
            "transform": {"scale": 1.0, "offset": 0.0},
            "fit": "fill",
            "rate": 0.0,
            "name_item": name_item, "value_item": value_item,
            "hz_item": hz_item, "type_combo": type_combo,
            "scale_edit": scale_edit, "offset_edit": offset_edit,
            "fit_btn": fit_btn,
        }

    # -- inline table editing (spec point 2) --------------------------------

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """Handles a committed edit of the Name cell (double-click to
        rename) - the table's other QTableWidgetItem-backed columns
        (Value, Hz) are marked non-editable but still route through
        this same signal on every programmatic setText() from
        refresh_plot(), hence the early-out on column."""
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

    def _on_fit_clicked(self, key: str) -> None:
        """Per-row Fit toggle (spec point 4): switches this channel
        between the two lane targets Auto-lane groups channels by -
        "fill" (this channel alone fills the whole view) or "own" (it
        gets one band of a multi-channel stack). The toggle only
        changes which group the channel belongs to; the actual
        scale/offset computation is Auto-lane's job - if Auto-lane is
        currently on, recompute immediately so the regrouping is
        visible right away rather than waiting on the next 200 ms
        tick."""
        entry = self._channels.get(key)
        if entry is None:
            return
        entry["fit"] = "own" if entry["fit"] == "fill" else "fill"
        entry["fit_btn"].setText("Own" if entry["fit"] == "own" else "Fill")
        if self._auto_lane:
            self._apply_auto_lane()
            self.refresh_plot()

    # -- Auto-lane (spec point 4) --------------------------------------------

    def _on_auto_lane_toggled(self, on: bool) -> None:
        """While on, every refresh_plot() tick recomputes scale/offset
        for every channel from its per-row Fit setting (see
        _apply_auto_lane) - turning it off just stops the recompute,
        leaving whatever scale/offset was last computed in the
        (still-editable) fields for hand-tuning."""
        self._auto_lane = on
        if on:
            self._apply_auto_lane()
            self.refresh_plot()

    def _apply_auto_lane(self) -> None:
        """Compute scale/offset for every channel from its stored Fit
        setting (spec point 4): "fill" channels each map their own
        current-window min..max to the full [0, 1] view; "own"
        channels split [0, 1] into as many equal bands as there are
        "own" channels and each maps its own min..max into just its
        band, in table order. Reads self._last_series (this tick's
        already-fetched samples - refresh_plot() runs this before
        decoding/plotting, see its own comment) rather than calling
        engine.history.series() again."""
        own_keys = [k for k, e in self._channels.items() if e["fit"] == "own"]
        fill_keys = [k for k, e in self._channels.items()
                    if e["fit"] == "fill"]
        for key in fill_keys:
            lo, hi = self._window_min_max(key)
            scale, offset = _fit_scale_offset(lo, hi, 0.0, 1.0)
            self.set_channel_transform(key, scale, offset)
        n = len(own_keys)
        for i, key in enumerate(own_keys):
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
        row = self.channel_table.currentRow()
        if row < 0:
            return
        item = self.channel_table.item(row, COL_NAME)
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
        _on_load_elf_clicked is this panel's one and only dialog. On
        success, auto-expands the collapsible symbol picker (spec
        point 8) so the newly loaded list is immediately visible."""
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
        self._set_elf_expanded(True)

    def add_symbol_channel(self, name: str) -> str:
        """Add a channel for a symbol already loaded by load_elf(), by
        its first word - a thin wrapper over add_address_channel (same
        synthetic key, same EngineError-through behavior), not a
        separate code path. Raises KeyError for an unknown name.
        Preselects a type from the symbol's declared size (spec point
        3: unsigned default)."""
        symbol = self.elf_symbols[name]
        return self.add_address_channel(
            symbol.addr, symbol.name, _default_type_for_size(symbol.size))

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
        # cap guards hidden/frozen accumulation; window pruning
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
        self._refresh_reg_combo()
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
            entry["rate"] = _effective_rate_hz(series, now)
            entry["hz_item"].setText("%.1f" % entry["rate"])
            entry["value_item"].setText(self._value_text_for(key))
        self._prune_markers(now)
        if self._cursor_line is not None:
            self._cursor_line.setPos(self._cursor_t - now)
        # Roll mode: the viewport is pinned to a fixed window every
        # tick rather than left to auto-range - x auto-range was
        # disabled once, in __init__, so this setXRange is the only
        # thing moving the x axis at all, and it moves the SAME two
        # numbers every time (the axis never re-labels).
        self.plot.setXRange(-self.engine.history.window_s, 0, padding=0.01)
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
