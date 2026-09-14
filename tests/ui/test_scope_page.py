import math
import struct
import time

import numpy as np
import pyqtgraph as pg
import pytest
from PySide6.QtCore import Qt

from core.adapter.mock import MockAdapter
from core.engine.core import Engine, EngineError
from core.engine.poller import PollerState
from core.trace.contract import MAX_CH, RING_COUNT, TraceDesc, TraceRecord
from core.trace.reader import READ_CAP_DIVISOR, TraceError, TraceReader
from core.trace.sim import FakeTraceFirmware
from ui.bridge import EngineBridge
from ui.demo import ADC_SR, S0CR
from ui.elf_symbols import Symbol
from ui.main_window import MainWindow
from ui.panels.scope_page import (COL_NAME, COL_SWATCH, COL_VALUE,
                                  CURVE_COLORS, DEFAULT_TYPE, DRAIN_MS_CAP,
                                  NO_SOURCE_TEXT, TYPES, ScopePage,
                                  _decode_series, _default_type_for_size,
                                  _drain_interval_ms, _fit_scale_offset,
                                  decode_value, format_value, value_at)

TARGET = "targets/f411"


def _make_plain_engine():
    """An Engine with no trace target at all - no engine.trace_desc_addr
    attribute (real engines don't have one; only ui/demo.py's
    make_demo_engine sets it) - and no poller thread ever started.
    Construction stays in NO_SOURCE and never calls engine.read_words,
    so these tests (channel-independent plumbing: crosshair, roll
    mode, event markers, the ELF picker) never need the poller
    running."""
    adapter = MockAdapter({})
    return Engine.load(TARGET, adapter, interval_s=0.01)


def _make_demo_like_engine(period_us=5000):
    """A REAL engine + REAL FakeTraceFirmware wired the same way
    ui/demo.py's make_demo_engine() wires one (engine.trace_desc_addr,
    engine._demo_trace_fw, and 0x20000000 pre-seeded in adapter.mem so
    the firmware whitelist's own "addr not in adapter.mem" check
    doesn't reject a channel watching it - see core/trace/sim.py's
    _validate()) - discover()/set_watch()/refresh() all work for real
    against it - but WITHOUT make_demo_engine()'s background animate
    thread, which ui/demo.py never stops (a daemon thread that keeps
    ticking every ~50 ms for the rest of the process, not just the
    test). None of this file's tests need that thread's own simulated
    behavior (the S0NDTR/ADC_SR/ADC_SAMPLE sine/fault-window
    animation) - they only ever used make_demo_engine() for the
    trace_desc_addr/_demo_trace_fw convenience - so every call site
    uses this instead, avoiding one more permanently-running thread
    per test."""
    adapter = MockAdapter({0x20000000: 0})
    engine = Engine.load(TARGET, adapter, interval_s=0.01)
    fw = FakeTraceFirmware(adapter, period_us=period_us)
    engine.trace_desc_addr = fw.desc_addr
    engine._demo_trace_fw = fw
    return engine


def _slot_tuple(slot, value, max_ch=MAX_CH):
    """A MAX_CH-length trace record slots tuple with `value` at `slot`
    and 0 everywhere else - the shape TraceRecord.slots always has
    (every record samples every watch-table entry simultaneously, even
    though only one channel's value matters to a given test)."""
    slots = [0] * max_ch
    slots[slot] = value
    return tuple(slots)


def _stub_desc(period_us=1_000_000, max_ch=MAX_CH):
    """A hand-built TraceDesc for tests that stub TraceReader entirely
    (see _make_stubbed_trace_page) - period_us defaults to 1 second/
    sample so a test can hand seq=0,1,2,... directly as whole seconds."""
    return TraceDesc(endian="<", version=1, max_ch=max_ch, status=0,
                     period_us=period_us, record_size=8 + 4 * max_ch,
                     ring_count=RING_COUNT, ring_addr=0x20001000, wr_seq=0,
                     watch_addrs=tuple([0] * max_ch), watch_count=0,
                     generation=0)


def _make_stubbed_trace_page(qtbot, monkeypatch, period_us=1_000_000):
    """A ScopePage wired to a fully deterministic, poller-free trace
    target: TraceReader.discover/set_watch/refresh are monkeypatched at
    the class level so add_address_slot()/add_symbol_channel()/
    add_register_channel() and refresh_plot() all work without any real
    hardware or the demo engine's background animate thread. Tests that
    only care about display/decode/UI behavior (not the watch-table
    wire protocol itself, covered separately by the real demo-engine
    tests below) use this to stay fast and free of any race with that
    animate thread - see test_stop_and_hidden_drain_keeps_reader_lost_
    from_growing's docstring for why calling engine._demo_trace_fw.
    step() concurrently with that thread would be unsafe."""
    desc = _stub_desc(period_us=period_us)
    monkeypatch.setattr(TraceReader, "discover", lambda self, addr: desc)
    monkeypatch.setattr(TraceReader, "set_watch", lambda self, addrs: None)
    monkeypatch.setattr(TraceReader, "refresh", lambda self: [])
    engine = _make_plain_engine()
    engine.trace_desc_addr = 0x20004000
    page = ScopePage(engine)
    qtbot.addWidget(page)
    return page


# -- page states & trace discovery (spec point 1, M7 rework) ---------------

def test_scope_shows_no_source_state_without_trace(qtbot):
    """Neither a demo engine.trace_desc_addr nor a loaded ELF symbol
    exists for a plain engine - the page starts, and stays, in
    NO_SOURCE."""
    engine = _make_plain_engine()
    page = ScopePage(engine)
    qtbot.addWidget(page)
    assert page.error_label.text() == NO_SOURCE_TEXT
    assert "ps_trace" in page.error_label.text()
    assert page.rate_label_text() == ""
    assert page.channel_table.rowCount() == MAX_CH


def test_scope_discovers_demo_trace_and_shows_rate(qtbot):
    """Demo mode (engine.trace_desc_addr, see ui/demo.py) discovers
    automatically at construction - reaching READY needs the poller
    thread actually running to service the descriptor read, so
    engine.start() runs first, same as any other test exercising a
    real read through the engine."""
    engine = _make_demo_like_engine()
    engine.start()
    try:
        page = ScopePage(engine)
        qtbot.addWidget(page)
        assert page.channel_table.rowCount() == MAX_CH
        assert "Hz (firmware)" in page.rate_label_text()
        assert page.error_label.text() == ""
        assert page.desc is not None
    finally:
        engine.stop()


def test_scope_shows_trace_error_state_from_bad_descriptor(qtbot, monkeypatch):
    """A TraceError from discover() (bad magic, unsupported version,
    bad geometry - core/trace/contract.py's ContractError, wrapped by
    TraceReader) renders its message verbatim in error_label, never a
    dialog. Monkeypatching TraceReader.discover directly (rather than
    pointing trace_desc_addr at genuinely empty memory) keeps this
    test independent of the poller thread entirely - discover() is
    never actually called through the engine."""
    def _boom(self, addr):
        raise TraceError("unsupported trace version 2")
    monkeypatch.setattr(TraceReader, "discover", _boom)

    engine = _make_demo_like_engine()     # has trace_desc_addr
    page = ScopePage(engine)
    qtbot.addWidget(page)
    assert page.error_label.text() == "unsupported trace version 2"
    assert page.desc is None
    assert page.rate_label_text() == ""
    assert page.channel_table.rowCount() == MAX_CH


def test_scope_discovery_survives_engine_error_inline(qtbot, monkeypatch):
    """The real-world trigger: hardware unplugged (or still
    connecting) when the user switches to the Scope tab - lazy
    construction (MainWindow._activate_scope_tab) must not crash.
    Underneath TraceReader.discover(), Engine.read_words() raises
    EngineError - not TraceError - when the poller thread isn't
    draining its command queue yet (not started), a command times
    out, or the target is lost mid-discovery; _discover_at() must
    catch that alongside TraceError, at the same boundary, rendering
    str(e) inline exactly like the TraceError state rather than
    letting it escape ScopePage.__init__/load_elf() uncaught.

    The literal real path - engine.trace_desc_addr set, engine.start()
    never called - reproduces this for real (Engine._exec's q.get()
    blocks for its full 2 s timeout, then raises EngineError("command
    timed out")), but paying that 2 s in this test would be slow;
    monkeypatching TraceReader.discover to raise EngineError directly
    exercises the same catch-and-render path deterministically and
    fast."""
    def _lost(self, addr):
        raise EngineError("command timed out")
    monkeypatch.setattr(TraceReader, "discover", _lost)

    engine = _make_demo_like_engine()     # has trace_desc_addr
    page = ScopePage(engine)              # must not raise
    qtbot.addWidget(page)
    assert page.error_label.text() == "command timed out"
    assert page.desc is None
    assert page.rate_label_text() == ""
    assert page.channel_table.rowCount() == MAX_CH


