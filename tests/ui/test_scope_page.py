import math
import struct
import time

import pyqtgraph as pg
import pytest
from PySide6.QtCore import Qt

from core.adapter.mock import MockAdapter
from core.engine.core import Engine
from ui.bridge import EngineBridge
from ui.demo import ADC_SR, S0CR, make_demo_engine
from ui.elf_symbols import Symbol
from ui.main_window import MainWindow
from ui.panels.scope_page import (COL_NAME, COL_VALUE, CURVE_COLORS,
                                  DEFAULT_TYPE, TYPES, ScopePage,
                                  _default_type_for_size, _fit_scale_offset,
                                  _gapped_xy, decode_value, format_value,
                                  value_at)

TARGET = "targets/f411"


def test_scope_plots_polled_register(qtbot):
    engine = make_demo_engine("targets/f411")
    engine.start()
    try:
        page = ScopePage(engine)
        qtbot.addWidget(page)
        page.add_channel("DMA2.S0NDTR")
        qtbot.waitUntil(
            lambda: page.channel_sample_count("DMA2.S0NDTR") >= 5,
            timeout=3000)
        page.refresh_plot()
        assert page.curve_point_count("DMA2.S0NDTR") >= 5
    finally:
        engine.stop()


def test_scope_addr_channel_via_engine(qtbot):
    engine = make_demo_engine("targets/f411")
    engine.start()
    try:
        page = ScopePage(engine)
        qtbot.addWidget(page)
        key = page.add_address_channel(0x20000000, "buf0")
        qtbot.waitUntil(
            lambda: page.channel_sample_count(key) >= 3, timeout=3000)
    finally:
        engine.stop()


def test_add_address_channel_relabels_existing(qtbot):
    """Re-adding the same fixed address with a different label (the
    engine's add_addr_watch() already re-sets its own label
    idempotently) must update the existing channel's label - both the
    channel-table row and the curve's legend entry - rather than
    leaving the first label in place or creating a duplicate row."""
    engine = make_demo_engine("targets/f411")
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key1 = page.add_address_channel(0x20000000, "first")
    key2 = page.add_address_channel(0x20000000, "second")

    assert key1 == key2
    assert len(page._channels) == 1
    assert page.channel_table.rowCount() == 1

    entry = page._channels[key1]
    assert entry["label"] == "second"
    assert entry["curve"].opts["name"] == "second"
    assert page.channel_table.item(0, COL_NAME).text() == "second"
    legend_label = page.plot.legend.getLabel(entry["curve"])
    assert legend_label.text == "second"


def test_gapped_xy_even_interval_count_uses_averaged_median():
    """_gapped_xy's median-of-intervals must average the two middle
    values for an even interval count, not pick the upper one
    (intervals[len//2]) - the old code's choice is always the larger
    of a pair, which can never itself exceed GAP_FACTOR times itself,
    so it under-detects gaps. A 2-interval (3-sample) series can never
    demonstrate this either way - the gap value is necessarily one of
    only two numbers averaged into its own threshold, so 3x the
    average can never exceed it, regardless of which of the two
    formulas is used. The smallest series where the fix is observable
    has one more interval: 3 evenly-spaced samples (dt=0.05) followed
    by a stall - the old median (intervals[2], one of the two largest
    values) sets a threshold the gap fails to clear, while the
    correct average of the two middle intervals sets it low enough
    to flag the stall with a NaN."""
    series = [(0.0, 0), (0.05, 1), (0.10, 2), (1.10, 3), (4.0, 4)]
    _x, y = _gapped_xy(series, 0.0)
    assert any(math.isnan(v) for v in y)


def test_event_marker_hard_cap_guards_unbounded_growth(qtbot):
    """add_event_marker's list must never grow past MARKER_HARD_CAP
    even when nothing is calling refresh_plot()'s window-based
    _prune_markers (dock hidden or stopped) - the primary pruning
    mechanism, which this cap only backstops."""
    engine = make_demo_engine("targets/f411")
    page = ScopePage(engine)
    qtbot.addWidget(page)
    for i in range(250):
        page.add_event_marker(float(i), "evt %d" % i)
    assert len(page._markers) <= 200


