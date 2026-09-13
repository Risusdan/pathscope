# Packaging: PyInstaller onedir bundle

This directory builds a standalone PathScope desktop bundle with
PyInstaller, per the binding rule in spec section 3 (Deployment Form,
`docs/superpowers/specs/2026-09-12-data-path-explorer-design.md`):
PyInstaller **onedir** (not onefile - a single-file exe was rejected
for slow cold start and antivirus false positives), with `targets/`
shipped *next to* the executable, never inside the bundle, so users
can add or edit target description files without rebuilding.

## Files

- `entry.py` - the PyInstaller entry script. Always launches the
  `gui` subcommand and forwards whatever flags the user passed
  (`--demo`, `--shot`, etc.). Because the frozen app's current working
  directory is not necessarily the install directory, it also computes
  a `targets/f411` path next to the executable and passes it as
  `--target-dir` whenever the user did not already supply one - an
  explicit `--target-dir` always wins.
- `pathscope.spec` - the PyInstaller spec (onedir, `name="pathscope"`).
  See the pyOCD findings below for why it needs extra `hiddenimports`,
  `datas`, and `binaries` beyond a stock `pyi-makespec` output.
- `build.sh` - POSIX build script (macOS/Linux): installs the `build`
  extra, runs PyInstaller against the spec, copies `targets/` next to
  the built executable, and zips `dist/pathscope-<version>-<os>.zip`.
- `build.ps1` - Windows PowerShell equivalent, producing
  `dist\pathscope-<version>-windows.zip`. Windows exes must be built on
  Windows (PyInstaller does not cross-compile); CI runs this on the
  `windows-latest` runner (see the M5 plan's CI task).

## Building

```sh
# macOS / Linux
packaging/build.sh

# Windows (PowerShell)
packaging\build.ps1
```

Both scripts assume `.venv` already exists (created with Python 3.9)
and resolve the repo root themselves, so they can be run from any
directory. They install `.[ui,build]` themselves, so a bare
`python -m venv .venv` is enough starting point.

Output: `dist/pathscope/` (the onedir bundle - executable at
`dist/pathscope/pathscope`, or `pathscope.exe` on Windows) with a
sibling `targets/` directory, plus the zip containing both.

## Verifying a build

From the built bundle (not the source tree):

```sh
dist/pathscope/pathscope --demo --shot /tmp/pkg.png
```

`--shot` makes `ui/app.py` set `QT_QPA_PLATFORM=offscreen` itself, pump
the demo engine for one update, grab a frame, and exit 0 - no manual
offscreen env var or display needed. This was run from a directory
other than the repo root during verification to confirm the
exe-adjacent `targets/` default (not a CWD-relative one) is what
actually resolves.

## console=True everywhere (an MVP wrinkle)

The spec sets `console=True` on every OS, including Windows, where the
idiomatic choice for a GUI app would be `console=False` (no
background console window). For MVP this is deliberate: the app has no
error-reporting mechanism yet, so a console window is currently the
only place a startup crash (unhandled exception, a missing/invalid
`--target-dir`, no probe found) becomes visible instead of the app
just silently failing to appear. Revisit once there is a log file or
crash dialog the user can find without one.

## pyOCD plugin and packaging findings

PySide6's PyInstaller hooks are bundled in PyInstaller itself and
needed no extra configuration - `hook-PySide6*.py` and the
`pyi_rth_pyside6` runtime hook picked up Qt automatically.

pyOCD needed real, non-optional extra configuration. All three issues
below were found by actually running the built bundle (`dist/pathscope/
pathscope --demo --shot ...`), not by build-time warnings - PyInstaller's
analysis phase completed cleanly (aside from benign "missing module"
notes for Windows-only/optional imports) in every case; the bundle only
failed once actually launched.

1. **Probe/RTOS plugins are entry-point based, so hiddenimports are
   required.** pyOCD discovers its probe and RTOS backends dynamically
   via `importlib_metadata.entry_points(group="pyocd.probe" /
   "pyocd.rtos")` (see `pyocd/core/plugin.py`), not by importing them
   by name anywhere PyInstaller's static analysis can see. Without
   listing the plugin modules as `hiddenimports`, the frozen exe
   raised `ModuleNotFoundError` while `pyocd.probe.aggregator` tried to
   load them. Added to the spec:
   `pyocd.probe.cmsis_dap_probe`, `pyocd.probe.jlink_probe`,
   `pyocd.probe.picoprobe`, `pyocd.probe.tcp_client_probe`,
   `pyocd.probe.stlink_probe`, and the five `pyocd.rtos.*` plugins
   (`argon`, `freertos`, `rtx5`, `threadx`, `zephyr`).

2. **pyOCD's own dist-info must be copied in, or entry_points()
   silently finds nothing.** Even with the hiddenimports above, if
   pyOCD's `entry_points.txt` metadata is not physically present in the
   bundle, `entry_points(group=...)` just returns an empty list - no
   exception, no build warning, just "no probes found" at connect time.
   Fixed with `copy_metadata("pyocd")` in the spec.

3. **Two data files pyOCD loads by path at import time.**
   `pyocd/debug/sequences/sequences.py` calls
   `lark.lark.Lark.open("sequences.lark", rel_to=__file__, ...)` at
   class-definition time (i.e. as soon as
   `pyocd.debug.sequences.sequences` is imported), and
   `default_sequences.yaml` sits next to it for the same reason.
   PyInstaller's import scanner only follows code, not these
   open()-by-relative-path calls, so leaving them out crashed the
   frozen exe with `FileNotFoundError` before any window appeared.
   Fixed with `collect_data_files("pyocd")` in the spec, which sweeps
   up both files (and anything else non-Python pyOCD ships) preserving
   their package-relative layout.

4. **`cmsis_pack_manager`'s native library isn't picked up by binary
   dependency walking.** Importing pyOCD's CMSIS-DAP probe path pulls
   in `cmsis_pack_manager`, whose Rust core is a prebuilt
   `libcmsis_pack_manager.dylib` (or `.so`/`.dll`) loaded by a cffi
   shim (`cmsis_pack_manager/cmsis_pack_manager/ffi.py`) at a path
   next to that shim - not a `ctypes.CDLL(<absolute path>)` call
   PyInstaller's binary scanner recognizes, and not linked into any
   binary PyInstaller already collected, so it was never copied in.
   The frozen exe raised `OSError: cannot load library
   '.../cmsis_pack_manager/cmsis_pack_manager/libcmsis_pack_manager.dylib'`
   (dlopen failed, file not found) at import time. Fixed with
   `collect_dynamic_libs("cmsis_pack_manager")` added to `binaries` in
   the spec.

None of these needed a custom PyInstaller hook file - `PyInstaller.
utils.hooks.{collect_data_files, collect_dynamic_libs, copy_metadata}`
called directly in `pathscope.spec` were enough. If a future pyOCD
upgrade adds another dynamically-loaded probe backend or native
library, expect the same failure mode (a runtime `ModuleNotFoundError`
or `OSError` from the *built and launched* bundle, not a build-time
warning) and the same fix shape.

## Version and OS tag in the zip filename

`build.sh`/`build.ps1` extract `version` out of `pyproject.toml` with a
small regex rather than `tomllib`, since `tomllib` is Python 3.11+ and
this project targets Python 3.9.
