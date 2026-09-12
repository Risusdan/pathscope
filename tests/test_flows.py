import pytest
from core.engine.evaluator import Evaluator
from core.engine.history import History
from core.target.flows import load_flows, needed_registers, FlowError
from core.target.registers import RegisterModel
from core.target.topology import load_topology
from tests.test_registers import FIXTURE
from tests.test_topology import GOOD as TOPO

FLOWS = """
activities:
  - name: adc_to_sram
    path: [adc1, mux0, dma2, sram1]
    active_when: "DMA2.S0CR.EN == 1 and DMA2.S0CR.CHSEL == 0"
    progress: "DMA2.S0CR"
    anomalies:
      - {rule: "ADC1.SR.OVR == 1", msg: "ADC overrun", target: adc1}
poll:
  force_poll: []
"""


@pytest.fixture
def env(tmp_path):
    (tmp_path / "t.svd").write_text(FIXTURE)
    (tmp_path / "t.topology.yaml").write_text(TOPO)
    model = RegisterModel.from_svd(str(tmp_path / "t.svd"))
    topo = load_topology(str(tmp_path / "t.topology.yaml"), model)
    return tmp_path, Evaluator(model, History()), topo


def _load(env, text):
    tmp_path, ev, topo = env
    p = tmp_path / "t.flows.yaml"
    p.write_text(text)
    return load_flows(str(p), ev, topo)


def test_loads_and_compiles(env):
    spec = _load(env, FLOWS)
    a = spec.activities[0]
    assert a.name == "adc_to_sram"
    assert a.progress == "DMA2.S0CR"
    assert a.rules[0].target == "adc1"
    assert needed_registers(spec) == {"DMA2.S0CR", "ADC1.SR"}


def test_unknown_path_block_rejected(env):
    with pytest.raises(FlowError):
        _load(env, FLOWS.replace("mux0", "ghost"))


def test_unknown_rule_target_rejected(env):
    with pytest.raises(FlowError):
        _load(env, FLOWS.replace("target: adc1", "target: ghost"))


def test_bad_expression_rejected(env):
    with pytest.raises(FlowError):
        _load(env, FLOWS.replace("ADC1.SR.OVR == 1", "GHOST.X == 1"))


def test_guarded_overlay_parsed(env):
    text = FLOWS.replace("force_poll: []",
                         "force_poll: []\n  guarded: [\"DMA2.S0CR\"]")
    spec = _load(env, text)
    assert spec.guarded == ["DMA2.S0CR"]


def test_guarded_overlay_unknown_ref_rejected(env):
    text = FLOWS.replace("force_poll: []",
                         "force_poll: []\n  guarded: [\"GHOST.REG\"]")
    with pytest.raises(FlowError):
        _load(env, text)
