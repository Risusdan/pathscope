"""Scope panel: live pyqtgraph plot of History-backed channels, task
2's "Scope" dock (task-2-brief.md).

Not a port of anything in prototype/ui_proto.py - the prototype had no
scope view. New widget, following the same conventions as the other
Inspector-family pages (register_page.py, memory_page.py): a plain
QWidget, EngineError caught at the boundary and rendered as text in
the panel rather than a QMessageBox (never a dialog - task-2-brief.md
is explicit about this for the address-watch add path).

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
are part of the produced interface per task-2-brief.md.

`jump_to(t)` / `cursor_time()` (task-4-brief.md's event-log-click ->
scope-cursor sync) are NOT implemented here - left entirely to Task 4,
per task-2-brief.md's "you may stub cursor_time now or leave to Task
4; document which" - this file leaves both to Task 4.

ELF symbol picker (task 3, task-3-brief.md): "Load ELF..." opens a
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
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QVBoxLayout, QWidget)

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
    median = intervals[len(intervals) // 2]
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


class ScopePage(QWidget):
    def __init__(self, engine: Engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.frozen = False
        self._t0 = time.monotonic()
        self._channels: Dict[str, dict] = {}
        self._markers: List[Tuple[float, pg.InfiniteLine]] = []
        # name -> ui.elf_symbols.Symbol (a namedtuple, hence `tuple`
        # here) - populated by load_elf(); ui.elf_symbols is imported
        # lazily there, not at this module's top, so this attribute is
        # typed structurally rather than by importing the class.
        self.elf_symbols: Dict[str, tuple] = {}

        outer = QHBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)

        side = QVBoxLayout()
        side.addWidget(QLabel("Channels"))
        self.channel_list = QListWidget()
        side.addWidget(self.channel_list, 1)

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

        remove_btn = QPushButton("Remove channel")
        remove_btn.clicked.connect(self._on_remove_clicked)
        side.addWidget(remove_btn)

        self.window_label = QLabel(
            "window: %.0f s (History)" % engine.history.window_s)
        side.addWidget(self.window_label)

        side_widget = QWidget()
        side_widget.setLayout(side)
        side_widget.setMaximumWidth(260)
        outer.addWidget(side_widget)

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.plot_widget.setLabel("bottom", "t", units="s")
        self.plot = self.plot_widget.getPlotItem()
        self.plot.addLegend()
        outer.addWidget(self.plot_widget, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh_plot)
        self._timer.start()

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

    def add_address_channel(self, addr: int, label: str) -> str:
        """Add a fixed-address channel via engine.add_addr_watch().
        Raises EngineError straight through (guarded/misaligned
        address) - the button handler below is what catches it and
        renders the message inline, never a dialog."""
        key = self.engine.add_addr_watch(addr, label)
        if key not in self._channels:
            self._add_channel_common(key, label)
        return key

    def remove_channel(self, key: str) -> None:
        entry = self._channels.pop(key, None)
        if entry is None:
            return
        self.plot.removeItem(entry["curve"])
        row = self.channel_list.row(entry["item"])
        if row >= 0:
            self.channel_list.takeItem(row)
        if key.startswith("@"):
            # the scope itself asked for this address watch - clean it
            # up so the poller stops reading it once nothing displays
            # it any more.
            self.engine.remove_addr_watch(key)

    def _add_channel_common(self, key: str, label: str) -> None:
        color = CURVE_COLORS[len(self._channels) % len(CURVE_COLORS)]
        curve = self.plot.plot([], [], pen=pg.mkPen(color=color, width=2),
                               name=label, connect="finite")
        item = QListWidgetItem(label)
        item.setData(Qt.UserRole, key)
        self.channel_list.addItem(item)
        self._channels[key] = {"label": label, "curve": curve, "item": item}

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

    def _prune_markers(self, now: float) -> None:
        window = self.engine.history.window_s
        kept = []
        for t, line in self._markers:
            if now - t > window:
                self.plot.removeItem(line)
            else:
                kept.append((t, line))
        self._markers = kept

    # -- repaint -----------------------------------------------------------

    def refresh_plot(self) -> None:
        self._refresh_reg_combo()
        now = time.monotonic()
        for key, entry in self._channels.items():
            series = self.engine.history.series(key)
            x, y = _gapped_xy(series, self._t0)
            entry["curve"].setData(x, y, connect="finite")
            rate = _effective_rate_hz(series, now)
            entry["item"].setText("%s  (%.1f Hz)" % (entry["label"], rate))
        self._prune_markers(now)

    # -- test-support accessors ---------------------------------------------

    def channel_sample_count(self, key: str) -> int:
        return len(self.engine.history.series(key))

    def curve_point_count(self, key: str) -> int:
        entry = self._channels.get(key)
        if entry is None:
            return 0
        xdata, _ydata = entry["curve"].getData()
        return 0 if xdata is None else len(xdata)