def test_repaint_timer_stops_when_hidden(qtbot):
    """Same bug class as tests/ui/test_memory_page.py's
    test_auto_refresh_timer_stops_when_hidden: a hidden ScopePage
    (tabbed away behind Event log) must not keep repainting every
    200 ms forever."""
    engine = make_demo_engine("targets/f411")
    engine.start()
    try:
        page = ScopePage(engine)
        qtbot.addWidget(page)
        page.show()
        assert page._timer.isActive()
        page.hide()
        assert not page._timer.isActive()
        page.show()
        assert page._timer.isActive()
    finally:
        engine.stop()


def test_plot_theme_is_light(qtbot):
    """pathscope is light-theme only by design - pyqtgraph's own
    defaults (black background, grey-on-black foreground) must be
    overridden before any PlotWidget is constructed."""
    engine = make_demo_engine("targets/f411")
    engine.start()
    try:
        page = ScopePage(engine)
        qtbot.addWidget(page)
        assert pg.getConfigOption("background") == "w"
        assert pg.getConfigOption("foreground") == "k"
    finally:
        engine.stop()


def _make_overrun_engine():
    """Same seeded-OVR helper pattern as
    tests/ui/test_flow_and_log.py's _make_overrun_engine: the ADC
    overrun anomaly (f411.flows.yaml's "ADC1.SR.OVR == 1" rule) is
    already firing on the very first sweep, so this test doesn't have
    to wait out the demo's normal-streaming window."""
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
    series (History.series()'s own guarantee)."""
    series = [(0, 10), (1, 20), (2, 30)]
    assert value_at(series, 1.5) == 20
    assert value_at(series, 0) == 10
    assert value_at(series, -1) is None
    assert value_at([], 5) is None


def test_crosshair_readout_shows_raw_values(qtbot):
    """_update_crosshair(view_t) takes plot-relative seconds (the same
    domain mapSceneToView would hand it - roll mode: sample_t -
    self._last_now, the last refresh's now) and must update both the
    channel row's Value cell and the time label - reading only the
    per-channel series cached by the prior refresh_plot(), per the
    module's cheapness requirement. History.record() is called
    directly (no engine.start()) so sample timestamps are fully
    controlled and the test needs no polling wait."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key = "DMA2.S0NDTR"
    page.add_channel(key)
    t0 = page._t0
    engine.history.record(key, t0 + 0.0, 100)
    engine.history.record(key, t0 + 1.0, 200)
    engine.history.record(key, t0 + 2.0, 300)
    page.refresh_plot()

    page._update_crosshair(1.5)

    value_text = page._channels[key]["value_item"].text()
    assert value_text == format_value(200, DEFAULT_TYPE)
    assert page.time_label.text() == "t-now = 1.50 s"


def test_scale_offset_transforms_curve(qtbot):
    """set_channel_transform() is the programmatic surface the table's
    scale/offset text fields drive; refresh_plot() must apply y' =
    (y-offset) * scale when building the curve's data."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key = "DMA2.S0NDTR"
    page.add_channel(key)
    t0 = page._t0
    values = [100, 200, 300, 400]
    for i, v in enumerate(values):
        engine.history.record(key, t0 + i * 0.1, v)

    page.set_channel_transform(key, scale=2.0, offset=5.0)
    page.refresh_plot()

    ys = page.curve_y(key)
    expected = [(v - 5.0) * 2.0 for v in values]
    assert len(ys) == len(expected)
    for e, a in zip(expected, ys):
        assert a == pytest.approx(e)

    # the Value column always shows the raw decoded value, never the
    # scaled display curve - the whole point of the readout.
    page._update_crosshair(0.1)
    value_text = page._channels[key]["value_item"].text()
    assert value_text == format_value(200, DEFAULT_TYPE)


def test_budget_label_shows_no_rate_before_any_sweep_rate_received(qtbot):
    """Before MainWindow ever calls set_sweep_rate() (no sweep has
    reported a rate yet, e.g. right after the dock opens), the budget
    label must show the read-op count without a rate suffix."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    page.refresh_plot()
    assert page.budget_label.text() == "sweep %d reads" % engine.read_ops
    assert "E65100" not in page.budget_label.styleSheet()


def test_set_sweep_rate_styles_label_orange_below_threshold(qtbot):
    """set_sweep_rate(10.0) (below the 15 Hz budget threshold) must
    show the rate in the label and style it orange (#E65100) with a
    tooltip explaining why; a subsequent set_sweep_rate(38.0) (a
    healthy rate, same as the real hardware validation figure in the
    README) must clear both the color and the tooltip."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)

    page.set_sweep_rate(10.0)
    page.refresh_plot()
    assert "10.0" in page.budget_label.text()
    assert "E65100" in page.budget_label.styleSheet()
    assert "prefer contiguous addresses" in page.budget_label.toolTip()

    page.set_sweep_rate(38.0)
    page.refresh_plot()
    assert "38.0" in page.budget_label.text()
    assert "E65100" not in page.budget_label.styleSheet()


def test_budget_label_updates_immediately_after_add_and_remove_channel(
        qtbot):
    """The label must reflect a changed read-op count right after
    add_channel()/remove_channel() returns, not only on the next
    200 ms refresh_plot() timer tick. The timer is stopped here so the
    check is deterministic - proof that the add/remove call sites
    themselves refresh the label, not a lucky race with the timer."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    page._timer.stop()

    page.add_channel("DMA2.S0NDTR")
    assert page.budget_label.text() == "sweep %d reads" % engine.read_ops

    page.remove_channel("DMA2.S0NDTR")
    assert page.budget_label.text() == "sweep %d reads" % engine.read_ops



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

    Asserting the ViewBox's childrenBoundingRect() is byte-for-byte
    unchanged after placing a crosshair (or cursor) far outside the
    plotted data's span - and unchanged again after moving it further
    still - demonstrates both: an included item would have moved the
    rect's edge to track it, so an unmoved rect proves the item is
    excluded from bounds, not merely that its effect happens to be
    small."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key = "DMA2.S0NDTR"
    page.add_channel(key)
    t0 = page._t0
    for i, v in enumerate([100, 200, 300]):
        engine.history.record(key, t0 + i * 0.1, v)
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
    assert after_second_move.right() < 1000.0

    # cursor (jump_to): same exclusion, same reasoning.
    page.jump_to(t0 + 9000.0)
    after_cursor = page.plot.vb.childrenBoundingRect()
    assert after_cursor == baseline
    assert after_cursor.right() < 1000.0


def test_side_panel_scrolls_and_remove_button_above_elf_section(qtbot):
    """Hardware-session finding (carried into v2): at typical dock
    heights the side panel's content (channel table, add rows, ELF
    section, Remove channel, window/budget labels) exceeds the
    available height with no scrollbar, pushing the bottom controls
    off-screen and unreachable. The panel must scroll, and "Remove
    channel" - a channel-table operation - must sit directly under the
    table (above the ELF section), so it stays reachable even when the
    ELF section further down is scrolled out of view."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    page.resize(480, 150)
    page.show()
    qtbot.waitExposed(page)

    # content taller than the viewport => a vertical scrollbar with
    # room to move.
    assert page.side_scroll.verticalScrollBar().maximum() > 0
    assert (page.side_scroll.horizontalScrollBarPolicy()
           == Qt.ScrollBarAlwaysOff)

    side_layout = page.side_scroll.widget().layout()
    remove_index = side_layout.indexOf(page.remove_btn)
    elf_index = side_layout.indexOf(page.elf_content)
    assert remove_index >= 0 and elf_index >= 0
    assert remove_index < elf_index


def test_roll_mode_viewport_fixed(qtbot):
    """Roll mode (standard-scope style): the viewport must be pinned
    to a fixed (-window_s, 0) x-range on every refresh - identical
    across refreshes, never sliding to track wall-clock time - and an
    event marker (which stores an ABSOLUTE t) must reposition on every
    refresh by exactly the elapsed real-time delta between refreshes,
    scrolling left with its moment in history rather than sitting
    still."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key = "DMA2.S0NDTR"
    page.add_channel(key)
    window_s = engine.history.window_s

    t0 = page._t0
    engine.history.record(key, t0 + 0.0, 100)
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
    engine.history.record(key, t0 + 0.1, 200)
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


def test_channel_table_has_spec_columns_and_default_type(qtbot):
    """The table replaces the old list+strip (spec point 2): 8 columns
    (swatch/name/type/Value/Hz/scale/offset/Fit), and a freshly added
    channel starts at the default type (u32, spec point 3)."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    assert page.channel_table.columnCount() == 8

    key = "DMA2.S0NDTR"
    page.add_channel(key)
    row = page._row_for_key(key)
    assert row is not None
    assert page._channels[key]["type"] == DEFAULT_TYPE
    type_combo = page._channels[key]["type_combo"]
    assert type_combo.currentText() == DEFAULT_TYPE
    assert list(type_combo.itemText(i) for i in range(type_combo.count())) \
        == TYPES
    assert len(TYPES) == 11


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


def test_type_change_updates_curve_and_value_column(qtbot):
    """Changing a row's type combo re-decodes both the plotted curve
    and the Value column readout - display-side only, core untouched
    (the History still stores the plain 32-bit raw word)."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key = "DMA2.S0NDTR"
    page.add_channel(key)
    t0 = page._t0
    engine.history.record(key, t0 + 0.0, 0xFFFFFFFF)
    page.refresh_plot()

    assert page.curve_y(key) == [pytest.approx(0xFFFFFFFF)]

    page._channels[key]["type_combo"].setCurrentText("i32")
    assert page._channels[key]["type"] == "i32"
    assert page.curve_y(key) == [pytest.approx(-1)]

    page._update_crosshair(0.1)
    assert page._channels[key]["value_item"].text() == "-1 (0xFFFFFFFF)"


def test_scale_offset_text_field_commit_and_invalid_revert(qtbot):
    """Scale/offset cells are plain text fields (spec point 2), not
    spinboxes - Enter commits a valid numeric entry (including
    scientific notation), and an invalid entry reverts to the last
    good value and flashes the field's background."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key = "DMA2.S0NDTR"
    page.add_channel(key)
    entry = page._channels[key]

    entry["scale_edit"].setText("2.5e1")
    entry["scale_edit"].returnPressed.emit()
    assert entry["transform"]["scale"] == pytest.approx(25.0)
    assert "FFCDD2" not in entry["scale_edit"].styleSheet()

    entry["scale_edit"].setText("not-a-number")
    entry["scale_edit"].returnPressed.emit()
    assert entry["transform"]["scale"] == pytest.approx(25.0)
    assert "FFCDD2" in entry["scale_edit"].styleSheet()
    assert entry["scale_edit"].text() == "%g" % 25.0


def test_add_symbol_channel_preselects_type_by_size(qtbot):
    """ELF preselect (spec point 3): a channel added from a loaded
    symbol starts at a type chosen from the symbol's declared size,
    unsigned default - 1 byte -> u8.0, 2 bytes -> u16.lo, anything
    else -> u32."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    page.elf_symbols = {
        "byte_flag": Symbol(name="byte_flag", addr=0x20000100, size=1),
        "half_word": Symbol(name="half_word", addr=0x20000200, size=2),
        "full_word": Symbol(name="full_word", addr=0x20000300, size=4),
    }
    k1 = page.add_symbol_channel("byte_flag")
    k2 = page.add_symbol_channel("half_word")
    k3 = page.add_symbol_channel("full_word")
    assert page._channels[k1]["type"] == "u8.0"
    assert page._channels[k2]["type"] == "u16.lo"
    assert page._channels[k3]["type"] == "u32"


def test_elf_symbol_section_collapsed_by_default_and_auto_expands(
        qtbot, monkeypatch):
    """Add-channel area is compact (spec point 8): the ELF symbol
    picker starts collapsed, and load_elf() auto-expands it once
    symbols actually land - no reason to show an empty list before
    anything is loaded, but no reason to hide it once something is."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
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


def test_fit_button_toggles_fill_own_label(qtbot):
    """Per-row Fit toggle (spec point 4): starts at "fill" (button
    text "Fill") and flips to "own" ("Own") and back on each click -
    the actual lane math lands with Auto-lane in a later commit, but
    the per-row state and its label already exist here."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key = "DMA2.S0NDTR"
    page.add_channel(key)
    entry = page._channels[key]
    assert entry["fit"] == "fill"
    assert entry["fit_btn"].text() == "Fill"

    entry["fit_btn"].click()
    assert entry["fit"] == "own"
    assert entry["fit_btn"].text() == "Own"

    entry["fit_btn"].click()
    assert entry["fit"] == "fill"
    assert entry["fit_btn"].text() == "Fill"


def test_rename_channel_via_table_updates_label_and_legend(qtbot):
    """Name is inline-editable too (spec point 2: "ALL
    inline-editable") - committing an edit to the Name cell updates
    the channel's stored label, the curve's legend entry, and (an
    empty name is rejected, reverting to the previous label)."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key = "DMA2.S0NDTR"
    page.add_channel(key)
    entry = page._channels[key]
    row = page._row_for_key(key)

    page.channel_table.item(row, COL_NAME).setText("ndtr")
    assert entry["label"] == "ndtr"
    assert entry["curve"].opts["name"] == "ndtr"
    legend_label = page.plot.legend.getLabel(entry["curve"])
    assert legend_label.text == "ndtr"

    page.channel_table.item(row, COL_NAME).setText("   ")
    assert entry["label"] == "ndtr"
    assert page.channel_table.item(row, COL_NAME).text() == "ndtr"


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


def test_auto_lane_fills_and_stacks_own_channels(qtbot):
    """Auto-lane (spec point 4): a "fill" channel's own window maps to
    the full [0, 1] view; "own" channels split [0, 1] into as many
    equal bands as there are "own" channels, each mapping its own
    window into just its band, in table (add) order."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    t0 = page._t0

    fill_key = "DMA2.S0NDTR"
    page.add_channel(fill_key)
    for i, v in enumerate([100, 200, 300]):
        engine.history.record(fill_key, t0 + i * 0.1, v)

    own_key1 = "SCOPE.OWN1"
    page.add_channel(own_key1)
    page._channels[own_key1]["fit_btn"].click()
    for i, v in enumerate([0, 100]):
        engine.history.record(own_key1, t0 + i * 0.1, v)

    own_key2 = "SCOPE.OWN2"
    page.add_channel(own_key2)
    page._channels[own_key2]["fit_btn"].click()
    for i, v in enumerate([1000, 2000]):
        engine.history.record(own_key2, t0 + i * 0.1, v)

    page.auto_lane_check.setChecked(True)
    page.refresh_plot()

    fill_ys = page.curve_y(fill_key)
    assert min(fill_ys) == pytest.approx(0.0)
    assert max(fill_ys) == pytest.approx(1.0)

    own1_ys = page.curve_y(own_key1)
    assert min(own1_ys) == pytest.approx(0.0)
    assert max(own1_ys) == pytest.approx(0.5)

    own2_ys = page.curve_y(own_key2)
    assert min(own2_ys) == pytest.approx(0.5)
    assert max(own2_ys) == pytest.approx(1.0)


def test_auto_lane_off_leaves_last_computed_values_editable(qtbot):
    """Turning Auto-lane off stops the recompute but does not reset
    scale/offset - they stay exactly as last computed, still plain
    editable fields the user can hand-tune from there."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key = "DMA2.S0NDTR"
    page.add_channel(key)
    t0 = page._t0
    engine.history.record(key, t0 + 0.0, 100)
    engine.history.record(key, t0 + 0.1, 300)

    page.auto_lane_check.setChecked(True)
    page.refresh_plot()
    computed = dict(page._channels[key]["transform"])
    assert computed != {"scale": 1.0, "offset": 0.0}

    page.auto_lane_check.setChecked(False)
    page.refresh_plot()
    assert page._channels[key]["transform"] == computed


def test_fit_click_does_one_shot_pass_even_with_auto_lane_off(qtbot):
    """T5 hardware finding: clicking a row's Fit button must perform
    one fit pass right now (a scope autoset), even while Auto-lane is
    unchecked - not just silently regroup for a computation that
    never runs."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key = "DMA2.S0NDTR"
    page.add_channel(key)
    t0 = page._t0
    engine.history.record(key, t0 + 0.0, 100)
    engine.history.record(key, t0 + 0.1, 300)

    assert not page.auto_lane_check.isChecked()
    assert page._channels[key]["transform"] == {"scale": 1.0,
                                                "offset": 0.0}

    page._channels[key]["fit_btn"].click()
    computed = dict(page._channels[key]["transform"])
    assert computed != {"scale": 1.0, "offset": 0.0}
    # and the computed values landed in the editable fields.
    assert page._channels[key]["scale_edit"].text() != "1"


def test_y_axis_hidden_with_no_selection_and_follows_selected_channel(qtbot):
    """Spec point 7: no selection hides the axis tick numbers; a
    selected row titles the axis with that channel's name/color and
    makes the tick numbers that channel's own raw decoded domain (the
    inverse of its scale/offset), not the transformed display range
    the curve is drawn in."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key = "DMA2.S0NDTR"
    page.add_channel(key)
    axis = page.plot.getAxis("left")

    assert axis.style["showValues"] is False

    row = page._row_for_key(key)
    page.channel_table.selectRow(row)
    assert page._selected_key == key
    assert axis.style["showValues"] is True
    entry = page._channels[key]
    assert entry["label"] in axis.labelText
    assert entry["color"] in axis.labelText

    page.set_channel_transform(key, scale=2.0, offset=5.0)
    # displayed = (raw - offset) * scale, so tickStrings must invert:
    # raw = displayed / scale + offset.
    ticks = axis.tickStrings([0.0, 1.0], 1, 1)
    assert ticks[0] == "%g" % (0.0 / 2.0 + 5.0)
    assert ticks[1] == "%g" % (1.0 / 2.0 + 5.0)

    page.channel_table.clearSelection()
    assert page._selected_key is None
    assert axis.style["showValues"] is False


def test_removing_selected_channel_clears_y_axis(qtbot):
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key = "DMA2.S0NDTR"
    page.add_channel(key)
    row = page._row_for_key(key)
    page.channel_table.selectRow(row)
    assert page._selected_key == key

    page.remove_channel(key)
    assert page._selected_key is None
    axis = page.plot.getAxis("left")
    assert axis.style["showValues"] is False


# -- v2: two-state cursor readout, no PIN (spec point 5) -------------------

def test_value_column_shows_newest_sample_when_mouse_off_plot(qtbot):
    """State 1 (mouse off the plot, the default - no PIN, no third
    "locked" state): the Value column shows each channel's newest
    sample and the column header reads plain "Value"."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key = "DMA2.S0NDTR"
    page.add_channel(key)
    t0 = page._t0
    engine.history.record(key, t0 + 0.0, 100)
    engine.history.record(key, t0 + 1.0, 200)
    page.refresh_plot()

    assert page._channels[key]["value_item"].text() == \
        format_value(200, DEFAULT_TYPE)
    assert page.channel_table.horizontalHeaderItem(COL_VALUE).text() == "Value"


def test_value_column_switches_to_hover_value_and_header_on_crosshair(qtbot):
    """State 2 (mouse on the plot): the Value column shows the value
    at the crosshair's time and the header switches to "Value @
    -X.Xs"."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key = "DMA2.S0NDTR"
    page.add_channel(key)
    page.refresh_plot()
    now = page._last_now
    engine.history.record(key, now - 2.0, 100)
    engine.history.record(key, now - 1.0, 200)
    engine.history.record(key, now - 0.5, 300)
    page.refresh_plot()

    page._update_crosshair(-0.4)

    assert page.channel_table.horizontalHeaderItem(COL_VALUE).text() == \
        "Value @ -0.4s"
    # -0.4s relative to the last refresh's now is just after the
    # newest (now-0.5) sample - value_at finds the newest sample at or
    # before that absolute time, which is 300.
    assert page._channels[key]["value_item"].text() == \
        format_value(300, DEFAULT_TYPE)


def test_clear_crosshair_reverts_to_newest_and_default_header(qtbot):
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key = "DMA2.S0NDTR"
    page.add_channel(key)
    t0 = page._t0
    engine.history.record(key, t0 + 0.0, 100)
    engine.history.record(key, t0 + 1.0, 200)
    page.refresh_plot()

    page._update_crosshair(0.5)
    assert page.channel_table.horizontalHeaderItem(COL_VALUE).text() != "Value"

    page._clear_crosshair()

    assert page.channel_table.horizontalHeaderItem(COL_VALUE).text() == "Value"
    assert page._channels[key]["value_item"].text() == \
        format_value(200, DEFAULT_TYPE)
    assert page._crosshair_t is None


def test_leave_event_on_plot_widget_clears_crosshair(qtbot):
    """The mouse can leave plot_widget without a trailing sigMouseMoved
    inside the scene (e.g. a fast move straight off an edge) -
    eventFilter's Leave handling is the backstop for that case."""
    from PySide6.QtCore import QEvent

    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key = "DMA2.S0NDTR"
    page.add_channel(key)
    page._update_crosshair(0.5)
    assert page._crosshair_t is not None

    page.eventFilter(page.plot_widget, QEvent(QEvent.Type.Leave))

    assert page._crosshair_t is None


def test_event_log_click_only_moves_cursor_line_not_value_column(qtbot):
    """spec point 5: an event-log click (jump_to) is a purely visual
    cursor-line move - it must never touch the Value column or its
    header (no value-locking, no PIN); cursor_time() still returns
    exactly the t passed in, unchanged from the pre-v2 contract."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key = "DMA2.S0NDTR"
    page.add_channel(key)
    t0 = page._t0
    engine.history.record(key, t0 + 0.0, 100)
    page.refresh_plot()
    page._update_crosshair(0.0)

    header_before = page.channel_table.horizontalHeaderItem(COL_VALUE).text()
    value_before = page._channels[key]["value_item"].text()

    page.jump_to(t0 + 50.0)

    assert page.cursor_time() == t0 + 50.0
    header_after = page.channel_table.horizontalHeaderItem(COL_VALUE).text()
    assert header_after == header_before
    assert page._channels[key]["value_item"].text() == value_before


# -- v2: per-page run/stop replaces global freeze (spec point 1) -----------

def test_set_stopped_and_hidden_timer_matrix(qtbot):
    """Same bug class the old set_frozen()/hideEvent() pairing guarded
    against (Task 2's "hidden-dock timer pause w/ frozen matrix" fix,
    carried into the run/stop rename): the repaint timer's active
    state must be exactly (visible AND NOT stopped) after every
    visibility/stop transition, in either order - in particular,
    stopping while hidden must not let a later show() wake the timer
    back up (set_stopped(True) has to win over a plain show/hide
    cycle, same as the old set_frozen(True))."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
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
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
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
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    assert not page.is_stopped()

    qtbot.keyClick(page, Qt.Key_Space)
    assert page.is_stopped()

    qtbot.keyClick(page, Qt.Key_Space)
    assert not page.is_stopped()
