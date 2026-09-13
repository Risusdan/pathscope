# Data Path Explorer

Visual data-path debug tool. Core engine + CLI (M0-M2); UI follows.

## Quick start (dev)

    python3 -m venv .venv
    .venv/bin/pip install -e ".[dev]"
    .venv/bin/pytest              # no hardware needed
    .venv/bin/python -m cli probe # needs Blackpill + ST-Link

## M1 poll-rate measurement

Setup: Blackpill (STM32F411CE), clone ST-Link v2, macOS, pyOCD 0.45.1.
Command: `python -m cli monitor --seconds 15`
Result (2026-09-12): M1 RESULT: avg 38.0 Hz, min 37.3 Hz over 572 snapshots
Gate: PASS (threshold 20 Hz; 4 registers polled in 2 batched block reads).

## Probe notes

- STM32F411 is not a pyOCD builtin target: run
  `pyocd pack install stm32f411ce` once (downloads Keil.STM32F4xx_DFP).
- macOS needs no ST-Link driver; on Windows install the ST-Link USB driver.
- Note: on an unflashed/idle target the "DMA stalled" rule fires once at
  startup (NDTR frozen at reset value) - expected, rules evaluate even
  while their flow is inactive.

## M2 validation (2026-09-12, Blackpill F411CE + clone ST-Link, macOS)

- [x] baseline ACTIVE + moving progress
- [x] KEY press -> "ADC overrun, data lost" then "DMA stalled" (~0.5 s later)
- [x] flow goes idle while stalled, prog frozen
- [x] KEY press -> recovery to ACTIVE, no spurious events (2 anomalies total)
- [x] USB unplug/replug -> target_lost -> running, no crash
- [x] PC13 LED heartbeat visually confirmed

Session rate: avg 37.9 Hz over 3191 snapshots (90 s including the outage).

## M3/M4 GUI validation (2026-09-13, Blackpill + clone ST-Link, macOS)

- [x] real-adapter GUI connects; diagram live, ACTIVE path animating, ~38 Hz
- [x] register inspector: live values flash; ADC1.DR shows guarded (overlay),
      forced read prompts and updates on confirm, declines read nothing
- [x] double-click read keeps register rows expanded (fixed during session)
- [x] KEY fault: badges + red events + flow idle; badge click clears count;
      recovery clean
- [x] memory viewer: SRAM buffer churns under auto-refresh; guarded-range
      read (0x40012000 x 256B over ADC1.DR) refused as designed
- [x] USB unplug: animation stops, rate shows --, auto-recovery on replug
- [x] freeze: anomaly events still reach the log while panels hold