# -- fixed MAX_CH-row channel table skeleton (spec point 2) ----------------

def test_channel_table_is_fixed_max_ch_rows_with_placeholder_slots(qtbot):
    """The table ALWAYS has exactly MAX_CH rows (8 columns now - the
    old per-channel Hz column is gone, see the module docstring), one
    permanently-colored swatch per row regardless of the NO_SOURCE
    state this plain engine leaves the page in, and every slot starts
    as a grey, non-editable "-" placeholder."""
    engine = _make_plain_engine()
    page = ScopePage(engine)
    qtbot.addWidget(page)

    from PySide6.QtWidgets import QLabel

    assert page.channel_table.columnCount() == 8
    assert page.channel_table.rowCount() == MAX_CH
    assert len(CURVE_COLORS) == MAX_CH
    for row in range(MAX_CH):
        name_item = page.channel_table.item(row, COL_NAME)
        assert name_item.text() == "-"
        assert not (name_item.flags() & Qt.ItemIsEditable)
        swatch_box = page.channel_table.cellWidget(row, COL_SWATCH)
        assert swatch_box is not None
        swatch = swatch_box.findChild(QLabel)
        assert CURVE_COLORS[row] in swatch.styleSheet()


def test_channel_table_row_count_is_max_ch_in_every_page_state(qtbot):
    """Spec point: "ALWAYS exactly MAX_CH rows" - true in NO_SOURCE, in
    the TraceError state, and once READY alike."""
    no_source_page = ScopePage(_make_plain_engine())
    qtbot.addWidget(no_source_page)
    assert no_source_page.channel_table.rowCount() == MAX_CH

    engine = _make_demo_like_engine()
    engine.start()
    try:
        ready_page = ScopePage(engine)
        qtbot.addWidget(ready_page)
        assert ready_page.channel_table.rowCount() == MAX_CH
    finally:
        engine.stop()


def test_splitter_shows_all_slot_rows_by_default(qtbot):
    """Spec point 7: all MAX_CH rows visible without dragging the
    splitter - the top pane's height must be at least the table's own
    computed header+rows+frame height."""
    page = ScopePage(_make_plain_engine())
    qtbot.addWidget(page)
    table = page.channel_table
    needed = (table.horizontalHeader().height()
             + table.verticalHeader().length()
             + 2 * table.frameWidth())
    assert page.splitter.sizes()[0] >= needed


# -- channel slot assignment, refusals, watch protocol (spec points 2/3) ---

def test_add_address_slot_occupies_first_free_slot_in_order(qtbot, monkeypatch):
    page = _make_stubbed_trace_page(qtbot, monkeypatch)
    slot0 = page.add_address_slot(0x20000000, "a")
    slot1 = page.add_address_slot(0x20000004, "b")
    assert (slot0, slot1) == (0, 1)
    slots = page.channel_slots()
    assert slots[0]["label"] == "a"
    assert slots[1]["label"] == "b"
    assert all(s is None for s in slots[2:])
    assert page.error_label.text() == ""


def test_address_cell_uses_mono_font(qtbot, monkeypatch):
    """MUST-FIX m5: the Address column's font is MONO - lost in the T8
    rework (this module's own MONO constant went unused) and restored
    here."""
    from ui.panels.scope_page import COL_ADDR, MONO

    page = _make_stubbed_trace_page(qtbot, monkeypatch)
    page.add_address_slot(0x20000000, "a")
    addr_item = page.channel_table.item(0, COL_ADDR)
    assert addr_item.font().families() == MONO.families()


def test_add_address_slot_table_full(qtbot, monkeypatch):
    page = _make_stubbed_trace_page(qtbot, monkeypatch)
    for i in range(MAX_CH):
        slot = page.add_address_slot(0x20000000 + 4 * i, "c%d" % i)
        assert slot == i
    overflow = page.add_address_slot(0x20000000 + 4 * MAX_CH, "overflow")
    assert overflow is None
    assert page.error_label.text() == "table full"
    assert all(s is not None for s in page.channel_slots())


def test_add_address_slot_refuses_guarded_address_before_any_write(
        qtbot, monkeypatch):
    """spec point 3: reuses the same engine.guarded_addrs set
    register_page.py's show_block() reads off Engine.load - refusal
    happens before reader.set_watch() is ever called, so no partial
    slot is left behind."""
    page = _make_stubbed_trace_page(qtbot, monkeypatch)
    page.engine.guarded_addrs.add(0x20000000)
    slot = page.add_address_slot(0x20000000, "buf0")
    assert slot is None
    assert "guarded" in page.error_label.text()
    assert all(s is None for s in page.channel_slots())


def test_add_address_slot_refuses_misaligned_address(qtbot, monkeypatch):
    """spec point 3: firmware's own whitelist check does not check
    alignment (core/trace/sim.py's _validate()), so the host must."""
    page = _make_stubbed_trace_page(qtbot, monkeypatch)
    slot = page.add_address_slot(0x20000001, "buf0")
    assert slot is None
    assert "4-byte aligned" in page.error_label.text()
    assert all(s is None for s in page.channel_slots())


def test_add_register_channel_rejected_by_firmware_renders_inline(qtbot):
    """DMA2/ADC1 register addresses live in the 0x4... peripheral
    space, outside the demo trace target's SRAM-only whitelist
    (core/trace/sim.py's default (0x20000000, 0x20020000)) - firmware
    genuinely refuses the watch-table write over the REAL reader/sim,
    and that TraceError must render inline verbatim with the tentative
    slot rolled back (no half-added channel left behind)."""
    engine = _make_demo_like_engine()
    engine.start()
    try:
        page = ScopePage(engine)
        qtbot.addWidget(page)
        slot = page.add_register_channel("ADC1.SR")
        assert slot is None
        assert "firmware rejected table" in page.error_label.text()
        assert all(s is None for s in page.channel_slots())
    finally:
        engine.stop()


def test_remove_channel_compacts_table_and_rebinds_color(qtbot, monkeypatch):
    """Removing a middle slot shifts every later channel up one row -
    watch index/table row/record slot must stay in lockstep, so a
    shifted channel's curve is rebound to its new row's color (spec
    point 2: "palette stays bound to ROW index")."""
    page = _make_stubbed_trace_page(qtbot, monkeypatch)
    page.add_address_slot(0x20000000, "a")
    page.add_address_slot(0x20000004, "b")
    page.add_address_slot(0x20000008, "c")

    page.remove_channel(0)

    slots = page.channel_slots()
    assert slots[0]["label"] == "b"
    assert slots[1]["label"] == "c"
    assert slots[2] is None
    assert slots[0]["color"] == CURVE_COLORS[0]
    assert slots[1]["color"] == CURVE_COLORS[1]
    assert slots[0]["curve"].opts["pen"].color().name().lower() == \
        CURVE_COLORS[0].lower()


def test_remove_middle_slot_store_history_follows_compaction(qtbot, monkeypatch):
    """The bug the table-only test above cannot see: TraceStore columns
    are positional, independent of ScopePage._slots. Add A, B, C with
    DISTINCT data on real per-record slots (not just per-channel keys),
    remove the middle (B), and assert the SURVIVING channels' curves
    show only their OWN values afterward - not lengths, actual
    y-values - proving TraceStore.remove_slot's column shift actually
    ran alongside the table compaction."""
    page = _make_stubbed_trace_page(qtbot, monkeypatch, period_us=100_000)
    a = page.add_address_slot(0x20000000, "A")
    b = page.add_address_slot(0x20000004, "B")
    c = page.add_address_slot(0x20000008, "C")
    records = []
    for i in range(3):
        slots = [0] * MAX_CH
        slots[a], slots[b], slots[c] = 100 + i, 200 + i, 300 + i
        records.append(TraceRecord(seq=i, gen=0, slots=tuple(slots)))
    page.store.append(records)

    page.remove_channel(b)          # middle removal - C compacts to slot 1

    assert page.channel_slots()[0]["label"] == "A"
    assert page.channel_slots()[1]["label"] == "C"
    _t, y0 = page.store.series(0)
    _t, y1 = page.store.series(1)
    assert list(y0) == [100.0, 101.0, 102.0]     # A - untouched
    assert list(y1) == [300.0, 301.0, 302.0]     # C shifted in, not B's data
    # refresh_plot()'s own decode/redraw path must show the same thing.
    page.refresh_plot()
    assert page.curve_y(0) == [pytest.approx(v) for v in (100, 101, 102)]
    assert page.curve_y(1) == [pytest.approx(v) for v in (300, 301, 302)]


