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
    # directly from source for local testing), fall back to this
    # script's own directory.
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def main():
    import cli

    argv = sys.argv[1:]
    if "--target-dir" not in argv:
        default_target_dir = os.path.join(_exe_dir(), "targets", "f411")
        argv = argv + ["--target-dir", default_target_dir]
    sys.exit(cli.main(["gui"] + argv))


if __name__ == "__main__":
    main()
