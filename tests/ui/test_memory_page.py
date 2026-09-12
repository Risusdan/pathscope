import shutil

from core.adapter.mock import MockAdapter
from core.engine.core import Engine
from ui.demo import make_demo_engine
from ui.panels.memory_page import MemoryPage

TARGET = "targets/f411"
ADC1_DR_ADDR = 0x4001204C


def _guarded_target(tmp_path):
    """Copy targets/f411 and overlay f411.flows.yaml so ADC1.DR becomes
    guarded purely via the poll.guarded overlay - same pattern as
    tests/ui/test_register_page.py's _guarded_target()."""
    tdir = tmp_path / "t"
    shutil.copytree(TARGET, tdir)
    fl = tdir / "f411.flows.yaml"
    fl.write_text(fl.read_text().replace(
        "force_poll: []",
        "force_poll: []\n  guarded: [\"ADC1.DR\"]"))
    return str(tdir)


def test_memory_read_renders_hex(qtbot):
    engine = make_demo_engine("targets/f411")
    engine.start()
    try:
        page = MemoryPage(engine)
        qtbot.addWidget(page)
        page.addr_edit.setText("0x40026410")   # S0CR: known nonzero
        page.do_read()
        text = page.dump.toPlainText()
        assert "40026410" in text.replace(" ", "").lower() or "0x4002" in text
        assert "00000001" in text              # EN bit set by demo
    finally:
        engine.stop()


def test_auto_refresh_timer_stops_when_hidden(qtbot):
    engine = make_demo_engine("targets/f411")
    engine.start()
    try:
        page = MemoryPage(engine)
        qtbot.addWidget(page)
        page.show()
        page.addr_edit.setText("0x20000000")
        page.auto_check.setChecked(True)
        assert page._timer.isActive()
        page.hide()
        assert not page._timer.isActive()
        page.show()
        assert page._timer.isActive()       # preference preserved
    finally:
        engine.stop()


def test_do_read_refuses_range_covering_guarded_register(tmp_path, qtbot):
    """FIX 4 regression: the memory viewer must refuse a read whose
    address range sweeps over a guarded register (readAction or
    overlay), exactly like the poller's own read plan does - a raw
    hex dump has no confirm-and-force path, so the only safe move is
    not to read at all."""
    target_dir = _guarded_target(tmp_path)
    adapter = MockAdapter({})
    engine = Engine.load(target_dir, adapter, interval_s=0.01)
    assert ADC1_DR_ADDR in engine.guarded_addrs
    engine.start()
    try:
        page = MemoryPage(engine)
        qtbot.addWidget(page)
        page.addr_edit.setText("0x40012000")       # ADC1 base
        idx = page.len_combo.findData(256)
        assert idx >= 0
        page.len_combo.setCurrentIndex(idx)         # covers 0x4001204C

        page.do_read()

        text = page.dump.toPlainText()
        assert "refused" in text
        assert "0x%08X" % ADC1_DR_ADDR in text
        assert all(
            not (addr <= ADC1_DR_ADDR < addr + 4 * count)
            for addr, count in adapter.read_log)
    finally:
        engine.stop()


def test_do_read_guarded_range_stops_auto_refresh(tmp_path, qtbot):
    target_dir = _guarded_target(tmp_path)
    adapter = MockAdapter({})
    engine = Engine.load(target_dir, adapter, interval_s=0.01)
    engine.start()
    try:
        page = MemoryPage(engine)
        qtbot.addWidget(page)
        page.show()
        # start auto-refresh over a safe range - this must succeed and
        # leave the timer running, same as before this fix.
        page.addr_edit.setText("0x20000000")
        page.auto_check.setChecked(True)
        assert page._timer.isActive()

        # then point the same live auto-refresh at the guarded range,
        # as if the user retyped the address field - the next refresh
        # tick (simulated here by calling do_read() directly) must
        # refuse the read and shut auto-refresh back off rather than
        # keep re-triggering the same refusal every second.
        page.addr_edit.setText("0x40012000")       # ADC1 base
        idx = page.len_combo.findData(256)
        page.len_combo.setCurrentIndex(idx)         # covers 0x4001204C

        page.do_read()

        assert not page.auto_check.isChecked()
        assert not page._timer.isActive()
    finally:
        engine.stop()
