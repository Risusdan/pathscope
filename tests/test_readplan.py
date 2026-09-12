import pytest
from core.engine.readplan import build_read_plan
from core.target.registers import RegisterModel
from tests.test_registers import FIXTURE


@pytest.fixture
def model(tmp_path):
    (tmp_path / "t.svd").write_text(FIXTURE)
    return RegisterModel.from_svd(str(tmp_path / "t.svd"))


def test_adjacent_registers_merge(model):
    # DMA2.S0CR @ +0x10 and DMA2.S1CR @ +0x28: gap 5 words -> one op
    plan = build_read_plan({"DMA2.S0CR", "DMA2.S1CR"}, model)
    assert len(plan) == 1
    op = plan[0]
    assert op.addr == 0x40026410
    assert op.count == 7                      # 0x10..0x28 inclusive
    assert ("DMA2.S0CR", 0) in op.targets
    assert ("DMA2.S1CR", 6) in op.targets


def test_distant_registers_split(model):
    plan = build_read_plan({"ADC1.SR", "DMA2.S0CR"}, model)
    assert len(plan) == 2
    assert plan[0].addr == 0x40012000         # sorted by address


def test_merge_gap_zero_never_merges(model):
    plan = build_read_plan({"DMA2.S0CR", "DMA2.S1CR"}, model,
                           merge_gap_words=0)
    assert len(plan) == 2
