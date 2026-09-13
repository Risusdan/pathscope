from core.trace.reader import TraceReader
from ui.demo import make_demo_engine


def test_demo_engine_exposes_live_trace():
    engine = make_demo_engine("targets/f411")
    engine.start()
    try:
        r = TraceReader(engine)
        desc = r.discover(engine.trace_desc_addr)
        assert desc.period_us > 0
        r.set_watch([0x20000000])
        import time; time.sleep(0.2)
        assert len(r.refresh()) > 0
    finally:
        engine.stop()
