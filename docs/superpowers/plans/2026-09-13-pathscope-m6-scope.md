# PathScope M6: Scope View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An oscilloscope-style panel that plots any polled register and
any fixed-address firmware variable over time, fed by the engine's
existing History buffer, with anomaly events marked on the time axis and
ELF symbol lookup for naming addresses.

**Architecture:** Core grows exactly one concept: an address watch (a
polled location that is NOT an SVD register). It flows through the
existing read plan (guarded-address splitting included) and lands in the
same History buffer under a synthetic key. The UI adds a Scope dock built
on pyqtgraph, repainting from `History.series()` on a timer - the engine
already records everything the plot needs. ELF symbol lookup (pyelftools)
is a pure convenience layer that turns a symbol name into an address
watch.

**Tech Stack:** existing project + pyqtgraph (ui extra) + pyelftools (ui
extra).

**Spec:** `docs/superpowers/specs/2026-09-12-data-path-explorer-design.md`
(section 11 backlog entry "Scope view" is the source requirement; honest
rate display doctrine from 6.7 applies to the plot).

## Global Constraints

- Python 3.9 syntax; pure ASCII in all source files.
- `core/` never imports Qt, pyqtgraph, or pyelftools (ELF parsing lives
  in `ui/` - it is presentation-side convenience; core sees only
  addresses).
- Address watches obey the guarded-address rules: an address inside
  `engine.guarded_addrs` is refused exactly like the memory viewer
  refuses it.
- The plot must never imply a rate the poller does not deliver: the
  scope panel shows the effective sample rate, and gaps (TARGET_LOST)
  must appear as gaps, not interpolated lines.
- badges/updates access patterns unchanged; UI touches widgets on the
  main thread only (bridge discipline).
- Commit after every task, Conventional Commits. Suite green before
  every commit. `.venv/bin/` prefixes throughout.

---

### Task 1: Core address watch

**Files:**
- Modify: `core/engine/core.py`
- Test: `tests/test_engine.py` (extend)

**Interfaces:**
- Produces:
  - `Engine.add_addr_watch(addr: int, label: str) -> str` - registers a
    32-bit word watch at `addr`; returns the synthetic key
    `"@%08X" % addr` used everywhere (snapshot values, History). `label`
    is stored in `Engine.addr_watch_labels: Dict[str, str]` for UI use.
    Raises `EngineError` if `addr` is in `guarded_addrs` or not
    word-aligned. Idempotent for the same addr.
  - `Engine.remove_addr_watch(addr_or_key) -> None`.
  - Address watches ride the same plan rebuild as `set_watch` extras:
    internally the plan is built from resolved reg addresses PLUS the
    addr-watch set; `build_read_plan` needs no change if `Engine` maps
    synthetic keys to `(addr, key)` entries - implement by extending the
    plan build to accept a prepared `{key: addr}` mapping (small
    refactor: `Engine._build_plan()` collects both sources and calls
    `build_read_plan` with a key->addr dict instead of reg_keys; adjust
    `build_read_plan` signature to take `entries: Dict[str, int]`
    (key -> address) - update its two existing callers and its tests
    mechanically; the merge/forbidden logic is unchanged).
- Poller/History need zero changes: snapshot values keyed by the
  synthetic key flow into History automatically via
  `Engine._handle_snapshot`.

- [ ] **Step 1: Write the failing tests** (extend tests/test_engine.py)

```python
def test_addr_watch_polls_and_records(rig):
    a, e, updates = rig
    a.set_word(0x20000010, 0xBEEF)
    key = e.add_addr_watch(0x20000010, "my_var")
    assert key == "@20000010"
    assert e.addr_watch_labels[key] == "my_var"
    assert wait_for(
        lambda: updates and updates[-1].snapshot.value(key) == 0xBEEF)
    assert wait_for(lambda: len(e.history.series(key)) >= 2)


def test_addr_watch_guarded_refused(rig):
    a, e, updates = rig
    import pytest as _pytest
    with _pytest.raises(EngineError):
        e.add_addr_watch(0x4001204C, "adc_dr")   # ADC1.DR readAction


def test_addr_watch_alignment_refused(rig):
    a, e, updates = rig
    import pytest as _pytest
    with _pytest.raises(EngineError):
        e.add_addr_watch(0x20000001, "misaligned")


def test_remove_addr_watch(rig):
    a, e, updates = rig
    key = e.add_addr_watch(0x20000020, "gone")
    e.remove_addr_watch(key)
    assert key not in e.polled
```

