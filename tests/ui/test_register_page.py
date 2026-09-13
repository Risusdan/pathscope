import shutil

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidgetItem

from core.adapter.mock import MockAdapter
from core.engine.core import Engine, EngineError
from ui.demo import make_demo_engine
from ui.panels import register_page as register_page_module
from ui.panels.register_page import RegisterPage

TARGET = "targets/f411"


def _guarded_target(tmp_path):
    """Copy targets/f411 into tmp_path and overlay f411.flows.yaml so
    ADC1.DR (readAction-free in the real SVD - confirmed by grep, this
    target has no readAction registers at all) becomes guarded purely
    via the flowspec overlay. Same replace pattern as
    tests/test_engine.py's test_overlay_guards_needed_register."""
    tdir = tmp_path / "t"
    shutil.copytree(TARGET, tdir)
    fl = tdir / "f411.flows.yaml"
    fl.write_text(fl.read_text().replace(
        "force_poll: []",
        "force_poll: []\n  guarded: [\"ADC1.DR\"]"))
    return str(tdir)


def _guarded_force_polled_target(tmp_path):
    """Copy targets/f411 and overlay f411.flows.yaml so ADC1.DR is
    both guarded (readAction/overlay) and force-polled - the spec
    6.3c case where force_poll pulls a side-effecting register back
    onto the live watch list, which needs a persistent warning marker
    rather than reading as an ordinary watched row."""
    tdir = tmp_path / "t"
    shutil.copytree(TARGET, tdir)
    fl = tdir / "f411.flows.yaml"
    fl.write_text(fl.read_text().replace(
        "force_poll: []",
        "force_poll: [\"ADC1.DR\"]\n  guarded: [\"ADC1.DR\"]"))
    return str(tdir)


def _find_row(page, reg_key):
    for i in range(page.topLevelItemCount()):
        it = page.topLevelItem(i)
        data = it.data(0, Qt.UserRole)
        if data is not None and len(data) == 3 and data[0] == reg_key:
            return it
    return None


def test_show_block_watches_visible_registers(qtbot):
    engine = make_demo_engine("targets/f411")
    engine.start()
    try:
        page = RegisterPage(engine)
        qtbot.addWidget(page)
        page.show_block("dma2")
        assert page.topLevelItemCount() > 0
        # DMA2 registers went live via set_watch
        assert any(k.startswith("DMA2.") for k in engine.polled)
    finally:
        engine.stop()


def test_switching_block_replaces_watch_set(qtbot):
    engine = make_demo_engine("targets/f411")
    engine.start()
    try:
        page = RegisterPage(engine)
        qtbot.addWidget(page)
        page.show_block("dma2")
        page.show_block("tim1")
        assert any(k.startswith("TIM1.") for k in engine.polled)
    finally:
        engine.stop()


def test_guarded_double_click_confirm_reads(tmp_path, qtbot, monkeypatch):
    target_dir = _guarded_target(tmp_path)
    adapter = MockAdapter({})
    engine = Engine.load(target_dir, adapter, interval_s=0.01)
    dr_addr = engine.model.resolve("ADC1.DR").address
    adapter.set_word(dr_addr, 0x00000ABC)
    engine.start()
    try:
        page = RegisterPage(engine)
        qtbot.addWidget(page)
        page.show_block("adc1")

        item = _find_row(page, "ADC1.DR")
        assert item is not None
        reg_key, mode, addr = item.data(0, Qt.UserRole)
        assert mode == "guarded"
        assert addr == dr_addr
        assert "ADC1.DR" not in engine.polled   # confirms it's guarded

        monkeypatch.setattr(register_page_module.QMessageBox, "question",
                            staticmethod(lambda *a, **k:
                                        register_page_module.QMessageBox.Yes))
        page._on_double(item, 1)

        assert item.text(1).endswith("(forced)")
        assert "0x00000ABC" in item.text(1)
    finally:
        engine.stop()


