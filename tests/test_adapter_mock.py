import pytest
from core.adapter.base import TargetAdapter, TargetInfo, TargetLostError
from core.adapter.mock import MockAdapter


def test_mock_is_an_adapter():
    assert issubclass(MockAdapter, TargetAdapter)


def test_block_read_returns_words_and_logs():
    a = MockAdapter({0x40000000: 0x11, 0x40000004: 0x22, 0x40000008: 0x33})
    info = a.connect()
    assert isinstance(info, TargetInfo)
    assert a.read_block32(0x40000000, 3) == [0x11, 0x22, 0x33]
    assert a.read_log == [(0x40000000, 3)]


def test_unbacked_addresses_read_zero():
    a = MockAdapter({})
    a.connect()
    assert a.read_block32(0x20000000, 2) == [0, 0]


def test_set_word_and_write32():
    a = MockAdapter({})
    a.connect()
    a.set_word(0x40000000, 5)
    a.write32(0x40000004, 7)
    assert a.read_block32(0x40000000, 2) == [5, 7]


def test_fail_next_raises_target_lost_then_recovers():
    a = MockAdapter({0x0: 1})
    a.connect()
    a.fail_next(2)
    with pytest.raises(TargetLostError):
        a.read_block32(0x0, 1)
    with pytest.raises(TargetLostError):
        a.read_block32(0x0, 1)
    assert a.read_block32(0x0, 1) == [1]


def test_halt_resume_state():
    a = MockAdapter({})
    a.connect()
    assert a.is_running() is True
    a.halt()
    assert a.is_running() is False
    a.resume()
    assert a.is_running() is True
