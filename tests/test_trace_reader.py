import dataclasses
import time

import numpy as np
import pytest
from core.adapter.mock import MockAdapter
from core.engine.core import Engine
from core.trace.contract import (WATCH_ADDRS_OFFSET, WATCH_COUNT_OFFSET,
                                 TraceRecord)
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
    # overwritten before refresh() ever asked for it: that is the
    # pre-read gap, K records.
    #
    # The read window refresh() then requests starts exactly at
    # 3+K - the ring's oldest still-live point. Nothing writes again
    # before refresh()'s post-read descriptor re-check, so that
    # re-check sees the SAME wr_seq, making margin = wr_seq-256 =
    # 3+K too: the very first record of the batch (seq 3+K) sits
    # exactly at the margin. Per the torn-read guard's at-or-behind
    # rule, a record at the margin is not provably untorn (firmware's
    # next, still-unpublished write would land in that exact ring
    # slot) and is dropped as well - one more record lost, and the
    # resumed read effectively starts one past it.
    K = 8
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        r.discover(fw.desc_addr)
        r.set_watch([0x20000000])
        fw.step(3); r.refresh()           # last_seq becomes 2
        fw.step(256 + K)                  # > ring_count: oldest overwritten
        recs = r.refresh()
        assert r.lost == K + 1
        assert recs[0].seq == 3 + K + 1   # resumed past the hole and
                                           # the unprovable margin slot
    finally:
        engine.stop()


def test_naive_raw_count_write_pins_generation_but_set_watch_does_not():
    # CRITICAL 1 regression: WATCH_COUNT_OFFSET is a real 32-bit word
    # shared by watch_count, generation and 2 reserved bytes
    # (contract.py offsets 64/65/66-67). A naive write of just the
    # count - the bug this guards against - clobbers generation to
    # whatever the write's own upper bytes are (0, for a plain
    # literal), and the sim (post-fix) reproduces that exactly like
    # real SRAM would: every such "close gate, write addr, request
    # count" cycle re-clobbers generation to 0 right before firmware's
    # own accept bumps it straight back to 1, so it never advances no
    # matter how many edits happen.
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        r.discover(fw.desc_addr)

        for _ in range(2):
            engine.write_word(fw.desc_addr + WATCH_COUNT_OFFSET, 0)
            engine.write_word(fw.desc_addr + WATCH_ADDRS_OFFSET, 0x20000000)
            engine.write_word(fw.desc_addr + WATCH_COUNT_OFFSET, 1)
        assert r.status().generation == 1     # pinned - reproduces the bug

        # TraceReader.set_watch composes every count-word write from
        # the current word instead (TraceReader._write_count) -
        # generation advances normally, edit after edit.
        r.set_watch([0x20000004])
        assert r.status().generation == 2
        r.set_watch([0x20000000])
        assert r.status().generation == 3
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


def test_set_watch_reject_then_valid_retry_succeeds():
    # IMPORTANT 5 regression: firmware (and the sim, faithfully - see
    # FakeTraceFirmware._handle_count_write) leaves a rejection's
    # status in place until the next accept, rather than clearing it
    # the moment the gate closes. set_watch's own success criterion is
    # generation advancing, not "status happens to read OK" - a valid
    # retry right after a rejection must succeed, not be mistaken for
    # a repeat of the stale BAD_ADDR still sitting in status.
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        r.discover(fw.desc_addr)
        with pytest.raises(TraceError):
            r.set_watch([0x99999990])     # outside sim whitelist
        r.set_watch([0x20000000])         # must succeed, no spurious BAD_ADDR
        desc = r.status()
        assert desc.status == 0
        assert desc.watch_count == 1
    finally:
        engine.stop()


def test_set_watch_big_endian_round_trip():
    # IMPORTANT 6 regression: set_watch must byte-swap the words it
    # writes for a big-endian target - a raw (unswapped) write would
    # store the wrong bytes for both the address and the count/
    # generation word, and the sim, faithfully modeling a BE target's
    # memory (see FakeTraceFirmware._decode_addr_word/_resync), would
    # then either reject the table or sample the wrong address.
    adapter = MockAdapter({0x20000000: 111, 0x20000004: 222})
    fw = FakeTraceFirmware(adapter, endian=">")
    engine = Engine.load("targets/f411", adapter, interval_s=0.01)
    engine.start()
    try:
        r = TraceReader(engine)
        desc = r.discover(fw.desc_addr)
        assert desc.endian == ">"
        r.set_watch([0x20000000, 0x20000004])
        fw.step(3)
        recs = r.refresh()
        assert len(recs) >= 3
        assert recs[-1].slots[0] == 111
        assert recs[-1].slots[1] == 222
    finally:
        engine.stop()