def test_add_reuses_freed_slot_without_stale_values(qtbot, monkeypatch):
    """After A is removed, B compacts from slot 1 into slot 0, freeing
    slot 1 - B's OLD physical column. The next channel added (D) lands
    there; its history must be clean, not B's leftover values (or
    plain unwatched-placeholder zeros predating D's own existence)."""
    page = _make_stubbed_trace_page(qtbot, monkeypatch, period_us=100_000)
    a = page.add_address_slot(0x20000000, "A")
    b = page.add_address_slot(0x20000004, "B")
    records = []
    for i in range(3):
        slots = [0] * MAX_CH
        slots[a], slots[b] = 10 + i, 20 + i
        records.append(TraceRecord(seq=i, gen=0, slots=tuple(slots)))
    page.store.append(records)

    page.remove_channel(a)
    assert page.channel_slots()[0]["label"] == "B"

    d = page.add_address_slot(0x20000008, "D")
    assert d == 1
    page.store.append([TraceRecord(seq=3, gen=0, slots=_slot_tuple(d, 999))])

    _t, y = page.store.series(d)
    real_values = [v for v in y.tolist() if v == v]      # drop NaN
    assert real_values == [999.0]


def test_set_watch_receives_compacted_address_list_after_middle_removal(
        qtbot, monkeypatch):
    """The exact ordered address list reader.set_watch() receives at
    every step of add-add-add-remove(middle)-add - watch index must
    stay the compacted table row throughout, not the original add
    order."""
    calls = []
    page = _make_stubbed_trace_page(qtbot, monkeypatch)
    monkeypatch.setattr(TraceReader, "set_watch",
                        lambda self, addrs: calls.append(list(addrs)))

    page.add_address_slot(0x20000000, "A")
    page.add_address_slot(0x20000004, "B")
    page.add_address_slot(0x20000008, "C")
    page.remove_channel(1)                       # remove B, the middle
    page.add_address_slot(0x2000000C, "D")

    assert calls == [
        [0x20000000],
        [0x20000000, 0x20000004],
        [0x20000000, 0x20000004, 0x20000008],
        [0x20000000, 0x20000008],
        [0x20000000, 0x20000008, 0x2000000C],
    ]


def test_curve_decodes_from_store_and_skips_nan_gap_rows(qtbot, monkeypatch):
    """spec point 4: curves decode display-side from the store's raw
    f64 (cast back to int for decode); a NaN gap row (TraceStore's own
    seq-gap detection, triggered here by jumping straight from seq=0
    to seq=5) stays NaN and is not decoded."""
    page = _make_stubbed_trace_page(qtbot, monkeypatch, period_us=100_000)
    slot = page.add_address_slot(0x20000000, "buf0")
    page.store.append([TraceRecord(seq=0, gen=0,
                                   slots=_slot_tuple(slot, 0xFFFF0000))])
    page.store.append([TraceRecord(seq=5, gen=0,
                                   slots=_slot_tuple(slot, 0x0000FFFF))])

    page.refresh_plot()

    ys = page.curve_y(slot)
    assert len(ys) == 3
    assert ys[0] == pytest.approx(0xFFFF0000)
    assert math.isnan(ys[1])
    assert ys[2] == pytest.approx(0x0000FFFF)


def test_decode_series_casts_to_int_and_skips_nan():
    y = np.array([4660.0, float("nan"), 4294967295.0])
    out = _decode_series(y, "u32")
    assert out[0] == pytest.approx(4660)
    assert math.isnan(out[1])
    assert out[2] == pytest.approx(4294967295)


def test_drain_timer_stays_active_when_hidden_or_stopped(qtbot):
    """spec point 5: the slow drain timer is independent of Run/Stop
    and page visibility - only the fast repaint timer pauses for
    those."""
    engine = _make_demo_like_engine()
    engine.start()
    try:
        page = ScopePage(engine)
        qtbot.addWidget(page)
        assert page._drain_timer.isActive()
        page.set_stopped(True)
        assert page._drain_timer.isActive()
        page.hide()
        assert page._drain_timer.isActive()
    finally:
        engine.stop()


def test_stop_and_hidden_drain_keeps_reader_lost_from_growing(qtbot):
    """spec point 5: Run/Stop and page visibility are DISPLAY-only -
    the ring (RING_COUNT records - contract.py) must never overflow
    just because nobody is looking. Simulates the always-on slow
    drain timer's periodic ticks by calling its handler (_drain_once) directly
    between step() bursts, each comfortably under RING_COUNT, so no
    single gap between drains ever exceeds the ring - this proves the
    PERIODIC cadence (not any single call) is what keeps the reader
    caught up, even while stopped, even though the cumulative total of
    stepped records (400) far exceeds the ring size.

    Uses an isolated FakeTraceFirmware (not ui/demo.py's
    animate-thread-driven one) so this test's own step() calls are the
    only writer - the demo module's animate thread calls step()
    concurrently on ITS OWN instance every ~50 ms, and driving that
    same instance from a second thread would be a genuine data race
    with no lock on either side (see core/trace/sim.py's own module
    docstring on the safety of concurrent READS, which does not extend
    to concurrent WRITES from two threads)."""
    adapter = MockAdapter({})
    engine = Engine.load(TARGET, adapter, interval_s=0.01)
    engine.start()
    try:
        fw = FakeTraceFirmware(adapter, period_us=1000)
        engine.trace_desc_addr = fw.desc_addr
        page = ScopePage(engine)
        qtbot.addWidget(page)
        assert page.desc is not None
        page.set_stopped(True)

        for _ in range(8):
            fw.step(50)
            page._drain_once()
        assert page.reader.lost == 0

        page.set_stopped(False)
        page.refresh_plot()
        assert page.reader.lost == 0
    finally:
        engine.stop()


def test_drain_surfaces_nonzero_status_inline(qtbot):
    """MUST-FIX m3 (spec 3.4): a nonzero desc.status observed during a
    drain tick must be surfaced inline, naming the code - previously
    only a set_watch() rejection (synchronously, from
    add_address_slot()'s own try/except) ever raised on a bad status.
    Triggers the rejection directly through reader.set_watch()
    (bypassing add_address_slot()'s own error_label write) so the ONLY
    thing that can have written error_label afterward is
    _drain_once()'s own status check. A subsequent valid table clears
    it again once status returns OK."""
    adapter = MockAdapter({0x20000000: 0})
    engine = Engine.load(TARGET, adapter, interval_s=0.01)
    engine.start()
    try:
        fw = FakeTraceFirmware(adapter, period_us=1000)
        engine.trace_desc_addr = fw.desc_addr
        page = ScopePage(engine)
        qtbot.addWidget(page)
        assert page.desc is not None

        with pytest.raises(TraceError):
            page.reader.set_watch([0x99999990])   # outside sim whitelist

        page._drain_once()
        assert "BAD_ADDR" in page.error_label.text()
        assert page._status_error_active

        page.reader.set_watch([0x20000000])       # a valid retry
        page._drain_once()
        assert page.error_label.text() == ""
        assert not page._status_error_active
    finally:
        engine.stop()


