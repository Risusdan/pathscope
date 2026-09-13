import time

import cli
from core.adapter.base import TargetLostError


def test_prog_name_is_pathscope():
    assert cli.build_parser().prog == "pathscope"


def test_parser_has_both_commands():
    p = cli.build_parser()
    a = p.parse_args(["probe"])
    assert a.cmd == "probe"
    a = p.parse_args(["monitor", "--seconds", "3", "--interval", "10"])
    assert a.cmd == "monitor"
    assert a.seconds == 3.0


def test_monitor_returns_1_and_never_starts_engine_when_connect_fails(monkeypatch):
    class StubAdapter(object):
        def __init__(self, target):
            self.target = target

        def connect(self):
            raise TargetLostError("no probe")

    monkeypatch.setattr(cli, "PyOCDAdapter", StubAdapter)
    args = cli.build_parser().parse_args(["monitor", "--seconds", "1"])
    t0 = time.monotonic()
    assert cli.cmd_monitor(args) == 1
    # If the engine/poller had started it would run for --seconds (1s)
    # before returning; bailing out on connect() must be near-instant.
    assert time.monotonic() - t0 < 0.5
