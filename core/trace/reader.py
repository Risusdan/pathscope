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
transition. Firmware validates whenever count != 0 and reports the
result by bumping generation (accepted) or setting a nonzero status
(rejected) - set_watch polls status() for that instead of assuming
success.

The word at WATCH_COUNT_OFFSET physically holds four fields -
watch_count, generation, and two reserved bytes (contract.py:
offsets 64/65/66-67) - so any write to it is a real 32-bit store that
touches all four bytes at once, generation included, no matter what
the host meant to change. Every count-word write set_watch makes
(both the count=0 and the count=N write) is therefore composed from
the CURRENT word read back from the target first, changing only the
count byte and leaving generation/reserved exactly as read
(_compose_count_word/_write_count below) - writing the raw count
alone would clobber generation to whatever the write's other bytes
happened to be, pinning it there on real hardware forever after
(every subsequent naive write repeats the same clobber). This is
race-free: firmware only ever changes generation on an accept, which
cannot precede the host's own final write of that same word.

A record surviving the torn-read guard above can still be honestly
sampled and still be WRONG to show: while a table edit is in flight
(the gate closed, addresses being rewritten, not yet accepted),
firmware keeps emitting records every tick, still tagged with the
OLD (unchanged) generation, with every slot reading 0 since
watch_count is 0 for that whole span - rendered as-is, a channel that
was legitimately being watched before AND after the edit would show a
spurious drop to zero exactly at the edit. Symmetrically, a record
sampled before an edit but not yet drained out of the ring by the time
the edit's set_watch() call returns can be appended after the
reader's caller has already re-laid-out its OWN storage for the new
table (e.g. TraceStore's column compaction on a channel removal,
ui/trace_store.py) - it would land in a column that used to mean what
it did but no longer does, a real value under the wrong channel.

