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
faster than the reader drains them; when that happens (wr_seq has
advanced by more than ring_count since the last refresh) the
overwritten span is counted into self.lost and the read window is
clamped to start at wr_seq - ring_count instead of an address that no
longer holds what its sequence number would claim. The window is
contiguous in ring order except when it straddles the wrap point, so
at most two block reads cover it.

set_watch() mirrors the watch-table gate protocol firmware implements:
writing count=0 closes the gate, then the pending addresses are
written one word each, then the new count is written to request the
transition. Firmware validates only on a 0->N transition and reports
the result by bumping generation (accepted) or setting a nonzero
status (rejected) - set_watch polls status() for that instead of
assuming success.
"""
import struct
import time
from typing import List, Optional

from ..engine.core import Engine
from .contract import (DESC_SIZE, MAX_CH, STATUS_BAD_ADDR, STATUS_BAD_COUNT,
                       STATUS_OK, ContractError, TraceDesc, TraceRecord,
                       parse_desc, parse_records, record_word_addr)

# Byte offsets of the watch-table fields inside the descriptor - see
# core/trace/contract.py's _DESC_FMT_BODY ("IHBBIHHII{max_ch}IBB2x"):
# 24 bytes of fixed header, then the MAX_CH-entry address table, then
# the count/generation byte pair (which shares its word with two bytes
# of tail padding).
_HEADER_FMT = "IHBBIHHII"           # magic..wr_seq
WATCH_ADDRS_OFFSET = struct.calcsize("<" + _HEADER_FMT)        # 24
WATCH_COUNT_OFFSET = WATCH_ADDRS_OFFSET + 4 * MAX_CH            # 64

_DESC_WORDS = DESC_SIZE // 4
_STATUS_NAMES = {STATUS_BAD_ADDR: "BAD_ADDR", STATUS_BAD_COUNT: "BAD_COUNT"}


class TraceError(Exception):
    pass


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

        if wr_seq - self.last_seq > ring_count:
            self.lost += (wr_seq - self.last_seq) - ring_count
            start = wr_seq - ring_count
        else:
            start = self.last_seq + 1

        if start >= wr_seq:
            self.last_seq = wr_seq - 1
            return []

        records = self._read_records(desc, start, wr_seq)
        self.last_seq = wr_seq - 1
        return records

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
