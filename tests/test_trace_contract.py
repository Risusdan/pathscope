import struct

import pytest
from core.trace.contract import (MAGIC, VERSION, MAX_CH, RECORD_SIZE,
                                 RING_COUNT, ContractError, TraceDesc,
                                 encode_desc, encode_record, parse_desc,
                                 parse_records, record_word_addr)

def _desc_words(endian):
    return encode_desc(period_us=1000, ring_addr=0x20001000,
                       ring_count=RING_COUNT, wr_seq=7,
                       watch_addrs=[0x20000000] * 3 + [0] * 7,
                       watch_count=3, generation=2, status=0,
                       endian=endian)

def _patch_bytes(words, byte_offset, fmt_char, value):
    """Corrupt one little-endian field of an already-encoded (endian="<")
    descriptor at the raw-byte level, then re-derive the word list the
    same way the adapter would compose it - so these tests exercise
    parse_desc()'s own validation, independent of encode_desc()."""
    raw = bytearray(struct.pack("<{0}I".format(len(words)), *words))
    struct.pack_into("<" + fmt_char, raw, byte_offset, value)
    return list(struct.unpack("<{0}I".format(len(words)), bytes(raw)))

@pytest.mark.parametrize("endian", ["<", ">"])
def test_desc_round_trip(endian):
    d = parse_desc(_desc_words(endian))
    assert d.endian == endian
    assert (d.version, d.max_ch, d.period_us) == (VERSION, MAX_CH, 1000)
    assert d.watch_count == 3 and d.generation == 2
    assert d.watch_addrs[0] == 0x20000000 and d.watch_addrs[9] == 0

@pytest.mark.parametrize("endian", ["<", ">"])
def test_record_round_trip(endian):
    d = parse_desc(_desc_words(endian))
    words = encode_record(seq=41, gen=2,
                          slots=list(range(100, 110)), endian=endian)
    assert len(words) * 4 == RECORD_SIZE
    (r,) = parse_records(words, d)
    assert (r.seq, r.gen) == (41, 2)
    assert r.slots == tuple(range(100, 110))

def test_bad_magic_and_version_raise():
    words = _desc_words("<")
    with pytest.raises(ContractError):
        parse_desc([0xDEADBEEF] + words[1:])
    bad = encode_desc(period_us=1000, ring_addr=0, ring_count=RING_COUNT,
                      wr_seq=0, watch_addrs=[0] * 10, watch_count=0,
                      generation=0, status=0, endian="<", version=9)
    with pytest.raises(ContractError):
        parse_desc(bad)

def test_bad_max_ch_raises():
    # max_ch is the u8 at byte offset 6 of the descriptor.
    words = _patch_bytes(_desc_words("<"), 6, "B", 12)
    with pytest.raises(ContractError):
        parse_desc(words)

def test_bad_record_size_raises():
    # record_size is the u16 at byte offset 12 of the descriptor.
    words = _patch_bytes(_desc_words("<"), 12, "H", 44)
    with pytest.raises(ContractError):
        parse_desc(words)

def test_bad_ring_count_raises():
    # ring_count is the u16 at byte offset 14 of the descriptor.
    words = _patch_bytes(_desc_words("<"), 14, "H", 0)
    with pytest.raises(ContractError):
        parse_desc(words)

def test_record_word_addr_wraps():
    d = parse_desc(_desc_words("<"))
    assert record_word_addr(d, 0) == 0x20001000
    assert record_word_addr(d, RING_COUNT) == 0x20001000
    assert record_word_addr(d, 5) == 0x20001000 + 5 * RECORD_SIZE
