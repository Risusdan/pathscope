from core.adapter.mock import MockAdapter
from core.engine.core import Engine
from ui.bridge import EngineBridge
from ui.demo import ADC_SR, S0CR, make_demo_engine
from ui.main_window import MainWindow

TARGET = "targets/f411"


def _anomaly_row_texts(win):
    return [win.event_log.item(i).text()
           for i in range(win.event_log.count())]


def _has_anomaly_row(win):
    return any("ANOMALY" in t or "ADC overrun" in t
              for t in _anomaly_row_texts(win))


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


def _make_overrun_engine():
    """A MockAdapter seeded so the ADC overrun anomaly (f411.flows.yaml's
    "ADC1.SR.OVR == 1" rule) is already firing on the very first sweep -
    same addresses ui/demo.py uses (S0CR=DMA2.S0CR, ADC_SR=ADC1.SR) -
    so the log-related tests below don't have to wait out the demo's
    ~9 s normal-streaming window before the fault fires."""
    mem = {S0CR: 1, ADC_SR: 0x30}          # OVR bit set
    adapter = MockAdapter(mem)
    return Engine.load(TARGET, adapter, interval_s=0.01)


def test_anomaly_events_reach_event_log(qtbot):
    engine = _make_overrun_engine()
    bridge = EngineBridge(engine)
    win = MainWindow(engine, bridge)
    qtbot.addWidget(win)
    win.show()
    engine.start()
    try:
        qtbot.waitUntil(lambda: _has_anomaly_row(win), timeout=4000)
    finally:
        engine.stop()


def test_run_stop_does_not_drop_anomaly_from_event_log(qtbot):
    """FIX 1 regression, carried into per-page run/stop (spec point
    1): event_log.add_events() must fire from apply_update() before
    the Data Path stop check, not from inside _apply() (which stopping
    skips entirely) - otherwise an anomaly that occurs while stopped
    is permanently lost from the log (only its badge survives),
    violating "event log always live"."""
    engine = _make_overrun_engine()
    bridge = EngineBridge(engine)
    win = MainWindow(engine, bridge)
    qtbot.addWidget(win)
    win.show()

    # stop before the engine even starts, so the very first sweep -
    # the one carrying the overrun anomaly - is delivered while
    # stopped.
    win.set_datapath_stopped(True)
    stopped_rate_text = win.rate_label.text()
    stopped_active_edges = set(win.diagram_state.active_edges)

    engine.start()
    try:
        qtbot.waitUntil(lambda: _has_anomaly_row(win), timeout=4000)
        # the stopped display itself must not have moved: last_update
        # advances (bridge keeps delivering), but the visible panels
        # (driven only by _apply(), which apply_update() short-circuits
        # while stopped) stay put.
        assert win.last_update is not None
        assert win.rate_label.text() == stopped_rate_text
        assert win.diagram_state.active_edges == stopped_active_edges
    finally:
        win.set_datapath_stopped(False)
        engine.stop()