def test_target_reboot_resyncs_trace_session(qtbot):
    """T11 hardware gate: an interactive unplug/replug test found that,
    because the Blackpill reference target is powered by the debug
    probe's own USB connection, a replug is a REAL power cycle -
    firmware reboots (wr_seq/generation/watch_count all reset near 0)
    while this reader/page still carry session state (last_seq in the
    hundreds of thousands by the time this matters in practice, the
    occupied watch table) describing a target that no longer exists.
    Drives a REAL TARGET_LOST -> RUNNING transition through the poller
    (MockAdapter.fail_next() plus FakeTraceFirmware.reboot(), following
    tests/test_poller.py's own test_target_lost_and_reconnect pattern)
    rather than injecting a fake state string, so this exercises the
    real _on_engine_state callback wiring end to end - not just
    _recover_after_reboot() in isolation. Once RUNNING again, the next
    _drain_once() tick (standing in for a real timer tick) must notice
    the latch, re-discover, and re-submit the SAME channels in the SAME
    order against a store that restarts from empty - never silently
    freezing at whatever sample count it had at the moment of
    disconnect."""
    adapter = MockAdapter({0x20000000: 0, 0x20000004: 0})
    engine = Engine.load(TARGET, adapter, interval_s=0.01)
    engine._poller.reconnect_s = 0.05   # keep the test fast
    fw = FakeTraceFirmware(adapter, period_us=1000)
    engine.trace_desc_addr = fw.desc_addr
    states = []
    engine.on_state(states.append)
    engine.start()
    try:
        page = ScopePage(engine)
        qtbot.addWidget(page)
        assert page.desc is not None

        slot_a = page.add_address_slot(0x20000000, "chan_a", "u32")
        slot_b = page.add_address_slot(0x20000004, "chan_b", "u32")
        assert slot_a == 0 and slot_b == 1

        fw.step(20)
        page._drain_once()
        assert page.channel_sample_count(slot_a) > 0

        # Simulate the unplug: the very next adapter command fails,
        # tripping the poller's own TARGET_LOST detection and its
        # reconnect loop - the same trigger tests/test_poller.py's
        # test_target_lost_and_reconnect uses. The board is powered by
        # the probe, so the firmware side of a real unplug is a power
        # cycle too - modeled here by reboot() while disconnected,
        # exactly as it would happen on real hardware (the board loses
        # power before the probe itself reconnects).
        adapter.fail_next(1)
        qtbot.waitUntil(lambda: PollerState.TARGET_LOST in states,
                        timeout=2000)
        fw.reboot()
        qtbot.waitUntil(lambda: states[-1] == PollerState.RUNNING,
                        timeout=2000)

        # The tick that notices "running again after lost" - recovery,
        # not a normal drain.
        page._drain_once()

        assert page.error_label.text() == ""
        slots = page.channel_slots()
        assert slots[0] is not None
        assert slots[0]["label"] == "chan_a"
        assert slots[0]["addr"] == 0x20000000
        assert slots[1] is not None
        assert slots[1]["label"] == "chan_b"
        assert slots[1]["addr"] == 0x20000004
        assert slots[2] is None

        # The store restarted, not spliced onto the old (huge) seq axis.
        assert page.channel_sample_count(0) == 0

        # Streaming actually resumes against the rebooted target - not
        # just "channels look occupied".
        fw.step(10)
        page._drain_once()
        assert page.channel_sample_count(0) > 0
    finally:
        engine.stop()


def test_slow_drain_interval_derived_from_real_firmware_ring_span(qtbot):
    """Hardware finding (Task 10 E2E suite): a fixed 500ms slow-drain
    interval is only safe for firmware slow enough that 500ms sits
    under the trace ring's own span (RING_COUNT * period_us) - a real
    board sampling at 1 kHz (period_us=1000) with the ORIGINAL
    RING_COUNT=256 (a 256ms span) was not, and would lose data on
    every stopped tick under the old fixed interval. _discover_at()
    must derive the slow timer's interval from the descriptor actually
    discovered rather than use a constant.

    T11 hardware gate, fix round 2: bumping RING_COUNT to 1024 made
    half the ring's own span (512ms) exceed DRAIN_MS_CAP, which
    _drain_interval_ms's fix round 1 form would have read as "the cap
    binds, 500ms" - but TraceReader.refresh() itself never drains more
    than ring_count // READ_CAP_DIVISOR (256) records per call, and a
    500ms interval at this 1 kHz rate lets ~500 records accumulate per
    tick: more than one refresh() call can ever drain, a backlog that
    compounds and silently reopens TraceReader.lost growth during a
    long enough Stop hold (the round-1 hw test's ~1s Stop window was
    too short to expose it - see tests/hw/test_trace_hw.py's now-
    lengthened one). _drain_interval_ms now also bounds the interval to
    DRAIN_CAP_SLACK of how long firmware takes to PRODUCE one cap's
    worth of records (256 * 1ms * 0.8 = 204.8ms here) - the tighter of
    the two real constraints, landing this real-firmware case at 204ms.
    Inspects the timer directly, no sleeps: discovery (and the interval
    it sets) happens synchronously in ScopePage.__init__ once
    engine.start() has the poller thread up to service it."""
    engine = _make_demo_like_engine(period_us=1000)
    engine.start()
    try:
        page = ScopePage(engine)
        qtbot.addWidget(page)
        assert page.desc is not None
        assert page.desc.period_us == 1000
        expected = _drain_interval_ms(page.desc.ring_count, 1000)
        assert expected == 204
        assert page._drain_timer.interval() == 204

        # The invariant fix round 2 exists to preserve: what a single
        # drain interval lets accumulate must stay under what one
        # refresh() call can actually drain.
        cap_records = page.desc.ring_count // READ_CAP_DIVISOR
        assert expected * 1000 / page.desc.period_us < cap_records
    finally:
        engine.stop()


def test_drain_interval_ms_lands_between_floor_and_cap():
    """Pure-function coverage proving _drain_interval_ms actually
    computes a derived value rather than just picking DRAIN_MS_FLOOR or
    DRAIN_MS_CAP. T11 hardware gate, fix round 2: with
    READ_CAP_DIVISOR=4 and DRAIN_CAP_SLACK=0.8, the per-call-read-cap
    span (ring_count // 4 * period_us / 1000 * 0.8, which works out to
    0.2 * the ring's own span) is ALWAYS tighter than half the ring's
    own span (0.5 * the span) - so the cap constraint is what lands
    period_us=400 strictly between the two bounds here
    (cap_records=1024//4=256, 256 * 400us/1000 * 0.8 = 81.92ms), not
    the ring-span constraint (1024 * 400us/1000 / 2 = 204.8ms) that
    used to bind this same case pre-round-2."""
    result = _drain_interval_ms(RING_COUNT, 400)
    assert result == 81
    assert 50 < result < DRAIN_MS_CAP

    cap_records = RING_COUNT // READ_CAP_DIVISOR
    assert result * 1000 / 400 < cap_records


def test_slow_drain_interval_caps_at_default_for_slower_firmware(qtbot):
    """The other half of the same fix: a firmware slow enough that
    BOTH half its ring span AND its own per-call-read-cap span (T11
    hardware gate, fix round 2 - see _drain_interval_ms's docstring)
    would exceed DRAIN_MS_CAP (500ms) still drains at that cap, not
    slower - the demo target's own default (period_us=5000, a 5.12s
    ring span at RING_COUNT=1024, a 1.024s per-call-read-cap span) is
    exactly this case, and was already safe under the old fixed 500ms
    constant; this pins that the cap - not an ever-growing interval -
    is what binds for it."""
    engine = _make_demo_like_engine()   # default period_us=5000
    engine.start()
    try:
        page = ScopePage(engine)
        qtbot.addWidget(page)
        assert page.desc is not None
        assert page.desc.period_us == 5000
        expected = _drain_interval_ms(page.desc.ring_count, 5000)
        assert expected == DRAIN_MS_CAP == 500
        assert page._drain_timer.interval() == 500

        # This IS the "slow firmware where the 500 cap still binds"
        # case - the invariant still holds even though DRAIN_MS_CAP,
        # not the cap span itself, is what's actually binding here.
        cap_records = page.desc.ring_count // READ_CAP_DIVISOR
        assert expected * 1000 / page.desc.period_us < cap_records
    finally:
        engine.stop()


# -- v2 (M6): channel/plot behavior, adapted to the M7 trace path -----------

def test_scope_plots_trace_channel(qtbot):
    """Channels ride the trace path (M7): add_address_slot()/
    engine._demo_trace_fw.step() replace the M6-era add_channel()/
    wait-for-the-poller pattern that plotted a polled register via
    History. Uses the isolated (non-animate-thread)
    _make_demo_like_engine() so this test's own deterministic step()
    call is the only writer."""
    engine = _make_demo_like_engine()
    engine.start()
    try:
        page = ScopePage(engine)
        qtbot.addWidget(page)
        slot = page.add_address_slot(0x20000000, "adc_sample", "u16.lo")
        assert slot is not None
        engine._demo_trace_fw.step(5)
        page.refresh_plot()
        assert page.curve_point_count(slot) >= 5
    finally:
        engine.stop()


