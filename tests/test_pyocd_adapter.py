# tests/test_pyocd_adapter.py
import pytest
from core.adapter.pyocd_swd import PyOCDAdapter

pytestmark = pytest.mark.hw


def test_connect_and_read_cpuid():
    a = PyOCDAdapter()
    info = a.connect()
    try:
        assert info.idcode & 0xFFF == 0x431          # F411 device id
        cpuid = a.read_block32(0xE000ED00, 1)[0]     # SCB->CPUID
        assert (cpuid >> 4) & 0xFFF == 0xC24         # Cortex-M4 part
        assert a.is_running() in (True, False)
    finally:
        a.disconnect()
