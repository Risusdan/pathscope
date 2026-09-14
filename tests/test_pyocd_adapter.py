# tests/test_pyocd_adapter.py
import pytest
from core.adapter import pyocd_swd
from core.adapter.pyocd_swd import PyOCDAdapter


def test_connect_survives_non_stm32_idcode_read_failure(monkeypatch):
    """DBGMCU_IDCODE (0xE0042000) is an STM32-specific debug register;
    a non-STM32 target faults or transfer-errors reading it. That read
    is display-only (cli.py, ui/app.py), so connect() must still
    succeed, with idcode falling back to 0, instead of surfacing an
    opaque TargetLostError on the very first connect()."""
    class FakeTarget:
        def read32(self, addr):
            raise RuntimeError("transfer fault: no such register")

    class FakeSession:
        def __init__(self):
            self.target = FakeTarget()

        def open(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(
        pyocd_swd.ConnectHelper, "session_with_chosen_probe",
        staticmethod(lambda **kw: FakeSession()))

    a = PyOCDAdapter()
    info = a.connect()
    assert info.idcode == 0


@pytest.mark.hw
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
