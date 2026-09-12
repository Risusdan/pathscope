from ui.bridge import EngineBridge
from ui.demo import make_demo_engine
from ui.main_window import MainWindow


def test_live_updates_light_up_the_diagram(qtbot):
    engine = make_demo_engine("targets/f411")
    bridge = EngineBridge(engine)
    win = MainWindow(engine, bridge)
    qtbot.addWidget(win)
    win.show()
    engine.start()
    try:
        qtbot.waitUntil(lambda: len(win.diagram_state.active_edges) > 0,
                        timeout=4000)
        assert win.rate_label.text() != ""
    finally:
        engine.stop()


def test_freeze_holds_display(qtbot):
    engine = make_demo_engine("targets/f411")
    bridge = EngineBridge(engine)
    win = MainWindow(engine, bridge)
    qtbot.addWidget(win)
    win.show()
    engine.start()
    try:
        qtbot.waitUntil(lambda: win.last_update is not None, timeout=4000)
        win.freeze_act.setChecked(True)
        held = win.rate_label.text()
        qtbot.wait(300)
        assert win.rate_label.text() == held      # frozen display
        win.freeze_act.setChecked(False)
    finally:
        engine.stop()