def test_scope_addr_channel_via_engine(qtbot):
    engine = _make_demo_like_engine()
    engine.start()
    try:
        page = ScopePage(engine)
        qtbot.addWidget(page)
        slot = page.add_address_slot(0x20000000, "buf0")
        assert slot == 0
        engine._demo_trace_fw.step(3)
        page.refresh_plot()
        assert page.channel_sample_count(slot) >= 3
    finally:
        engine.stop()


def test_add_address_channel_relabels_existing(qtbot, monkeypatch):
    """M7 channels are slot-based (slot i == trace watch index == table
    row), not deduped by address the way this test used to exercise via
    engine.add_addr_watch() before the M7 rework (add_addr_watch itself
    is still kept as public Engine API, just no longer used by this
    panel) - re-adding the same address just occupies a second,
    independent slot; renaming an existing slot in place is the Name
    column's job (see
    test_rename_channel_via_table_updates_label_and_legend)."""
    page = _make_stubbed_trace_page(qtbot, monkeypatch)
    slot1 = page.add_address_slot(0x20000000, "first")
    slot2 = page.add_address_slot(0x20000000, "second")

    assert slot1 != slot2
    assert page.channel_slots()[slot1]["label"] == "first"
    assert page.channel_slots()[slot2]["label"] == "second"
    assert page.channel_table.item(slot1, COL_NAME).text() == "first"
    assert page.channel_table.item(slot2, COL_NAME).text() == "second"


def test_event_marker_hard_cap_guards_unbounded_growth(qtbot):
    """add_event_marker's list must never grow past MARKER_HARD_CAP
    even when nothing is calling refresh_plot()'s window-based
    _prune_markers (dock hidden or stopped) - the primary pruning
    mechanism, which this cap only backstops. Channel-independent
    (event markers survive the M7 rework verbatim), so a plain engine
    is enough."""
    page = ScopePage(_make_plain_engine())
    qtbot.addWidget(page)
    for i in range(250):
        page.add_event_marker(float(i), "evt %d" % i)
    assert len(page._markers) <= 200


def test_repaint_timer_stops_when_hidden(qtbot):
    """Same bug class as tests/ui/test_memory_page.py's
    test_auto_refresh_timer_stops_when_hidden: a hidden ScopePage
    (tabbed away behind Event log) must not keep repainting every
    200 ms forever. The FAST repaint timer starts/stops on show/hide
    unconditionally, independent of trace discovery state, so a plain
    (NO_SOURCE) engine is enough - no poller needed."""
    page = ScopePage(_make_plain_engine())
    qtbot.addWidget(page)
    page.show()
    assert page._timer.isActive()
    page.hide()
    assert not page._timer.isActive()
    page.show()
    assert page._timer.isActive()


def test_plot_theme_is_light(qtbot):
    """pathscope is light-theme only by design - pyqtgraph's own
    defaults (black background, grey-on-black foreground) must be
    overridden before any PlotWidget is constructed. Module-level
    config, independent of trace discovery state."""
    page = ScopePage(_make_plain_engine())
    qtbot.addWidget(page)
    assert pg.getConfigOption("background") == "w"
    assert pg.getConfigOption("foreground") == "k"


def _make_overrun_engine():
    """Same seeded-OVR helper pattern as
    tests/ui/test_flow_and_log.py's _make_overrun_engine: the ADC
    overrun anomaly (f411.flows.yaml's "ADC1.SR.OVR == 1" rule) is
    already firing on the very first sweep, so this test doesn't have
    to wait out the demo's normal-streaming window. Not demo mode (no
    trace_desc_addr attribute), so ScopePage construction here stays
    in NO_SOURCE and never touches the poller for a trace read."""
    mem = {S0CR: 1, ADC_SR: 0x30}          # OVR bit set
    adapter = MockAdapter(mem)
    return Engine.load(TARGET, adapter, interval_s=0.01)


def _has_anomaly_row(win):
    return any("ANOMALY" in win.event_log.item(i).text()
              for i in range(win.event_log.count()))


def test_log_click_moves_scope_cursor_to_event_time(qtbot):
    """Clicking an event-log anomaly row must move
    the scope cursor to that event's timestamp (t, in the same
    time.monotonic domain as AnomalyEvent.t/ScopePage._t0), but
    clicking a plain info row (no event time in its payload) must
    leave the cursor untouched."""
    engine = _make_overrun_engine()
    bridge = EngineBridge(engine)
    captured_events = []
    bridge.update.connect(lambda u: captured_events.extend(u.events))
    win = MainWindow(engine, bridge)
    qtbot.addWidget(win)
    win.show()
    win.tabs.setCurrentIndex(1)   # activate the Scope tab
    engine.start()
    try:
        qtbot.waitUntil(lambda: _has_anomaly_row(win), timeout=4000)
        assert captured_events, "expected the OVR anomaly to have fired"
        expected_t = captured_events[0].t

        anomaly_row = next(i for i in range(win.event_log.count())
                           if "ANOMALY" in win.event_log.item(i).text())
        win.event_log._clicked(win.event_log.item(anomaly_row))

        cursor_t = win.scope_page.cursor_time()
        assert cursor_t is not None
        assert abs(cursor_t - expected_t) < 1e-6

        # a plain info row's payload carries no event time - clicking
        # it must not move the cursor already placed above.
        win.event_log.add_info("poller: running")
        info_row = win.event_log.count() - 1
        win.event_log._clicked(win.event_log.item(info_row))
        assert win.scope_page.cursor_time() == cursor_t
    finally:
        engine.stop()


# -- scope UX upgrade (crosshair readout + per-channel scaling) ------------

def test_value_at():
    """Pure helper: newest sample with sample_t <= t, sorted-ascending
    series."""
    series = [(0, 10), (1, 20), (2, 30)]
    assert value_at(series, 1.5) == 20
    assert value_at(series, 0) == 10
    assert value_at(series, -1) is None
    assert value_at([], 5) is None


def test_crosshair_readout_shows_raw_values(qtbot, monkeypatch):
    """_update_crosshair(view_t) takes plot-relative seconds (the same
    domain mapSceneToView would hand it - roll mode: sample_t -
    self._last_now, the last refresh's now) and must update both the
    channel row's Value cell and the time label - reading only the
    per-slot series cached by the prior refresh_plot(). store.append()
    feeds synthetic TraceRecords directly (M7: replaces the old
    History.record() call) so sample timestamps are fully controlled
    and the test needs no polling wait."""
    page = _make_stubbed_trace_page(qtbot, monkeypatch, period_us=1_000_000)
    slot = page.add_address_slot(0x20000000, "buf0")
    records = [TraceRecord(seq=i, gen=0, slots=_slot_tuple(slot, v))
              for i, v in enumerate([100, 200, 300])]
    page.store.append(records)
    page.refresh_plot()

    # 1.5s: comfortably between seq=1 (t=1.0s) and seq=2 (t=2.0s) -
    # not exactly ON a sample boundary, since the float round-trip
    # through page._last_now (subtract here, re-add in
    # _value_text_for) is not guaranteed bit-exact for an arbitrary
    # time.monotonic() magnitude, and value_at()'s "<=" comparison
    # would be one-ULP-fragile against a sample sitting exactly at the
    # boundary.
    view_t = 1.5 - page._last_now
    page._update_crosshair(view_t)

    value_text = page.channel_slots()[slot]["value_item"].text()
    assert value_text == format_value(200, DEFAULT_TYPE)
    assert page.time_label.text() == "t-now = %.2f s" % view_t


def test_scale_offset_transforms_curve(qtbot, monkeypatch):
    """set_channel_transform() is the programmatic surface the table's
    scale/offset text fields drive; the redraw must apply y' =
    (y-offset) * scale when building the curve's data."""
    page = _make_stubbed_trace_page(qtbot, monkeypatch, period_us=100_000)
    slot = page.add_address_slot(0x20000000, "buf0")
    values = [100, 200, 300, 400]
    records = [TraceRecord(seq=i, gen=0, slots=_slot_tuple(slot, v))
              for i, v in enumerate(values)]
    page.store.append(records)

    page.set_channel_transform(slot, scale=2.0, offset=5.0)
    page.refresh_plot()

    ys = page.curve_y(slot)
    expected = [(v - 5.0) * 2.0 for v in values]
    assert len(ys) == len(expected)
    for e, a in zip(expected, ys):
        assert a == pytest.approx(e)

    # the Value column always shows the raw decoded value, never the
    # scaled display curve - the whole point of the readout. 0.15s:
    # comfortably between seq=1 (t=0.1s) and seq=2 (t=0.2s), not
    # exactly on a sample boundary - see the identical note in
    # test_crosshair_readout_shows_raw_values.
    page._update_crosshair(0.15 - page._last_now)
    value_text = page.channel_slots()[slot]["value_item"].text()
    assert value_text == format_value(200, DEFAULT_TYPE)


