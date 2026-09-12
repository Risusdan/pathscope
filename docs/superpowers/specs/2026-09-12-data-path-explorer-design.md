# Data Path Explorer - Design Spec

Date: 2026-09-12
Status: Draft for review
Scope: MVP feasibility phase (target: STM32F411 "Blackpill" board)

## 1. Purpose

A desktop debug tool that visualizes an MCU/SoC's internal data paths as a
live block diagram: which paths data is flowing through, how the involved
registers evolve, and rule-based detection of anomalies (overrun flags,
stalled DMA, dead buffers).

Positioning: a *state-evolution observer* for system-level bring-up and
debugging, not a logic analyzer. It samples register state over a debug
probe at tens of Hz; it does not capture per-transaction bus traffic.

The tool is target-agnostic by design. The MVP ships with an STM32F411
target description and an SWD (pyOCD) transport. New SoCs are added by
writing description files and, where needed, a new transport adapter
(e.g. a memory-mapped debug support unit reachable over UART/JTAG) -- no
changes to the core engine or UI.

## 2. Non-Goals

- Cycle-accurate or per-transaction bus tracing.
- Replacing existing debuggers (GDB, vendor IDEs); this tool complements
  them for system-level data-flow visibility.
- ML-based anomaly detection. Anomalies are declarative rules only.
- Firmware-side instrumentation. The target firmware is never required to
  cooperate; observation is via the debug port only.
- Multi-probe / multi-target sessions (single target per session for MVP).

## 3. Deployment Form

Standalone desktop application.

- Development: Python + PySide6 (Qt6), runs from source on macOS/Linux/
  Windows.
- Distribution: PyInstaller **onedir** bundle zipped per release
  (single-file exe rejected: slow start, antivirus false positives).
  Windows exe built by GitHub Actions (`windows-latest` runner) on tag
  push; developer machine may be macOS.
- Target description files live in a `targets/` directory *next to* the
  executable, never inside the bundle, so users can add or edit targets
  without rebuilding.
- No separate Qt installation required (PySide6 wheels bundle Qt), and no
  admin install; unzip and run. ST-Link on Windows needs the usual WinUSB
  driver (documented in README).

Architecture keeps the core headless (see 4) so a future split into a
daemon + external UI (e.g. an embedding C++ application speaking JSON over
a local socket) remains a bounded change, but no such protocol is built
for MVP.

## 4. Architecture

```
data-path-explorer/
  core/                     # no Qt imports anywhere in this package
    adapter/
      base.py               # TargetAdapter ABC
      pyocd_swd.py          # SWD via pyOCD (MVP)
    target/
      svd_loader.py         # CMSIS-SVD -> register model (wraps pyOCD's parser)
      topology.py           # *.topology.yaml -> blocks/edges model
      flows.py              # *.flows.yaml -> activity/anomaly rules
    engine/
      poller.py             # scheduling, batching, command queue, reconnect
      evaluator.py          # rule evaluation over snapshots + history
      snapshot.py           # data model: RegisterSnapshot, FlowEvent, AnomalyEvent
  ui/                       # PySide6
    main_window.py
    diagram/                # QGraphicsScene block diagram
    panels/                 # register inspector, memory viewer, event log
  targets/
    f411.svd
    f411.topology.yaml
    f411.flows.yaml
  firmware/                 # Blackpill test firmware (ADC->DMA->SRAM)
  docs/
```

Data flows one way: `adapter -> poller -> evaluator -> UI`. The UI only
consumes events; user actions that touch hardware (halt, memory dump)
are enqueued as commands to the poller thread.

Core/UI boundary: `core` exposes callback registration only. The UI wraps
callbacks into Qt signals (queued connections) on its side. `core` is
fully testable under pytest with no display and no hardware.

### 4.1 TargetAdapter interface

```python
class TargetAdapter(ABC):
    def connect(self) -> TargetInfo: ...
    def disconnect(self) -> None: ...
    def read_mem(self, addr: int, size: int) -> bytes: ...
    def read_block32(self, addr: int, count: int) -> list[int]: ...
    def write_mem(self, addr: int, data: bytes) -> None: ...
    def halt(self) -> None: ...
    def resume(self) -> None: ...
    def is_running(self) -> bool: ...
```

