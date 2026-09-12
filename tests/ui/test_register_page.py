from ui.demo import make_demo_engine
from ui.panels.register_page import RegisterPage


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
