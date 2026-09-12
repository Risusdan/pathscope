"""Memory viewer panel: on-demand hex-dump reads for memory-kind
topology blocks (SRAM, Flash), task-12-brief.md.

Not a port of anything in prototype/ui_proto.py - the prototype had no
memory viewer. New widget, same conventions as the other Inspector
pages (register_page.py, flow_page.py): a plain QWidget, MONO font for
hex text, `show_block(block_id)` entry point MainWindow calls on a
block click, EngineError caught at the boundary and rendered as text
in the widget rather than a QMessageBox (register_page.py raises a
dialog only for the guarded-read confirmation prompt, never for a
failure - same rule followed here, and here there is no confirmation
prompt at all since a memory read has no side effects to warn about).

Address field takes decimal or 0x-hex text (`int(text, 0)`); an
unparsable address is reported in the dump pane the same way a read
failure is, rather than via a dialog or a silent no-op, since brief
7's "any EngineError lands in the widget as text, never a dialog" only
covers the engine call - this covers the one other way do_read() can
fail (bad user input) the same way for consistency.
"""
from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton,
                               QVBoxLayout, QWidget)

from core.engine.core import Engine, EngineError

MONO = QFont()
MONO.setFamilies(["Menlo", "Consolas", "Courier New"])
MONO.setPointSize(10)

WORDS_PER_ROW = 8
LENGTH_CHOICES = (64, 256, 1024)          # bytes
AUTO_REFRESH_MS = 1000


class MemoryPage(QWidget):
    def __init__(self, engine: Engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.block_id: Optional[str] = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)

        row = QHBoxLayout()
        row.addWidget(QLabel("addr"))
        self.addr_edit = QLineEdit()
        self.addr_edit.setFont(MONO)
        row.addWidget(self.addr_edit, 1)
        self.len_combo = QComboBox()
        for n in LENGTH_CHOICES:
            self.len_combo.addItem("%d bytes" % n, n)
        row.addWidget(self.len_combo)
        self.read_btn = QPushButton("Read")
        self.read_btn.clicked.connect(self.do_read)
        row.addWidget(self.read_btn)
        lay.addLayout(row)

        self.auto_check = QCheckBox("Auto-refresh (1 s)")
        self.auto_check.toggled.connect(self._toggle_auto)
        lay.addWidget(self.auto_check)

        self.dump = QPlainTextEdit()
        self.dump.setReadOnly(True)
        self.dump.setFont(MONO)
        lay.addWidget(self.dump, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(AUTO_REFRESH_MS)
        self._timer.timeout.connect(self.do_read)

    # -- selection -----------------------------------------------------------

    def show_block(self, block_id: str) -> None:
        """Called by MainWindow on a memory-kind block click: prefills
        the address field with the block's `base` (topology.Block.base)
        and clears any previous dump - the new block's contents are
        unrelated to whatever was last read."""
        self.block_id = block_id
        block = self.engine.topology.blocks.get(block_id)
        if block is not None and block.base is not None:
            self.addr_edit.setText("0x%08X" % block.base)
        self.dump.setPlainText("")

    # -- auto-refresh ------------------------------------------------------

    def _toggle_auto(self, on: bool) -> None:
        if on:
            self._timer.start()
            self.do_read()
        else:
            self._timer.stop()

    # -- read / dump ---------------------------------------------------------

    def do_read(self) -> None:
        try:
            addr = int(self.addr_edit.text().strip(), 0)
        except ValueError:
            self.dump.setPlainText(
                "invalid address: %r" % self.addr_edit.text())
            return
        length = self.len_combo.currentData()
        if length is None:
            length = LENGTH_CHOICES[0]
        count = length // 4
        try:
            words = self.engine.read_words(addr, count)
        except EngineError as e:
            self.dump.setPlainText("read failed: %s" % e)
            return
        self.dump.setPlainText(_format_dump(addr, words))


def _format_dump(base_addr: int, words) -> str:
    """Classic hex dump: 8 words per row, `%08X:  ` address column then
    space-joined `%08X` words - exact format task-12-brief.md's Step 3
    specifies."""
    lines = []
    for i in range(0, len(words), WORDS_PER_ROW):
        row = words[i:i + WORDS_PER_ROW]
        row_addr = base_addr + 4 * i
        lines.append("%08X:  " % row_addr
                     + " ".join("%08X" % w for w in row))
    return "\n".join(lines)
