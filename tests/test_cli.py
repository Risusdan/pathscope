import re
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


def test_bench_read_parser_defaults():
    p = cli.build_parser()
    a = p.parse_args(["bench-read"])
    assert a.cmd == "bench-read"
    assert a.seconds == 5.0
    assert a.block_words == 1024
    assert a.addr == 0x20000000


def test_bench_read_parser_accepts_hex_addr():
    p = cli.build_parser()
    a = p.parse_args(["bench-read", "--addr", "0x20001000"])
    assert a.addr == 0x20001000


def test_bench_read_returns_1_and_never_reads_when_connect_fails(monkeypatch):
    class StubAdapter(object):
        def __init__(self, target):
            self.target = target

        def connect(self):
            raise TargetLostError("no probe")

        def read_block32(self, addr, count):
            raise AssertionError("must not read after failed connect")

    monkeypatch.setattr(cli, "PyOCDAdapter", StubAdapter)
    args = cli.build_parser().parse_args(["bench-read", "--seconds", "1"])
    assert cli.cmd_bench_read(args) == 1


def test_bench_read_prints_ceiling_math(monkeypatch, capsys):
    class StubAdapter(object):
        def __init__(self, target):
            self.target = target

        def connect(self):
            return None

        def disconnect(self):
            pass

        def read_block32(self, addr, count):
            return [0] * count

    monkeypatch.setattr(cli, "PyOCDAdapter", StubAdapter)

    # Deterministic fake clock: each call to time.monotonic() advances
    # by a fixed step, regardless of caller, so the number of loop
    # iterations (and thus every number in the printed line) is exact.
    calls = {"n": 0}
    dt = 0.25

    def fake_monotonic():
        val = calls["n"] * dt
        calls["n"] += 1
        return val

    monkeypatch.setattr(cli.time, "monotonic", fake_monotonic)

    args = cli.build_parser().parse_args(
        ["bench-read", "--seconds", "1", "--block-words", "1024",
         "--addr", "0x20000000"])
    rc = cli.cmd_bench_read(args)
    assert rc == 0

    out = capsys.readouterr().out.strip()
    m = re.match(
        r"^blocks=(\d+) bytes=(\d+) throughput=([\d.]+) KB/s "
        r"ceiling@48B=(\d+) Hz$", out)
    assert m, "unexpected output: %r" % out
    blocks = int(m.group(1))
    total_bytes = int(m.group(2))
    throughput = float(m.group(3))
    ceiling = int(m.group(4))

    # Cross-check the printed numbers against the same math the
    # implementation is supposed to use, driven by the fake clock
    # above: blocks=3, elapsed=1.25s.
    assert blocks == 3
    assert total_bytes == blocks * 1024 * 4
    bytes_per_second = total_bytes / 1.25
    assert throughput == round(bytes_per_second / 1024.0, 1)
    assert ceiling == int(bytes_per_second / 48)
