# PathScope

PathScope is a desktop tool that shows an MCU's internal data paths as a
live block diagram - which paths data is flowing through, how the
registers behind them evolve, and where declarative rules catch anomalies
such as an overrun flag or a stalled DMA channel. It reads registers over
a debug probe (SWD via pyOCD) at tens of Hz, so bring-up and debugging
sessions get a live picture of what a peripheral chain is actually doing
instead of scattered register dumps. New targets are added with plain
description files - no changes to the core engine or UI.

![PathScope: live diagram, legend, event log](docs/img/pathscope-demo.png)

![Register inspector: live DMA2 field values](docs/img/pathscope-inspector.png)

## Features

- Live block diagram of a data path, with active edges animated and
  annotated with a real progress value (e.g. a DMA byte count).
- Declarative rules (YAML) turn register conditions into anomaly badges
  and event-log entries - no code changes to add a new check.
- Guarded-register safety: registers with a read side effect (SVD
  `readAction`, or a `guarded` entry in flows.yaml for vendor SVDs that
  don't annotate it) are never swept by the poller; reading one requires
  an explicit, per-click confirmation.
- Register inspector dock with live values for polled registers and
  on-demand reads for everything else.
- Memory viewer for on-demand hex dumps of memory blocks, with the same
  guarded-range protection as the register inspector.
- Demo mode (`--demo`) runs the full UI against a scripted engine, no
  probe or target required.

## Quick start

    python3 -m venv .venv
    .venv/bin/pip install -e ".[ui,dev]"

No hardware needed:

    .venv/bin/pathscope gui --demo

With a Blackpill (STM32F411CE) + ST-Link:

    .venv/bin/pyocd pack install stm32f411ce   # one-time, see Probe notes
    .venv/bin/pathscope gui
    .venv/bin/pathscope probe                  # print target identity
    .venv/bin/pathscope monitor --seconds 15   # headless poll-rate check

## Adding your own target

A target is three files under `targets/<name>/`; adding one touches no
code.

1. **`<chip>.svd`** - the vendor's CMSIS-SVD file (registers, fields,
   addresses, `readAction`). Most vendors publish these directly, or
   pyOCD's pack manager can fetch one.
2. **`<name>.topology.yaml`** - the diagram: blocks and the edges between
   them.
3. **`<name>.flows.yaml`** - which register conditions mean a path is
   active, and which mean something is wrong:

       activities:
         - name: adc_to_sram
           path: [adc1, dma2, sram1]
           active_when: "DMA2.S0CR.EN == 1"
           progress: "DMA2.S0NDTR"
           anomalies:
             - {rule: "ADC1.SR.OVR == 1", msg: "ADC overrun, data lost"}
       poll:
         guarded: ["ADC1.DR"]   # has a read side effect; never auto-polled

Point `--target-dir` at the new folder and run.

## Hardware validation

Blackpill (STM32F411CE), clone ST-Link v2, macOS, pyOCD 0.45.1.

- M1 poll rate: avg 38.0 Hz, min 37.3 Hz over 572 snapshots (threshold
  20 Hz, PASS; 4 registers polled in 2 batched block reads).
- M2 (CLI): baseline active, fault injection and recovery via a key
  press (ADC overrun then DMA stalled, then back to active with no
  spurious events), USB unplug/replug recovers cleanly. Avg 37.9 Hz over
  3191 snapshots including the outage.
- M3/M4 (GUI): live diagram and inspector confirmed against real
  hardware at ~38 Hz; guarded ADC1.DR prompts for forced reads and
  updates correctly on confirm; fault badges, event log and recovery all
  behave as in the CLI validation; memory viewer refuses a guarded-range
  read as designed; freeze keeps the event log live while panels hold.

## Probe notes

- STM32F411 is not a pyOCD builtin target: run
  `pyocd pack install stm32f411ce` once (downloads Keil.STM32F4xx_DFP).
- macOS needs no ST-Link driver; on Windows install the ST-Link USB
  driver.
- On an unflashed/idle target the "DMA stalled" rule fires once at
  startup (NDTR frozen at reset value) - expected, rules evaluate even
  while their flow is inactive.
- USB transaction latency dominates on a clone ST-Link (roughly 0.5-1 ms
  per transaction); the poller batches reads per peripheral to keep the
  snapshot rate up.

## License

MIT - see [LICENSE](LICENSE).
