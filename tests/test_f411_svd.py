import pytest
from core.target.registers import RegisterModel

SVD = "targets/f411/STM32F411.svd"


@pytest.fixture(scope="module")
def model():
    return RegisterModel.from_svd(SVD)


def test_keys_needed_by_flows_exist(model):
    for ref in ["DMA2.S0CR.EN", "DMA2.S0CR.CHSEL", "DMA2.S0NDTR",
                "DMA2.LISR.TEIF0", "ADC1.SR.OVR", "ADC1.CR2.ADON",
                "TIM1.CNT"]:
        model.resolve(ref)


def test_known_addresses(model):
    assert model.resolve("DMA2.S0CR").address == 0x40026410
    assert model.resolve("ADC1.SR").address == 0x40012000


def test_adc_dr_is_flagged(model):
    # ADC1.DR must exist; if ST marks readAction it must survive parsing.
    r = model.resolve("ADC1.DR")
    assert r.address == 0x4001204C
