"""Hardware end-to-end suite for the trace scope (M7 Task 10).

Everything in this module is marked `hw` (see pytest.ini's
`addopts = -m "not hw"`, the same marker/convention
tests/test_pyocd_adapter.py already uses) - deselected by default, run
explicitly by the coordinating agent against a real Blackpill
(STM32F411CE) wired to an ST-Link, flashed with
firmware/blackpill_adc_dma/fw.elf (the ps_trace module linked in,
sampling at 1 kHz - PS_TRACE_PERIOD_US in firmware/ps_trace/ps_trace.h).

No hardware access happens at import time or at module scope: every
adapter/engine/window is constructed INSIDE a test function, so a
plain `import tests.hw.test_trace_hw` (or pytest's own collection
pass) never touches a probe. This is what lets the module import
cleanly, and collect under `-m hw`, on a machine with no board
attached at all - only actually RUNNING a test (which the default
addopts skip) needs one.

Each test is independent: it builds its own PyOCDAdapter + Engine +
MainWindow, and tears both down in a `finally` (engine.stop() before
adapter.disconnect() - the poller thread that owns the adapter must
have exited, which engine.stop() blocks on via Poller.join(), before
anything outside that thread touches the adapter again). A screenshot
of the final window state is grabbed on the way out (even when an
assertion above it failed) into tests/hw/artifacts/ (gitignored) so
the coordinating agent can eyeball what the scope looked like without
needing the board a second time.

test_unplug_recovery is the one exception: it needs a human standing
next to the board to physically pull and reinsert the USB cable, so it
is additionally gated behind PS_HW_INTERACTIVE=1 and skipped otherwise
- the rest of this suite (and the default `-m "not hw"` CI run) stays
fully automatic either way.
"""
import os
from pathlib import Path
from typing import List

import pytest

from core.adapter.pyocd_swd import PyOCDAdapter
from core.engine.core import Engine
from core.engine.poller import PollerState
from ui.bridge import EngineBridge
from ui.main_window import MainWindow

pytestmark = pytest.mark.hw

TARGET_DIR = "targets/f411"
FW_ELF = "firmware/blackpill_adc_dma/fw.elf"
ARTIFACT_DIR = Path(__file__).parent / "artifacts"

# A plain SRAM address, 4-byte aligned, deliberately far from where a
# firmware image this small would ever place adc_buf's own .bss (which
# starts at the bottom of the 0x20000000-0x20020000 SRAM range the
# firmware's ps_trace whitelist covers - see main.c's
# ps_trace_whitelist) - distinct from both channels test 1/2 add, but
# still guaranteed to pass the firmware's own address whitelist and
# the host's alignment/guarded-address checks.
SRAM_PROBE_ADDR = 0x2001F000

# 1.5x the trace ring's span at the firmware's 1 kHz rate (RING_COUNT
# (256, core/trace/contract.py) * 1 sample/ms = 256 ms) - see
# test_run_stop_no_data_loss.
RING_SPAN_S = 0.256


def _shot(win: MainWindow, name: str) -> None:
    """win.grab().save() into tests/hw/artifacts/ (gitignored) - the
    per-test visual pass the coordinating agent runs after hardware
    execution. Never raises: a screenshot is a nice-to-have, not
    something that should mask (or be masked by) a real assertion
    failure already in flight in the caller's finally block."""
    try:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        win.grab().save(str(ARTIFACT_DIR / name))
    except Exception:
        pass


def _live_engine() -> "tuple[Engine, PyOCDAdapter]":
    """Connect the real adapter (attach-only, never resets the target -
    see PyOCDAdapter's own docstring) and load the real f411 target,
    the same order cli.py's cmd_monitor uses: connect() BEFORE
    Engine.load() (Poller.run() reads through the adapter directly,
    with no connect() call of its own - see core/engine/poller.py),
    then start() to bring the poller thread up. Caller owns both
    returned objects on success and must stop()/disconnect() them, in
    that order, in a finally block (see every call site below).

    The connect-succeeds-but-setup-fails case is handled HERE, not by
    callers: once connect() has claimed the probe, an exception from
    Engine.load() or engine.start() must still disconnect it before
    propagating - otherwise a dead session keeps holding the ST-Link
    and every LATER test in the same run fails to connect too (the
    cross-test poisoning this suite exists to rule out). Centralizing
    the try/except here, rather than a None-guarded finally at each of
    the four call sites, means a fifth test added later gets this for
    free just by calling _live_engine()."""
    adapter = PyOCDAdapter()
    adapter.connect()
    try:
        engine = Engine.load(TARGET_DIR, adapter, interval_s=0.02)
        engine.start()
    except Exception:
        adapter.disconnect()
        raise
    return engine, adapter


