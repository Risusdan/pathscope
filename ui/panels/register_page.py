"""Register inspector panel: shows one topology block's SVD peripheral
registers live, updated from bridge-delivered EngineUpdates.

Ported from prototype/ui_proto.py's RegisterPage, with the following
semantic upgrades:

  - the prototype's static REG_DEFS[block]["watched"/"cold"/"guarded"]
    table is gone; row mode is derived live from engine state
    (engine.polled / RegRef.read_action / engine.flowspec.guarded and
    force_poll) at show_block time instead.
  - show_block() calls engine.set_watch() with the shown block's
    non-guarded register keys so the visible peripheral actually goes
    live (spec 6.2 "visible panels"), replacing whatever the previously
    shown block had requested.
  - cold/guarded double-click reads go through the real
    engine.read_words(addr, 1) instead of the prototype's fake
    reg_value()/hardcoded stub values.

The flash-on-change mechanics (persistent `changed_at` dict keyed by
reg_key, `ts is not None` guard) and the column setup are carried over
unchanged - see refresh() below.
"""
import time
from typing import Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import QMessageBox, QTreeWidget, QTreeWidgetItem

from core.engine.core import Engine, EngineError
from core.engine.rules import EngineUpdate

MONO = QFont()
MONO.setFamilies(["Menlo", "Consolas", "Courier New"])
MONO.setPointSize(10)

COL_GREY = QColor("#B0B0B0")
COL_ANOM = QColor("#C62828")
COL_FLASH = QColor("#FFF59D")
COL_TRANSPARENT = QColor("transparent")
COL_WARN = QColor("#E65100")


