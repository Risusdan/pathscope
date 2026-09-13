"""Unit-style regression test for test_trace_hw.py's _live_engine()
adapter cleanup on setup failure (Task 10 review, fix round 1): if
PyOCDAdapter.connect() succeeds but Engine.load() or engine.start()
then raises, the adapter must be disconnected before the exception
propagates - otherwise a dead session keeps the ST-Link claimed and
every LATER hw test in the same run fails to connect too, exactly the
cross-test poisoning the hw suite must rule out.

Exercises _live_engine() directly against a stub adapter and a
monkeypatched Engine.load - no real probe touched, so this test
carries no `hw` marker (deliberately not inherited from
test_trace_hw.py's module-level `pytestmark`, since this file has none
of its own) and runs in every default suite pass, unlike the rest of
tests/hw/."""
import pytest

import tests.hw.test_trace_hw as hw_mod
from core.engine.core import Engine


class _StubAdapter:
    """Minimal stand-in for PyOCDAdapter: records connect()/disconnect()
    calls without touching any real hardware."""

    def __init__(self):
        self.connected = False
        self.disconnected = False

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.disconnected = True


def test_live_engine_disconnects_adapter_when_setup_fails_after_connect(
        monkeypatch):
    """connect() succeeds, then Engine.load() raises - _live_engine()
    must call adapter.disconnect() before letting the exception
    propagate, not leave the stub (standing in for a real ST-Link
    session) claimed."""
    stub = _StubAdapter()
    monkeypatch.setattr(hw_mod, "PyOCDAdapter", lambda: stub)

    def _boom(cls, *args, **kwargs):
        raise RuntimeError("Engine.load blew up")
    monkeypatch.setattr(Engine, "load", classmethod(_boom))

    with pytest.raises(RuntimeError, match="Engine.load blew up"):
        hw_mod._live_engine()

    assert stub.connected, "connect() must have run before the failure"
    assert stub.disconnected, \
        "adapter must be disconnected when setup fails after connect()"
