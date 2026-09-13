"""Scope panel: M7 replaces polling (a History-fed channel table) with
a firmware trace-buffer scope. Task 7 ("trace plumbing") got this page
as far as: discover the trace target, show discovery/error state, and
render the channel table's fixed MAX_CH-row skeleton. This task
(task 8, "channels, protocol, render") finishes the page: occupying a
slot (watching an address on a trace channel), the watch-table
protocol (TraceReader.set_watch), and the plotted curves themselves,
now driven by TraceStore rather than the old per-register History.

Page states (spec point 1), all rendered inline in `error_label` -
never a dialog, the same convention as every other Inspector-family
page (register_page.py, memory_page.py):

  NO_SOURCE - neither `engine.trace_desc_addr` (demo mode) nor a
    loaded ELF's `ps_trace_desc` symbol exists yet. error_label reads
    NO_SOURCE_TEXT.
  TraceError/EngineError - TraceReader.discover() raised, from either
    of two exception families - error_label renders str(e) verbatim
    either way, and self.desc stays None: TraceError (bad magic,
    unsupported version, bad geometry - core/trace/contract.py's
    ContractError, wrapped by reader.py) and EngineError (the poller
    thread isn't running yet, a command timed out, or the target was
    lost mid-discovery - Engine._exec's own failure mode; the
    real-world trigger is a user opening the Scope tab against
    unplugged/still-connecting hardware). Both are caught at the same
    boundary, `_discover_at()` below, so neither can ever escape
    ScopePage construction or load_elf() uncaught. A failed discovery
    also invalidates the reader's own desc/desc_addr (not just this
    page's self.desc) - see _discover_at's comment - so a channel add
    can never silently keep talking to a stale, no-longer-current
    target after a re-discovery attempt fails.
  READY - discover() succeeded: `self.desc` is set, error_label is
    clear, and `rate_label` shows the firmware's own sample rate,
    "%d Hz (firmware)" % round(1e6 / desc.period_us) - a property of
    the trace buffer's period_us, not a measurement of how fast this
    page happens to be redrawing. A successful (re-)discovery also
    (re)creates self.store (a ui.trace_store.TraceStore) and clears
    every channel slot - see _reset_channels - since a new/changed
    trace target invalidates whatever watch table was built against
    the previous one.

Discovery (`self.reader`, a `core.trace.reader.TraceReader`) runs at
construction if `engine.trace_desc_addr` exists (demo mode - see
ui/demo.py), and again, at whatever address the loaded ELF's
`ps_trace_desc` symbol resolves to, every time `load_elf()` finds one
- a real target engine has no `trace_desc_addr` attribute at all, so
an ELF built with the ps_trace module linked in is its only path to
READY. A missing symbol after a real ELF load leaves the page in
NO_SOURCE rather than silently keeping whatever unrelated state was
already showing.

Channel slots (spec point 2, this task): `self._slots` is a
List[Optional[dict]] of exactly MAX_CH entries, always kept COMPACTED
- every occupied entry sits in a contiguous prefix starting at index 0,
every trailing entry is None. Index i is simultaneously: the trace
watch-table index sent to TraceReader.set_watch (slot i's address is
list element i), the channel table's row i (CURVE_COLORS[i] is
permanently bound to row i's swatch, per Task 7), and TraceStore
column i (TraceStore.series(i)/.newest(i) read that same index). Three
different systems staying in lockstep through one shared integer is
the whole point of the compaction invariant: add_address_slot() always
occupies the first free slot (== the current occupied count, since the
prefix has no gaps), and remove_channel() always closes any gap it
opens by shifting every later entry up one slot (Python list
pop()+append(None) does exactly this) - so watch index, table row, and
record slot can never drift apart. The one visible cost: removing a
middle channel changes which CURVE_COLORS entry every LATER channel is
drawn in (rebound in remove_channel via _rebind_slot_colors) - a
channel's color is a property of its current row, not something that
follows it permanently. add_symbol_channel()/add_register_channel()
both resolve an address and hand it, with a label and a preselected
type, to add_address_slot() - the one primitive that actually touches
self._slots and the firmware watch table; ui/app.py's --shot-scope
flag drives add_address_slot() directly for the same reason a manual
address add would (the demo target has no register model to resolve a
reg_key against).

Refusals (spec point 3) happen BEFORE any hardware write, in this
order: a full table ("table full"), an address in
engine.guarded_addrs (the same guarded-address set register_page.py's
show_block() reads off Engine.load - "address 0x%08X is guarded"),
then 4-byte alignment ("address must be 4-byte aligned" - the
firmware's own whitelist check, core/trace/sim.py's _validate(), does
NOT check alignment, so the host has to). A refusal at any of these
three leaves self._slots completely unchanged. Only after all three
pass does add_address_slot() actually occupy the slot and call
reader.set_watch() - if THAT raises (TraceError: firmware rejected the
table, e.g. an address outside its whitelist; EngineError: the write
itself failed) the tentative slot is rolled back and the error renders
inline verbatim, exactly like the discovery states above. Every
refusal/error path calls error_label.setText() itself; every success
path clears it.

Rendering (spec point 4): the repaint tick drains the reader
(`self.reader.refresh()` -> `self.store.append()`) via `_drain_once()`,
then rebuilds each occupied slot's curve from `self.store.series(row)`
- a raw-domain (t, y) numpy pair covering the whole store window.
`_decode_series()` runs `decode_value()` over the finite (non-NaN)
elements of y (cast back to int first - TraceStore's f64 is an exact
integer-valued encoding of the wire's raw u32) and leaves NaN gap rows
(TraceStore's own seq/gen-exact break detection, not this module's
older statistical `_gapped_xy` - see below) untouched, so a curve drawn
with connect="finite" still breaks there. `_apply_transform()` then
applies the row's scale/offset. x is `t - t_latest`, t_latest being the
newest real sample time across every occupied slot this tick, so the
newest data always sits at x=0 (roll-mode, the same convention the
fixed viewport below already uses). Each curve gets
`setDownsampling(auto=True, method="peak")` and `setClipToView(True)`
once, at creation (_make_slot_entry) - pyqtgraph decimates the huge
in-memory series down to what the pixel width can actually show,
rather than this page doing that work itself.

`_gapped_xy`/`value_at` (below) are this panel's ORIGINAL (M6) gap/
lookup helpers - both are still directly unit-tested as pure functions
and `value_at` is still reused by the crosshair hover readout
(`_value_text_for`, against a NaN-filtered view of the cached
per-slot series), but neither channel curve any longer runs through
`_gapped_xy` itself: TraceStore already knows exactly (from seq/gen,
not a statistical median-interval guess) where a break happened, so
gap detection for channel data moved there in this task.

Drain independent of paint state (spec point 5): a SEPARATE QTimer
(`self._drain_timer`, DRAIN_MS = 500 ms) calls `_drain_once()` on its
own, and is started once discovery first succeeds and left running for
the page's lifetime - completely independent of Run/Stop
(`self._stopped`) and of page visibility (hideEvent/showEvent only
ever touch the FAST repaint timer, `self._timer`). The trace ring only
holds RING_COUNT (256) records; the fast timer's own `refresh_plot()`
also calls `_drain_once()` itself (so live drawing isn't limited to the
slow timer's 500 ms cadence while actually running/visible), and
TraceReader.refresh() is safe to call redundantly (it only ever asks
for records past what it has already delivered) - so the two timers
never need to coordinate, and a page that's stopped or tabbed away
still drains fast enough that reader.lost never grows just because
nobody's looking.

Drain health (spec point 6): `lost_label`, on the header row next to
`rate_label`, shows "lost N samples" when TraceReader.lost has grown
in roughly the last roll-mode window (`engine.history.window_s`) and
is blank otherwise - `_update_drain_health()`, called from every
`_drain_once()`.

X axis: ROLL MODE, standard-scope style, unchanged from Task 7/M6.
Every sample plots at sample_t - now, where now = time.monotonic() is
captured once per refresh_plot() call and cached as `self._last_now`
(channel curves instead use `self._last_t_latest`, the newest real
SAMPLE time across occupied slots - see "Rendering" above - which
tracks `self._last_now` closely but is not required to equal it
exactly). The newest data always sits at x=0 (the view's right edge)
and scrolls left as it ages, and the viewport itself never slides: x
auto-range is disabled entirely (`self.plot.vb.enableAutoRange(x=False)`,
set once in __init__) and every refresh pins it to exactly
(-History.window_s, 0) via `setXRange(..., padding=0.01)` - the same
two numbers every tick, so the axis never re-labels. Y auto-range stays
on (`enableAutoRange(y=True)`), adapting only to the currently-visible
data. Event markers and the jump cursor (see "Cursor sync" below)
store their ABSOLUTE time.monotonic t and are repositioned every
refresh (t - now) so they scroll left with their moment in history,
exactly like the data curves. `self._t0` (dock-open time, still
captured at construction) is no longer used for axis positioning -
only `self._last_now` is; it survives only as a convenience anchor for
a few tests.

Event markers: `add_event_marker(t, msg)` (fed by MainWindow from the
same update path that feeds the event log) adds a vertical
pg.InfiniteLine with a short label; markers older than the History
window are pruned on each refresh so the marker set never outlives the
data it annotates.

Test-support accessors `channel_sample_count(slot)` (raw TraceStore row
count for that slot's window, independent of whether refresh_plot()
has run - only _drain_once() has to have run) and `curve_point_count
(slot)` (plotted point count, post gap-NaN) are part of this panel's
produced interface, alongside `channel_slots()` (the raw self._slots
list) and `curve_y(slot)` (the currently plotted, post-transform y
data).

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
discovery at that address - see "Discovery" above. Picking a symbol and
clicking "Add symbol" calls `add_symbol_channel()` on the selected
list item's text. load_symbols() reports every OBJECT symbol
regardless of size; symbol_list carries a tooltip noting that a
channel only ever reads one 32-bit word once wired up - this is
documented here rather than filtered at load time, per the brief.
"""
import struct
import time
from typing import Dict, List, Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtGui import QColor, QDoubleValidator, QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QSplitter,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from core.engine.core import Engine, EngineError
from core.engine.poller import PollerState
from core.target.registers import SvdError
from core.trace.contract import MAX_CH
from core.trace.reader import TraceError, TraceReader
from ui.trace_store import TraceStore

