import pytest
from core.engine.evaluator import Evaluator
from core.engine.history import History
from core.engine.rules import RuleEngine
from core.engine.snapshot import Sample, Snapshot
from core.target.flows import load_flows
from core.target.registers import RegisterModel
from core.target.topology import load_topology
from tests.test_registers import FIXTURE
from tests.test_topology import GOOD as TOPO
from tests.test_flows import FLOWS


@pytest.fixture
def engine(tmp_path):
    (tmp_path / "t.svd").write_text(FIXTURE)
    (tmp_path / "t.topology.yaml").write_text(TOPO)
    (tmp_path / "t.flows.yaml").write_text(FLOWS)
    model = RegisterModel.from_svd(str(tmp_path / "t.svd"))
    topo = load_topology(str(tmp_path / "t.topology.yaml"), model)
    ev = Evaluator(model, History())
    return RuleEngine(load_flows(str(tmp_path / "t.flows.yaml"), ev, topo))


def snap(t, sr=0, s0cr=1):
    return Snapshot(values={"ADC1.SR": Sample(sr, t),
                            "DMA2.S0CR": Sample(s0cr, t)},
                    t=t, rate_hz=30.0)


def test_flow_active_and_progress(engine):
    u = engine.process(snap(1.0))
    f = u.flows["adc_to_sram"]
    assert f.active is True
    assert f.progress == 1                    # progress reg is DMA2.S0CR
    assert u.events == []


def test_rising_edge_emits_once_and_latches(engine):
    engine.process(snap(1.0, sr=0))
    u2 = engine.process(snap(1.1, sr=1 << 5))     # OVR fires
    assert len(u2.events) == 1
    assert u2.events[0].msg == "ADC overrun"
    assert u2.badges["adc1"].count == 1
    assert u2.badges["adc1"].active is True
    u3 = engine.process(snap(1.2, sr=1 << 5))     # still true: no new event
    assert u3.events == []
    assert u3.badges["adc1"].count == 1
    u4 = engine.process(snap(1.3, sr=0))          # condition gone: latched
    assert u4.badges["adc1"].count == 1
    assert u4.badges["adc1"].active is False


def test_clear_badge_and_refire(engine):
    engine.process(snap(1.0, sr=1 << 5))
    engine.clear_badge("adc1")
    u = engine.process(snap(1.1, sr=1 << 5))      # no rising edge
    assert u.badges["adc1"].count == 0
    assert u.badges["adc1"].active is True        # honest: still firing
    engine.process(snap(1.2, sr=0))
    u2 = engine.process(snap(1.3, sr=1 << 5))     # new rising edge
    assert u2.badges["adc1"].count == 1


def test_missing_data_is_inactive_not_firing(engine):
    u = engine.process(Snapshot(values={}, t=1.0, rate_hz=0.0))
    assert u.flows["adc_to_sram"].active is False
    assert u.events == []
