# cli.py
"""Command-line driver for the PathScope core engine."""
import argparse
import sys
import threading
import time

from core.adapter.base import TargetLostError
from core.adapter.pyocd_swd import PyOCDAdapter
from core.engine.core import Engine


def _version() -> str:
    """Installed distribution version, for --version and bug reports.
    Falls back to "unknown" where no dist metadata is reachable (e.g.
    a frozen bundle that did not collect the dist-info)."""
    try:
        from importlib.metadata import version
        return version("pathscope")
    except Exception:
        return "unknown"


def cmd_probe(args: argparse.Namespace) -> int:
    a = PyOCDAdapter(target=args.target)
    info = a.connect()
    try:
        cpuid = a.read_block32(0xE000ED00, 1)[0]
        print("target   : %s" % info.name)
        print("idcode   : 0x%08X" % info.idcode)
        print("cpuid    : 0x%08X" % cpuid)
        print("running  : %s" % a.is_running())
    finally:
        a.disconnect()
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    adapter = PyOCDAdapter(target=args.target)
    try:
        info = adapter.connect()
        print("connected: %s idcode=0x%08X" % (info.name, info.idcode))
    except TargetLostError as e:
        print("ERROR: cannot connect to target: %s" % e)
        return 1
    engine = Engine.load(args.target_dir, adapter,
                         interval_s=args.interval / 1000.0)
    if engine.excluded:
        print("note: not polled (readAction): %s"
              % ", ".join(engine.excluded))
    lock = threading.Lock()
    latest = {"update": None, "rates": []}

    def on_update(u):
        with lock:
            latest["update"] = u
            latest["rates"].append(u.snapshot.rate_hz)
        for ev in u.events:
            print("[%8.3f] ANOMALY %s: %s (block %s)"
                  % (ev.t, ev.flow, ev.msg, ev.target))

    def on_state(state):
        print("state -> %s" % state)

    engine.on_update(on_update)
    engine.on_state(on_state)
    engine.start()
    end = time.monotonic() + args.seconds
    try:
        while time.monotonic() < end:
            time.sleep(1.0)
            with lock:
                u = latest["update"]
            if u is None:
                print("(no data yet)")
                continue
            parts = ["rate %5.1f Hz" % u.snapshot.rate_hz]
            for name, f in sorted(u.flows.items()):
                p = "" if f.progress is None else " prog=0x%04X" % f.progress
                parts.append("%s %s%s"
                             % (name, "ACTIVE" if f.active else "idle", p))
            print(" | ".join(parts))
    finally:
        engine.stop()
        with lock:
            rates = [r for r in latest["rates"] if r > 0]
        if rates:
            print("M1 RESULT: avg %.1f Hz, min %.1f Hz over %d snapshots"
                  % (sum(rates) / len(rates), min(rates), len(rates)))
    return 0


def cmd_bench_read(args: argparse.Namespace) -> int:
    adapter = PyOCDAdapter(target=args.target)
    try:
        adapter.connect()
    except TargetLostError as e:
        print("ERROR: cannot connect to target: %s" % e)
        return 1
    blocks = 0
    start = time.monotonic()
    end = start + args.seconds
    try:
        while time.monotonic() < end:
            adapter.read_block32(args.addr, args.block_words)
            blocks += 1
        elapsed = time.monotonic() - start
    finally:
        adapter.disconnect()
    total_bytes = blocks * args.block_words * 4
    bytes_per_second = total_bytes / elapsed if elapsed > 0 else 0.0
    kb_per_s = bytes_per_second / 1024.0
    ceiling = int(bytes_per_second / 48)
    print("blocks=%d bytes=%d throughput=%.1f KB/s ceiling@48B=%d Hz"
          % (blocks, total_bytes, kb_per_s, ceiling))
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    # Imported here, not at module scope, so the core CLI (probe/monitor)
    # keeps working on machines without PySide6 installed.
    from ui.app import main as gui_main
    argv = ["--target", args.target, "--target-dir", args.target_dir]
    if args.demo:
        argv.append("--demo")
    if args.shot:
        argv += ["--shot", args.shot]
    return gui_main(argv)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pathscope")
    p.add_argument("--version", action="version",
                   version="pathscope %s" % _version())
    p.add_argument("--target", default="stm32f411ce")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe", help="connect and print target identity")
    mon = sub.add_parser("monitor", help="poll flows and print states")
    mon.add_argument("--target-dir", default="targets/f411")
    mon.add_argument("--seconds", type=float, default=10.0)
    mon.add_argument("--interval", type=float, default=20.0,
                     help="sweep interval in ms")
    bench = sub.add_parser("bench-read",
                           help="measure block-read throughput")
    bench.add_argument("--seconds", type=float, default=5.0)
    bench.add_argument("--block-words", type=int, default=1024)
    bench.add_argument("--addr", type=lambda s: int(s, 0),
                       default=0x20000000)
    gui = sub.add_parser("gui", help="launch the live diagram window")
    gui.add_argument("--target-dir", default="targets/f411")
    gui.add_argument("--demo", action="store_true",
                     help="run against the hardware-free demo engine")
    gui.add_argument("--shot", default=None, metavar="PATH",
                     help="offscreen smoke: save one frame to PATH, exit 0")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "probe":
        return cmd_probe(args)
    if args.cmd == "monitor":
        return cmd_monitor(args)
    if args.cmd == "bench-read":
        return cmd_bench_read(args)
    if args.cmd == "gui":
        return cmd_gui(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