def test_crosshair_and_cursor_excluded_from_autorange(qtbot):
    """Hardware-session regression: the crosshair (and, identically,
    jump_to()'s cursor) must never itself contribute to the plot's
    auto-range. Two symptoms from the same root cause -
    ViewBox.childrenBounds() (which drives auto-range) includes every
    added item by default, unless addItem() was called with
    ignoreBounds=True:

    1. Stale pin - a crosshair/cursor left parked at an old x (mouse
       moved away, or an old event's t) permanently stretches the
       view's range to include that x even as live data scrolls past
       it, while History's own window keeps the real data span fixed.
    2. Live jitter - since every single sigMouseMoved repositions the
       crosshair line, if that line counted toward bounds then merely
       moving the mouse (no clicks) would recompute and visibly
       rescale/jitter the view on every event.

    This is crosshair/cursor plumbing, independent of any channel
    data (a plain engine, no channels, is enough) - asserting the
    ViewBox's childrenBoundingRect() is byte-for-byte unchanged after
    placing a crosshair (or cursor) far outside the origin - and
    unchanged again after moving it further still - demonstrates both:
    an included item would have moved the rect's edge to track it, so
    an unmoved rect proves the item is excluded from bounds, not
    merely that its effect happens to be small."""
    page = ScopePage(_make_plain_engine())
    qtbot.addWidget(page)
    page.refresh_plot()

    baseline = page.plot.vb.childrenBoundingRect()

    # crosshair: placing it far outside the data's span, then moving
    # it again to a different far point, must not move the bounds at
    # all - not "not much", not at all.
    page._update_crosshair(1000.0)
    after_first_move = page.plot.vb.childrenBoundingRect()
    assert after_first_move == baseline

    page._update_crosshair(5000.0)
    after_second_move = page.plot.vb.childrenBoundingRect()
    assert after_second_move == baseline

    # cursor (jump_to): same exclusion, same reasoning.
    page.jump_to(page._t0 + 9000.0)
    after_cursor = page.plot.vb.childrenBoundingRect()
    assert after_cursor == baseline


def test_per_row_minus_button_removes_channel(qtbot, monkeypatch):
    """Each channel row's leftmost "-" button removes THAT channel
    directly - no selection step, no separate Remove button (which
    doesn't exist)."""
    page = _make_stubbed_trace_page(qtbot, monkeypatch)
    page.add_address_slot(0x20000000, "chan0")
    page.add_address_slot(0x20000004, "chan1")
    assert not hasattr(page, "remove_btn")

    minus_box = page.channel_table.cellWidget(0, 0)
    from PySide6.QtWidgets import QPushButton
    minus_btn = minus_box.findChild(QPushButton)
    minus_btn.click()

    slots = page.channel_slots()
    assert slots[0]["label"] == "chan1"    # compacted into row 0
    assert slots[1] is None


def test_roll_mode_viewport_fixed(qtbot):
    """Roll mode (standard-scope style): the viewport must be pinned
    to a fixed (-window_s, 0) x-range on every refresh - identical
    across refreshes, never sliding to track wall-clock time - and an
    event marker (which stores an ABSOLUTE t) must reposition on every
    refresh by exactly the elapsed real-time delta between refreshes,
    scrolling left with its moment in history rather than sitting
    still. Channel-independent (roll mode/event markers survive the
    M7 rework verbatim), so a plain engine is enough."""
    engine = _make_plain_engine()
    page = ScopePage(engine)
    qtbot.addWidget(page)
    window_s = engine.history.window_s

    page.refresh_plot()
    now1 = page._last_now

    (x_min1, x_max1), _y_range1 = page.plot.vb.viewRange()
    # padding=0.01 headroom on each side of the (-window_s, 0) range -
    # not exactly those two numbers, but close, and identical between
    # the two refreshes below (the "not sliding" requirement).
    assert x_min1 == pytest.approx(-window_s, abs=window_s * 0.02)
    assert x_max1 == pytest.approx(0.0, abs=window_s * 0.02)

    marker_t = time.monotonic()
    page.add_event_marker(marker_t, "evt")
    _stored_t, line = page._markers[-1]
    pos_before = line.value()
    assert pos_before == pytest.approx(marker_t - now1)

    time.sleep(0.05)
    page.refresh_plot()
    now2 = page._last_now
    assert now2 > now1

    (x_min2, x_max2), _y_range2 = page.plot.vb.viewRange()
    assert x_min2 == pytest.approx(-window_s, abs=window_s * 0.02)
    assert x_max2 == pytest.approx(0.0, abs=window_s * 0.02)
    # the core "fixed viewport" assertion: identical, not sliding.
    assert x_min2 == pytest.approx(x_min1, abs=1e-9)
    assert x_max2 == pytest.approx(x_max1, abs=1e-9)

    pos_after = line.value()
    assert pos_after == pytest.approx(marker_t - now2)
    # the marker scrolled left (more negative) by exactly the elapsed
    # real-time delta between the two refreshes.
    assert (pos_before - pos_after) == pytest.approx(now2 - now1, abs=1e-6)


# -- v2: channel table, type decode, inline editing (spec points 2,3,6) ----

def test_curve_colors_has_ten_distinct_entries_not_sharing_marker_or_cursor():
    """Palette (user-approved, 10 entries): all distinct, and none may
    collide with MARKER_PEN (event markers) or CURSOR_PEN (the
    jump_to() cursor line) - a curve must never be mistaken for
    either of the plot's other line kinds."""
    from ui.panels.scope_page import CURSOR_PEN, MARKER_PEN
    assert len(CURVE_COLORS) == 10
    assert len(set(CURVE_COLORS)) == 10
    assert MARKER_PEN not in CURVE_COLORS
    assert CURSOR_PEN not in CURVE_COLORS


@pytest.mark.parametrize("raw,type_name,expected_decoded,expected_text", [
    (0x1234, "u32", 0x1234, "4660 (0x00001234)"),
    (0xFFFFFFFF, "i32", -1, "-1 (0xFFFFFFFF)"),
    (0xABCD1234, "u16.lo", 0x1234, "4660 (0x1234)"),
    (0xABCD1234, "u16.hi", 0xABCD, "43981 (0xABCD)"),
    (0x0000FFFF, "i16.lo", -1, "-1 (0xFFFF)"),
    (0xFFFF0000, "i16.hi", -1, "-1 (0xFFFF)"),
    (0x123456AB, "u8.0", 0xAB, "171 (0xAB)"),
    (0x12345678, "u8.3", 0x12, "18 (0x12)"),
])
def test_decode_and_format_integer_types(raw, type_name, expected_decoded,
                                         expected_text):
    decoded = decode_value(raw, type_name)
    assert decoded == expected_decoded
    assert format_value(decoded, type_name) == expected_text


def test_decode_and_format_f32_shows_float_only():
    # 1.5 as IEEE-754 little-endian bits.
    raw = int.from_bytes(struct.pack("<f", 1.5), "little")
    decoded = decode_value(raw, "f32")
    assert decoded == pytest.approx(1.5)
    text = format_value(decoded, "f32")
    assert text == "1.5"
    assert "0x" not in text


def test_default_type_for_size():
    assert _default_type_for_size(1) == "u8.0"
    assert _default_type_for_size(2) == "u16.lo"
    assert _default_type_for_size(4) == "u32"
    assert _default_type_for_size(16) == "u32"


def test_type_change_updates_curve_and_value_column(qtbot, monkeypatch):
    """Changing a row's type combo re-decodes both the plotted curve
    and the Value column readout - display-side only, the store still
    keeps the plain raw f64 word."""
    page = _make_stubbed_trace_page(qtbot, monkeypatch, period_us=100_000)
    slot = page.add_address_slot(0x20000000, "buf0")
    page.store.append([TraceRecord(seq=0, gen=0,
                                   slots=_slot_tuple(slot, 0xFFFFFFFF))])
    page.refresh_plot()

    assert page.curve_y(slot) == [pytest.approx(0xFFFFFFFF)]

    page.channel_slots()[slot]["type_combo"].setCurrentText("i32")
    assert page.channel_slots()[slot]["type"] == "i32"
    assert page.curve_y(slot) == [pytest.approx(-1)]

    page._update_crosshair(0.0 - page._last_now)
    assert page.channel_slots()[slot]["value_item"].text() == \
        "-1 (0xFFFFFFFF)"


