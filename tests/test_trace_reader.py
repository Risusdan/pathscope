import pytest
from core.adapter.mock import MockAdapter
from core.engine.core import Engine
from core.trace.reader import TraceError, TraceReader
from tests.trace_sim import FakeTraceFirmware


def _rig():
    adapter = MockAdapter({0x20000000: 111, 0x20000004: 222})
    fw = FakeTraceFirmware(adapter)
    engine = Engine.load("targets/f411", adapter, interval_s=0.01)
    engine.start()
    return adapter, fw, engine


def test_discover_and_drain_records():
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        desc = r.discover(0x20002000)
        assert desc.period_us == 1000
        r.set_watch([0x20000000, 0x20000004])
        fw.step(5)
        recs = r.refresh()
        assert len(recs) >= 5
        assert recs[-1].slots[0] == 111 and recs[-1].slots[1] == 222
        assert r.refresh() == []          # nothing new, no re-delivery
    finally:
        engine.stop()


def test_overflow_counts_lost_and_resumes():
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        r.discover(0x20002000)
        r.set_watch([0x20000000])
        fw.step(3); r.refresh()
        fw.step(300)                      # > ring_count: oldest overwritten
        recs = r.refresh()
        assert r.lost > 0
        assert recs[0].seq > 3            # resumed past the hole
    finally:
        engine.stop()


def test_set_watch_rejected_address_raises():
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        r.discover(0x20002000)
        with pytest.raises(TraceError):
            r.set_watch([0x99999990])     # outside sim whitelist
        assert r.status().watch_count == 0
    finally:
        engine.stop()


def test_set_watch_too_many_raises_before_touching_target():
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        r.discover(0x20002000)
        with pytest.raises(TraceError):
            r.set_watch([0x20000000] * 11)
    finally:
        engine.stop()


def test_generation_marks_table_change():
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        r.discover(0x20002000)
        r.set_watch([0x20000000]); fw.step(2)
        first = r.refresh()
        r.set_watch([0x20000004]); fw.step(2)
        second = r.refresh()
        assert second[-1].gen == first[-1].gen + 1
    finally:
        engine.stop()