def _open_scope(qtbot, engine: Engine):
    """MainWindow + EngineBridge wiring, then switch to the Scope tab
    (index 1) - MainWindow._activate_scope_tab constructs the real
    ScopePage lazily on this first activation (see ui/main_window.py),
    which is why win.scope_page is None until this runs."""
    win = MainWindow(engine, EngineBridge(engine))
    qtbot.addWidget(win)
    win.show()
    win.tabs.setCurrentIndex(1)
    page = win.scope_page
    assert page is not None, "scope tab activation did not construct ScopePage"
    return win, page


def test_live_trace_end_to_end(qtbot):
    """Connects for real, loads the real firmware ELF, watches one
    symbol channel (adc_buf) and one register channel (DMA2.S0NDTR),
    and proves data is actually flowing: >500 samples land in slot 0
    within 5s (well under the 1 kHz rate's own budget of 500ms for
    that many), both slots are occupied, both channels' values
    actually change over time (not stuck at a constant - the
    "coherence by construction" the brief calls out: both slots come
    from the same trace records), and the firmware's own declared rate
    (1 kHz, PS_TRACE_PERIOD_US=1000 in firmware/ps_trace/ps_trace.h)
    shows up verbatim in rate_label_text()."""
    engine, adapter = _live_engine()
    win = None
    try:
        win, page = _open_scope(qtbot, engine)
        page.load_elf(FW_ELF)
        assert page.error_label.text() == "", \
            "ELF load / discovery failed: %s" % page.error_label.text()
        assert page.desc is not None

        slot_buf = page.add_symbol_channel("adc_buf")
        assert slot_buf is not None, page.error_label.text()
        slot_reg = page.add_register_channel("DMA2.S0NDTR")
        assert slot_reg is not None, page.error_label.text()

        qtbot.waitUntil(
            lambda: page.channel_sample_count(slot_buf) > 500,
            timeout=5000)

        slots = page.channel_slots()
        assert slots[slot_buf] is not None
        assert slots[slot_reg] is not None

        assert "1000 Hz" in page.rate_label_text()

        # Values change over time (set cardinality > 1) for BOTH
        # channels - a stuck-at-constant reading on either slot would
        # mean the watch table or the ADC/DMA pipeline isn't actually
        # live, even though records are arriving.
        for slot in (slot_buf, slot_reg):
            _t, y = page.store.series(slot)
            distinct = {v for v in y.tolist() if v == v}   # drop NaN gaps
            assert len(distinct) > 1, \
                "slot %d never changed value" % slot
    finally:
        if win is not None:
            _shot(win, "test_live_trace_end_to_end.png")
            win.close()
        engine.stop()
        adapter.disconnect()


def test_table_edit_under_load(qtbot):
    """Adds a third channel and removes the (now) middle one while the
    first two are already streaming live, then checks all three of the
    table-edit invariants at once: channel_slots() stays compacted
    (the surviving third channel shifts into the removed one's row),
    reader.status().generation has advanced (the firmware's watch-table
    gate protocol - core/trace/reader.py's set_watch - only bumps
    generation on an accepted 0->N transition, and both the add and the
    remove each trigger one), and the drain keeps running straight
    through the edit (the store's sample count for the still-present
    first channel keeps growing afterward, not just before)."""
    engine, adapter = _live_engine()
    win = None
    try:
        win, page = _open_scope(qtbot, engine)
        page.load_elf(FW_ELF)
        assert page.desc is not None

        slot0 = page.add_symbol_channel("adc_buf")
        slot1 = page.add_register_channel("DMA2.S0NDTR")
        assert slot0 is not None and slot1 is not None

        # Establish "streaming live" before touching the table.
        qtbot.waitUntil(
            lambda: page.channel_sample_count(slot0) > 50, timeout=3000)

        gen_before = page.reader.status().generation

        slot2 = page.add_address_slot(SRAM_PROBE_ADDR, "sram_probe", "u32")
        assert slot2 == 2, page.error_label.text()

        qtbot.wait(50)   # a beat of streaming with all three occupied
        count_before_edit = page.channel_sample_count(slot0)

        page.remove_channel(1)   # remove DMA2.S0NDTR, the middle slot

        slots = page.channel_slots()
        assert slots[0] is not None and slots[0]["label"] == "adc_buf"
        assert slots[1] is not None and slots[1]["label"] == "sram_probe"
        assert slots[2] is None

        gen_after = page.reader.status().generation
        assert gen_after > gen_before, \
            "watch-table generation did not advance across the edit"

        # Streaming continues after the edit - the store for the
        # still-present first channel keeps growing, not stalled.
        qtbot.waitUntil(
            lambda: page.channel_sample_count(0) > count_before_edit,
            timeout=3000)
    finally:
        if win is not None:
            _shot(win, "test_table_edit_under_load.png")
            win.close()
        engine.stop()
        adapter.disconnect()


