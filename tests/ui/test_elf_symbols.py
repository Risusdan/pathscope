import shutil
import subprocess

import pytest

from core.adapter.mock import MockAdapter
from core.engine.core import Engine
from core.trace.sim import FakeTraceFirmware
from ui.elf_symbols import load_symbols
from ui.panels.scope_page import ScopePage

FWDIR = "firmware/blackpill_adc_dma"
TARGET = "targets/f411"


def _make_demo_like_engine(period_us=5000):
    """A real engine + real FakeTraceFirmware wired like ui/demo.py's
    make_demo_engine() (engine.trace_desc_addr set) but WITHOUT its
    background animate thread, which never stops for the life of the
    test process - none of this file's tests need that thread's own
    simulated behavior, only the trace_desc_addr convenience, and one
    permanent daemon thread per make_demo_engine() call measurably
    slowed the whole scope test suite down (see
    tests/ui/test_scope_page.py's identical helper for the full
    writeup)."""
    adapter = MockAdapter({})
    engine = Engine.load(TARGET, adapter, interval_s=0.01)
    fw = FakeTraceFirmware(adapter, period_us=period_us)
    engine.trace_desc_addr = fw.desc_addr
    return engine


def _built_fw_elf():
    """Build the repo's real firmware ELF and return its path, or skip
    the test if no ARM toolchain is present (CI has none; the local
    dev box does) - pyelftools only reads ELFs, so the only realistic
    fixture is a real one."""
    if shutil.which("arm-none-eabi-gcc") is None:
        pytest.skip("no ARM toolchain")
    subprocess.run(["make"], cwd=FWDIR, check=True)
    return FWDIR + "/fw.elf"


def test_firmware_symbols_include_adc_buf():
    path = _built_fw_elf()
    syms = {s.name: s for s in load_symbols(path)}
    assert "adc_buf" in syms
    s = syms["adc_buf"]
    assert 0x20000000 <= s.addr < 0x20020000
    assert s.size == 2000


def test_symbols_sorted_by_name():
    path = _built_fw_elf()
    syms = load_symbols(path)
    names = [s.name for s in syms]
    assert names == sorted(names)


def test_symbols_include_both_global_and_static():
    """adc_buf is `static` (local binding) and main.c's `main`/`vectors`
    are global - the brief is explicit both bindings must appear, not
    just exported globals."""
    path = _built_fw_elf()
    names = {s.name for s in load_symbols(path)}
    assert "adc_buf" in names          # static (local binding)
    assert "vectors" in names          # global


def test_symbols_exclude_zero_size_and_non_object():
    """Section symbols, FILE symbols, and functions (dma_start, main)
    have size 0 or are not OBJECT-typed and must not appear."""
    path = _built_fw_elf()
    names = {s.name for s in load_symbols(path)}
    assert "main" not in names         # FUNC, not OBJECT
    assert "dma_start" not in names    # FUNC, not OBJECT
    assert "" not in names             # section/file symbols have no name


# -- ScopePage symbol picker (the ELF picker's second interface) -----------


def _list_texts(list_widget):
    return [list_widget.item(i).text() for i in range(list_widget.count())]


def test_load_elf_populates_symbol_list(qtbot):
    path = _built_fw_elf()
    engine = _make_demo_like_engine()
    engine.start()
    try:
        page = ScopePage(engine)
        qtbot.addWidget(page)
        page.load_elf(path)
        assert "adc_buf" in _list_texts(page.symbol_list)
        assert "vectors" in _list_texts(page.symbol_list)
        # M7: this firmware is ps_trace-instrumented, so load_elf()
        # now also finds its ps_trace_desc symbol and (correctly)
        # attempts discovery at that real-hardware address - which
        # the demo engine's simulated memory does not back, so
        # error_label legitimately carries a TraceError message here
        # rather than staying blank. The symbol list itself (this
        # test's actual point) is unaffected either way.
    finally:
        engine.stop()


def test_symbol_filter_narrows_list(qtbot):
    path = _built_fw_elf()
    engine = _make_demo_like_engine()
    engine.start()
    try:
        page = ScopePage(engine)
        qtbot.addWidget(page)
        page.load_elf(path)
        page.symbol_filter_edit.setText("adc")
        texts = _list_texts(page.symbol_list)
        assert texts == ["adc_buf"]
    finally:
        engine.stop()


def test_add_symbol_channel_uses_add_address_channel(qtbot, monkeypatch):
    """add_symbol_channel must ride the same add_address_slot path as
    a manual address add - not a separate code path. TraceReader.
    discover/set_watch are stubbed so this test exercises the REAL ELF
    symbol table (adc_buf's real addr/size, from a real toolchain
    build) against a deterministic trace target, independent of
    whether THIS firmware's own ps_trace_desc symbol happens to be
    backed by the demo engine's simulated memory (it isn't - see
    test_load_elf_populates_symbol_list's comment above)."""
    from core.trace.contract import MAX_CH, RING_COUNT, TraceDesc
    from core.trace.reader import TraceReader

    path = _built_fw_elf()
    desc = TraceDesc(endian="<", version=1, max_ch=MAX_CH, status=0,
                     period_us=5000, record_size=8 + 4 * MAX_CH,
                     ring_count=RING_COUNT, ring_addr=0x20001000, wr_seq=0,
                     watch_addrs=tuple([0] * MAX_CH), watch_count=0,
                     generation=0)
    monkeypatch.setattr(TraceReader, "discover", lambda self, addr: desc)
    monkeypatch.setattr(TraceReader, "set_watch", lambda self, addrs: None)

    engine = _make_demo_like_engine()
    engine.start()
    try:
        page = ScopePage(engine)
        qtbot.addWidget(page)
        page.load_elf(path)
        slot = page.add_symbol_channel("adc_buf")
        assert slot is not None
        entry = page.channel_slots()[slot]
        assert entry["label"] == "adc_buf"
        assert entry["addr"] == page.elf_symbols["adc_buf"].addr
    finally:
        engine.stop()


def test_load_elf_bad_path_shows_inline_error_not_dialog(qtbot):
    engine = _make_demo_like_engine()
    engine.start()
    try:
        page = ScopePage(engine)
        qtbot.addWidget(page)
        page.load_elf("/no/such/file.elf")
        assert page.error_label.text() != ""
        assert page.symbol_list.count() == 0
    finally:
        engine.stop()
