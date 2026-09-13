import pytest
from core.trace.contract import (MAGIC, VERSION, MAX_CH, RECORD_SIZE,
                                 ContractError, TraceDesc, encode_desc,
                                 encode_record, parse_desc,
                                 parse_records, record_word_addr)

def _desc_words(endian):
    return encode_desc(period_us=1000, ring_addr=0x20001000,
                       ring_count=256, wr_seq=7,
                       watch_addrs=[0x20000000] * 3 + [0] * 7,
                       watch_count=3, generation=2, status=0,
                       endian=endian)

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
    bad = encode_desc(period_us=1000, ring_addr=0, ring_count=256,
                      wr_seq=0, watch_addrs=[0] * 10, watch_count=0,
                      generation=0, status=0, endian="<", version=9)
    with pytest.raises(ContractError):
        parse_desc(bad)

def test_record_word_addr_wraps():
    d = parse_desc(_desc_words("<"))
    assert record_word_addr(d, 0) == 0x20001000
    assert record_word_addr(d, 256) == 0x20001000
    assert record_word_addr(d, 5) == 0x20001000 + 5 * RECORD_SIZE
