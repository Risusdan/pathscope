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
real firmware would (count=0 closes the gate, a 0->N count transition
validates the pending addresses against a whitelist and, on success,
bumps generation and starts sampling), and step() advances the ring
the way the periodic ISR would.

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
"""
from typing import Tuple

from core.adapter.mock import MockAdapter
from core.trace.contract import (DESC_SIZE, MAX_CH, RECORD_SIZE, RING_COUNT,
                                 STATUS_BAD_ADDR, STATUS_BAD_COUNT,
                                 STATUS_OK, TraceDesc, VERSION,
                                 WATCH_ADDRS_OFFSET, WATCH_COUNT_OFFSET,
                                 encode_desc, encode_record,
                                 record_word_addr)


class FakeTraceFirmware:
    def __init__(self, adapter: MockAdapter, desc_addr: int = 0x20004000,
                 ring_addr: int = 0x20001000, period_us: int = 1000,
                 whitelist: Tuple[Tuple[int, int], ...] =
                 ((0x20000000, 0x20020000),), endian: str = "<"):
        # The ring physically spans RING_COUNT * RECORD_SIZE bytes from
        # ring_addr (0x3000 bytes at the contract's current constants) -
        # a desc_addr inside that span would have the descriptor's own
        # resync silently clobbering whichever ring record(s) land on
        # the same bytes. Guard it here instead of letting a future
        # caller rediscover that the hard way.
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
            self._watch_addrs[idx] = value & 0xFFFFFFFF
        elif offset == WATCH_COUNT_OFFSET:
            self._handle_count_write(value & 0xFF)
        # Any other in-range offset (status, period_us, ring geometry,
        # wr_seq, generation, ...) is firmware-owned, not part of the
        # host write protocol - ignore it rather than let a stray write
        # corrupt state that _current_desc() re-derives below.
        self._resync()

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
            # Closing the gate (N->0) or restating a count needs no
            # validation; a fresh STATUS_OK clears any status left
            # over from a previous rejection.
            self._status = STATUS_OK
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
        desc = self._current_desc()
        words = encode_desc(period_us=desc.period_us,
                            ring_addr=desc.ring_addr,
                            ring_count=desc.ring_count,
                            wr_seq=desc.wr_seq,
                            watch_addrs=list(desc.watch_addrs),
                            watch_count=desc.watch_count,
                            generation=desc.generation,
                            status=desc.status, endian=desc.endian)
        for i, w in enumerate(words):
            self._adapter.set_word(self.desc_addr + 4 * i, w)
