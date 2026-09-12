# tests/test_poller.py
import time

import pytest
from core.adapter.mock import MockAdapter
from core.engine.poller import Poller, PollerState
from core.engine.readplan import ReadOp


def make_poller(adapter, snaps, states, interval=0.005):
    plan = [ReadOp(addr=0x40000000, count=2,
                   targets=[("P.A", 0), ("P.B", 1)])]
    p = Poller(adapter, plan, interval_s=interval, reconnect_s=0.02)
    p.on_snapshot(snaps.append)
    p.on_state(states.append)
    return p


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


def test_snapshots_flow_and_carry_values():
    a = MockAdapter({0x40000000: 7, 0x40000004: 9})
    snaps, states = [], []
    p = make_poller(a, snaps, states)
    p.start()
    assert wait_for(lambda: len(snaps) >= 3)
    p.stop()
    s = snaps[-1]
    assert s.value("P.A") == 7
    assert s.value("P.B") == 9
    assert s.rate_hz > 0


def test_command_queue_runs_between_sweeps():
    a = MockAdapter({})
    snaps, states = [], []
    p = make_poller(a, snaps, states)
    p.start()
    q = p.submit(lambda ad: ad.read_block32(0x0, 1))
    ok, result = q.get(timeout=2.0)
    p.stop()
    assert ok is True
    assert result == [0]


def test_command_error_reported_not_fatal():
    a = MockAdapter({})
    snaps, states = [], []
    p = make_poller(a, snaps, states)
    p.start()

    def boom(ad):
        raise ValueError("nope")
    ok, result = p.submit(boom).get(timeout=2.0)
    n = len(snaps)
    assert wait_for(lambda: len(snaps) > n)   # still polling
    p.stop()
    assert ok is False
    assert isinstance(result, ValueError)


def test_target_lost_and_reconnect():
    a = MockAdapter({0x40000000: 1})
    snaps, states = [], []
    p = make_poller(a, snaps, states)
    p.start()
    assert wait_for(lambda: len(snaps) >= 1)
    a.fail_next(3)
    assert wait_for(lambda: PollerState.TARGET_LOST in states)
    assert wait_for(lambda: states[-1] == PollerState.RUNNING)
    n = len(snaps)
    assert wait_for(lambda: len(snaps) > n)
    p.stop()
    assert states[-1] == PollerState.STOPPED