def test_run_stop_no_data_loss(qtbot):
    """Run/Stop is display-only (module docstring, ui/panels/
    scope_page.py, spec point 5): set_stopped() pauses only the 200ms
    fast repaint timer, never the always-on 500ms slow drain timer, so
    the trace ring (256 records - RING_COUNT, core/trace/contract.py)
    must never overflow just because the display is held. Stops the
    page, waits ~1s (comfortably over 1.5x the ring's own span at this
    firmware's 1 kHz rate - RING_SPAN_S above, 256ms), and asserts
    reader.lost did not grow across that wait; then resumes and
    confirms streaming actually continues, rather than merely not
    having crashed."""
    engine, adapter = _live_engine()
    win = None
    try:
        win, page = _open_scope(qtbot, engine)
        page.load_elf(FW_ELF)
        assert page.desc is not None

        slot = page.add_symbol_channel("adc_buf")
        assert slot is not None
        qtbot.waitUntil(
            lambda: page.channel_sample_count(slot) > 50, timeout=3000)

        page.set_stopped(True)
        assert page.is_stopped()

        lost_before = page.reader.lost
        qtbot.wait(int(1.5 * RING_SPAN_S * 1000) + 600)   # ~1s
        lost_after_stop = page.reader.lost
        assert lost_after_stop == lost_before, (
            "reader.lost grew from %d to %d while stopped - the slow "
            "drain timer fell behind the trace ring during Stop"
            % (lost_before, lost_after_stop))

        page.set_stopped(False)
        assert not page.is_stopped()

        count_before_run = page.channel_sample_count(slot)
        qtbot.waitUntil(
            lambda: page.channel_sample_count(slot) > count_before_run,
            timeout=3000)
    finally:
        if win is not None:
            _shot(win, "test_run_stop_no_data_loss.png")
            win.close()
        engine.stop()
        adapter.disconnect()


@pytest.mark.skipif(
    os.environ.get("PS_HW_INTERACTIVE") != "1",
    reason="needs a human to physically unplug/replug the ST-Link USB "
           "cable; set PS_HW_INTERACTIVE=1 to run this test")
def test_unplug_recovery(qtbot):
    """Interactive-only skeleton: a machine cannot pull its own USB
    cable, so this proves the poller's reconnect loop
    (core/engine/poller.py's RUNNING -> TARGET_LOST -> reconnect ->
    RUNNING cycle) against a REAL probe only when a human is present
    to drive the physical fault. Fully skipped (not merely deselected
    by the module's `hw` marker - this needs its own extra opt-in)
    unless PS_HW_INTERACTIVE=1, so the rest of this suite stays
    completely automatic.

    Prints its own instructions and waits (up to 60s each way) for the
    poller's own state callback to report the transition - no fixed
    sleep, since a human's reaction time varies."""
    engine, adapter = _live_engine()
    states: List[str] = []
    engine.on_state(states.append)
    win = None
    try:
        win, page = _open_scope(qtbot, engine)
        page.load_elf(FW_ELF)
        assert page.desc is not None

        slot = page.add_symbol_channel("adc_buf")
        assert slot is not None
        qtbot.waitUntil(
            lambda: page.channel_sample_count(slot) > 50, timeout=3000)

        print("\n>>> PS_HW_INTERACTIVE: unplug the ST-Link USB cable now <<<")
        qtbot.waitUntil(lambda: PollerState.TARGET_LOST in states,
                        timeout=60000)

        print(">>> PS_HW_INTERACTIVE: replug the ST-Link USB cable now <<<")
        qtbot.waitUntil(
            lambda: states and states[-1] == PollerState.RUNNING,
            timeout=60000)

        # Recovery is real, not just a state-flag flip: streaming
        # actually resumes.
        count_before = page.channel_sample_count(slot)
        qtbot.waitUntil(
            lambda: page.channel_sample_count(slot) > count_before,
            timeout=5000)
    finally:
        if win is not None:
            _shot(win, "test_unplug_recovery.png")
            win.close()
        engine.stop()
        adapter.disconnect()