SYMBOL_LIST_TOOLTIP = (
    "Symbols from the loaded ELF. A channel added from one of these "
    "always reads the symbol's first 32-bit word, regardless of its "
    "declared size.")

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
DRAIN_MS = 500
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
# swatch row per trace slot, CURVE_COLORS[i] for row i (see the module
# docstring's "Channel slots" paragraph for what "row i" means once a
# middle slot is removed and later slots compact up).


def _gapped_xy(series: List[Tuple[float, int]], t0: float
              ) -> Tuple[List[float], List[float]]:
    """series -> (x, y) with a NaN inserted wherever the gap to the
    previous sample exceeds GAP_FACTOR times the median sample
    interval, so a pyqtgraph curve drawn with connect="finite" breaks
    the line instead of interpolating across a stall. This is the
    panel's original (M6) statistical gap detector; no channel curve
    runs through it any longer (TraceStore does exact seq/gen-based
    detection instead - see the module docstring's "Rendering"
    paragraph) but it stays as a directly-tested pure function."""
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
    ascending by t - a linear scan from the end finds it without a
    bisect import. Used by the crosshair hover readout
    (_value_text_for) against a NaN-filtered view of a slot's cached
    TraceStore series, and directly unit-tested as a pure function."""
    for i in range(len(series) - 1, -1, -1):
        if series[i][0] <= t:
            return series[i][1]
    return None


def decode_value(raw: int, type_name: str):
    """Display-side type decode (spec point 3) of a raw 32-bit trace
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


