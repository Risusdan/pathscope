import time

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

        # 0.3s of the animate loop's ~50ms cadence is ~6 ticks
        # (fw.step(10) each), so ~60 records, but the watched sine
        # value only changes once per tick - assert the batch actually
        # carries more than one distinct sample, not just that
        # refresh() returned *something* (a regression that always
        # sampled 0 would still pass a bare len() > 0 check). Retry
        # once with a longer sleep in case the first batch landed
        # entirely within one tick's worth of identical samples.
        recs = _refresh_after(r, 0.3)
        distinct = len({rec.slots[0] for rec in recs})
        if distinct <= 1:
            recs = _refresh_after(r, 0.3)
            distinct = len({rec.slots[0] for rec in recs})
        assert distinct > 1
    finally:
        engine.stop()


def _refresh_after(reader: TraceReader, seconds: float):
    time.sleep(seconds)
    recs = reader.refresh()
    assert len(recs) > 0
    return recs


def test_engine_stop_ends_the_animate_thread():
    """A demo engine's animate thread must die with engine.stop().
    One leaked while-True thread per demo engine ever created
    accumulated across a test run until a garbage-collection pass
    inside one of them hard-aborted the interpreter (CI: 'Fatal
    Python error: Aborted' with dozens of demo.py animate frames in
    the dump)."""
    import threading
    before = {th.ident for th in threading.enumerate()}
    engine = make_demo_engine("targets/f411")
    engine.start()
    engine.stop()
    leaked = [th.name for th in threading.enumerate()
              if th.ident not in before and th.is_alive()]
    assert leaked == []
