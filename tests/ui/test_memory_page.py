from ui.demo import make_demo_engine
from ui.panels.memory_page import MemoryPage


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