Everything above the adapter sees addresses and words, never probe
details. A future DSU-style adapter implements the same interface over a
serial link.

## 5. Target Description Files (three layers)

A target = three files. Adding a SoC touches zero code.

### 5.1 Register layer: CMSIS-SVD (existing standard)

Standard SVD XML. Provides peripheral/register/field names, addresses,
bit definitions, access attributes, and `readAction` (see 6.3). STM32
SVDs are published by ST; other SoCs supply their own file.

### 5.2 Topology layer: `<target>.topology.yaml` (this project's schema)

Declares blocks and directed edges of the data-path diagram.

```yaml
blocks:
  - id: adc1
    kind: peripheral        # peripheral | dma | memory | cpu | interconnect | pin
    svd: ADC1               # link into SVD (peripheral kinds only)
  - id: dma2
    kind: dma
    svd: DMA2
  - id: sram1
    kind: memory
    base: 0x20000000
    size: 0x20000
  - id: busmx
    kind: interconnect
edges:
  - { from: adc1, to: dma2,  via: busmx, label: "S0 CH0" }
  - { from: dma2, to: sram1, via: busmx }
layout:                     # optional; absent -> graphviz dot auto-layout
  adc1: { x: 0, y: 120 }
```

### 5.3 Flow layer: `<target>.flows.yaml` (this project's schema)

Maps register state to path activity and anomalies. Expressions are a
Python-subset evaluated by a sandboxed evaluator (simpleeval-class); no
custom grammar.

```yaml
activities:
  - name: adc_dma_stream
    path: [adc1, dma2, sram1]          # ordered block ids; edges derived
    active_when: "DMA2.S0CR.EN == 1"
    progress: "DMA2.S0NDTR"            # decreasing value == data moving
    anomalies:
      - { rule: "DMA2.LISR.TEIF0 == 1",        msg: "DMA transfer error" }
      - { rule: "ADC1.SR.OVR == 1",            msg: "ADC overrun, data lost" }
      - { rule: "stalled(DMA2.S0NDTR, 500)",   msg: "DMA stalled" }  # window in ms
poll:
  force_poll: []             # registers to poll despite readAction (explicit opt-in)
```

Built-in functions for MVP: `stalled(reg, window)`, `changed(reg)`.
Register references are `PERIPH.REG` or `PERIPH.REG.FIELD`, resolved
against the SVD model at load time (load fails fast on unknown names).

## 6. Polling Engine

The riskiest layer; design constraints below are the core of the MVP.

### 6.1 Single owner thread

One poller thread owns the adapter exclusively. All hardware access --
periodic polling, user-triggered halt/resume, on-demand memory dumps --
goes through this thread via a priority command queue. pyOCD is never
touched from two threads.

### 6.2 Batched reads

USB transaction latency dominates (~0.5-1 ms per transaction on clone
ST-Links). The poller:

1. Collects the union of registers referenced by loaded rules and by the
   currently visible UI panels.
2. Groups them by peripheral; contiguous register banks are fetched with
   one `read_block32` per group.

Budget assumption to be validated in milestone M1: a few hundred
transactions/second on a clone ST-Link; 3-4 monitored peripherals at
20-50 Hz overall snapshot rate.

### 6.3 Read side effects (hard safety rule)

Some registers change hardware state when read (read-to-clear status
flags, FIFO-popping data registers, e.g. STM32 `SPI->DR`). Polling them
would make the tool corrupt the system it observes.

- The SVD `readAction` attribute (`clear`, `modifyExternal`, ...) marks
  such registers; the loader honors it.
- Policy: a register is polled only if (a) some rule or visible panel
  needs it AND (b) it has no `readAction`, unless (c) it is explicitly
  listed under `poll.force_poll` in flows.yaml, in which case the UI
  displays a persistent warning badge on it.

### 6.4 Snapshot semantics

