import time

import pytest
from core.adapter.mock import MockAdapter
from core.engine.core import Engine, EngineError
from core.engine.poller import PollerState

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


def test_set_watch_adds_and_refuses(rig):
    a, e, updates = rig
    # ADC2 is the fixture's derived peripheral (base 0x40012100);
    # ADC2.SR is watchable, ADC1.DR is guarded by readAction.
    refused = e.set_watch({"ADC2.SR", "ADC1.DR"})
    assert refused == ["ADC1.DR"]              # guarded stays guarded
    assert "ADC2.SR" in e.polled
    assert wait_for(
        lambda: updates and "ADC2.SR" in updates[-1].snapshot.values)


def test_set_watch_survives_target_lost_reconnect(rig):
    """FIX 2 regression: a set_watch() swap command queued right
    before the adapter drops can be drained-and-failed by
    poller.py's _fail_pending() during reconnect without ever being
    applied, leaving poller.plan stale while engine.polled (and
    set_watch()'s own return value) claim the swap took effect.
    Engine now re-subscribes to poller.on_state and rebuilds+resubmits
    the plan from the current self.polled on every transition back to
    RUNNING, so the watched register shows up regardless of whether
    the original swap survived the race with the failure below."""
    a, e, updates = rig
    states = []
    e.on_state(states.append)

    refused = e.set_watch({"ADC2.SR"})
    assert refused == []
    a.fail_next(1)                             # force one TARGET_LOST

    assert wait_for(lambda: PollerState.TARGET_LOST in states)
    assert wait_for(lambda: states and states[-1] == PollerState.RUNNING,
                    timeout=3.0)
    assert wait_for(
        lambda: updates and "ADC2.SR" in updates[-1].snapshot.values,
        timeout=3.0)


def test_read_words_on_demand(rig):
    a, e, updates = rig
    a.set_word(0x20000000, 0xCAFE)
    assert e.read_words(0x20000000, 1) == [0xCAFE]


def test_halt_resume_via_engine(rig):
    a, e, updates = rig
    e.halt()
    assert a.is_running() is False
    e.resume()
    assert a.is_running() is True


def test_addr_watch_polls_and_records(rig):
    a, e, updates = rig
    a.set_word(0x20000010, 0xBEEF)
    key = e.add_addr_watch(0x20000010, "my_var")
    assert key == "@20000010"
    assert e.addr_watch_labels[key] == "my_var"
    assert wait_for(
        lambda: updates and updates[-1].snapshot.value(key) == 0xBEEF)
    assert wait_for(lambda: len(e.history.series(key)) >= 2)


def test_addr_watch_guarded_refused(rig):
    a, e, updates = rig
    import pytest as _pytest
    with _pytest.raises(EngineError):
        e.add_addr_watch(0x4001204C, "adc_dr")   # ADC1.DR readAction


def test_addr_watch_alignment_refused(rig):
    a, e, updates = rig
    import pytest as _pytest
    with _pytest.raises(EngineError):
        e.add_addr_watch(0x20000001, "misaligned")


def test_remove_addr_watch(rig):
    a, e, updates = rig
    key = e.add_addr_watch(0x20000020, "gone")
    e.remove_addr_watch(key)
    assert key not in e.polled


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
