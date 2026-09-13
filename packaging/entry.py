"""PyInstaller entry point for the pathscope onedir bundle.

The bundled executable defaults to launching the `gui` subcommand
(forwarding whatever flags the user passed, e.g. --demo, --shot). Per
spec section 3 (Deployment Form), targets/ ships NEXT TO the executable,
never inside the bundle, so the frozen app cannot rely on the source
tree's "targets/f411" relative-to-CWD default: it computes a
target-dir sitting beside the executable and passes it explicitly
whenever the user did not already supply --target-dir.
"""
import os
import sys


def _exe_dir():
    # In a PyInstaller onedir build, sys.executable is the bundled exe
    # itself and sys.frozen is set; its directory is the install root
    # that targets/ lives beside. Unfrozen (e.g. running this file
    # directly from source for local testing) is not a supported run
    # mode - there is no bundle to sit beside - but fall back to the
    # repo root (this script's parent directory) rather than
    # packaging/ itself, since repo_root/targets/f411 at least exists
    # in a checkout, unlike packaging/targets/f411.
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _has_target_dir(argv):
    # Detect both argparse forms the user might pass: "--target-dir
    # VALUE" and "--target-dir=VALUE". A naive "--target-dir" in argv
    # check misses the "=" form, so entry.py would append its own
    # default afterwards and argparse - which keeps the last
    # occurrence of an option - would silently discard the user's
    # explicit path instead of erroring or honoring it.
    return any(a == "--target-dir" or a.startswith("--target-dir=")
               for a in argv)


def main():
    import cli

    argv = sys.argv[1:]
    if not _has_target_dir(argv):
        default_target_dir = os.path.join(_exe_dir(), "targets", "f411")
        argv = argv + ["--target-dir", default_target_dir]
    sys.exit(cli.main(["gui"] + argv))


if __name__ == "__main__":
    main()
