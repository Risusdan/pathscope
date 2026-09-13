import math

import pyqtgraph as pg

from core.adapter.mock import MockAdapter
from core.engine.core import Engine
from ui.bridge import EngineBridge
from ui.demo import ADC_SR, S0CR, make_demo_engine
from ui.main_window import MainWindow
from ui.panels.scope_page import ScopePage, _gapped_xy

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
