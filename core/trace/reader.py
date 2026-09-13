"""TraceReader: drains firmware trace records and drives the
watch-table gate protocol described in the design doc (section 3).

All target access goes through Engine.read_words/write_word, so every
transaction is serialized onto the poller thread like everything else
hardware-facing in this codebase - the reader itself does no locking
and assumes single-threaded callers, the same assumption Engine makes
of its own callers.

refresh() never re-delivers a record: it tracks the highest sequence
number it has already returned (self.last_seq) and only asks for
[last_seq+1, wr_seq). Firmware's ring can overwrite unread records
faster than the reader drains them; the ring only ever holds the most
recent ring_count records, so wr_seq - ring_count is always the oldest
still-live sequence number regardless of what has been delivered. When
that oldest-live point has moved past last_seq+1 (the first
undelivered record), everything strictly between them was both
undelivered and overwritten - that gap is counted into self.lost and
the read window is clamped to start at wr_seq - ring_count instead of
an address that no longer holds what its sequence number would claim.
The window is contiguous in ring order except when it straddles the
wrap point, so at most two block reads cover it.

Reading the descriptor and then the record range is two separate,
non-atomic transactions - nothing stops firmware from advancing (or
even wrapping) wr_seq while the record reads are in flight, which
could otherwise hand back torn or stale data under a seq number that
claims it is something it no longer is, with last_seq silently
advancing past it as if it had been delivered cleanly. refresh() below
guards against that with two layers, applied by _filter_stable() after
the record reads complete:

  1. Embedded-seq equality: record i of the batch was requested to be
     seq start+i. If what's actually there carries a different seq,
     that ring slot was completely overwritten by a newer record
     during the read - whole-record replacement, caught by comparing
     the record's own embedded seq field against what was asked for.
  2. Overwrite margin: a record can still be torn mid-write even when
     its seq field happens to read back matching, if firmware started
     overwriting that same slot again before finishing. To catch that,
     the descriptor is re-read once more after the record reads; any
     record whose seq now falls at or behind the ring's current oldest
     still-live point (new_wr_seq - ring_count) is no longer
     trustworthy and is dropped too, even though it passed layer 1.

A record surviving both layers was stably published for the entire
read. Every dropped record - from either layer - adds to self.lost;
last_seq still advances to the end of the originally requested window
regardless, since a dropped record is never re-delivered on the next
refresh() (there is nothing to redeliver - the ring has moved past it
by then in exactly the way self.lost already accounts for on the
"before we even asked" side).

set_watch() mirrors the watch-table gate protocol firmware implements:
writing count=0 closes the gate, then the pending addresses are
written one word each, then the new count is written to request the
transition. Firmware validates only on a 0->N transition and reports
the result by bumping generation (accepted) or setting a nonzero
status (rejected) - set_watch polls status() for that instead of
assuming success.
"""
import time
from typing import List, Optional, Tuple

from ..engine.core import Engine
from .contract import (DESC_SIZE, STATUS_BAD_ADDR, STATUS_BAD_COUNT,
                       STATUS_OK, WATCH_ADDRS_OFFSET, WATCH_COUNT_OFFSET,
                       ContractError, TraceDesc, TraceRecord, parse_desc,
                       parse_records, record_word_addr)

_DESC_WORDS = DESC_SIZE // 4
_STATUS_NAMES = {STATUS_BAD_ADDR: "BAD_ADDR", STATUS_BAD_COUNT: "BAD_COUNT"}


class TraceError(Exception):
    pass


def _filter_stable(records: List[TraceRecord], start: int, new_wr_seq: int,
                   ring_count: int) -> Tuple[List[TraceRecord], int]:
    """The two-layer torn-read guard described in the module docstring,
    factored out as a pure function so it can be unit-tested directly
    against crafted TraceRecord lists with no sim/engine machinery.

    `records` is the batch parsed from the window requested as
    [start, start + len(records)) at the time of the first descriptor
    read. `new_wr_seq` is wr_seq from re-reading the descriptor AFTER
    the record reads completed; `ring_count` is that same re-read's
    ring_count (a fixed protocol constant, but taken from the same
    read as new_wr_seq for consistency).

    Returns (kept, dropped_count) - kept preserves the input order."""
    margin = new_wr_seq - ring_count
    kept = []
    dropped = 0
    for i, rec in enumerate(records):
        expected_seq = start + i
        if rec.seq != expected_seq:
            dropped += 1              # layer 1: whole-record replacement
        elif rec.seq < margin:
            dropped += 1              # layer 2: torn mid-write
        else:
            kept.append(rec)
    return kept, dropped


