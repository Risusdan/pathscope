from core.adapter.mock import MockAdapter
from core.engine.core import Engine


def test_f411_target_loads_with_mock_adapter():
    e = Engine.load("targets/f411", MockAdapter({}))
    assert "adc_to_sram" in [a.name for a in e.flowspec.activities]
    assert "mux0" in e.topology.blocks
    # whitelist policy: nothing in this flow file needs a guarded reg
    assert e.excluded == []
