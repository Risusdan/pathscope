import pyqtgraph as pg

from ui.demo import make_demo_engine
from ui.panels.scope_page import ScopePage


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
