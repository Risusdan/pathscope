# cli.py
"""Command-line driver for the data path explorer core engine."""
import argparse
import sys

from core.adapter.pyocd_swd import PyOCDAdapter


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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dpe")
    p.add_argument("--target", default="stm32f411ce")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe", help="connect and print target identity")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "probe":
        return cmd_probe(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