class RegisterPage(QTreeWidget):
    def __init__(self, engine: Engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.block_id: Optional[str] = None
        self.setColumnCount(2)
        self.setHeaderLabels(["Register / Field", "Value"])
        self.setColumnWidth(0, 170)
        self.itemDoubleClicked.connect(self._on_double)
        self.setExpandsOnDoubleClick(False)  # double-click is a read command, not expand/collapse
        self.changed_at: Dict[str, float] = {}   # reg_key -> monotonic ts
        self._last_update: Optional[EngineUpdate] = None

    # -- mode derivation -------------------------------------------------

    def _is_guarded(self, reg_key: str, read_action) -> bool:
        guarded = (read_action is not None
                  or reg_key in self.engine.flowspec.guarded)
        return guarded and reg_key not in self.engine.flowspec.force_poll

    def _force_poll_warn(self, reg_key: str, read_action) -> bool:
        """spec 6.3c: a register that force_poll pulls back onto the
        watch list despite having read side effects (readAction or a
        guarded overlay) needs a persistent warning, since it reads
        as an ordinary "watched" row otherwise - _is_guarded() above
        returns False for it precisely because force_poll overrides
        the guard, which is the condition this checks for."""
        would_be_guarded = (read_action is not None
                            or reg_key in self.engine.flowspec.guarded)
        return would_be_guarded and reg_key in self.engine.flowspec.force_poll

    def _mode(self, reg_key: str, read_action, refused) -> str:
        if reg_key in self.engine.polled:
            return "watched"
        if reg_key in refused or self._is_guarded(reg_key, read_action):
            return "guarded"
        return "cold"

    # -- selection ---------------------------------------------------------

    def show_block(self, block_id: str) -> None:
        self.block_id = block_id
        self.clear()
        block = self.engine.topology.blocks.get(block_id)
        if block is None or block.svd is None:
            self.engine.set_watch(set())
            hint = QTreeWidgetItem(["(no registers for this block)", ""])
            hint.setForeground(0, QBrush(COL_GREY))
            self.addTopLevelItem(hint)
            return

        peripheral = self.engine.model.peripherals[block.svd]
        visible = {
            "%s.%s" % (block.svd, name)
            for name, reg in peripheral.registers.items()
            if not self._is_guarded("%s.%s" % (block.svd, name),
                                    reg.read_action)
        }
        # spec 6.2 "visible panels": the shown block's non-guarded
        # registers go live; the previous block's extra watch set is
        # replaced wholesale by this call. `visible` already excludes
        # every key our own _is_guarded() predicate flags, so under
        # normal operation `refused` comes back empty - but the brief
        # asks that a refused key still render as guarded regardless of
        # what our own predicate said, so it is folded into _mode()'s
        # guarded check below rather than trusted to be empty.
        refused = set(self.engine.set_watch(visible))

        for name, reg in peripheral.registers.items():
            reg_key = "%s.%s" % (block.svd, name)
            mode = self._mode(reg_key, reg.read_action, refused)
            force_warn = self._force_poll_warn(reg_key, reg.read_action)
            display_name = ("[!] " + name) if force_warn else name
            it = QTreeWidgetItem([display_name, "--"])
            it.setData(0, Qt.UserRole, (reg_key, mode, reg.address))
            it.setFont(1, MONO)
            if mode == "cold":
                it.setForeground(0, QBrush(COL_GREY))
                it.setForeground(1, QBrush(COL_GREY))
                it.setToolTip(0, "not polled - double-click to read once")
            elif mode == "guarded":
                it.setForeground(0, QBrush(COL_ANOM))
                it.setText(1, "[guarded]")
                it.setToolTip(0, "readAction register: reading has side "
                                 "effects. Double-click to force one read.")
            if force_warn:
                # persistent marker (spec 6.3c): this row reads as an
                # ordinary "watched" row otherwise, but force_poll put
                # it back on the watch list despite its read side
                # effects - overrides any tooltip/color the mode
                # branches above may have already set.
                it.setForeground(0, QBrush(COL_WARN))
                it.setToolTip(0, "force-polled despite read side effects")
            for fname, f in reg.fields.items():
                ch = QTreeWidgetItem(
                    ["  .%s [%d:%d]" % (fname, f.msb, f.lsb), "--"])
                ch.setData(0, Qt.UserRole, (reg_key, "field", f.lsb, f.msb))
                ch.setFont(1, MONO)
                it.addChild(ch)
            self.addTopLevelItem(it)
        self.expandAll()
        self.refresh(self._last_update)

    # -- live data -----------------------------------------------------------

    def refresh(self, update: Optional[EngineUpdate] = None) -> None:
        if update is not None:
            self._last_update = update
        update = self._last_update
        if update is None or self.block_id is None:
            return
        now = time.monotonic()
        for i in range(self.topLevelItemCount()):
            it = self.topLevelItem(i)
            data = it.data(0, Qt.UserRole)
            if data is None or len(data) != 3:
                continue          # the "no registers" hint row
            reg_key, mode, _addr = data
            if mode != "watched":
                continue          # cold/guarded text is only ever set
                                   # at show_block time or by a
                                   # double-click read - refresh() never
                                   # touches it, same as the prototype.
            sample = update.snapshot.values.get(reg_key)
            v = sample.value if sample is not None else None
            new = "0x%08X" % v if v is not None else "--"
            if it.text(1) != new and it.text(1) != "--":
                self.changed_at[reg_key] = now
            it.setText(1, new)
            ts = self.changed_at.get(reg_key)
            flash = ts is not None and now - ts < 0.5
            it.setBackground(1, QBrush(COL_FLASH if flash
                                       else COL_TRANSPARENT))
            for j in range(it.childCount()):
                ch = it.child(j)
                _, _, lo, hi = ch.data(0, Qt.UserRole)
                mask = (1 << (hi - lo + 1)) - 1
                ch.setText(1, str((v >> lo) & mask) if v is not None
                          else "--")

    # -- one-shot / forced reads --------------------------------------------

    def _on_double(self, item: QTreeWidgetItem, _column: int) -> None:
        data = item.data(0, Qt.UserRole)
        if data is None or len(data) != 3:
            return                # header hint row or a field child row
        reg_key, mode, addr = data
        if mode == "cold":
            try:
                words = self.engine.read_words(addr, 1)
            except EngineError:
                item.setText(1, "(read failed)")
                return
            item.setText(1, "0x%08X (stale)" % words[0])
        elif mode == "guarded":
            answer = QMessageBox.question(
                self, "Guarded register",
                "%s has read side effects (readAction).\n"
                "Reading it may disturb the running firmware.\n\n"
                "Read anyway?" % reg_key)
            if answer != QMessageBox.Yes:
                return
            try:
                words = self.engine.read_words(addr, 1)
            except EngineError:
                item.setText(1, "(read failed)")
                return
            item.setText(1, "0x%08X (forced)" % words[0])