(rig's mini fixture: ADC1.DR at 0x4001204C carries readAction - already
guarded. `EngineError` import exists in the file.)

- [ ] **Step 2: Run to verify failure** (AttributeError: add_addr_watch)
- [ ] **Step 3: Implement** per the interface block. `Engine.polled`
  includes synthetic keys. `set_watch` keeps working (extras + addr
  watches both survive each other's rebuilds).
- [ ] **Step 4: Targeted tests, then full suite.**
- [ ] **Step 5: Commit** `feat(core): fixed-address watches for the scope view`

---

### Task 2: Scope panel (pyqtgraph)

**Files:**
- Modify: `pyproject.toml` (ui extra += pyqtgraph)
- Create: `ui/panels/scope_page.py`
- Modify: `ui/main_window.py`
- Test: `tests/ui/test_scope_page.py`

**Interfaces:**
- Produces: `ScopePage(engine)` - a dock panel ("Scope") with:
  - Channel list (QListWidget): add-register button (dropdown of
    currently polled reg_keys), add-address button (dialog-free inline
    QLineEdit pair: address hex + label; EngineError shown inline, no
    dialog), remove-channel.
  - pyqtgraph PlotWidget: one curve per channel, repainted from
    `engine.history.series(key)` on a 200 ms QTimer (paused with the
    global Freeze - MainWindow already exposes `frozen`; ScopePage
    polls `window.frozen` via a callback given at construction, or
    simpler: MainWindow calls `scope.set_frozen(bool)` from its
    existing freeze toggle).
  - Gap honesty: when consecutive samples are more than 3x the median
    interval apart, insert a NaN so pyqtgraph breaks the line (spec 6.7
    doctrine). Effective rate label per channel: computed from the
    last 2 s of samples.
  - Event markers: MainWindow feeds `scope.add_event_marker(t, msg)`
    from the same update path that feeds the event log; markers render
    as vertical InfiniteLine with a short label.
  - X axis: seconds relative to engine start; window selector 10 s
    (History default) - display only what History holds.
- MainWindow: Scope dock (bottom tab with Event log or right stack -
  bottom tabbed dock next to the event log), created lazily on first
  open (pyqtgraph import cost) via a toolbar toggle "Scope".

- [ ] **Step 1: Write the failing test**

```python
# tests/ui/test_scope_page.py
from ui.demo import make_demo_engine
from ui.panels.scope_page import ScopePage


def test_scope_plots_polled_register(qtbot):
    engine = make_demo_engine("targets/f411")
    engine.start()
    try:
        page = ScopePage(engine)
        qtbot.addWidget(page)
        page.add_channel("DMA2.S0NDTR")
        qtbot.waitUntil(
            lambda: page.channel_sample_count("DMA2.S0NDTR") >= 5,
            timeout=3000)
        page.refresh_plot()
        assert page.curve_point_count("DMA2.S0NDTR") >= 5
    finally:
        engine.stop()


def test_scope_addr_channel_via_engine(qtbot):
    engine = make_demo_engine("targets/f411")
    engine.start()
    try:
        page = ScopePage(engine)
        qtbot.addWidget(page)
        key = page.add_address_channel(0x20000000, "buf0")
        qtbot.waitUntil(
            lambda: page.channel_sample_count(key) >= 3, timeout=3000)
    finally:
        engine.stop()
```

(`channel_sample_count`/`curve_point_count` are small test-support
accessors on ScopePage - part of the produced interface.)

- [ ] **Step 2: Run to verify failure**
- [ ] **Step 3: Implement** per interface. pyqtgraph into the ui extra;
  reinstall.
- [ ] **Step 4: Tests + full suite; offscreen shot smoke:**
  `--shot` variant showing the scope dock open with the NDTR sawtooth
  (add `--shot-scope` arg to ui/app.py mirroring --shot-select). READ
  the PNG: sawtooth visible = the demo engine's NDTR decrement/wrap is
  the perfect self-verifying waveform.
- [ ] **Step 5: Commit** `feat(ui): pyqtgraph scope panel with event markers`

---

### Task 3: ELF symbol lookup

**Files:**
- Modify: `pyproject.toml` (ui extra += pyelftools)
- Create: `ui/elf_symbols.py`
- Modify: `ui/panels/scope_page.py`
- Test: `tests/ui/test_elf_symbols.py`

**Interfaces:**
- Produces: `ui.elf_symbols.load_symbols(path) -> List[Symbol]` where
  `Symbol = namedtuple("Symbol", "name addr size")` - all SHN_ABS/defined
  OBJECT symbols with size > 0 from the ELF symtab (globals and statics
  both have entries; statics appear with local binding - include both),
  sorted by name. Word-size filtering happens at ADD time, not load
  time (show all, but adding a channel for a symbol reads its first
  word - document in a tooltip).
- ScopePage gains "Load ELF..." (QFileDialog) + a symbol picker
  (QListWidget filtered by a QLineEdit) that calls the same
  add_address_channel with the symbol's name as label.
- Test fixture: compile nothing - craft a minimal ELF in the test using
  pyelftools? No - pyelftools reads, doesn't write. Use the repo's own
  firmware ELF IF arm-none-eabi-gcc exists (build it in the test via
  make, skip the test with pytest.skip if the toolchain is absent);
  assert `adc_buf` appears with a plausible SRAM address (0x20000000
  <= addr < 0x20020000) and size 2000.

- [ ] **Step 1: Write the failing test**

```python
# tests/ui/test_elf_symbols.py
import shutil
import subprocess

import pytest

from ui.elf_symbols import load_symbols

FWDIR = "firmware/blackpill_adc_dma"


def test_firmware_symbols_include_adc_buf():
    if shutil.which("arm-none-eabi-gcc") is None:
        pytest.skip("no ARM toolchain")
    subprocess.run(["make"], cwd=FWDIR, check=True)
    syms = {s.name: s for s in load_symbols(FWDIR + "/fw.elf")}
    assert "adc_buf" in syms
    s = syms["adc_buf"]
    assert 0x20000000 <= s.addr < 0x20020000
    assert s.size == 2000
```

- [ ] **Step 2: Run to verify failure**
- [ ] **Step 3: Implement** loader + picker UI.
- [ ] **Step 4: Tests + full suite.**
- [ ] **Step 5: Commit** `feat(ui): elf symbol lookup for scope channels`

---

### Task 4: Event-log cursor sync

**Files:**
- Modify: `ui/panels/event_log.py`, `ui/panels/scope_page.py`,
  `ui/main_window.py`
- Test: `tests/ui/test_scope_page.py` (extend)

**Interfaces:**
- Clicking an event-log row moves a scope cursor (a distinct vertical
  line) to that event's timestamp and flashes the corresponding marker;
  EventLog rows already carry a focus payload - extend it to carry the
  event time; MainWindow routes to `scope.jump_to(t)` when the scope
  dock exists.
- Test: feed a synthetic AnomalyEvent through the MainWindow update
  path (reuse the seeded-OVR engine helper from
  tests/ui/test_flow_and_log.py), click the log row programmatically,
  assert `scope.cursor_time()` equals the event's t (within float eps).

- [ ] Steps: failing test -> implement -> suite -> commit
  `feat(ui): event-to-scope cursor sync`

---

### Task 5: Hardware gate M6 (controller + user)

- [ ] Real board: add DMA2.S0NDTR channel - sawtooth at ~38 Hz effective
  rate; add `adc_buf[0]` via ELF (build firmware, load fw.elf, pick
  adc_buf) - noisy ADC line; press KEY - stall visible as flat line +
  event marker aligned; unplug - gap in the trace (NaN break), replug -
  trace resumes. Record results in README's validation section
  (one compact block), commit `docs: record M6 scope validation`.

---

## After this plan

- M7 candidate: trace-buffer ingestion (AHB-trace-class sources) - the
  company-side lever; public repo keeps the interface generic.
- Deferred within M6 unless trivially reached: per-channel y-axis
  scaling UI, CSV export of a channel window.
