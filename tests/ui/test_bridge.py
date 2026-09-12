from ui.bridge import EngineBridge
from ui.demo import make_demo_engine


def test_bridge_delivers_updates_on_main_thread(qtbot):
    engine = make_demo_engine("targets/f411")
    bridge = EngineBridge(engine)
    with qtbot.waitSignal(bridge.update, timeout=3000) as blocker:
        engine.start()
    u = blocker.args[0]
    assert "adc_to_sram" in u.flows
    engine.stop()


def test_demo_engine_flow_goes_active(qtbot):
    engine = make_demo_engine("targets/f411")
    bridge = EngineBridge(engine)
    seen = []
    bridge.update.connect(lambda u: seen.append(u))
    engine.start()
    qtbot.waitUntil(
        lambda: any(u.flows["adc_to_sram"].active for u in seen),
        timeout=3000)
    engine.stop()