A snapshot is not atomic; registers within one sweep are read up to a few
ms apart. Every value carries its own timestamp. Documentation and UI
copy state this plainly. Rules must not depend on same-instant
consistency across registers.

### 6.5 History

The engine keeps a per-register ring buffer (value, timestamp) sized by
time window (default 10 s). `stalled()`/`changed()` evaluate against this
history; the UI can render sparklines from the same buffer.

### 6.6 Target-loss handling

Adapter exceptions (unplug, target reset, power loss) move the engine to
`TARGET_LOST`: UI greys out, poller retries reconnection periodically,
recovery is automatic. No modal error dialogs.

### 6.7 Honest rate display

Measured achieved snapshot rate (Hz) is always visible in the toolbar.
Users must never mistake the display for real time.

## 7. UI (draft -- to be refined via prototype)

This section is a starting point only. Layout and interaction will be
iterated on a throwaway UI prototype with stub data before implementation
(see 9, milestone M3 gate).

Single main window:

- Center: QGraphicsView block diagram. Blocks/edges from topology.yaml;
  coordinates from graphviz `dot` unless overridden in `layout:`.
  Active edges animate (dash-offset) and highlight; anomaly turns the
  involved blocks red and logs an event.
- Right dock: Register Inspector for the selected block -- register/field
  tree from SVD, live values, just-changed values flash. Field view shows
  bit definitions.
- Bottom dock: Event log (timestamped flow/anomaly events; click to
  focus the related block).
- Memory viewer: on-demand hex dump for memory blocks (command-queue
  read; optional periodic refresh). Waveform rendering of buffers is a
  post-MVP stretch goal.
- Toolbar: connect/disconnect, halt/resume, target name, measured poll
  rate.

## 8. Testing Strategy

- **Core (bulk of coverage):** pytest with a `MockAdapter` that replays
  scripted or recorded register sequences. Poller scheduling, rule
  evaluation, history functions, target-loss transitions -- all covered
  with no hardware. Recorded real sessions become deterministic
  regression fixtures.
- **Adapter:** a small set of integration tests marked `@pytest.mark.hw`
  requiring a connected Blackpill; skipped in CI.
- **Firmware:** ADC->DMA->SRAM circular-buffer firmware, plus
  fault-injection modes (button-triggered: disable DMA stream, force ADC
  overrun) used to demonstrate anomaly rules end-to-end.
- **UI:** manual testing for MVP; pytest-qt smoke tests later.

## 9. Milestones (each independently demoable)

- **M0** - Repo scaffold, venv, pyOCD connects to Blackpill, read and
  print CPUID.
- **M1** - SVD load + whitelist polling + measured poll rate, CLI output.
  *Validates the bandwidth budget; the project's biggest technical risk
  retires here. If clone ST-Link rates are unusable, reassess (better
  probe or repositioning) before building more.*
- **M2** - Test firmware + flows rule engine; CLI prints flow
  active/stalled/anomaly events. Core value proven.
- **M3** - UI/UX prototype review gate (stub data, throwaway allowed),
  then PySide6 diagram + activity highlight + register inspector.
- **M4** - Memory viewer + event log + fault-injection demo. Feasibility
  line: the build shown to stakeholders.
- **M5** - PyInstaller onedir packaging + GitHub Actions Windows build.

## 10. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Clone ST-Link poll rate too low | Tool feels dead | M1 measures first; J-Link fallback; scope displayed rate honestly |
| Read side effects corrupt target state | Trust destroyed | readAction whitelist policy (6.3), default-deny |
| Diagram auto-layout ugly/unstable | Poor first impression | graphviz dot + manual layout override in YAML |
| Rule expression scope creep | DSL rabbit hole | Python-subset evaluator, two built-ins only for MVP |
| pyOCD thread misuse | Random USB errors | Single owner thread + command queue (6.1) |

## 11. Out of Scope for MVP (recorded for later)

- Additional transport adapters (UART-based debug units, J-Link native).
- Buffer waveform view, trace-buffer ingestion (AHB-trace-class sources).
- Session record/replay in the UI (engine-level recording exists for tests).
- Multi-target sessions; remote daemon protocol.
