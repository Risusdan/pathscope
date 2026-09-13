"""Host-side stand-in for the target's ps_trace.c firmware trace
peripheral (design doc section 3), used by both the demo engine
(ui/demo.py, for --demo mode and screenshot self-checks with no
hardware attached) and the test suite (tests/trace_sim.py re-exports
this module's FakeTraceFirmware unchanged). Lives under core/trace/
rather than tests/ because shipped code must not import from tests/;
it stays stdlib-only itself, importing only core.trace.contract and
monkey-patching a MockAdapter - both core - so the core zero-dependency
rule holds.

It interprets writes into the descriptor's watch-table fields the way
real firmware would (count=0 closes the gate, a nonzero count
transition validates the pending addresses against a whitelist and,
on success, bumps generation and starts sampling), and step() advances
the ring the way the periodic ISR would. The count-word write in
particular is modeled as the real 32-bit store it is: watch_count,
generation and 2 reserved bytes all physically share that one word
(contract.py: offsets 64/65/66-67), so writing it decomposes the full
word and overwrites this sim's own generation state from whatever byte
pattern the write carried, exactly as SRAM would - a host that writes
a raw, non-generation-preserving count word sees generation pinned
here exactly like it would on real hardware (see
core/trace/reader.py's TraceReader._write_count for the write host
code actually makes, which preserves it).

The sim never keeps a second copy of "what the descriptor/ring
contain" that could drift from what a reader actually parses: after
every externally visible mutation (a watch-table write, or a step()
call) it re-derives the descriptor words via contract.encode_desc from
its own authoritative fields and pokes them straight into adapter.mem
with MockAdapter.set_word - the exact bytes a TraceReader will later
run back through contract.parse_desc/parse_records. Ring records are
written the same way via encode_record. This means tests exercise the
real wire-format parsing path end to end, not a shortcut.

Writes into the descriptor's [desc_addr, desc_addr + DESC_SIZE) range
are intercepted by monkey-patching the adapter instance's write32;
everything outside that range passes through unmodified to the
original bound method, so ordinary register writes elsewhere still
behave like a plain MockAdapter.

Cross-thread contract: ui/demo.py's animate thread calls step() while
a TraceReader on the engine's poller thread may be reading the same
adapter.mem concurrently, with no lock on either side. This is safe by
construction, not by accident, for the same three reasons real
firmware-over-a-probe is safe despite the same lack of a lock:

  (i)   Every word write is a single MockAdapter.set_word() call, which
        is one dict-item assignment - atomic under the GIL. A reader
        never observes a torn (half-old, half-new) word.
  (ii)  Publish order mirrors firmware: _resync() writes every
        descriptor word EXCEPT wr_seq first, then wr_seq itself last,
        as its own set_word() call - and step() writes all of a
        batch's ring-record words before it ever calls _resync(). So a
        concurrent reader can only ever see wr_seq claim a record
        exists after that record's words are already in memory, never
        before.
  (iii) Whatever a reader catches mid-step anyway (e.g. wr_seq
        advancing between its two descriptor reads within one
        refresh() call) is exactly the torn-read window
        TraceReader._filter_stable() already guards against - the same
        two-layer check documented in core/trace/reader.py, unchanged
        by whether the "firmware" on the other end is silicon or this
        sim.
"""
import struct
from typing import Tuple

from core.adapter.mock import MockAdapter
from core.trace.contract import (DESC_SIZE, MAX_CH, RECORD_SIZE, RING_COUNT,
                                 STATUS_BAD_ADDR, STATUS_BAD_COUNT,
                                 STATUS_OK, TraceDesc, VERSION,
                                 WATCH_ADDRS_OFFSET, WATCH_COUNT_OFFSET,
                                 encode_desc, encode_record,
                                 record_word_addr)

# wr_seq is the last header field before the watch table begins (see
# contract._HEADER_FMT: "...II" ends with ring_addr then wr_seq, and
# watch_addrs starts immediately after) and, thanks to the struct's
# standard sizes with no padding, it occupies a whole word of its own.
# Deriving its offset from the already-imported WATCH_ADDRS_OFFSET
# instead of a new literal keeps this tied to the one place the layout
# is defined.
_WR_SEQ_WORD_OFFSET = WATCH_ADDRS_OFFSET - 4