def test_set_watch_empty_skips_generation_wait():
    # MUST-FIX m4 regression: firmware's gate never validates (and so
    # never bumps generation) for an empty table - waiting for a
    # generation change here would always run to the full ~500ms
    # timeout for no reason. set_watch([]) must return promptly, with
    # success judged purely by watch_count reading back 0.
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        r.discover(fw.desc_addr)
        r.set_watch([0x20000000])
        gen_before = r.status().generation

        start = time.monotonic()
        r.set_watch([])
        elapsed = time.monotonic() - start

        assert elapsed < 0.1              # no 10x0.05s generation poll
        desc = r.status()
        assert desc.watch_count == 0
        assert desc.generation == gen_before   # firmware never bumps it
    finally:
        engine.stop()


def test_table_edits_drop_stale_generation_records_as_honest_gap():
    # IMPORTANT 3+4 regression, through the (now hardware-faithful)
    # sim: add A, add B (a table edit) while A's just-sampled records
    # are still sitting undrained in the ring, add C the same way, then
    # remove B - the middle channel - compacting C into B's old
    # column. fw.step() is called BEFORE each drain that follows a
    # set_watch(), so every refresh() after the first sees a batch
    # mixing the OLD generation's tail (sampled before that edit, not
    # yet drained) with the NEW generation's fresh records - exactly
    # the window a pre-fix reader would let bleed into the wrong
    # TraceStore column. Post-fix: those stale-generation records never
    # reach the store at all - self.lost grows instead, and every real
    # value that DOES land in a column belongs to that column's actual
    # channel, with a gap where each edit's stale tail was dropped.
    from ui.trace_store import TraceStore

    adapter = MockAdapter({0x20000000: 111, 0x20000004: 222,
                           0x20000008: 333})
    fw = FakeTraceFirmware(adapter)
    engine = Engine.load("targets/f411", adapter, interval_s=0.01)
    engine.start()
    try:
        r = TraceReader(engine)
        desc = r.discover(fw.desc_addr)
        store = TraceStore(desc, window_s=10.0)
        lost_before = r.lost

        store.clear_slot(0)
        r.set_watch([0x20000000])                    # A -> slot 0
        fw.step(3)                                    # left undrained

        store.clear_slot(1)
        r.set_watch([0x20000000, 0x20000004])         # + B -> slot 1
        fw.step(3)
        store.append(r.refresh())                     # mixed-gen batch

        store.clear_slot(2)
        r.set_watch([0x20000000, 0x20000004, 0x20000008])  # + C -> slot 2
        fw.step(3)
        store.append(r.refresh())                     # mixed-gen batch

        store.remove_slot(1)                          # compact: C -> slot 1
        r.set_watch([0x20000000, 0x20000008])          # remove B (middle)
        fw.step(3)
        store.append(r.refresh())                     # mixed-gen batch

        assert r.lost > lost_before

        _t, y0 = store.series(0)
        real0 = y0[~np.isnan(y0)]
        assert real0.size > 0
        assert np.all(real0 == 111)        # slot 0 is always A - never a
                                             # gate-window 0, never B/C's
                                             # value bleeding in
        assert np.isnan(y0).any()           # honest gap at an edit

        _t, y1 = store.series(1)
        real1 = y1[~np.isnan(y1)]
        assert real1.size > 0
        # slot 1 was B (222) until the removal, C (333) afterward -
        # never A's value, never a stray 0.
        assert np.all((real1 == 222) | (real1 == 333))
        assert np.isnan(y1).any()
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
    #
    # 264 records are produced from a fresh discover() (last_seq=-1),
    # so the pre-read clamp starts the batch at seq 264-256=8 - the
    # ring's oldest still-live point at the time of the first
    # descriptor read. The post-read re-check sees the same wr_seq (no
    # further writes happen in between), so margin is also 8: seq 8
    # sits exactly at the margin and is dropped by the torn-read
    # guard's at-or-behind rule, leaving 255 records (seq 9..263).
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        r.discover(fw.desc_addr)
        r.set_watch([0x20000000, 0x20000004])
        fw.step(256 + 8)                  # wraps the ring once, plus 8
        recs = r.refresh()                # window spans the wrap point
        assert len(recs) == 255
        assert recs[0].seq == 9
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


def test_filter_stable_drops_records_at_or_inside_overwrite_margin():
    # Every embedded seq matches its expected position. margin =
    # new_wr_seq - ring_count = 11: seq 10 is fully behind it (already
    # overwritten by the time of the post-read check), and seq 12 is
    # safely ahead of it. seq 11 sits EXACTLY at the margin - that
    # ring slot is exactly the one firmware's next, still-unpublished
    # write (seq new_wr_seq) would land on. That write may already be
    # in flight the instant new_wr_seq becomes visible to us, so a
    # record sitting exactly at the margin is not provably untorn
    # either - it must be dropped too, not just records strictly
    # behind it.
    records = [_rec(10), _rec(11), _rec(12)]
    kept, dropped = _filter_stable(records, start=10, new_wr_seq=267,
                                   ring_count=256)   # margin = 11
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
        # margin = new_wr_seq(261) - ring_count(256) = 5, one past the
        # batch's last record (seq 4) - the batch never lands exactly
        # on the margin here, so this count is unaffected by the
        # at-or-behind vs. strictly-behind boundary fix; all 5 are
        # dropped either way.
        assert recs == []
        assert r.lost == before_lost + 5
    finally:
        engine.stop()
