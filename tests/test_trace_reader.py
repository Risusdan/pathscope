import dataclasses

import pytest
from core.adapter.mock import MockAdapter
from core.engine.core import Engine
from core.trace.contract import TraceRecord
from core.trace.reader import TraceError, TraceReader, _filter_stable
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


def _rec(seq, gen=0):
    return TraceRecord(seq=seq, gen=gen, slots=(0,) * 10)


def test_filter_stable_keeps_records_matching_seq_above_margin():
    records = [_rec(10), _rec(11), _rec(12)]
    kept, dropped = _filter_stable(records, start=10, new_wr_seq=13,
                                   ring_count=256)
    assert kept == records
    assert dropped == 0


def test_filter_stable_drops_mismatched_embedded_seq():
    # Position 1 was requested to be seq 11 but actually holds seq 99 -
    # that ring slot was overwritten by a newer record during the read
    # (layer 1: whole-record replacement).
    records = [_rec(10), _rec(99), _rec(12)]
    kept, dropped = _filter_stable(records, start=10, new_wr_seq=13,
                                   ring_count=256)
    assert [r.seq for r in kept] == [10, 12]
    assert dropped == 1


def test_filter_stable_drops_records_inside_overwrite_margin():
    # Every embedded seq matches its expected position, but new_wr_seq
    # (from the post-read descriptor re-check) has moved on far enough
    # that seq 10 and 11 now sit at or behind the ring's oldest
    # still-live point (margin = new_wr_seq - ring_count = 12) - torn
    # mid-write even though layer 1 alone wouldn't have caught it.
    records = [_rec(10), _rec(11), _rec(12)]
    kept, dropped = _filter_stable(records, start=10, new_wr_seq=268,
                                   ring_count=256)
    assert [r.seq for r in kept] == [12]
    assert dropped == 2


def test_filter_stable_drops_everything_when_margin_engulfs_batch():
    records = [_rec(10), _rec(11), _rec(12)]
    kept, dropped = _filter_stable(records, start=10, new_wr_seq=270,
                                   ring_count=256)   # margin = 14
    assert kept == []
    assert dropped == 3


def test_refresh_drops_records_torn_by_wrap_during_read(monkeypatch):
    # Integration-level check that refresh() actually wires the
    # post-read descriptor re-check to _filter_stable(): patch just
    # the SECOND of refresh()'s two _read_desc() calls to report
    # wr_seq advanced by a full ring_count, simulating firmware having
    # wrapped the whole ring while the record reads were in flight.
    # Every record just parsed then falls inside the overwrite margin
    # even though nothing about the underlying data actually tore.
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        r.discover(fw.desc_addr)
        r.set_watch([0x20000000])
        fw.step(5)                        # seq 0..4, wr_seq=5

        real_read_desc = r._read_desc
        calls = {"n": 0}

        def fake_read_desc():
            calls["n"] += 1
            desc = real_read_desc()
            if calls["n"] == 2:
                desc = dataclasses.replace(
                    desc, wr_seq=desc.wr_seq + desc.ring_count)
            return desc

        monkeypatch.setattr(r, "_read_desc", fake_read_desc)
        before_lost = r.lost
        recs = r.refresh()
        assert recs == []
        assert r.lost == before_lost + 5
    finally:
        engine.stop()
