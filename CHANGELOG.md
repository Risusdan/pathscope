# Changelog

Notable changes per release. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/).

## 0.2.0 - 2026-09-16

The scope acquisition rework and the layout editor.

### Added

- Firmware trace-buffer scope (M7): sampling moved from probe polling
  into a firmware timer ISR (`firmware/ps_trace`, C99, no vendor
  deps), so every record's channels are captured in the same instant
  by construction. Fixed 10-slot channel table, per-channel type
  decode / scale / offset, ELF symbol channels, generation-marked
  honest gaps, target-reboot detection and auto-resync. Measured
  ceiling on the reference clone ST-Link: 65.8 KB/s = 1403 Hz at
  48-byte records (`pathscope bench-read` measures your own link).
- Layout edit mode (M8): drag blocks, wire endpoints and waypoints on
  the diagram; grid snap, arrow-key nudge, alignment guides,
  Visio-style connector glue (wires follow a moving block live),
  endpoints always re-anchor onto their block, undo, Seed layout
  first-draft generator. Save patches `topology.yaml` surgically -
  untouched bytes (comments, formatting) survive.
- Zoom anchors under the mouse cursor; live coordinate readout inside
  the drawing area; per-page Run/Stop.
- `--shot` screenshot modes for maintainers (`--shot-edit`,
  `--shot-elf`, `--shot-wait`, `--shot-select`, `--shot-scope`) - see
  CONTRIBUTING.md.

### Changed

- The M6 polling scope was retired: cross-channel coherence cannot be
  guaranteed by a transaction-per-read probe, so the trace path is
  the only scope acquisition mode. Targets without the firmware
  module get an explicit inline error.
- Engine internals: dead address-watch API removed, dynamic
  attributes declared, progress-text edge re-picked after layout
  edits.

## 0.1.0 - 2026-09-13

First release: live data-path block diagram over SWD (pyOCD),
declarative flow/anomaly rules, guarded-register safety chain,
register inspector, memory viewer, event log, demo mode, Windows
one-dir build. Hardware-validated on an STM32F411 Blackpill with a
clone ST-Link (about 38 Hz sustained polling).