Both are fixed the same way, at the reader: self._expected_gen is the
generation this reader currently trusts - set once at discover() (to
whatever generation was already running) and again on every successful
non-empty set_watch() (to the NEW, just-accepted generation; set_watch
records the OLD generation on entry, before any write, purely to
detect that accept). refresh() drops - and counts into self.lost -
every record whose own gen tag doesn't match self._expected_gen. Since
self._expected_gen only advances at the exact moment a set_watch()
call returns successfully, EVERY record from before that moment still
sitting undelivered (in-flight gate-window zeros and not-yet-drained
pre-edit stragglers alike) reads as a mismatch on the first refresh()
after the edit and is dropped as an honest gap, rather than rendered
as real data under a layout it no longer describes; only records
sampled under the table this reader is actually watching now ever
reach a caller.
"""
import struct
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
        elif rec.seq <= margin:
            dropped += 1              # layer 2: torn mid-write
        else:
            kept.append(rec)
    return kept, dropped


def _filter_current_gen(records: List[TraceRecord], expected_gen: Optional[int]
                        ) -> Tuple[List[TraceRecord], int]:
    """IMPORTANT 3+4's honest-gap filter (see the module docstring): a
    record surviving _filter_stable() can still have been sampled
    under a table this reader no longer trusts - a gate-window zero
    from mid-edit, or a pre-edit straggler drained after the caller
    already re-laid-out its own storage for the new table. Both carry
    a gen tag that doesn't match `expected_gen` (the generation this
    reader currently trusts - see TraceReader._expected_gen) and are
    dropped, uniformly, the same way a torn read is: not delivered,
    counted into self.lost. `expected_gen` of None (nothing known yet)
    keeps every record. Returns (kept, dropped_count) - kept preserves
    the input order, same contract as _filter_stable."""
    if expected_gen is None:
        return list(records), 0
    kept = [r for r in records if r.gen == expected_gen]
    return kept, len(records) - len(kept)


class TraceReader:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.desc_addr = None  # type: Optional[int]
        self.desc = None  # type: Optional[TraceDesc]
        self.last_seq = -1
        self.lost = 0
        # IMPORTANT 3+4: the generation this reader currently trusts -
        # see the module docstring and _filter_current_gen. Set at
        # discover() and on every successful set_watch().
        self._expected_gen = None  # type: Optional[int]

    def discover(self, desc_addr: int) -> TraceDesc:
        self.desc_addr = desc_addr
        desc = self._read_desc()
        self.desc = desc
        self.last_seq = desc.wr_seq - 1
        self._expected_gen = desc.generation
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

        # IMPORTANT 3+4: drop anything sampled under a table this
        # reader no longer trusts (a gate-window zero, or a pre-edit
        # straggler) - see the module docstring and
        # _filter_current_gen.
        kept, gen_dropped = _filter_current_gen(kept, self._expected_gen)
        self.lost += gen_dropped

        self.last_seq = wr_seq - 1
        return kept

    def set_watch(self, addrs: List[int]) -> None:
        if self.desc is None:
            raise TraceError("discover() must run before set_watch()")
        if len(addrs) > self.desc.max_ch:
            raise TraceError(
                "too many watch channels: %d > max %d"
                % (len(addrs), self.desc.max_ch))

        if not addrs:
            # MUST-FIX m4: firmware's gate never validates - and so
            # never bumps generation - for an empty table (count == 0
            # is always the "gate closed" state, never itself
            # accepted). Polling for a generation change here would
            # always run to the full timeout for no reason (a ~500ms
            # stall on every remove-to-empty edit); success is simply
            # the count reading back 0, with nothing left to validate.
            self._write_count(0)
            desc = self.status()
            if desc.watch_count != 0:
                raise TraceError("firmware did not clear the watch table")
            return

        prev_gen = self.desc.generation
        self._write_count(0)
        # IMPORTANT 2(c): dwell at least one sample period after
        # closing the gate before writing the new addresses - ps_trace.h
        # documents this as a should, not a must (the firmware's
        # level-based gate, see ps_trace.c, no longer depends on it for
        # correctness), but it's still what keeps a mid-edit sample
        # from ever observing a half-written address table.
        period_s = self.desc.period_us / 1e6
        time.sleep(max(2 * period_s, 0.002))
        for i, addr in enumerate(addrs):
            self.engine.write_word(
                self.desc_addr + WATCH_ADDRS_OFFSET + 4 * i,
                self._compose_addr_word(addr))
        self._write_count(len(addrs))

        desc = self.status()
        tries = 1
        while desc.generation == prev_gen and tries < 10:
            time.sleep(0.05)
            desc = self.status()
            tries += 1

        if desc.generation == prev_gen:
            # IMPORTANT 5(b): generation advancing is the only
            # trustworthy accept signal (reliable now that CRITICAL 1
            # keeps a stray write from ever clobbering it) - a status
            # observed WHILE still polling is not trusted, since
            # firmware (and the sim, faithfully - see IMPORTANT 5(a))
            # leaves a REJECTED status in place until the next accept,
            # so it could be stale from a wholly earlier rejection
            # rather than a verdict on this submission. Only once no
            # accept ever arrived is status consulted, to name why.
            if desc.status != STATUS_OK:
                name = _STATUS_NAMES.get(desc.status, str(desc.status))
                raise TraceError("firmware rejected table: %s" % name)
            raise TraceError("firmware did not accept watch table")

        # IMPORTANT 3+4: this reader now trusts the NEW generation -
        # every record still in flight under the old one (gate-window
        # zeros, pre-edit stragglers) will read as a mismatch on the
        # next refresh() and be dropped as an honest gap instead of
        # rendered under a layout it no longer describes.
        self._expected_gen = desc.generation

    def _compose_addr_word(self, addr: int) -> int:
        """IMPORTANT 6: wire word for one watch_addrs[] uint32 slot,
        byte-swapped for a big-endian target the same way
        contract.encode_desc handles every multi-byte field - pack the
        value in the target's own byte order, then reinterpret those
        same raw bytes as the little-endian word every Engine
        transaction actually carries (contract.py's module
        docstring)."""
        raw = struct.pack(self.desc.endian + "I", addr & 0xFFFFFFFF)
        return struct.unpack("<I", raw)[0]

    def _compose_count_word(self, new_count: int, current_word: int) -> int:
        """CRITICAL 1: the word at WATCH_COUNT_OFFSET physically holds
        watch_count (byte 0), generation (byte 1) and 2 reserved bytes
        (bytes 2-3) - see contract.py's WATCH_COUNT_OFFSET/
        GENERATION_OFFSET. `current_word` is the wire word most
        recently read back from that address; this recovers its raw
        target bytes the same way contract.py's _words_to_bytes does
        (struct.pack("<I", word) - a word is always the LE composition
        of the raw bytes, regardless of target endianness), reads
        watch_count/generation/reserved out of that raw byte order,
        and repacks with new_count in place of the old watch_count
        byte, generation and reserved carried through unchanged.
        Single-byte struct fields are never reordered by endianness,
        so this is correct for either target byte order, but is still
        routed through self.desc.endian's pack/unpack calls to match
        the idiom contract.py uses everywhere else for this data."""
        raw = struct.pack("<I", current_word & 0xFFFFFFFF)
        endian = self.desc.endian
        _old_count, gen, res0, res1 = struct.unpack(endian + "BBBB", raw)
        new_raw = struct.pack(endian + "BBBB", new_count & 0xFF, gen,
                              res0, res1)
        return struct.unpack("<I", new_raw)[0]

    def _write_count(self, count: int) -> None:
        """Read-modify-write the watch_count word, preserving whatever
        generation/reserved bytes are currently there (see
        _compose_count_word and the module docstring) - used for both
        the count=0 and count=N writes set_watch makes."""
        current_word = self.engine.read_words(
            self.desc_addr + WATCH_COUNT_OFFSET, 1)[0]
        new_word = self._compose_count_word(count, current_word)
        self.engine.write_word(self.desc_addr + WATCH_COUNT_OFFSET, new_word)

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
