"""Binary layout of the firmware trace-buffer contract (design doc
section 3). Firmware writes structures in its own native endianness;
the adapter that transports memory reads always composes each 32-bit
word little-endian regardless of target byte order (pyOCD semantics,
see core/adapter/base.py). So a word list read off the wire is not
directly the struct's bytes - it is the struct's bytes regrouped
4-at-a-time through an LE composition step. Every function here
inverts that step first (struct.pack("<%dI", *words) recovers the
exact memory image byte-for-byte, no matter which endianness the
target used) and only then applies the target's native endian format
string to interpret the fields. `encode_*` runs the same path in
reverse so a simulated big-endian target, encoded and fed back through
a little-endian word transport, reproduces real firmware behavior
exactly - this is what lets tests and the firmware simulator share one
code path with the real probe.
"""
import struct
from dataclasses import dataclass
from typing import List

MAGIC = 0x50435350          # 'PSCP' read as little-endian u32
VERSION = 1
MAX_CH = 10
RING_COUNT = 256
RECORD_SIZE = 8 + 4 * MAX_CH          # 48
DESC_SIZE = 24 + 4 * MAX_CH + 4       # 68: header 24 + table 40 + tail 4
STATUS_OK, STATUS_BAD_ADDR, STATUS_BAD_COUNT = 0, 1, 2

# struct format bodies (endian prefix added by caller). Standard size,
# no implicit alignment - the wire layout is the spec table verbatim.
_DESC_FMT_BODY = "IHBBIHHII{0}IBB2x".format(MAX_CH)
_RECORD_FMT_BODY = "IB3x{0}I".format(MAX_CH)


class ContractError(Exception):
    pass


@dataclass(frozen=True)
class TraceDesc:
    endian: str          # "<" or ">"
    version: int
    max_ch: int
    status: int
    period_us: int
    record_size: int
    ring_count: int
    ring_addr: int
    wr_seq: int
    watch_addrs: tuple   # length max_ch
    watch_count: int
    generation: int


@dataclass(frozen=True)
class TraceRecord:
    seq: int
    gen: int
    slots: tuple         # length max_ch, raw u32


def _words_to_bytes(words: List[int]) -> bytes:
    """Recover the raw memory image from adapter-composed words: each
    word was built as int.from_bytes(raw4, "little"), so packing it
    back with "<I" yields exactly those same 4 raw bytes."""
    return struct.pack("<{0}I".format(len(words)), *words)


def _bytes_to_words(raw: bytes) -> List[int]:
    """Inverse of _words_to_bytes: simulate the adapter composing raw
    memory bytes into words, little-endian, 4 bytes at a time."""
    n = len(raw) // 4
    return list(struct.unpack("<{0}I".format(n), raw))


def _detect_endian(first_word: int) -> str:
    raw = struct.pack("<I", first_word)
    if struct.unpack("<I", raw)[0] == MAGIC:
        return "<"
    if struct.unpack(">I", raw)[0] == MAGIC:
        return ">"
    raise ContractError("no trace descriptor at this address")


def parse_desc(words: List[int]) -> TraceDesc:
    if not words:
        raise ContractError("no trace descriptor at this address")
    endian = _detect_endian(words[0])

    expected_words = DESC_SIZE // 4
    if len(words) < expected_words:
        raise ContractError("no trace descriptor at this address")
    raw = _words_to_bytes(words[:expected_words])

    fields = struct.unpack(endian + _DESC_FMT_BODY, raw)
    (magic, version, max_ch, status, period_us, record_size, ring_count,
     ring_addr, wr_seq) = fields[:9]
    watch_addrs = fields[9:9 + MAX_CH]
    watch_count, generation = fields[9 + MAX_CH:9 + MAX_CH + 2]

    if version != VERSION:
        raise ContractError("unsupported trace version {0}".format(version))
    if max_ch != MAX_CH:
        raise ContractError(
            "unsupported channel count {0}".format(max_ch))
    if record_size != RECORD_SIZE:
        raise ContractError(
            "unsupported record_size {0}".format(record_size))
    if ring_count != RING_COUNT:
        raise ContractError(
            "unsupported ring_count {0}".format(ring_count))

    return TraceDesc(endian=endian, version=version, max_ch=max_ch,
                     status=status, period_us=period_us,
                     record_size=record_size, ring_count=ring_count,
                     ring_addr=ring_addr, wr_seq=wr_seq,
                     watch_addrs=tuple(watch_addrs),
                     watch_count=watch_count, generation=generation)


def parse_records(words: List[int], desc: TraceDesc) -> List[TraceRecord]:
    word_count = desc.record_size // 4
    fmt = desc.endian + "IB3x{0}I".format(desc.max_ch)
    records = []
    for i in range(0, len(words) - word_count + 1, word_count):
        raw = _words_to_bytes(words[i:i + word_count])
        seq, gen, *slots = struct.unpack(fmt, raw)
        records.append(TraceRecord(seq=seq, gen=gen, slots=tuple(slots)))
    return records


def encode_desc(*, period_us: int, ring_addr: int, ring_count: int,
                wr_seq: int, watch_addrs, watch_count: int,
                generation: int, status: int, endian: str,
                version: int = VERSION, max_ch: int = MAX_CH,
                record_size: int = RECORD_SIZE) -> List[int]:
    if len(watch_addrs) != max_ch:
        raise ContractError(
            "watch_addrs must have length {0}".format(max_ch))
    fmt = endian + "IHBBIHHII{0}IBB2x".format(max_ch)
    raw = struct.pack(fmt, MAGIC, version, max_ch, status, period_us,
                      record_size, ring_count, ring_addr, wr_seq,
                      *watch_addrs, watch_count, generation)
    return _bytes_to_words(raw)


def encode_record(seq: int, gen: int, slots: List[int],
                  endian: str) -> List[int]:
    if len(slots) != MAX_CH:
        raise ContractError("slots must have length {0}".format(MAX_CH))
    fmt = endian + "IB3x{0}I".format(MAX_CH)
    raw = struct.pack(fmt, seq, gen, *slots)
    return _bytes_to_words(raw)


def record_word_addr(desc: TraceDesc, seq: int) -> int:
    return desc.ring_addr + (seq % desc.ring_count) * desc.record_size
