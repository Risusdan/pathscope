"""`pathscope gui` entry point: QApplication, engine wiring (demo or real
hardware), MainWindow, event loop - plus an offscreen `--shot` smoke
mode used by CI/manual verification (mirrors prototype/ui_proto.py's
`--shot` behavior: pump events until the first live update lands or 3 s
pass, grab a frame, save it, exit 0 without opening a real window)."""
import argparse
import os
import sys
import time
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.adapter.base import TargetLostError
from core.adapter.pyocd_swd import PyOCDAdapter
from core.engine.core import Engine

from .bridge import EngineBridge
from .demo import make_demo_engine
from .main_window import MainWindow


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pathscope gui")
    p.add_argument("--target", default="stm32f411ce")
    p.add_argument("--target-dir", default="targets/f411")
    p.add_argument("--demo", action="store_true",
                   help="run against the hardware-free demo engine")
    p.add_argument("--shot", default=None, metavar="PATH",
                   help="offscreen smoke: save one frame to PATH, exit 0")
    p.add_argument("--shot-select", default=None, metavar="BLOCK_ID",
                   help="manual check only, requires --shot: select this "
                        "block (opens the register inspector on it) "
                        "before saving the frame")
    p.add_argument("--shot-scope", action="store_true",
                   help="manual check only, requires --shot: switch to "
                        "the Scope tab, add the demo trace channel, and "
                        "let it ramp before saving the frame")
    p.add_argument("--shot-elf", default=None, metavar="ELF",
                   help="manual check only, requires --shot-scope: load "
                        "this firmware ELF and add the real trace "
                        "channels (adc_buf + DMA2.S0NDTR) instead of "
                        "the demo address slot")
    p.add_argument("--shot-edit", action="store_true",
                   help="manual check only, requires --shot: enter "
                        "layout edit mode (handles, edit strip) before "
                        "saving the frame")
    p.add_argument("--shot-wait", type=float, default=0.0, metavar="SEC",
                   help="manual check only, requires --shot: keep "
                        "pumping events this many extra seconds before "
                        "the capture (lets the event log and progress "
                        "values accumulate)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.shot is not None:
        # No display available (or wanted) for a one-shot render.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    app = QApplication.instance() or QApplication(sys.argv[:1] or ["pathscope"])
    # tool is light-theme only by design; never follow the OS dark mode
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    except AttributeError:
        pass

    if args.demo:
        engine = make_demo_engine(args.target_dir)
        target_label = "F411 (demo)"
    else:
        adapter = PyOCDAdapter(target=args.target)
        try:
            info = adapter.connect()
            print("connected: %s idcode=0x%08X" % (info.name, info.idcode))
        except TargetLostError as e:
            print("ERROR: cannot connect to target: %s" % e)
            return 1
        engine = Engine.load(args.target_dir, adapter)
        target_label = info.name

    bridge = EngineBridge(engine)
    win = MainWindow(engine, bridge, target_label=target_label)
    win.show()
    engine.start()
    try:
        if args.shot is not None:
            deadline = time.monotonic() + 3.0
            while win.last_update is None and time.monotonic() < deadline:
                app.processEvents()
            if args.shot_select is not None:
                win._select_block(args.shot_select)
                # pump events a bit longer so the newly live registers
                # get at least one real poll sweep before the capture
                select_deadline = time.monotonic() + 1.0
                while time.monotonic() < select_deadline:
                    app.processEvents()
            if args.shot_wait > 0:
                wait_deadline = time.monotonic() + args.shot_wait
                while time.monotonic() < wait_deadline:
                    app.processEvents()
            if args.shot_edit:
                win.edit_layout_btn.setChecked(True)
                app.processEvents()
            if args.shot_scope:
                # Switch to the Scope tab (lazily constructs the real
                # ScopePage). With --shot-elf, load the firmware ELF
                # and add the real hardware channels (the same pair
                # tests/hw/test_trace_hw.py streams); otherwise watch
                # the demo trace target's own lively ADC-sample slot
                # (ui/demo.py's ADC_SAMPLE, 0x20000000 - a u16-range
                # sine the animate thread keeps moving) so the
                # screenshot shows a real curve, not an empty plot.
                # add_address_slot() is the primitive
                # add_symbol_channel()/add_register_channel() both
                # wrap - used directly for the demo target, which has
                # no register model to resolve a reg_key against.
                win.tabs.setCurrentIndex(1)
                app.processEvents()
                if args.shot_elf is not None:
                    win.scope_page.load_elf(args.shot_elf)
                    app.processEvents()
                    slot = win.scope_page.add_symbol_channel("adc_buf")
                    win.scope_page.add_register_channel("DMA2.S0NDTR")
                    if slot is not None:
                        # adc_buf is a u16 sample array; the size-based
                        # default (u32) would fuse two samples per
                        # point. Driving the combo (not _on_type_changed
                        # directly) keeps the visible cell in sync.
                        win.scope_page.channel_slots()[slot][
                            "type_combo"].setCurrentText("u16.lo")
                    # commit/close any cell editor the adds left open
                    # so the Name column renders its text in the shot
                    win.scope_page.channel_table.setCurrentItem(None)
                    win.scope_page.channel_table.clearSelection()
                    win.scope_page.channel_table.clearFocus()
                else:
                    win.scope_page.add_address_slot(
                        0x20000000, "adc_sample", type_name="u16.lo")
                ramp_deadline = time.monotonic() + 2.5
                while time.monotonic() < ramp_deadline:
                    app.processEvents()
                win.scope_page.refresh_plot()
            app.processEvents()
            # processEvents() never delivers DeferredDelete (only a
            # real exec() loop does), so any cell widget replaced
            # during setup - e.g. the scope table re-rendering a row -
            # lingers undeleted at the viewport origin and paints over
            # row 0 in the grab. Flush them explicitly.
            from PySide6.QtCore import QCoreApplication, QEvent
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            app.processEvents()
            win.grab().save(args.shot)
            print("saved", args.shot)
            return 0
        return app.exec()
    finally:
        engine.stop()


if __name__ == "__main__":
    sys.exit(main())