def _decode_series(y: np.ndarray, type_name: str) -> List[float]:
    """Per-sample display-side decode of a raw-domain f64 array from
    TraceStore.series() - each finite element is an exact-integer-
    valued float (TraceStore's own invariant, good for every real u32
    value), cast back to int before decode_value() does its bit work;
    a NaN gap row (TraceStore's own seq/gen break detection) is left
    as NaN, NOT decoded, so a curve drawn with connect="finite" still
    breaks there."""
    out = []
    for v in y:
        if v != v:  # NaN
            out.append(float("nan"))
        else:
            out.append(float(decode_value(int(v), type_name)))
    return out


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
    """Display-only transform applied to an already-decoded y array
    (see _decode_series): y' = (y - offset) * scale. NaN gap
    placeholders pass through untouched, since a gap's midpoint
    carries no real sample to scale. Pure python lists throughout - no
    numpy dependency."""
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
        self._last_t_latest = self._t0
        # Channel slots (module docstring's "Channel slots" paragraph):
        # always exactly MAX_CH entries, always compacted - an occupied
        # prefix followed by a None suffix. Index i is simultaneously
        # the trace watch index, the table row, and the TraceStore
        # column.
        self._slots: List[Optional[dict]] = [None] * MAX_CH
        # per-slot (t, y) numpy pair cached by the last _redraw call -
        # the crosshair handler and Auto-lane both read this instead of
        # calling self.store.series() again, so a mouse-move event (or
        # a lane recompute) costs no extra TraceStore copy beyond the
        # refresh that already ran.
        self._last_series: Dict[int, Tuple[np.ndarray, np.ndarray]] = {}
        self._markers: List[Tuple[float, pg.InfiniteLine]] = []
        self._cursor_t: Optional[float] = None
        self._cursor_line: Optional[pg.InfiniteLine] = None
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
        self._selected_row: Optional[int] = None
        # Auto-lane (spec point 4): while on, every refresh_plot()
        # tick stacks ALL occupied channels into equal horizontal
        # bands (one lane each, in slot order - see _apply_auto_lane),
        # writing the computed scale/offset into the editable cells;
        # while off, the cells are whatever was last computed or
        # typed.
        self._auto_lane = False
        # Trace discovery (spec point 1/2 of the M7 rework): the
        # reader is always constructed, even in NO_SOURCE - discover()
        # only ever runs once an address is known, at the bottom of
        # this method (demo mode) or from load_elf() (a real ELF's
        # ps_trace_desc symbol). self.desc/self.store are None until a
        # discover() call succeeds.
        self.reader = TraceReader(engine)
        self.desc = None  # type: Optional[object]
        self.store: Optional[TraceStore] = None
        # Tracks whether error_label currently shows a _drain_once()
        # failure specifically, so a later successful drain can clear
        # it without also wiping out an unrelated channel-add refusal
        # message (e.g. "table full") that happens to still be
        # showing.
        self._refresh_error_active = False
        # Rolling (t, cumulative reader.lost) samples, pruned to the
        # roll-mode window - see _update_drain_health (spec point 6).
        self._lost_window: List[Tuple[float, int]] = []
        # Tracks whether the poller thread is actually draining
        # commands right now - _drain_once() below skips
        # reader.refresh() entirely once this goes False, rather than
        # blocking the Qt main thread for Engine._exec's full 2 s
        # command timeout on every drain tick (fast timer AND the
        # always-on slow drain timer) forever after the poller stops.
        # This is a real production freeze risk, not just a test
        # artifact: a discovered trace target whose poller later stops
        # (engine.stop(), a lost connection torn down elsewhere) would
        # otherwise hang the whole UI for ~2s out of every drain
        # cadence, repeated indefinitely, for as long as this page
        # stays alive. Defaults True (optimistic) rather than False,
        # since engine.start() is normally called BEFORE ScopePage is
        # constructed - Poller.on_state() only notifies FUTURE
        # transitions, so a late subscriber here would otherwise never
        # see the RUNNING emission that already happened and could
        # wrongly stay disabled forever; only an explicit STOPPED
        # transition (which, by construction, can only ever be
        # observed AFTER this subscription exists) flips it off.
        # TARGET_LOST is deliberately left enabled - poller.py's own
        # reconnect loop fails a queued command quickly via
        # _fail_pending(), not by blocking the full timeout the way a
        # fully stopped poller's abandoned queue does.
        self._poller_running = True
        self.engine.on_state(self._on_engine_state)

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
        # error surface, the firmware rate readout, a small drain-loss
        # readout, Auto-lane, and the big Run/Stop, which keeps the
        # page's top-right - same spot as the Data Path page's.
        top_row = QHBoxLayout()
        self.time_label = QLabel("t-now = -- s")
        top_row.addWidget(self.time_label)
        # Inline error surface (never a dialog, per the class-level
        # convention) - lives on the always-visible top row so an
        # error (or the NO_SOURCE state) can never be hidden by the
        # splitter. Doubles as this page's page-state indicator (spec
        # point 1): NO_SOURCE / TraceError text lives here, cleared
        # once READY - and also carries channel-add refusals and
        # _drain_once() failures (spec points 3/5), all via the same
        # single inline surface.
        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #C62828;")
        top_row.addWidget(self.error_label, 1)
        # Firmware sample rate (spec point: "Rate header") - a
        # property of the trace buffer's own period_us, set once
        # discovery succeeds; blank in NO_SOURCE/TraceError.
        self.rate_label = QLabel("")
        top_row.addWidget(self.rate_label)
        # Drain health (spec point 6): "lost N samples" when
        # TraceReader.lost has grown in roughly the last roll-mode
        # window, blank otherwise - see _update_drain_health.
        self.lost_label = QLabel("")
        self.lost_label.setStyleSheet("color: #EF6C00;")
        top_row.addWidget(self.lost_label)
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
        # a slot rewrites an existing row's cells in place, this page
        # never inserts or removes a row again after this constructor
        # runs.
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

        # Add-channel area: the ELF group is the primary source
        # control - Load ELF plus the collapsible symbol picker.
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
        outer.addWidget(self.splitter, 1)
        # All MAX_CH slot rows visible by default (spec point 7),
        # sized from the table's own row/header metrics rather than a
        # hardcoded pixel guess.
        self._size_splitter_for_all_rows()

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

        # Fast repaint timer (running + visible): drains and redraws.
        # Paused by Run/Stop and by hide/show - see set_stopped/
        # hideEvent/showEvent below.
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh_plot)
        self._timer.start()

        # Slow drain timer (spec point 5): keeps draining the trace
        # ring into self.store regardless of Run/Stop or page
        # visibility, so the ring (RING_COUNT=256 records) can never
        # overflow just because nobody's watching. Started once a
        # trace target is actually discovered (see _discover_at) and
        # never stopped by hideEvent/set_stopped.
        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(DRAIN_MS)
        self._drain_timer.timeout.connect(self._drain_once)

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
        resumes) the 200 ms FAST repaint timer only - the slow drain
        timer is unaffected (spec point 5: Run/Stop is display-only,
        the ring must keep draining regardless). refresh_plot() itself
        stays callable regardless (tests call it directly). Resuming
        only starts the timer if the page is currently visible -
        resuming while the dock is hidden (tabbed away) must not wake
        a timer that hideEvent() below deliberately paused;
        showEvent() re-checks self._stopped when the dock is next
        shown, so the timer still resumes correctly at that point.
        Syncs the big button's own label/checked state either way this
        was triggered - this method, the big button, or spacebar."""
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
        """Stop the FAST repaint timer while the page isn't shown -
        tabbed away behind Event log, same bug class as
        memory_page.py's auto-refresh: an invisible ScopePage has no
        reason to keep redrawing a plot nobody sees every 200 ms
        forever. The slow drain timer is untouched (spec point 5)."""
        self._timer.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        """Resume the FAST timer on redisplay, unless the user has
        stopped this page - set_stopped(True) wins over a plain
        show/hide cycle, mirroring memory_page.py's auto_check.
        isChecked() guard (there, the user's own preference; here,
        this page's own stop state)."""
        if not self._stopped:
            self._timer.start()
        super().showEvent(event)

    def closeEvent(self, event) -> None:
        """Stop BOTH timers unconditionally when this page is closed -
        unlike hideEvent (which only fires on a visible-to-hidden
        transition, so a page that was never shown never gets one),
        close() always delivers a close event regardless of prior
        visibility. This matters beyond tidiness: a closed-but-not-yet-
        destroyed page (Qt's deleteLater() only schedules destruction
        for whenever the event loop next runs, not immediately) would
        otherwise keep ticking the always-on slow drain timer forever,
        each tick doing real (if cheap) work against a page nothing
        can see any more - real-world impact if MainWindow itself
        closes while a ScopePage still exists; measured impact in this
        codebase's own test suite, where pytest-qt's per-test teardown
        calls close() on every widget registered via qtbot.addWidget()
        regardless of whether the test ever called show() - without
        this override, each such "zombie" page's timers kept ticking
        for the rest of the test process, and enough of them
        accumulated to make later tests progressively slower."""
        self._timer.stop()
        self._drain_timer.stop()
        super().closeEvent(event)

    # -- trace discovery & page state (spec point 1) -------------------------

    def _discover_at(self, addr: int) -> bool:
        """Attempt trace discovery at addr and update this page's
        state: READY (self.desc set, rate_label shows the firmware's
        own period, error_label cleared, self.store (re)created and
        every channel slot cleared - see _reset_channels) on success,
        or an inline error state (str(e) rendered verbatim in
        error_label, self.desc left None) on failure. Two exception
        families land here, both from Engine.read_words underneath
        TraceReader.discover(): TraceError (bad magic, unsupported
        version, bad geometry - core/trace/contract.py's
        ContractError, wrapped by reader.py) and EngineError (the
        poller isn't running yet, a command timed out, or the target
        was lost mid-discovery - Engine._exec's own failure mode).
        Both are genuine "no usable trace target right now" outcomes
        from this page's point of view, and neither may ever escape
        this method uncaught: the real-world path is a user switching
        to the Scope tab against unplugged/still-connecting hardware,
        where MainWindow's lazy construction (_activate_scope_tab)
        must not crash - the page has to stay alive and simply show
        why. On failure, self.reader's own desc/desc_addr are also
        reset to None - discover() only ever reassigns them on its
        success path, so a SECOND discover() call that fails (e.g.
        load_elf() finding a ps_trace_desc symbol after construction's
        own demo-mode discovery already succeeded) would otherwise
        leave the reader silently able to keep talking to the FIRST,
        now-superseded target even though self.desc (and error_label)
        say there is none - this keeps "page.desc is None" and "no
        channel can be added" in lockstep. Returns whether discovery
        succeeded, for callers (load_elf(), __init__) that branch on
        it."""
        try:
            self.desc = self.reader.discover(addr)
        except (TraceError, EngineError) as e:
            self.desc = None
            self.reader.desc = None
            self.reader.desc_addr = None
            self.rate_label.setText("")
            self.error_label.setText(str(e))
            return False
        self.rate_label.setText(
            "%d Hz (firmware)" % round(1e6 / self.desc.period_us))
        self.error_label.setText("")
        self._reset_channels()
        self.store = TraceStore(self.desc, window_s=self.engine.history.window_s)
        if not self._drain_timer.isActive():
            self._drain_timer.start()
        return True

    def rate_label_text(self) -> str:
        """Test-support accessor for the firmware rate readout (spec
        point: "Rate header") - "" before READY."""
        return self.rate_label.text()

    # -- channel table skeleton (spec point 2) --------------------------------

    def _build_channel_table_skeleton(self) -> None:
        """The table ALWAYS has exactly MAX_CH rows, row i permanently
        representing trace slot i - built once, here, and never grown
        or shrunk by insertRow/removeRow again (occupying a slot
        rewrites an existing row's cells in place). Every row starts
        as an empty-slot placeholder; CURVE_COLORS[i] binds to row i's
        swatch regardless of occupancy, so a slot's eventual curve
        color is visible even before anything watches it."""
        for row in range(MAX_CH):
            self._build_placeholder_row(row)

    def _build_placeholder_row(self, row: int) -> None:
        """Reset row to an empty-slot placeholder. The swatch is
        permanent (bound to CURVE_COLORS[row] for the table's whole
        lifetime) and is only ever built once - a placeholder rebuild
        (a slot freed by remove_channel's compaction, or the very
        first skeleton build) must not recreate or touch it if it
        already exists. Every OTHER per-occupied-row widget
        (_populate_slot_row's minus button/type combo/scale+offset
        edits) is explicitly cleared, since this row may previously
        have held a real channel."""
        if self.channel_table.cellWidget(row, COL_SWATCH) is None:
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
        self.channel_table.removeCellWidget(row, COL_REMOVE)
        self.channel_table.removeCellWidget(row, COL_TYPE)
        self.channel_table.removeCellWidget(row, COL_SCALE)
        self.channel_table.removeCellWidget(row, COL_OFFSET)

        self.channel_table.setItem(row, COL_NAME, self._placeholder_item("-"))
        for col in (COL_ADDR, COL_TYPE, COL_VALUE, COL_SCALE, COL_OFFSET):
            self.channel_table.setItem(row, col, self._placeholder_item(""))

    @staticmethod
    def _placeholder_item(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        item.setForeground(QColor(PLACEHOLDER_FG))
        return item

    def _size_splitter_for_all_rows(self) -> None:
        """Spec point 7: all MAX_CH slot rows visible by default,
        without dragging the splitter - computed from the channel
        table's own header/row metrics (not a hardcoded pixel guess)
        plus a small fixed allowance for the Load-ELF/toggle button
        row beneath it, so a future column label wrap or theme
        font-size change can't silently clip a row again."""
        table = self.channel_table
        rows_h = table.verticalHeader().length()
        header_h = table.horizontalHeader().height()
        frame = 2 * table.frameWidth()
        top_h = header_h + rows_h + frame + 40
        self.splitter.setSizes([top_h, 400])

    # -- channel slots (spec point 2, this task) ------------------------------

    def channel_slots(self) -> List[Optional[dict]]:
        """Test-support + the panel's own produced interface: the raw
        slot list, always length MAX_CH and always compacted (an
        occupied prefix, then a None suffix - see the module
        docstring's "Channel slots" paragraph)."""
        return self._slots

    def add_symbol_channel(self, name: str) -> Optional[int]:
        """Resolve name against self.elf_symbols (populated by
        load_elf()) and occupy a slot at its address, preselecting a
        type from its declared size (_default_type_for_size) - rides
        the same add_address_slot() path a manual address add would,
        not a separate code path."""
        symbol = self.elf_symbols.get(name)
        if symbol is None:
            self.error_label.setText("unknown symbol: %s" % name)
            return None
        return self.add_address_slot(
            symbol.addr, name, _default_type_for_size(symbol.size))

    def add_register_channel(self, reg_key: str) -> Optional[int]:
        """Resolve reg_key (e.g. "ADC1.SR") against the target's SVD
        model and occupy a slot at its address - rides the same
        add_address_slot() path a manual address add would. An
        unknown reg_key renders the SvdError inline, same as any other
        refusal."""
        try:
            addr = self.engine.model.resolve(reg_key).address
        except SvdError as e:
            self.error_label.setText(str(e))
            return None
        return self.add_address_slot(addr, reg_key, DEFAULT_TYPE)

    def add_address_slot(self, addr: int, label: str,
                         type_name: str = DEFAULT_TYPE) -> Optional[int]:
        """The primitive add_symbol_channel()/add_register_channel()
        both wrap, and the one ui/app.py's --shot-scope flag drives
        directly. Occupies the FIRST free slot (always the current
        occupied count, since self._slots is kept compacted) and pushes
        the full occupied-address list to reader.set_watch() so watch
        index == table row == record slot always holds (see the module
        docstring's "Channel slots" paragraph). Every refusal - a full
        table, a guarded address, a misaligned address, or a
        TraceError/EngineError from the firmware write itself - renders
        inline in error_label and leaves self._slots exactly as it was
        before the call (spec point 3: guarded/alignment refusal BEFORE
        any write; a firmware rejection rolls the tentative slot back)."""
        occupied = [s for s in self._slots if s is not None]
        if len(occupied) >= MAX_CH:
            self.error_label.setText("table full")
            return None
        if addr in self.engine.guarded_addrs:
            self.error_label.setText("address 0x%08X is guarded" % addr)
            return None
        if addr % 4 != 0:
            self.error_label.setText("address must be 4-byte aligned")
            return None

        row = len(occupied)
        entry = self._make_slot_entry(row, addr, label, type_name)
        self._slots[row] = entry
        try:
            self.reader.set_watch(
                [s["addr"] for s in self._slots if s is not None])
        except (TraceError, EngineError) as e:
            self._slots[row] = None
            self.plot.removeItem(entry["curve"])
            self.error_label.setText(str(e))
            self._render_slot_rows()
            return None

        self.error_label.setText("")
        self._render_slot_rows()
        return row

    def remove_channel(self, slot: int) -> None:
        """Free slot and COMPACT the table: every later occupied slot
        shifts up one place (Python's pop()+append(None) does exactly
        this), which is also why a channel's curve color is rebound
        (_rebind_slot_colors) - CURVE_COLORS[row] is a property of the
        row a channel currently sits in, not something that follows a
        channel permanently once it shifts. A no-op for an
        out-of-range or already-empty slot. The selection follows a
        shifted channel to its new row (or clears, if the removed slot
        itself was selected) - see the module docstring."""
        if not (0 <= slot < MAX_CH) or self._slots[slot] is None:
            return
        entry = self._slots.pop(slot)
        self._slots.append(None)
        self.plot.removeItem(entry["curve"])
        self._last_series.pop(slot, None)
        self._rebind_slot_colors()

        old_selected = self._selected_row
        self._render_slot_rows()
        if old_selected == slot:
            self.channel_table.clearSelection()
        elif old_selected is not None and old_selected > slot:
            self.channel_table.selectRow(old_selected - 1)

        try:
            self.reader.set_watch(
                [s["addr"] for s in self._slots if s is not None])
            self.error_label.setText("")
        except (TraceError, EngineError) as e:
            self.error_label.setText(str(e))

    def _reset_channels(self) -> None:
        """A (re-)discovery invalidates whatever watch table/curves
        were built against the PREVIOUS trace target (possibly a
        different geometry, or a different physical target entirely)
        - clear every slot back to its placeholder rather than leave
        stale curves bound to an address nothing is watching anymore."""
        for entry in self._slots:
            if entry is not None:
                self.plot.removeItem(entry["curve"])
        self._slots = [None] * MAX_CH
        self._last_series = {}
        self._selected_row = None
        self._render_slot_rows()
        self._update_y_axis()

    def _make_slot_entry(self, row: int, addr: int, label: str,
                         type_name: str) -> dict:
        color = CURVE_COLORS[row]
        curve = self.plot.plot([], [], pen=pg.mkPen(color=color, width=1.5),
                               name=label)
        # setDownsampling/setClipToView are set ONCE, here, at creation
        # (spec point 4) - pyqtgraph then decimates the in-memory
        # series down to what the pixel width can actually show on
        # every subsequent setData(), rather than this page doing that
        # work itself.
        curve.setDownsampling(auto=True, method="peak")
        curve.setClipToView(True)
        return {
            "addr": addr,
            "label": label,
            "type": type_name,
            "transform": {"scale": 1.0, "offset": 0.0},
            "curve": curve,
            "color": color,
        }

    def _rebind_slot_colors(self) -> None:
        for row, entry in enumerate(self._slots):
            if entry is None:
                continue
            color = CURVE_COLORS[row]
            if entry["color"] != color:
                entry["color"] = color
                entry["curve"].setPen(pg.mkPen(color=color, width=1.5))

    # -- channel table rendering (occupied + placeholder rows) ---------------

    def _render_slot_rows(self) -> None:
        """Rebuild every row's cells from self._slots - called after
        any add/remove since compaction can shift every subsequent
        slot's row. Full-width rebuild (MAX_CH=10 rows) is cheap and
        avoids special-casing "which rows actually moved" separately
        from "which rows are simply unchanged". Signals are blocked
        for the duration - each setItem() on the Name column would
        otherwise fire itemChanged for a row whose entry isn't fully
        populated yet (_on_item_changed's own early-out on an unknown
        key still makes this safe either way, but blocking avoids the
        redundant work)."""
        self.channel_table.blockSignals(True)
        try:
            for row in range(MAX_CH):
                entry = self._slots[row]
                if entry is None:
                    self._build_placeholder_row(row)
                else:
                    self._populate_slot_row(row, entry)
        finally:
            self.channel_table.blockSignals(False)

    def _populate_slot_row(self, row: int, entry: dict) -> None:
        minus_btn = QPushButton("-")
        minus_btn.setFixedWidth(22)
        minus_btn.clicked.connect(
            lambda _checked=False, r=row: self.remove_channel(r))
        minus_box = QWidget()
        minus_layout = QHBoxLayout(minus_box)
        minus_layout.setContentsMargins(0, 0, 0, 0)
        minus_layout.setAlignment(Qt.AlignCenter)
        minus_layout.addWidget(minus_btn)
        self.channel_table.setCellWidget(row, COL_REMOVE, minus_box)

        name_item = QTableWidgetItem(entry["label"])
        name_item.setData(Qt.UserRole, row)
        self.channel_table.setItem(row, COL_NAME, name_item)

        addr_item = QTableWidgetItem("0x%08X" % entry["addr"])
        addr_item.setFlags(addr_item.flags() & ~Qt.ItemIsEditable)
        self.channel_table.setItem(row, COL_ADDR, addr_item)

        # addItems()/setCurrentText() happen BEFORE connecting
        # currentTextChanged, deliberately - connecting first would let
        # setCurrentText() (when entry["type"] isn't the combo's
        # default first entry) fire _on_type_changed() -> refresh_plot()
        # mid-construction, before value_item/scale_edit/offset_edit
        # below are even set on this same entry dict yet.
        type_combo = QComboBox()
        type_combo.addItems(TYPES)
        type_combo.setCurrentText(entry["type"])
        type_combo.currentTextChanged.connect(
            lambda t, r=row: self._on_type_changed(r, t))
        self.channel_table.setCellWidget(row, COL_TYPE, type_combo)
        entry["type_combo"] = type_combo

        value_item = QTableWidgetItem("--")
        value_item.setFlags(value_item.flags() & ~Qt.ItemIsEditable)
        self.channel_table.setItem(row, COL_VALUE, value_item)
        entry["value_item"] = value_item

        scale_edit = self._make_number_edit(
            row, "scale", entry["transform"]["scale"])
        self.channel_table.setCellWidget(row, COL_SCALE, scale_edit)
        entry["scale_edit"] = scale_edit

        offset_edit = self._make_number_edit(
            row, "offset", entry["transform"]["offset"])
        self.channel_table.setCellWidget(row, COL_OFFSET, offset_edit)
        entry["offset_edit"] = offset_edit

    # -- inline table editing (spec point 2) --------------------------------

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """Handles a committed edit of the Name cell (double-click to
        rename) - the table's other QTableWidgetItem-backed columns
        (Address, Type, Value, Scale, Offset) are marked non-editable
        but still route through this same signal on every
        programmatic setText(), hence the early-out on column."""
        if item.column() != COL_NAME:
            return
        row = item.data(Qt.UserRole)
        if row is None or not (0 <= row < MAX_CH):
            return
        entry = self._slots[row]
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
        if row == self._selected_row:
            # the y-axis title (spec point 7) is this channel's name -
            # keep it in sync with a live rename.
            self._update_y_axis()

    def _on_type_changed(self, row: int, type_name: str) -> None:
        entry = self._slots[row] if 0 <= row < MAX_CH else None
        if entry is None:
            return
        entry["type"] = type_name
        self.refresh_plot()

    def _make_number_edit(self, row: int, field: str,
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
            lambda r=row, f=field, e=edit: self._commit_number_edit(r, f, e))
        return edit

    def _commit_number_edit(self, row: int, field: str,
                            edit: QLineEdit) -> None:
        entry = self._slots[row] if 0 <= row < MAX_CH else None
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
        """While on, every refresh_plot() tick restacks every occupied
        channel into its own lane (see _apply_auto_lane) - turning it
        off just stops the recompute, leaving whatever scale/offset
        was last computed in the (still-editable) fields for
        hand-tuning."""
        self._auto_lane = on
        if on:
            self.refresh_plot()

    def _apply_auto_lane(self) -> None:
        """Stack every OCCUPIED channel into equal horizontal bands of
        the [0, 1] view (one lane per channel, in slot order), mapping
        each channel's current-window min..max into its own band.
        Reads self._last_series (this tick's already-fetched samples -
        _redraw_channels runs this before applying Auto-lane, see its
        own comment) rather than calling self.store.series() again."""
        occupied = [row for row, e in enumerate(self._slots) if e is not None]
        n = len(occupied)
        for i, row in enumerate(occupied):
            band_lo, band_hi = i / n, (i + 1) / n
            lo, hi = self._window_min_max(row)
            scale, offset = _fit_scale_offset(lo, hi, band_lo, band_hi)
            self.set_channel_transform(row, scale, offset)

    def _window_min_max(self, row: int) -> Tuple[float, float]:
        """The raw-decoded (spec point 4: "readouts always decoded raw
        domain" - Auto-lane fits the same domain, pre scale/offset)
        min/max of a channel's currently cached window series, NaN gap
        rows excluded. (0.0, 0.0) - a flat window, per
        _fit_scale_offset's guard - for a channel with no real samples
        yet."""
        entry = self._slots[row]
        cached = self._last_series.get(row)
        if entry is None or cached is None:
            return 0.0, 0.0
        _t, y = cached
        decoded = [v for v in _decode_series(y, entry["type"]) if v == v]
        if not decoded:
            return 0.0, 0.0
        return float(min(decoded)), float(max(decoded))

    # -- y axis follows the selected channel (spec point 7) ------------------

    def _on_table_selection_changed(self) -> None:
        # selectionModel().selectedRows(), not currentRow(): Qt keeps
        # a "current" cell independent of the actual selection (e.g.
        # clearSelection() alone does not move it), so currentRow()
        # can still report a stale row after the selection is cleared.
        rows = self.channel_table.selectionModel().selectedRows()
        if not rows:
            self._selected_row = None
        else:
            row = rows[0].row()
            self._selected_row = row if self._slots[row] is not None else None
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
        entry = self._slots[self._selected_row] \
            if self._selected_row is not None else None
        if entry is None:
            axis.setStyle(showValues=False)
            self.plot.setLabel("left", "")
            return
        axis.setStyle(showValues=True)
        self.plot.setLabel(
            "left", '<span style="color:%s">%s</span>'
            % (entry["color"], entry["label"]))
        axis.tickStrings = self._make_tick_strings(self._selected_row)

    def _make_tick_strings(self, row: int):
        """A pyqtgraph AxisItem.tickStrings override bound to one row
        by index (not by a captured transform dict, which
        set_channel_transform replaces wholesale rather than mutating
        - looking the channel back up by row on every call always sees
        its current transform, however it last changed)."""
        def tick_strings(values, _scale, _spacing):
            entry = self._slots[row] if 0 <= row < MAX_CH else None
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
        item = self.symbol_list.currentItem()
        if item is None:
            return
        self.add_symbol_channel(item.text())

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

    # -- drain (spec point 5) -------------------------------------------------

    def _on_engine_state(self, state: str) -> None:
        """See the __init__ comment on self._poller_running - the
        single thing this flag exists for is letting _drain_once()
        skip reader.refresh() outright once the poller has actually
        stopped, instead of blocking the Qt main thread for a full
        Engine._exec timeout on every drain tick forever after."""
        self._poller_running = state != PollerState.STOPPED

    def _drain_once(self) -> None:
        """Drain the trace reader into the store, independent of paint
        state - called by BOTH the fast repaint timer (via
        refresh_plot) and the always-on slow drain timer;
        TraceReader.refresh() is safe to call redundantly (it returns
        only records newer than what it has already delivered), so no
        coordination between the two timers is needed. A TraceError/
        EngineError here (spec point 5) renders inline and the page
        stays alive - the next tick, from either timer, retries. Skips
        entirely once the poller has stopped (self._poller_running) -
        see its own comment for why that guard exists."""
        if self.store is None or not self._poller_running:
            return
        try:
            records = self.reader.refresh()
        except (TraceError, EngineError) as e:
            self.error_label.setText(str(e))
            self._refresh_error_active = True
            return
        if self._refresh_error_active:
            self.error_label.setText("")
            self._refresh_error_active = False
        self.store.append(records)
        self._update_drain_health(time.monotonic())

    def _update_drain_health(self, now: float) -> None:
        """Spec point 6: reader.lost delta within roughly the last
        roll-mode window (engine.history.window_s) - hidden entirely
        when nothing has been lost recently, rather than an all-time
        total that would never go back to zero even long after the
        target caught back up."""
        window = self.engine.history.window_s
        self._lost_window.append((now, self.reader.lost))
        self._lost_window = [(t, v) for t, v in self._lost_window
                             if now - t <= window]
        delta = self.reader.lost - self._lost_window[0][1]
        self.lost_label.setText("lost %d samples" % delta if delta > 0 else "")

    # -- repaint -----------------------------------------------------------

    def refresh_plot(self) -> None:
        now = time.monotonic()
        self._last_now = now
        self._drain_once()
        if self.store is not None:
            self._redraw_channels(now)
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

    def _redraw_channels(self, now: float) -> None:
        """Rebuild every occupied slot's curve from self.store - see
        the module docstring's "Rendering" paragraph. Series are
        fetched (and cached in self._last_series) for every occupied
        row FIRST, so Auto-lane (which reads that cache via
        _window_min_max) sees this tick's fresh data before the second
        loop applies scale/offset and calls setData()."""
        t_latest = None
        for row, entry in enumerate(self._slots):
            if entry is None:
                continue
            t, y = self.store.series(row)
            self._last_series[row] = (t, y)
            if t.size:
                candidate = float(t[-1])
                if t_latest is None or candidate > t_latest:
                    t_latest = candidate
        self._last_t_latest = now if t_latest is None else t_latest

        if self._auto_lane:
            self._apply_auto_lane()

        for row, entry in enumerate(self._slots):
            if entry is None:
                continue
            t, y = self._last_series[row]
            x = t - self._last_t_latest
            decoded = _decode_series(y, entry["type"])
            decoded = _apply_transform(decoded, entry["transform"])
            entry["curve"].setData(x, decoded, connect="finite")
            entry["value_item"].setText(self._value_text_for(row))

    # -- crosshair / Value column readout (feature 1, spec point 6) ---------

    def _value_text_for(self, row: int) -> str:
        """The Value column's text for one channel - two states, no
        PIN, no third "locked" state (spec point 5): mouse off the
        plot (self._crosshair_t is None) shows the newest REAL sample
        (self.store.newest(), which already skips NaN gap rows); mouse
        on the plot shows the value at the crosshair's time, found via
        value_at() against a NaN-filtered view of this row's cached
        series. Either way the reading is the type-decoded, dual-radix
        value (spec point 6), RAW (decode()'d, but never
        scale/offset'd - "Readouts always decoded raw domain", spec
        point 4) rather than the scaled/fit display value on the
        curve, which is the whole point of this readout existing
        alongside the curve. jump_to()'s cursor line never reaches
        this method at all - an event-log click only moves that line,
        per spec point 5; to read the value at an event's moment, stop
        and hover."""
        entry = self._slots[row]
        if self._crosshair_t is None:
            raw = self.store.newest(row)
        else:
            # roll mode: the crosshair's x is relative to the last
            # refresh's now, not the obsolete dock-open self._t0 - see
            # the module docstring's "X axis" paragraph.
            raw_t = self._crosshair_t + self._last_now
            cached = self._last_series.get(row)
            if cached is None:
                raw = None
            else:
                t, y = cached
                pairs = [(tt, vv) for tt, vv in zip(t.tolist(), y.tolist())
                        if vv == vv]
                raw = value_at(pairs, raw_t)
        if raw is None:
            return "--"
        return format_value(decode_value(int(raw), entry["type"]),
                            entry["type"])

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
        refresh_plot(): no store.series() call here, so a mouse-move
        event costs no extra TraceStore copy."""
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
        """Rebuilds the Value cell text for every occupied channel and
        the column header text, from the current two-state cursor
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
        for row, entry in enumerate(self._slots):
            if entry is None:
                continue
            entry["value_item"].setText(self._value_text_for(row))

    # -- per-channel scale/offset (feature 2) --------------------------------

    def set_channel_transform(self, row: int, scale: float,
                              offset: float) -> None:
        """The programmatic surface the table's scale/offset text
        fields drive - also the direct entry point for tests and for
        Auto-lane's own recompute (_apply_auto_lane). A display-only
        transform: _redraw_channels() applies it (y' = (y - offset) *
        scale) when building each curve's y array; the Value column
        readout always shows the raw decoded value regardless of this
        setting. Also mirrors scale/offset into the row's own text
        fields so a programmatic change is visible inline rather than
        only affecting the plotted curve."""
        entry = self._slots[row] if 0 <= row < MAX_CH else None
        if entry is None:
            return
        entry["transform"] = {"scale": scale, "offset": offset}
        entry["scale_edit"].setText(_fmt_num(scale))
        entry["offset_edit"].setText(_fmt_num(offset))

    # -- test-support accessors ---------------------------------------------

    def channel_sample_count(self, slot: int) -> int:
        """Raw TraceStore row count for this slot's current window -
        independent of whether refresh_plot() (the redraw) has run at
        all, only _drain_once() (fast or slow timer, or a direct
        call) has to have."""
        if self.store is None:
            return 0
        t, _y = self.store.series(slot)
        return int(t.size)

    def curve_point_count(self, slot: int) -> int:
        entry = self._slots[slot] if 0 <= slot < MAX_CH else None
        if entry is None:
            return 0
        xdata, _ydata = entry["curve"].getData()
        return 0 if xdata is None else len(xdata)

    def curve_y(self, slot: int) -> List[float]:
        """The curve's currently plotted y data - post gap-NaN
        (TraceStore) and post display transform (scale/offset)."""
        entry = self._slots[slot] if 0 <= slot < MAX_CH else None
        if entry is None:
            return []
        _xdata, ydata = entry["curve"].getData()
        return [] if ydata is None else list(ydata)
