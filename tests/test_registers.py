import pytest
from core.target.registers import RegisterModel, SvdError

FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<device><name>TESTCHIP</name>
  <peripherals>
    <peripheral>
      <name>ADC1</name>
      <baseAddress>0x40012000</baseAddress>
      <registers>
        <register>
          <name>SR</name><addressOffset>0x0</addressOffset>
          <fields>
            <field><name>OVR</name><bitOffset>5</bitOffset><bitWidth>1</bitWidth></field>
            <field><name>EOC</name><bitOffset>1</bitOffset><bitWidth>1</bitWidth></field>
          </fields>
        </register>
        <register>
          <name>DR</name><addressOffset>0x4C</addressOffset>
          <readAction>clear</readAction>
        </register>
      </registers>
    </peripheral>
    <peripheral derivedFrom="ADC1">
      <name>ADC2</name>
      <baseAddress>0x40012100</baseAddress>
    </peripheral>
    <peripheral>
      <name>DMA2</name>
      <baseAddress>0x40026400</baseAddress>
      <registers>
        <register>
          <dim>2</dim><dimIncrement>0x18</dimIncrement>
          <name>S%sCR</name><addressOffset>0x10</addressOffset>
          <fields>
            <field><name>EN</name><bitOffset>0</bitOffset><bitWidth>1</bitWidth></field>
            <field><name>CHSEL</name><bitOffset>25</bitOffset><bitWidth>3</bitWidth></field>
          </fields>
        </register>
      </registers>
    </peripheral>
  </peripherals>
</device>
"""


@pytest.fixture
def model(tmp_path):
    p = tmp_path / "test.svd"
    p.write_text(FIXTURE)
    return RegisterModel.from_svd(str(p))


def test_register_address(model):
    r = model.resolve("ADC1.SR")
    assert r.address == 0x40012000
    assert (r.lsb, r.msb) == (0, 31)
    assert r.reg_key == "ADC1.SR"
    assert r.read_action is None


def test_field_resolution(model):
    f = model.resolve("ADC1.SR.OVR")
    assert (f.lsb, f.msb) == (5, 5)
    assert f.reg_key == "ADC1.SR"           # field refs point at parent reg
    assert f.address == 0x40012000


def test_read_action_surfaces(model):
    assert model.resolve("ADC1.DR").read_action == "clear"


def test_derived_from_copies_registers(model):
    r = model.resolve("ADC2.SR.OVR")
    assert r.address == 0x40012100          # base of ADC2, not ADC1
    assert model.peripherals["ADC1"].registers["SR"].fields is not model.peripherals["ADC2"].registers["SR"].fields


def test_dim_expansion(model):
    s0 = model.resolve("DMA2.S0CR")
    s1 = model.resolve("DMA2.S1CR.CHSEL")
    assert s0.address == 0x40026410
    assert s1.address == 0x40026410 + 0x18
    assert (s1.lsb, s1.msb) == (25, 27)


def test_unknown_names_raise(model):
    with pytest.raises(SvdError):
        model.resolve("NOPE.REG")
    with pytest.raises(SvdError):
        model.resolve("ADC1.NOPE")
    with pytest.raises(SvdError):
        model.resolve("ADC1.SR.NOPE")
    with pytest.raises(SvdError):
        model.resolve("justoneword")