def test_scale_offset_text_field_commit_and_invalid_revert(qtbot, monkeypatch):
    """Scale/offset cells are plain text fields (spec point 2), not
    spinboxes - Enter commits a valid numeric entry (including
    scientific notation), and an invalid entry reverts to the last
    good value and flashes the field's background."""
    page = _make_stubbed_trace_page(qtbot, monkeypatch)
    slot = page.add_address_slot(0x20000000, "buf0")
    entry = page.channel_slots()[slot]

    entry["scale_edit"].setText("2.5e1")
    entry["scale_edit"].returnPressed.emit()
    assert entry["transform"]["scale"] == pytest.approx(25.0)
    assert "FFCDD2" not in entry["scale_edit"].styleSheet()

    entry["scale_edit"].setText("not-a-number")
    entry["scale_edit"].returnPressed.emit()
    assert entry["transform"]["scale"] == pytest.approx(25.0)
    assert "FFCDD2" in entry["scale_edit"].styleSheet()
    assert entry["scale_edit"].text() == "%g" % 25.0


def test_add_symbol_channel_preselects_type_by_size(qtbot, monkeypatch):
    """ELF preselect (spec point 3): a channel added from a loaded
    symbol starts at a type chosen from the symbol's declared size,
    unsigned default - 1 byte -> u8.0, 2 bytes -> u16.lo, anything
    else -> u32."""
    page = _make_stubbed_trace_page(qtbot, monkeypatch)
    page.elf_symbols = {
        "byte_flag": Symbol(name="byte_flag", addr=0x20000100, size=1),
        "half_word": Symbol(name="half_word", addr=0x20000200, size=2),
        "full_word": Symbol(name="full_word", addr=0x20000300, size=4),
    }
    k1 = page.add_symbol_channel("byte_flag")
    k2 = page.add_symbol_channel("half_word")
    k3 = page.add_symbol_channel("full_word")
    assert page.channel_slots()[k1]["type"] == "u8.0"
    assert page.channel_slots()[k2]["type"] == "u16.lo"
    assert page.channel_slots()[k3]["type"] == "u32"


def test_elf_symbol_section_collapsed_by_default_and_auto_expands(
        qtbot, monkeypatch):
    """Add-channel area is compact (spec point 8): the ELF symbol
    picker starts collapsed, and load_elf() auto-expands it once
    symbols actually land - no reason to show an empty list before
    anything is loaded, but no reason to hide it once something is.
    The ELF group survives the M7 rework as the page's primary
    add-source control, so this stays a real (not skipped) test - a
    plain engine is enough since it doesn't need discovery to
    succeed."""
    page = ScopePage(_make_plain_engine())
    qtbot.addWidget(page)
    # isVisibleTo(page), not isVisible(): the test never shows the
    # page on screen, and plain isVisible() always reads False for an
    # unmapped widget regardless of setVisible() calls.
    assert not page.elf_content.isVisibleTo(page)

    import ui.elf_symbols as elf_symbols_mod
    monkeypatch.setattr(
        elf_symbols_mod, "load_symbols",
        lambda path: [Symbol(name="adc_buf", addr=0x20000400, size=64)])

    page.load_elf("fake.elf")

    assert page.elf_content.isVisibleTo(page)
    assert page.elf_toggle_btn.isChecked()
    assert page.symbol_list.count() == 1


def test_rename_channel_via_table_updates_label_and_legend(qtbot, monkeypatch):
    """Name is inline-editable too (spec point 2: "ALL
    inline-editable") - committing an edit to the Name cell updates
    the channel's stored label, the curve's legend entry, and (an
    empty name is rejected, reverting to the previous label)."""
    page = _make_stubbed_trace_page(qtbot, monkeypatch)
    slot = page.add_address_slot(0x20000000, "buf0")
    entry = page.channel_slots()[slot]

    page.channel_table.item(slot, COL_NAME).setText("ndtr")
    assert entry["label"] == "ndtr"
    assert entry["curve"].opts["name"] == "ndtr"
    legend_label = page.plot.legend.getLabel(entry["curve"])
    assert legend_label.text == "ndtr"

    page.channel_table.item(slot, COL_NAME).setText("   ")
    assert entry["label"] == "ndtr"
    assert page.channel_table.item(slot, COL_NAME).text() == "ndtr"


# -- v2: Fit / Auto-lane replace Normalize, y-axis follows selection -------
# (spec points 4, 7)

def test_fit_scale_offset_maps_span_into_band():
    scale, offset = _fit_scale_offset(0.0, 100.0, 0.0, 1.0)
    assert (0.0 - offset) * scale == pytest.approx(0.0)
    assert (100.0 - offset) * scale == pytest.approx(1.0)

    scale, offset = _fit_scale_offset(0.0, 100.0, 0.5, 1.0)
    assert (0.0 - offset) * scale == pytest.approx(0.5)
    assert (100.0 - offset) * scale == pytest.approx(1.0)


def test_fit_scale_offset_flat_window_centers_on_band():
    """hi <= lo (no span to map, including "no data at all" -> (0,0))
    must not divide by zero - centers every value on the band's own
    midpoint instead."""
    scale, offset = _fit_scale_offset(5.0, 5.0, 0.0, 1.0)
    assert (5.0 - offset) * scale == pytest.approx(0.5)

    scale, offset = _fit_scale_offset(0.0, 0.0, 0.25, 0.75)
    assert (0.0 - offset) * scale == pytest.approx(0.5)


def test_auto_lane_stacks_every_channel_into_own_lane(qtbot, monkeypatch):
    """Auto-lane (spec point 4, simplified - the per-row Fill/Own Fit
    column is gone): every occupied channel gets one equal band of the
    [0, 1] view, in slot order, its own window min..max mapped into
    just its band."""
    page = _make_stubbed_trace_page(qtbot, monkeypatch, period_us=100_000)
    slot1 = page.add_address_slot(0x20000000, "chan1")
    slot2 = page.add_address_slot(0x20000004, "chan2")

    v1 = [100, 200, 300]
    v2 = [1000, 2000, 2000]
    records = []
    for i in range(3):
        slots = [0] * MAX_CH
        slots[slot1] = v1[i]
        slots[slot2] = v2[i]
        records.append(TraceRecord(seq=i, gen=0, slots=tuple(slots)))
    page.store.append(records)

    page.auto_lane_check.setChecked(True)
    page.refresh_plot()

    ys1 = page.curve_y(slot1)
    assert min(ys1) == pytest.approx(0.0)
    assert max(ys1) == pytest.approx(0.5)

    ys2 = page.curve_y(slot2)
    assert min(ys2) == pytest.approx(0.5)
    assert max(ys2) == pytest.approx(1.0)


def test_auto_lane_off_leaves_last_computed_values_editable(qtbot, monkeypatch):
    """Turning Auto-lane off stops the recompute but does not reset
    scale/offset - they stay exactly as last computed, still plain
    editable fields the user can hand-tune from there."""
    page = _make_stubbed_trace_page(qtbot, monkeypatch, period_us=100_000)
    slot = page.add_address_slot(0x20000000, "buf0")
    page.store.append([
        TraceRecord(seq=0, gen=0, slots=_slot_tuple(slot, 100)),
        TraceRecord(seq=1, gen=0, slots=_slot_tuple(slot, 300)),
    ])

    page.auto_lane_check.setChecked(True)
    page.refresh_plot()
    computed = dict(page.channel_slots()[slot]["transform"])
    assert computed != {"scale": 1.0, "offset": 0.0}

    page.auto_lane_check.setChecked(False)
    page.refresh_plot()
    assert page.channel_slots()[slot]["transform"] == computed