class FakeTraceFirmware:
    def __init__(self, adapter: MockAdapter, desc_addr: int = 0x20010000,
                 ring_addr: int = 0x20001000, period_us: int = 1000,
                 whitelist: Tuple[Tuple[int, int], ...] =
                 ((0x20000000, 0x20020000),), endian: str = "<"):
        # The ring physically spans RING_COUNT * RECORD_SIZE bytes from
        # ring_addr (0xC000 bytes at the contract's current constants,
        # since the T11 hardware gate's RING_COUNT bump to 1024 - was
        # 0x3000 at the old 256) - a desc_addr inside that span would
        # have the descriptor's own resync silently clobbering
        # whichever ring record(s) land on the same bytes. Guard it
        # here instead of letting a future caller rediscover that the
        # hard way. The default desc_addr (0x20010000) sits well past
        # the default ring's end (0x2000D000) for exactly this reason -
        # it moved out from 0x20004000 (inside the old, smaller ring's
        # span) when the ring grew.
        ring_end = ring_addr + RING_COUNT * RECORD_SIZE
        desc_end = desc_addr + DESC_SIZE
        if desc_addr < ring_end and ring_addr < desc_end:
            raise ValueError(
                "trace descriptor [0x%08X,0x%08X) overlaps the ring "
                "[0x%08X,0x%08X)" % (desc_addr, desc_end, ring_addr,
                                     ring_end))

        self._adapter = adapter
        self.desc_addr = desc_addr
        self._ring_addr = ring_addr
        self._period_us = period_us
        self._whitelist = whitelist
        self._endian = endian

        self._status = STATUS_OK
        self._watch_addrs = [0] * MAX_CH
        self._watch_count = 0
        self._generation = 0
        self._wr_seq = 0

        # Capture the original bound method before shadowing it with an
        # instance attribute - the standard way to monkey-patch one
        # instance without touching the MockAdapter class.
        self._original_write32 = adapter.write32
        adapter.write32 = self._patched_write32
        self._resync()

    def step(self, n: int = 1) -> None:
        """Emit n ring records, honoring the current gate: with the
        watch table closed (watch_count == 0) records are still
        written (wr_seq keeps advancing) but every slot is 0, matching
        real firmware's "still ticking, nothing sampled" behavior."""
        for _ in range(n):
            slots = [0] * MAX_CH
            for i in range(self._watch_count):
                slots[i] = self._adapter.mem.get(self._watch_addrs[i], 0)
            words = encode_record(seq=self._wr_seq, gen=self._generation,
                                  slots=slots, endian=self._endian)
            addr = record_word_addr(self._current_desc(), self._wr_seq)
            for i, w in enumerate(words):
                self._adapter.set_word(addr + 4 * i, w)
            self._wr_seq += 1
        self._resync()

    def _patched_write32(self, addr: int, value: int) -> None:
        if self.desc_addr <= addr < self.desc_addr + DESC_SIZE:
            self._handle_desc_write(addr, value)
        else:
            self._original_write32(addr, value)

    def _handle_desc_write(self, addr: int, value: int) -> None:
        offset = addr - self.desc_addr
        if (WATCH_ADDRS_OFFSET <= offset < WATCH_COUNT_OFFSET
                and (offset - WATCH_ADDRS_OFFSET) % 4 == 0):
            idx = (offset - WATCH_ADDRS_OFFSET) // 4
            self._watch_addrs[idx] = self._decode_addr_word(value)
        elif offset == WATCH_COUNT_OFFSET:
            self._handle_count_word_write(value & 0xFFFFFFFF)
        # Any other in-range offset (status, period_us, ring geometry,
        # wr_seq, generation, ...) is firmware-owned, not part of the
        # host write protocol - ignore it rather than let a stray write
        # corrupt state that _current_desc() re-derives below.
        self._resync()

    def _decode_addr_word(self, word: int) -> int:
        """IMPORTANT 6: inverse of TraceReader._compose_addr_word -
        recover the logical address a watch_addrs[] write actually
        means from the wire word it carried. The wire word is always
        the little-endian composition of the raw target bytes
        (contract.py's own adapter convention, _words_to_bytes);
        reinterpreting those same raw bytes per this target's own
        endianness recovers the value _validate()/step() need to use
        as a real memory key (self._adapter.mem is addressed by plain
        logical addresses, independent of wire byte order) - symmetric
        with how _resync() serializes watch_addrs back out through
        encode_desc for the read side."""
        raw = struct.pack("<I", word & 0xFFFFFFFF)
        return struct.unpack(self._endian + "I", raw)[0]

    def _handle_count_word_write(self, word: int) -> None:
        """CRITICAL 1: model the real 32-bit write, not just "the host
        wants watch_count changed" - the word at WATCH_COUNT_OFFSET
        physically holds watch_count (byte 0), generation (byte 1) and
        2 reserved bytes (bytes 2-3), so a real store to this address
        overwrites all four bytes in SRAM at once, generation included,
        regardless of what the host meant to change. Decompose the
        full word and OVERWRITE self._generation from it exactly as
        hardware would - deriving generation only from this sim's own
        accept logic and silently discarding whatever byte pattern the
        write actually carried would structurally hide a host bug that
        writes a raw, non-generation-preserving count word (which pins
        generation at 1 forever on real hardware: every such write
        clobbers generation to 0 first, and an accept always
        increments from whatever base is currently there)."""
        count = word & 0xFF
        self._generation = (word >> 8) & 0xFF
        self._handle_count_write(count)

    def _handle_count_write(self, new_count: int) -> None:
        if self._watch_count == 0 and new_count != 0:
            ok, status = self._validate(new_count)
            if ok:
                self._status = STATUS_OK
                self._watch_count = new_count
                self._generation += 1
            else:
                self._status = status
                # Rejected: count stays 0, the pending addresses simply
                # never get committed.
        else:
            # IMPORTANT 5(a): closing the gate (N->0) or restating a
            # count needs no validation - and, matching real firmware
            # exactly (ps_trace.c only ever touches status inside the
            # accept/reject branch above), does NOT touch status
            # either. A rejection's status is only ever replaced by
            # the NEXT accept/reject, not silently cleared here -
            # eagerly clearing it here (as this sim previously did)
            # diverged from firmware and hid a real race: a valid
            # retry right after a rejection could read back a
            # spuriously-cleared OK before an accept had actually run.
            self._watch_count = new_count

    def _validate(self, new_count: int) -> Tuple[bool, int]:
        if new_count > MAX_CH:
            return False, STATUS_BAD_COUNT
        for i in range(new_count):
            addr = self._watch_addrs[i]
            in_range = any(lo <= addr < hi for lo, hi in self._whitelist)
            if not in_range or addr not in self._adapter.mem:
                return False, STATUS_BAD_ADDR
        return True, STATUS_OK

    def _current_desc(self) -> TraceDesc:
        return TraceDesc(endian=self._endian, version=VERSION,
                         max_ch=MAX_CH, status=self._status,
                         period_us=self._period_us,
                         record_size=RECORD_SIZE, ring_count=RING_COUNT,
                         ring_addr=self._ring_addr, wr_seq=self._wr_seq,
                         watch_addrs=tuple(self._watch_addrs),
                         watch_count=self._watch_count,
                         generation=self._generation)

    def _resync(self) -> None:
        """Re-derive and write back the full descriptor. wr_seq - the
        word a concurrent TraceReader watches to know new data exists
        - is written LAST, as its own set_word() call, only after
        every other descriptor word is already in memory. Combined
        with step() writing all of a batch's ring-record words before
        ever calling _resync(), this mirrors firmware's own publish
        order (payload, then the sequence number that announces it) -
        see the module docstring's cross-thread contract."""
        desc = self._current_desc()
        words = encode_desc(period_us=desc.period_us,
                            ring_addr=desc.ring_addr,
                            ring_count=desc.ring_count,
                            wr_seq=desc.wr_seq,
                            watch_addrs=list(desc.watch_addrs),
                            watch_count=desc.watch_count,
                            generation=desc.generation,
                            status=desc.status, endian=desc.endian)
        wr_seq_idx = _WR_SEQ_WORD_OFFSET // 4
        for i, w in enumerate(words):
            if i != wr_seq_idx:
                self._adapter.set_word(self.desc_addr + 4 * i, w)
        self._adapter.set_word(self.desc_addr + 4 * wr_seq_idx,
                               words[wr_seq_idx])
