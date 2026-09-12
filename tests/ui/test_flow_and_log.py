from ui.bridge import EngineBridge
from ui.demo import make_demo_engine
from ui.main_window import MainWindow


def test_edge_click_selects_flow_and_events_reach_log(qtbot):
    engine = make_demo_engine("targets/f411")
    bridge = EngineBridge(engine)
    win = MainWindow(engine, bridge)
    qtbot.addWidget(win)
    win.show()
    engine.start()
    try:
        qtbot.waitUntil(lambda: win.last_update is not None, timeout=4000)
        # simulate the scene callback for a known f411 edge
        some_edge = [e.id for e in engine.topology.edges
                     if e.src == "dma2"][0]
        win.diagram_state.on_edge_clicked(some_edge)
        assert win.flow_page.flow_id == "adc_to_sram"
        assert len(win.diagram_state.flow_edges) > 0
        # demo engine fires OVR within ~12 s; wait for an event row
        qtbot.waitUntil(lambda: win.event_log.count() > 0, timeout=15000)
    finally:
        engine.stop()
