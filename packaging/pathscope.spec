# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the pathscope onedir bundle.

Invoke via packaging/build.sh (POSIX) or packaging/build.ps1 (Windows) -
those scripts install the build extra, run pyinstaller against this
spec, then copy targets/ next to the built executable and zip the
result. Running `pyinstaller packaging/pathscope.spec` directly also
works as long as the current directory is the repo root and .venv has
the `build` extra installed.

Deployment form (spec section 3, docs/superpowers/specs/
2026-09-12-data-path-explorer-design.md): onedir, not onefile - a
single-file exe was rejected for slow cold start and antivirus false
positives. Target description files (targets/) are NOT collected into
this bundle: they ship in a sibling directory next to the executable so
users can add or edit targets without rebuilding. build.sh/build.ps1
copy targets/ in after PyInstaller runs; this spec must never add it to
`datas`.
"""
import os

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    copy_metadata,
)

SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
REPO_ROOT = os.path.abspath(os.path.join(SPEC_DIR, os.pardir))
ENTRY_SCRIPT = os.path.join(SPEC_DIR, "entry.py")

# pyOCD discovers its probe and RTOS plugins dynamically, by reading
# each installed distribution's entry_points.txt at runtime
# (importlib_metadata.entry_points(group="pyocd.probe"/"pyocd.rtos"),
# see pyocd/core/plugin.py) rather than importing them by name from
# code PyInstaller's static analysis can see. Two consequences, found
# by running the build and exercising `pathscope probe`/`gui` from the
# bundle (see packaging/README.md "pyOCD plugin findings"):
#
#   1. The plugin modules themselves must be listed as hiddenimports,
#      or PyInstaller never bundles them and loading the entry point
#      raises ModuleNotFoundError inside the frozen app.
#   2. pyocd's own dist-info (entry_points.txt in particular) must be
#      copied into the bundle via copy_metadata(), or entry_points()
#      finds nothing and every probe/RTOS plugin silently fails to
#      register - no exception, connect() just reports no probes found.
#
# Separately (not a plugin issue): pyocd loads a couple of non-Python
# data files relative to their own module's __file__ at IMPORT time -
# pyocd/debug/sequences/sequences.lark (a lark grammar, read by
# lark.lark.Lark.open() when pyocd.debug.sequences.sequences is first
# imported) and default_sequences.yaml next to it. PyInstaller's
# import scanner only follows code, not these open()-by-path calls, so
# without collect_data_files("pyocd") the frozen exe crashes with
# FileNotFoundError before a window ever appears - found by running
# the onedir build and launching it, not by a build-time warning.
PYOCD_HIDDEN_IMPORTS = [
    "pyocd.probe.cmsis_dap_probe",
    "pyocd.probe.jlink_probe",
    "pyocd.probe.picoprobe",
    "pyocd.probe.tcp_client_probe",
    "pyocd.probe.stlink_probe",
    "pyocd.rtos.argon",
    "pyocd.rtos.freertos",
    "pyocd.rtos.rtx5",
    "pyocd.rtos.threadx",
    "pyocd.rtos.zephyr",
]

datas = copy_metadata("pyocd") + collect_data_files("pyocd")

# pyocd's stlink/cmsisdap probe path pulls in cmsis_pack_manager, whose
# Rust core is a prebuilt libcmsis_pack_manager dylib loaded by a cffi
# shim (cmsis_pack_manager/cmsis_pack_manager/ffi.py) at a path next to
# that shim, not via a regular ctypes.CDLL(<absolute path>) call
# PyInstaller's binary scanner recognizes. Left uncollected, the
# analysis phase logs nothing wrong, but the frozen exe crashes at
# import time - found the same way as the pyocd data files above, by
# running the built bundle - with OSError: cannot load library
# '.../cmsis_pack_manager/cmsis_pack_manager/libcmsis_pack_manager.dylib'.
binaries = collect_dynamic_libs("cmsis_pack_manager")

a = Analysis(
    [ENTRY_SCRIPT],
    pathex=[REPO_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=PYOCD_HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pathscope",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # console=True on every OS, including Windows, for MVP: this pops
    # a console window behind the GUI, which is uglier than a clean
    # launch, but it is currently the only place a startup crash
    # (unhandled exception, a bad --target-dir, no probe found) becomes
    # visible instead of the app just silently failing to appear.
    # Revisit (console=False plus a log file the user can find) once
    # the app has real error reporting.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="pathscope",
)
