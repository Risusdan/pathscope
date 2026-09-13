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
        desc = r.discover(fw.desc_addr)
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
    # ring_count is fixed at 256 (contract.RING_COUNT). Consuming 3
    # records first (seq 0,1,2) leaves last_seq=2, so the first
    # undelivered seq is 3. Producing 256+K more records advances
    # wr_seq to 3+256+K, making the oldest still-live seq
    # wr_seq-256 = 3+K. Everything from 3 (inclusive) up to 3+K
    # (exclusive) - exactly K records - was undelivered and then
    # overwritten before refresh() ever asked for it, so lost must
    # equal K exactly and the resumed read must start exactly at
    # seq 3+K.
    K = 8
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        r.discover(fw.desc_addr)
        r.set_watch([0x20000000])
        fw.step(3); r.refresh()           # last_seq becomes 2
        fw.step(256 + K)                  # > ring_count: oldest overwritten
        recs = r.refresh()
        assert r.lost == K
        assert recs[0].seq == 3 + K       # resumed exactly past the hole
    finally:
        engine.stop()


def test_set_watch_rejected_address_raises():
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        r.discover(fw.desc_addr)
        with pytest.raises(TraceError):
            r.set_watch([0x99999990])     # outside sim whitelist
        assert r.status().watch_count == 0
    finally:
        engine.stop()


def test_set_watch_too_many_raises_before_touching_target():
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        r.discover(fw.desc_addr)
        with pytest.raises(TraceError):
            r.set_watch([0x20000000] * 11)
    finally:
        engine.stop()


def test_generation_marks_table_change():
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        r.discover(fw.desc_addr)
        r.set_watch([0x20000000]); fw.step(2)
        first = r.refresh()
        r.set_watch([0x20000004]); fw.step(2)
        second = r.refresh()
        assert second[-1].gen == first[-1].gen + 1
    finally:
        engine.stop()


def test_full_ring_pass_not_clobbered_by_desc_resync():
    # Regression for the desc/ring address overlap bug: fill the ring
    # past a full lap so the read window spans the wrap point and
    # refresh() must split it into two block reads internally. Every
    # surviving record's sampled slots must still read back exactly
    # what was watched - if the descriptor's own resync ever lands on
    # ring bytes again, this is where a clobbered record would show up
    # as garbage instead of the constant watched values.
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        r.discover(fw.desc_addr)
        r.set_watch([0x20000000, 0x20000004])
        fw.step(256 + 8)                  # wraps the ring once, plus 8
        recs = r.refresh()                # window spans the wrap point
        assert len(recs) == 256
        for rec in recs:
            assert rec.slots[0] == 111
            assert rec.slots[1] == 222
            assert rec.slots[2:] == (0,) * 8
    finally:
        engine.stop()