class TraceReader:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.desc_addr = None  # type: Optional[int]
        self.desc = None  # type: Optional[TraceDesc]
        self.last_seq = -1
        self.lost = 0

    def discover(self, desc_addr: int) -> TraceDesc:
        self.desc_addr = desc_addr
        desc = self._read_desc()
        self.desc = desc
        self.last_seq = desc.wr_seq - 1
        return desc

    def status(self) -> TraceDesc:
        self.desc = self._read_desc()
        return self.desc

    def refresh(self) -> List[TraceRecord]:
        desc = self._read_desc()
        self.desc = desc
        wr_seq = desc.wr_seq
        ring_count = desc.ring_count

        # The ring only ever holds the most recent ring_count records,
        # so the oldest still-live sequence number is wr_seq -
        # ring_count regardless of what the reader has consumed.
        # last_seq + 1 is the first record the reader hasn't delivered
        # yet; whenever that lags behind the oldest still-live seq, the
        # gap between them was both undelivered and overwritten.
        first_live = wr_seq - ring_count
        first_undelivered = self.last_seq + 1
        if first_undelivered < first_live:
            self.lost += first_live - first_undelivered
            start = first_live
        else:
            start = first_undelivered

        if start >= wr_seq:
            self.last_seq = wr_seq - 1
            return []

        raw_records = self._read_records(desc, start, wr_seq)

        # The descriptor and record reads are non-atomic (see module
        # docstring) - re-read the descriptor once more and filter
        # anything that may have been torn while the record reads were
        # in flight.
        post_desc = self._read_desc()
        self.desc = post_desc
        kept, dropped = _filter_stable(raw_records, start, post_desc.wr_seq,
                                       post_desc.ring_count)
        self.lost += dropped

        self.last_seq = wr_seq - 1
        return kept

    def set_watch(self, addrs: List[int]) -> None:
        if self.desc is None:
            raise TraceError("discover() must run before set_watch()")
        if len(addrs) > self.desc.max_ch:
            raise TraceError(
                "too many watch channels: %d > max %d"
                % (len(addrs), self.desc.max_ch))

        prev_gen = self.desc.generation
        self.engine.write_word(self.desc_addr + WATCH_COUNT_OFFSET, 0)
        for i, addr in enumerate(addrs):
            self.engine.write_word(
                self.desc_addr + WATCH_ADDRS_OFFSET + 4 * i, addr)
        self.engine.write_word(
            self.desc_addr + WATCH_COUNT_OFFSET, len(addrs))

        desc = self.status()
        tries = 1
        while (desc.generation == prev_gen and desc.status == STATUS_OK
               and tries < 10):
            time.sleep(0.05)
            desc = self.status()
            tries += 1

        if desc.status != STATUS_OK:
            name = _STATUS_NAMES.get(desc.status, str(desc.status))
            raise TraceError("firmware rejected table: %s" % name)

    def _read_desc(self) -> TraceDesc:
        words = self.engine.read_words(self.desc_addr, _DESC_WORDS)
        try:
            return parse_desc(words)
        except ContractError as e:
            raise TraceError(str(e))

    def _read_records(self, desc: TraceDesc, start: int,
                      wr_seq: int) -> List[TraceRecord]:
        ring_count = desc.ring_count
        words_per_record = desc.record_size // 4
        count = wr_seq - start
        start_idx = start % ring_count

        if start_idx + count <= ring_count:
            addr = record_word_addr(desc, start)
            words = self.engine.read_words(addr, count * words_per_record)
            return parse_records(words, desc)

        # Wraps past the ring end: split into the tail of the ring and
        # the run that continues from index 0.
        first = ring_count - start_idx
        second = count - first
        addr1 = record_word_addr(desc, start)
        words1 = self.engine.read_words(addr1, first * words_per_record)
        addr2 = record_word_addr(desc, start + first)
        words2 = self.engine.read_words(addr2, second * words_per_record)
        return parse_records(words1, desc) + parse_records(words2, desc)
