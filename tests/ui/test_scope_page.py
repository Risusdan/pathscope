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
