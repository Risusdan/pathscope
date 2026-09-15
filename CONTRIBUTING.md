# Contributing

## Setup

    python3 -m venv .venv
    .venv/bin/pip install -e ".[ui,dev]"

`core/` is stdlib-only by design; everything Qt/numpy lives under
`ui/`. Keep it that way.

## Running the tests

Two tiers:

    .venv/bin/pytest -q          # full suite, no hardware needed
    .venv/bin/pytest -m hw       # hardware tests: Blackpill + ST-Link

Hardware tests are deselected by default (see `pytest.ini`). They
expect an STM32F411 Blackpill on a ST-Link with the demo firmware
flashed (`make` in `firmware/blackpill_adc_dma`, then flash `fw.elf`).
One interactive test (unplug/replug) only runs with
`PS_HW_INTERACTIVE=1`.

UI tests run headless: they set `QT_QPA_PLATFORM=offscreen`
themselves, but if you script Qt outside pytest, set it yourself. On
Linux CI the Qt xcb/EGL system packages are needed - the exact
apt-get list lives in `.github/workflows/ci.yml`.

## Conventions

- Pure ASCII in source/config files - no box-drawing or decorative
  Unicode.
- C code (firmware/) carries Doxygen comments on every public
  declaration.
- A target is three files under `targets/<name>/` (SVD +
  topology.yaml + flows.yaml); adding one must not touch code. The
  layout editor writes `topology.yaml` back as a surgical patch -
  tests assert byte-level preservation of untouched regions, so treat
  hand formatting in target files as load-bearing.
- Tests that talk to a fake target go through `core/trace/sim.py` /
  `core/adapter/mock.py`. The sim deliberately models real 32-bit
  write semantics and firmware publish order - never make it more
  forgiving than silicon.

## Regenerating the README screenshots

The screenshots in `docs/img/` are captured by the app itself in an
offscreen one-shot mode. Two things to know:

- The extended flags are parsed by `ui/app.py`, and `pathscope gui`
  forwards only `--shot` - so invoke the module directly:
  `python -m ui.app ...`.
- All four images come from real hardware: connect the Blackpill via
  ST-Link with `firmware/blackpill_adc_dma/fw.elf` flashed and
  running.

The exact commands:

    # live diagram (let the event log and progress values accumulate)
    .venv/bin/python -m ui.app --shot docs/img/pathscope-demo.png \
        --shot-wait 6

    # register inspector, DMA2 selected
    .venv/bin/python -m ui.app --shot docs/img/pathscope-inspector.png \
        --shot-wait 3 --shot-select dma2

    # scope: loads the ELF, adds adc_buf (u16.lo) + DMA2.S0NDTR
    .venv/bin/python -m ui.app --shot docs/img/pathscope-scope.png \
        --shot-scope --shot-elf firmware/blackpill_adc_dma/fw.elf

    # layout edit mode
    .venv/bin/python -m ui.app --shot docs/img/pathscope-edit.png \
        --shot-wait 2 --shot-edit

Flag reference (`python -m ui.app --help` has the same, briefer):

- `--shot PATH` - offscreen: render one frame, save, exit.
- `--shot-wait SEC` - keep pumping events before the capture.
- `--shot-select BLOCK_ID` - select a block first (opens the
  inspector on it).
- `--shot-scope` - switch to the Scope tab; without `--shot-elf` it
  adds the demo target's address slot instead.
- `--shot-elf ELF` - with `--shot-scope`: load this firmware ELF and
  add the real trace channels.
- `--shot-edit` - enter layout edit mode before the capture.

`--demo` combines with `--shot` for a hardware-free frame (that is
what CI's smoke test uses), but the committed README images should
come from hardware.
