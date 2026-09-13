import math

import pyqtgraph as pg
import pytest
from PySide6.QtCore import Qt

from core.adapter.mock import MockAdapter
from core.engine.core import Engine
from ui.bridge import EngineBridge
from ui.demo import ADC_SR, S0CR, make_demo_engine
from ui.main_window import MainWindow
from ui.panels.scope_page import (NORMALIZE_LABEL_FULL,
                                  NORMALIZE_LABEL_SHORT, ScopePage,
                                  _gapped_xy, value_at)

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
    channel-list row and the curve's legend entry - rather than
    leaving the first label in place or creating a duplicate curve."""
    engine = make_demo_engine("targets/f411")
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key1 = page.add_address_channel(0x20000000, "first")
    key2 = page.add_address_channel(0x20000000, "second")

    assert key1 == key2
    assert len(page._channels) == 1
    assert page.channel_list.count() == 1

    entry = page._channels[key1]
    assert entry["label"] == "second"
    assert entry["curve"].opts["name"] == "second"
    assert page.channel_list.item(0).text() == "second"
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
    _prune_markers (dock hidden or frozen) - the primary pruning
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
    win._toggle_scope(True)   # toolbar toggle handler, called directly
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
    domain mapSceneToView would hand it) and must update both the
    channel row's raw-value suffix and the time label - reading only
    the per-channel series cached by the prior refresh_plot(), per the
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

    item_text = page._channels[key]["item"].text()
    assert item_text.endswith("= %d (0x%X)" % (200, 200))
    assert page.time_label.text() == "t=1.500 s"


def test_scale_offset_transforms_curve(qtbot):
    """set_channel_transform() is the programmatic surface the scale/
    offset spinboxes drive; refresh_plot() must apply y' = (y-offset)
    * scale when building the curve's data."""
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

    # the crosshair readout always shows raw values, never the scaled
    # display curve - the whole point of the readout.
    page._update_crosshair(0.1)
    item_text = page._channels[key]["item"].text()
    assert item_text.endswith("= %d (0x%X)" % (200, 200))


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


def test_normalize_maps_to_unit_range(qtbot):
    """Normalize ignores scale/offset and maps the current window's
    min..max to 0..1; a flat (single-valued) series must map to 0.5
    everywhere rather than dividing by a zero span."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key = "DMA2.S0NDTR"
    page.add_channel(key)
    t0 = page._t0
    values = [100, 400, 250, 300]
    for i, v in enumerate(values):
        engine.history.record(key, t0 + i * 0.1, v)

    page.set_channel_transform(key, scale=1.0, offset=0.0, normalize=True)
    page.refresh_plot()

    ys = page.curve_y(key)
    assert min(ys) == pytest.approx(0.0)
    assert max(ys) == pytest.approx(1.0)

    flat_key = "SCOPE.FLAT"
    page.add_channel(flat_key)
    engine.history.record(flat_key, t0 + 0.0, 42)
    page.set_channel_transform(flat_key, scale=1.0, offset=0.0,
                               normalize=True)
    page.refresh_plot()

    flat_ys = page.curve_y(flat_key)
    assert flat_ys == [pytest.approx(0.5)]


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


def test_normalize_checkbox_full_label_and_disables_spinboxes(qtbot):
    """Hardware-session finding: the transform strip's single hbox row
    was too narrow for the ~260px side panel and visually truncated
    "Normalize" to "Norr" (and clipped the offset spinbox's value).
    The checkbox's text must remain the full word - not shortened by
    some future rename - regardless of which of the two fit-checked
    labels gets picked; and checking Normalize must grey out
    scale/offset (they are ignored by the transform in that mode),
    re-enabling them on uncheck."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    key = "DMA2.S0NDTR"
    page.add_channel(key)

    text = page.normalize_check.text()
    assert text.startswith("Normalize")
    assert text in (NORMALIZE_LABEL_FULL, NORMALIZE_LABEL_SHORT)

    page.channel_list.setCurrentRow(0)
    assert page.scale_spin.isEnabled()
    assert page.offset_spin.isEnabled()

    page.normalize_check.setChecked(True)
    assert not page.scale_spin.isEnabled()
    assert not page.offset_spin.isEnabled()

    page.normalize_check.setChecked(False)
    assert page.scale_spin.isEnabled()
    assert page.offset_spin.isEnabled()


def test_side_panel_scrolls_and_remove_button_above_transform_strip(qtbot):
    """Hardware-session finding: at typical dock heights the side
    panel's content (channel list, transform strip, add rows, budget
    label, ELF section, Remove channel, window label) exceeds the
    available height with no scrollbar, pushing the bottom controls
    off-screen and unreachable (a screenshot showed the panel cut off
    at "Add symbol"). The panel must now scroll, and "Remove channel"
    - a channel-list operation - must sit directly under the channel
    list (above the transform strip), so it stays reachable even when
    the ELF section further down is scrolled out of view."""
    engine = make_demo_engine(TARGET)
    page = ScopePage(engine)
    qtbot.addWidget(page)
    page.resize(360, 400)
    page.show()
    qtbot.waitExposed(page)

    # content taller than the viewport => a vertical scrollbar with
    # room to move.
    assert page.side_scroll.verticalScrollBar().maximum() > 0
    assert (page.side_scroll.horizontalScrollBarPolicy()
           == Qt.ScrollBarAlwaysOff)

    # order assertion via layout index, not geometry - geometry alone
    # is unreliable for a widget (transform_strip) that starts hidden
    # and so may report a stale/zero position before ever being shown.
    side_layout = page.side_scroll.widget().layout()
    remove_index = side_layout.indexOf(page.remove_btn)
    strip_index = side_layout.indexOf(page.transform_strip)
    assert remove_index >= 0 and strip_index >= 0
    assert remove_index < strip_index
