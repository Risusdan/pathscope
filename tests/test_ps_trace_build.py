import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("arm-none-eabi-gcc") is None,
                    reason="cross toolchain not installed")
def test_firmware_builds_with_ps_trace():
    subprocess.run(["make", "-C", "firmware/blackpill_adc_dma", "clean"],
                   check=True, capture_output=True)
    r = subprocess.run(["make", "-C", "firmware/blackpill_adc_dma"],
                       check=True, capture_output=True, text=True)
    nm = subprocess.run(["arm-none-eabi-nm", "firmware/blackpill_adc_dma/fw.elf"],
                        check=True, capture_output=True, text=True)
    assert "ps_trace_desc" in nm.stdout
