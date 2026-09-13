"""Event log dock: timestamped anomaly and info rows, wired to the
live engine's flow-rule events and misc info messages (poller state
transitions, badge clears).

Small new code (not a straight port - the prototype's `Main.log()`
took a caller-supplied `focus_id` for every call site regardless of
kind; here that becomes two entry points, `add_events()`/`add_info()`,
both funnelling into one private `_append()` that carries over the
prototype's `log()` row styling verbatim: `"[HH:MM:SS] SEVERITY   text"`
in the shared mono font, red text for anomaly rows, and the same
"don't auto-scroll while the user has scrolled up" scrollbar check
(`sb.value() >= sb.maximum() - 4`, checked *after* the row is added -
same order as the prototype).

Row payload (Qt.UserRole): extended from the original bare
focus_id into a `(focus_id, event_time)` pair, so a click can drive
both MainWindow._on_log_focus (block focus, unchanged) and
MainWindow._on_log_time_focus (scope cursor sync, new) independently -
an anomaly row carries its AnomalyEvent's `.t` as event_time; every
other row leaves it None, so clicking it never moves the scope
cursor."""
import time
from typing import Callable, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import QListWidget, QListWidgetItem

from core.engine.rules import AnomalyEvent

MONO = QFont()
MONO.setFamilies(["Menlo", "Consolas", "Courier New"])
MONO.setPointSize(10)

COL_ANOM = QColor("#C62828")


def _noop(_arg) -> None:
    pass


class EventLog(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_focus: Callable[[str], None] = _noop
        # event-to-scope cursor sync: fired on click
        # only for rows that carry an event time (anomaly rows), never
        # for a plain info row - see _clicked's guard below.
        self.on_event_time: Callable[[float], None] = _noop
        self.itemClicked.connect(self._clicked)

    def add_events(self, events: List[AnomalyEvent]) -> None:
        for ev in events:
            self._append("anomaly", "%s: %s" % (ev.flow, ev.msg), ev.target,
                         ev.t)

    def add_info(self, text: str, focus_id: Optional[str] = None) -> None:
        self._append("info", text, focus_id, None)

    def _append(self, severity: str, text: str, focus_id: Optional[str],
               event_time: Optional[float] = None) -> None:
        ts = time.strftime("%H:%M:%S")
        it = QListWidgetItem("[%s] %-5s %s" % (ts, severity.upper(), text))
        it.setFont(MONO)
        # payload extended from a bare focus_id to a
        # (focus_id, event_time) pair - event_time is None for every
        # row except an anomaly row, which carries the AnomalyEvent's
        # .t so a click can also sync the scope cursor.
        it.setData(Qt.UserRole, (focus_id, event_time))
        if severity == "anomaly":
            it.setForeground(QBrush(COL_ANOM))
        self.addItem(it)
        sb = self.verticalScrollBar()
        if sb.value() >= sb.maximum() - 4:
            self.scrollToBottom()

    def _clicked(self, item: QListWidgetItem) -> None:
        focus_id, event_time = item.data(Qt.UserRole)
        if focus_id is not None:
            self.on_focus(focus_id)
        if event_time is not None:
            self.on_event_time(event_time)
