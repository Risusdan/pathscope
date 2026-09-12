import pytest
from core.engine.evaluator import Evaluator, ExprError
from core.engine.history import History
from core.engine.snapshot import Sample, Snapshot
from core.target.registers import RegisterModel
from tests.test_registers import FIXTURE


@pytest.fixture
def ev(tmp_path):
    p = tmp_path / "t.svd"
    p.write_text(FIXTURE)
    return Evaluator(RegisterModel.from_svd(str(p)), History())


def snap(t=1.0, **kv):
    return Snapshot(values={k.replace("_", "."): Sample(v, t)
                            for k, v in kv.items()}, t=t, rate_hz=10.0)


def test_field_extraction_and_compare(ev):
    c = ev.compile("ADC1.SR.OVR == 1")
    assert c.refs == {"ADC1.SR"}
    assert c.eval(snap(ADC1_SR=1 << 5)) is True
    assert c.eval(snap(ADC1_SR=0)) is False


def test_boolean_and_arith(ev):
    c = ev.compile("DMA2.S0CR.EN == 1 and DMA2.S0CR.CHSEL == 6")
    v = (6 << 25) | 1
    assert c.eval(snap(DMA2_S0CR=v)) is True
    assert c.eval(snap(DMA2_S0CR=1)) is False


def test_missing_value_yields_none(ev):
    c = ev.compile("ADC1.SR.OVR == 1")
    assert c.eval(snap()) is None


def test_stalled_uses_history(ev):
    c = ev.compile("stalled(DMA2.S0NDTR, 500)")
    h = ev.history
    h.record("DMA2.S0NDTR", 0.0, 100)
    h.record("DMA2.S0NDTR", 0.1, 99)     # changing
    assert c.eval(snap(t=0.2, DMA2_S0NDTR=99)) is False
    h.record("DMA2.S0NDTR", 0.2, 99)
    h.record("DMA2.S0NDTR", 0.9, 99)     # frozen for 800 ms
    assert c.eval(snap(t=0.9, DMA2_S0NDTR=99)) is True


def test_changed_builtin(ev):
    c = ev.compile("changed(ADC1.SR)")
    ev.history.record("ADC1.SR", 0.0, 0)
    ev.history.record("ADC1.SR", 0.1, 2)
    assert c.eval(snap(t=0.1, ADC1_SR=2)) is True


@pytest.mark.parametrize("bad", [
    "__import__('os')",
    "[x for x in range(9)]",
    "ADC1.SR.OVR.__class__",
    "open('/etc/passwd')",
    "lambda: 1",
    "GHOST.REG == 1",
    "stalled(123, 500)",
    "ADC1.SR ==",
])
def test_rejects_disallowed(ev, bad):
    with pytest.raises(ExprError):
        ev.compile(bad)
