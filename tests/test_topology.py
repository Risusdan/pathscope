import pytest
from core.target.registers import RegisterModel
from core.target.topology import load_topology, TopologyError

# reuse the SVD fixture from test_registers
from tests.test_registers import FIXTURE

GOOD = """
blocks:
  - id: adc1
    kind: peripheral
    svd: ADC1
  - id: mux0
    kind: mux
    select: "DMA2.S0CR.CHSEL"
  - id: dma2
    kind: dma
    svd: DMA2
    ports: {periph: [l, 0.5], mem: [r, 0.5]}
  - id: sram1
    kind: memory
    base: 0x20000000
    size: 0x20000
edges:
  - {from: adc1, to: mux0, when_select: 0}
  - {from: mux0, to: dma2, to_port: periph}
  - {from: dma2, to: sram1, label: M0AR}
"""


@pytest.fixture
def model(tmp_path):
    p = tmp_path / "t.svd"
    p.write_text(FIXTURE)
    return RegisterModel.from_svd(str(p))


def _load(tmp_path, model, text):
    p = tmp_path / "t.topology.yaml"
    p.write_text(text)
    return load_topology(str(p), model)


def test_loads_blocks_and_edges(tmp_path, model):
    t = _load(tmp_path, model, GOOD)
    assert set(t.blocks) == {"adc1", "mux0", "dma2", "sram1"}
    assert t.blocks["sram1"].base == 0x20000000
    assert t.blocks["dma2"].ports["mem"] == ("r", 0.5)
    assert len(t.edges) == 3
    assert t.edges[0].when_select == 0
    assert t.edges[1].dst_port == "periph"
    assert t.edges[0].id == "adc1->mux0#0"


def test_unknown_edge_endpoint_rejected(tmp_path, model):
    bad = GOOD + "  - {from: adc1, to: ghost}\n"
    with pytest.raises(TopologyError):
        _load(tmp_path, model, bad)


def test_unknown_svd_name_rejected(tmp_path, model):
    with pytest.raises(TopologyError):
        _load(tmp_path, model, GOOD.replace("svd: ADC1", "svd: GHOST"))


def test_bad_kind_rejected(tmp_path, model):
    with pytest.raises(TopologyError):
        _load(tmp_path, model, GOOD.replace("kind: dma", "kind: banana"))


def test_duplicate_id_rejected(tmp_path, model):
    with pytest.raises(TopologyError):
        _load(tmp_path, model, GOOD.replace("id: sram1", "id: adc1"))


LAYOUT = GOOD + """
layout:
  adc1: {x: 70, y: 280, w: 140, h: 80}
  sram1: {x: 780, y: 230}
"""


def test_layout_parsed(tmp_path, model):
    t = _load(tmp_path, model, LAYOUT)
    assert (t.blocks["adc1"].x, t.blocks["adc1"].y) == (70, 280)
    assert t.blocks["adc1"].w == 140
    assert t.blocks["sram1"].w is None       # size optional
    assert t.blocks["dma2"].x is None        # absent -> None


def test_layout_unknown_id_rejected(tmp_path, model):
    with pytest.raises(TopologyError):
        _load(tmp_path, model, GOOD + "\nlayout:\n  ghost: {x: 1, y: 2}\n")


def test_layout_legend_absent_is_none(tmp_path, model):
    t = _load(tmp_path, model, LAYOUT)
    assert t.legend is None


def test_layout_legend_parsed(tmp_path, model):
    text = LAYOUT + "  legend: {x: 700, y: 400}\n"
    t = _load(tmp_path, model, text)
    assert t.legend == (700, 400)
    # legend is not itself a block:
    assert "legend" not in t.blocks


def test_edge_points_parsed(tmp_path, model):
    text = GOOD.replace(
        "  - {from: adc1, to: mux0, when_select: 0}",
        "  - {from: adc1, to: mux0, when_select: 0, "
        "points: [[210, 320], [232, 320], [232, 350]]}")
    t = _load(tmp_path, model, text)
    assert t.edges[0].points == [(210, 320), (232, 320), (232, 350)]
