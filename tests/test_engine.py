import time

import pytest
from core.adapter.mock import MockAdapter
from core.engine.core import Engine

TARGET = "tests/fixtures/minitarget"
S0CR = 0x40026410
ADC_SR = 0x40012000


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def rig():
    a = MockAdapter({S0CR: 1, ADC_SR: 0})
    e = Engine.load(TARGET, a, interval_s=0.005)
    updates = []
    e.on_update(updates.append)
    e.start()
    yield a, e, updates
    e.stop()


def test_guarded_register_excluded(rig):
    a, e, updates = rig
    assert "ADC1.DR" in e.excluded
    assert wait_for(lambda: len(updates) >= 1)
    assert "ADC1.DR" not in updates[-1].snapshot.values


def test_flow_state_and_anomaly_end_to_end(rig):
    a, e, updates = rig
    assert wait_for(lambda: len(updates) >= 2)
    assert updates[-1].flows["adc_to_sram"].active is True
    a.set_word(ADC_SR, 1 << 5)                 # inject overrun
    assert wait_for(lambda: any(u.events for u in updates))
    assert wait_for(
        lambda: updates[-1].badges.get("adc1") is not None
        and updates[-1].badges["adc1"].count >= 1)
    all_events = [ev for u in updates for ev in u.events]
    assert all(ev.msg != "never fires" for ev in all_events)
    assert updates[-1].badges["adc1"].count == 1   # exactly one rising edge, no spurious contribution


def test_history_populated(rig):
    a, e, updates = rig
    assert wait_for(lambda: len(e.history.series("DMA2.S0CR")) >= 2)


def test_overlay_guards_needed_register(tmp_path):
    import shutil
    tdir = tmp_path / "t"
    shutil.copytree(TARGET, tdir)
    fl = tdir / "mini.flows.yaml"
    fl.write_text(fl.read_text().replace(
        "force_poll: []",
        "force_poll: []\n  guarded: [\"ADC1.SR\"]"))
    e = Engine.load(str(tdir), MockAdapter({}))
    assert "ADC1.SR" in e.excluded          # overlay guards it
    assert "ADC1.DR" in e.excluded          # svd readAction still guards
    assert 0x40012000 in e.guarded_addrs    # ADC1.SR address
    assert 0x4001204C in e.guarded_addrs    # ADC1.DR address
