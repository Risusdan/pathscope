"""Event log dock: timestamped anomaly and info rows, wired to the
live engine's flow-rule events and misc info messages (poller state
transitions, badge clears).

Small new code (not a straight port - the prototype's `Main.log()`
took a caller-supplied `focus_id` for every call site regardless of
kind; here that becomes two entry points per task-11-brief.md's
interface, `add_events()`/`add_info()`, both funnelling into one
private `_append()` that carries over the prototype's `log()` row
styling verbatim: `"[HH:MM:SS] SEVERITY   text"` in the shared mono
font, red text for anomaly rows, and the same
"don't auto-scroll while the user has scrolled up" scrollbar check
(`sb.value() >= sb.maximum() - 4`, checked *after* the row is added -
same order as the prototype)."""
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


def _noop(_id: str) -> None:
    pass


class EventLog(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.on_focus: Callable[[str], None] = _noop
        self.itemClicked.connect(self._clicked)

    def add_events(self, events: List[AnomalyEvent]) -> None:
        for ev in events:
            self._append("anomaly", "%s: %s" % (ev.flow, ev.msg), ev.target)

    def add_info(self, text: str, focus_id: Optional[str] = None) -> None:
        self._append("info", text, focus_id)

    def _append(self, severity: str, text: str, focus_id: Optional[str]
               ) -> None:
        ts = time.strftime("%H:%M:%S")
        it = QListWidgetItem("[%s] %-5s %s" % (ts, severity.upper(), text))
        it.setFont(MONO)
        it.setData(Qt.UserRole, focus_id)
        if severity == "anomaly":
            it.setForeground(QBrush(COL_ANOM))
        self.addItem(it)
        sb = self.verticalScrollBar()
        if sb.value() >= sb.maximum() - 4:
            self.scrollToBottom()

    def _clicked(self, item: QListWidgetItem) -> None:
        focus_id = item.data(Qt.UserRole)
        if focus_id is not None:
            self.on_focus(focus_id)