def test_guarded_double_click_decline_no_read(tmp_path, qtbot, monkeypatch):
    target_dir = _guarded_target(tmp_path)
    adapter = MockAdapter({})
    engine = Engine.load(target_dir, adapter, interval_s=0.01)
    dr_addr = engine.model.resolve("ADC1.DR").address
    adapter.set_word(dr_addr, 0x00000ABC)
    engine.start()
    try:
        page = RegisterPage(engine)
        qtbot.addWidget(page)
        page.show_block("adc1")

        item = _find_row(page, "ADC1.DR")
        assert item is not None
        before = item.text(1)
        assert before == "[guarded]"

        monkeypatch.setattr(register_page_module.QMessageBox, "question",
                            staticmethod(lambda *a, **k:
                                        register_page_module.QMessageBox.No))
        page._on_double(item, 1)

        assert item.text(1) == before          # untouched
        assert "(forced)" not in item.text(1)
        # the address the forced read would have targeted was never
        # actually read - it's guarded, excluded from the poll plan,
        # and the user declined the one-shot read
        assert all(addr != dr_addr for addr, _count in adapter.read_log)
    finally:
        engine.stop()


def test_cold_read_failure_shows_read_failed(qtbot, monkeypatch):
    engine = make_demo_engine("targets/f411")
    engine.start()
    try:
        page = RegisterPage(engine)
        qtbot.addWidget(page)
        page.show_block("adc1")

        def _boom(*_a, **_k):
            raise EngineError("simulated read failure")
        monkeypatch.setattr(engine, "read_words", _boom)

        # hand-built cold-mode item: f411's SVD has no readAction
        # registers and its flowspec declares no guarded/force_poll
        # entries, so show_block() watches every non-guarded register
        # of a shown block (spec 6.2) and no row is naturally "cold" -
        # this exercises the UI branch directly, same technique used
        # for manual verification in the task-10 report.
        addr = engine.model.resolve("ADC1.SQR3").address
        item = QTreeWidgetItem(["SQR3", "--"])
        item.setData(0, Qt.UserRole, ("ADC1.SQR3", "cold", addr))

        page._on_double(item, 1)

        assert item.text(1) == "(read failed)"
    finally:
        engine.stop()


def test_force_polled_guarded_register_gets_warning_marker(tmp_path, qtbot):
    target_dir = _guarded_force_polled_target(tmp_path)
    adapter = MockAdapter({})
    engine = Engine.load(target_dir, adapter, interval_s=0.01)
    engine.start()
    try:
        page = RegisterPage(engine)
        qtbot.addWidget(page)
        page.show_block("adc1")

        item = _find_row(page, "ADC1.DR")
        assert item is not None
        assert item.text(0).startswith("[!]")
        reg_key, mode, _addr = item.data(0, Qt.UserRole)
        assert mode == "watched"          # force_poll put it back on the
                                           # live watch list, not guarded
        assert "ADC1.DR" in engine.polled
    finally:
        engine.stop()


def test_expanded_state_survives_double_click_on_guarded_row(tmp_path, qtbot, monkeypatch):
    """Double-click on a guarded row should not collapse its field children.
    RegisterPage.setExpandsOnDoubleClick(False) disables default tree expansion
    toggle, so double-click reads (a guarded row's intended use) don't
    accidentally collapse the tree."""
    target_dir = _guarded_target(tmp_path)
    adapter = MockAdapter({})
    engine = Engine.load(target_dir, adapter, interval_s=0.01)
    engine.start()
    try:
        page = RegisterPage(engine)
        qtbot.addWidget(page)
        page.show_block("adc1")

        item = _find_row(page, "ADC1.DR")
        assert item is not None
        # show_block calls expandAll, so tree should be expanded
        assert item.isExpanded()
        assert item.childCount() > 0

        # Monkeypatch to decline the guarded read without affecting expansion
        monkeypatch.setattr(register_page_module.QMessageBox, "question",
                            staticmethod(lambda *a, **k:
                                        register_page_module.QMessageBox.No))
        page._on_double(item, 0)

        # After double-click, the item should still be expanded
        assert item.isExpanded()
    finally:
        engine.stop()