def test_y_axis_hidden_with_no_selection_and_follows_selected_channel(
        qtbot, monkeypatch):
    """Spec point 7: no selection hides the axis tick numbers; a
    selected row titles the axis with that channel's name/color and
    makes the tick numbers that channel's own raw decoded domain (the
    inverse of its scale/offset), not the transformed display range
    the curve is drawn in."""
    page = _make_stubbed_trace_page(qtbot, monkeypatch)
    slot = page.add_address_slot(0x20000000, "buf0")
    axis = page.plot.getAxis("left")

    assert axis.style["showValues"] is False

    page.channel_table.selectRow(slot)
    assert page._selected_row == slot
    assert axis.style["showValues"] is True
    entry = page.channel_slots()[slot]
    assert entry["label"] in axis.labelText
    assert entry["color"] in axis.labelText

    page.set_channel_transform(slot, scale=2.0, offset=5.0)
    # displayed = (raw - offset) * scale, so tickStrings must invert:
    # raw = displayed / scale + offset.
    ticks = axis.tickStrings([0.0, 1.0], 1, 1)
    assert ticks[0] == "%g" % (0.0 / 2.0 + 5.0)
    assert ticks[1] == "%g" % (1.0 / 2.0 + 5.0)

    page.channel_table.clearSelection()
    assert page._selected_row is None
    assert axis.style["showValues"] is False


def test_removing_selected_channel_clears_y_axis(qtbot, monkeypatch):
    page = _make_stubbed_trace_page(qtbot, monkeypatch)
    slot = page.add_address_slot(0x20000000, "buf0")
    page.channel_table.selectRow(slot)
    assert page._selected_row == slot

    page.remove_channel(slot)
    assert page._selected_row is None
    axis = page.plot.getAxis("left")
    assert axis.style["showValues"] is False


# -- v2: two-state cursor readout, no PIN (spec point 5) -------------------

def test_value_column_shows_newest_sample_when_mouse_off_plot(qtbot, monkeypatch):
    """State 1 (mouse off the plot, the default - no PIN, no third
    "locked" state): the Value column shows each channel's newest real
    sample and the column header reads plain "Value"."""
    page = _make_stubbed_trace_page(qtbot, monkeypatch, period_us=1_000_000)
    slot = page.add_address_slot(0x20000000, "buf0")
    page.store.append([
        TraceRecord(seq=0, gen=0, slots=_slot_tuple(slot, 100)),
        TraceRecord(seq=1, gen=0, slots=_slot_tuple(slot, 200)),
    ])
    page.refresh_plot()

    assert page.channel_slots()[slot]["value_item"].text() == \
        format_value(200, DEFAULT_TYPE)
    assert page.channel_table.horizontalHeaderItem(COL_VALUE).text() == "Value"


def test_value_column_switches_to_hover_value_and_header_on_crosshair(
        qtbot, monkeypatch):
    """State 2 (mouse on the plot): the Value column shows the value
    at the crosshair's time and the header switches to "Value @
    -X.Xs"."""
    page = _make_stubbed_trace_page(qtbot, monkeypatch, period_us=100_000)
    slot = page.add_address_slot(0x20000000, "buf0")
    page.refresh_plot()
    now = page._last_now
    period_s = page.desc.period_us * 1e-6
    base_seq = int(now / period_s)
    page.store.append([
        TraceRecord(seq=base_seq - 20, gen=0, slots=_slot_tuple(slot, 100)),
        TraceRecord(seq=base_seq - 10, gen=0, slots=_slot_tuple(slot, 200)),
        TraceRecord(seq=base_seq - 5, gen=0, slots=_slot_tuple(slot, 300)),
    ])
    page.refresh_plot()

    page._update_crosshair(-0.4)

    assert page.channel_table.horizontalHeaderItem(COL_VALUE).text() == \
        "Value @ -0.4s"
    # -0.4s relative to the last refresh's now is just after the
    # newest (~now-0.5) sample - value_at finds the newest sample at or
    # before that absolute time, which is 300.
    assert page.channel_slots()[slot]["value_item"].text() == \
        format_value(300, DEFAULT_TYPE)


def test_clear_crosshair_reverts_to_newest_and_default_header(qtbot, monkeypatch):
    page = _make_stubbed_trace_page(qtbot, monkeypatch, period_us=1_000_000)
    slot = page.add_address_slot(0x20000000, "buf0")
    page.store.append([
        TraceRecord(seq=0, gen=0, slots=_slot_tuple(slot, 100)),
        TraceRecord(seq=1, gen=0, slots=_slot_tuple(slot, 200)),
    ])
    page.refresh_plot()

    page._update_crosshair(0.5)
    assert page.channel_table.horizontalHeaderItem(COL_VALUE).text() != "Value"

    page._clear_crosshair()

    assert page.channel_table.horizontalHeaderItem(COL_VALUE).text() == "Value"
    assert page.channel_slots()[slot]["value_item"].text() == \
        format_value(200, DEFAULT_TYPE)
    assert page._crosshair_t is None


def test_leave_event_on_plot_widget_clears_crosshair(qtbot):
    """The mouse can leave plot_widget without a trailing sigMouseMoved
    inside the scene (e.g. a fast move straight off an edge) -
    eventFilter's Leave handling is the backstop for that case. Pure
    crosshair-state plumbing, independent of channel data."""
    from PySide6.QtCore import QEvent

    page = ScopePage(_make_plain_engine())
    qtbot.addWidget(page)
    page._update_crosshair(0.5)
    assert page._crosshair_t is not None

    page.eventFilter(page.plot_widget, QEvent(QEvent.Type.Leave))

    assert page._crosshair_t is None


def test_event_log_click_only_moves_cursor_line_not_value_column(
        qtbot, monkeypatch):
    """spec point 5: an event-log click (jump_to) is a purely visual
    cursor-line move - it must never touch the Value column or its
    header (no value-locking, no PIN); cursor_time() still returns
    exactly the t passed in, unchanged from the pre-v2 contract."""
    page = _make_stubbed_trace_page(qtbot, monkeypatch, period_us=1_000_000)
    slot = page.add_address_slot(0x20000000, "buf0")
    page.store.append([TraceRecord(seq=0, gen=0, slots=_slot_tuple(slot, 100))])
    page.refresh_plot()
    page._update_crosshair(0.0)

    header_before = page.channel_table.horizontalHeaderItem(COL_VALUE).text()
    value_before = page.channel_slots()[slot]["value_item"].text()

    page.jump_to(page._t0 + 50.0)

    assert page.cursor_time() == page._t0 + 50.0
    header_after = page.channel_table.horizontalHeaderItem(COL_VALUE).text()
    assert header_after == header_before
    assert page.channel_slots()[slot]["value_item"].text() == value_before


# -- v2: per-page run/stop replaces global freeze (spec point 1) -----------

def test_set_stopped_and_hidden_timer_matrix(qtbot):
    """Same bug class the old set_frozen()/hideEvent() pairing guarded
    against (Task 2's "hidden-dock timer pause w/ frozen matrix" fix,
    carried into the run/stop rename): the FAST repaint timer's active
    state must be exactly (visible AND NOT stopped) after every
    visibility/stop transition, in either order - in particular,
    stopping while hidden must not let a later show() wake the timer
    back up (set_stopped(True) has to win over a plain show/hide
    cycle, same as the old set_frozen(True))."""
    page = ScopePage(_make_plain_engine())
    qtbot.addWidget(page)

    page.show()
    assert page._timer.isActive()

    page.set_stopped(True)
    assert not page._timer.isActive()
    page.hide()
    assert not page._timer.isActive()
    page.show()
    # stopped while hidden -> a later show() must not wake the timer.
    assert not page._timer.isActive()

    page.set_stopped(False)
    assert page._timer.isActive()

    page.hide()
    assert not page._timer.isActive()
    page.set_stopped(True)
    page.set_stopped(False)
    # still hidden -> resuming stop must not start a timer nobody can
    # see repaint.
    assert not page._timer.isActive()
    page.show()
    assert page._timer.isActive()


def test_run_stop_button_toggles_label_and_state(qtbot):
    page = ScopePage(_make_plain_engine())
    qtbot.addWidget(page)

    assert page.run_stop_btn.text() == "Stop"
    assert not page.is_stopped()

    page.run_stop_btn.click()
    assert page.is_stopped()
    assert page.run_stop_btn.text() == "Run"

    page.run_stop_btn.click()
    assert not page.is_stopped()
    assert page.run_stop_btn.text() == "Stop"


def test_spacebar_toggles_run_stop(qtbot):
    """spec point 1: spacebar is a scope-focus shortcut for the same
    big Run/Stop button, not a global app-wide binding."""
    page = ScopePage(_make_plain_engine())
    qtbot.addWidget(page)
    assert not page.is_stopped()

    qtbot.keyClick(page, Qt.Key_Space)
    assert page.is_stopped()

    qtbot.keyClick(page, Qt.Key_Space)
    assert not page.is_stopped()
