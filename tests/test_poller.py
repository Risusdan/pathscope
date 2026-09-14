# tests/test_poller.py
import time

import pytest
from core.adapter.base import TargetLostError
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


class DeadAdapter(MockAdapter):
    """connect() never succeeds - keeps the poller in TARGET_LOST."""
    def connect(self):
        raise TargetLostError("still dead")


def test_stop_prompt_during_target_lost():
    a = DeadAdapter({0x40000000: 1})
    snaps, states = [], []
    p = make_poller(a, snaps, states, interval=0.005)
    p.reconnect_s = 5.0                    # long sleep: stop must interrupt it
    p.start()
    assert wait_for(lambda: len(snaps) >= 1)
    a.fail_next(1)
    assert wait_for(lambda: PollerState.TARGET_LOST in states)
    t0 = time.monotonic()
    p.stop()
    assert time.monotonic() - t0 < 2.0
    assert states[-1] == PollerState.STOPPED


def test_pending_command_answered_on_stop():
    a = DeadAdapter({})
    snaps, states = [], []
    p = make_poller(a, snaps, states, interval=0.005)
    p.start()
    a.fail_next(1)
    assert wait_for(lambda: PollerState.TARGET_LOST in states)
    q = p.submit(lambda ad: ad.read_block32(0x0, 1))
    p.stop()
    ok, result = q.get(timeout=1.0)
    assert ok is False


def test_command_dying_mid_drain_reports_target_lost_not_none():
    """A command that raises AdapterError while it is actually running
    (as opposed to the periodic sweep) must answer with a named reason,
    not (False, None) - see core/engine/core.py's EngineError formatting,
    which would otherwise render the unhelpful "command failed: None"."""
    a = MockAdapter({})
    snaps, states = [], []
    p = make_poller(a, snaps, states)
    p.start()
    assert wait_for(lambda: len(snaps) >= 1)
    a.fail_next(1)
    ok, result = p.submit(lambda ad: ad.read_block32(0x0, 1)).get(timeout=2.0)
    p.stop()
    assert ok is False
    assert result is not None
    assert "target lost" in result


def test_snapshot_callback_exception_does_not_kill_poller():
    a = MockAdapter({0x40000000: 1})
    states = []
    good = []

    def bad_cb(snap):
        raise ValueError("boom")

    plan = [ReadOp(addr=0x40000000, count=2,
                   targets=[("P.A", 0), ("P.B", 1)])]
    p = Poller(a, plan, interval_s=0.005, reconnect_s=0.02)
    p.on_snapshot(bad_cb)
    p.on_snapshot(good.append)
    p.on_state(states.append)
    p.start()
    assert wait_for(lambda: len(good) >= 3)
    p.stop()
    assert states[-1] == PollerState.STOPPED


def test_submit_after_stop_fails_immediately():
    """A command submitted after the poller has fully stopped must
    fail right away, not sit in the abandoned queue until the
    caller's own Engine._exec timeout (2s in production) elapses -
    nobody is left running to ever drain it. Poller.submit() checks
    self._stop_evt itself and fast-fails rather than enqueueing."""
    a = MockAdapter({})
    snaps, states = [], []
    p = make_poller(a, snaps, states)
    p.start()
    assert wait_for(lambda: len(snaps) >= 1)
    p.stop()

    t0 = time.monotonic()
    q = p.submit(lambda ad: ad.read_block32(0x0, 1))
    ok, result = q.get(timeout=0.1)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1, "submit() after stop() must not need any wait"
    assert ok is False
    assert result == "poller stopped"


def test_rate_hz_is_not_distorted_during_warmup():
    a = MockAdapter({0x40000000: 1})
    snaps, states = [], []
    p = make_poller(a, snaps, states, interval=0.005)
    p.start()
    assert wait_for(lambda: len(snaps) >= 3)
    p.stop()
    assert snaps[2].rate_hz > 10
