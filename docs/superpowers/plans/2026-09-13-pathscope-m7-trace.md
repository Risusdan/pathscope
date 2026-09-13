# PathScope M7 Trace-Buffer Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the polling scope with a firmware trace-buffer
scope: the target samples records coherently in its own timer ISR,
PathScope drains, decodes and plots them.

**Architecture:** A pure-python memory contract (`core/trace/`)
encodes/decodes the descriptor, records and watch table in either
endianness; a `TraceReader` drives discovery, drain and the
count-gate table protocol through the existing Engine command queue;
a numpy `TraceStore` holds per-slot rings; the scope page becomes a
fixed-10-slot trace UI. A reference C sampler ships in
`firmware/ps_trace/` and is integrated into the Blackpill demo.

**Tech Stack:** Python 3.9+, PySide6, pyqtgraph (+numpy, already its
hard dep), pytest/pytest-qt (offscreen), C99 (arm-none-eabi-gcc).

**Spec:** docs/superpowers/specs/2026-09-13-pathscope-m7-trace-design.md
(binding; all magic numbers below are copied from it).

## Global Constraints

- Contract constants, verbatim from the spec: magic `0x50435350`
  ('PSCP' little-endian view), version `1`, `PS_MAX_CH = 10`,
  `ring_count = 256`, `record_size = 48`, status codes
  `0=OK, 1=BAD_ADDR, 2=BAD_COUNT`.
- `core/` stays dependency-free: numpy appears ONLY under `ui/`.
- Pure ASCII in all source/config files. C headers get Doxygen
  comments (`@file/@brief/@param/@return`, members via `/**< */`).
- Light theme only; errors render inline in panels, never a
  QMessageBox.
- No company identifiers anywhere (public repo); big-endian coverage
  is phrased generically.
- The Data Path page's polling (flows, badges, inspector) must keep
  working unchanged; `Engine.add_addr_watch`/`remove_addr_watch` and
  their tests STAY (generic core API) even though the scope no longer
  uses them.
- Tests run offscreen: `QT_QPA_PLATFORM=offscreen`. Hardware-only
  tests carry `@pytest.mark.hardware` (CI deselects the marker; the
  existing "1 deselected" test shows the pattern - copy its marker
  registration, do not invent a new mechanism).
- Commits: conventional commits, no AI attribution lines.

## File Structure

- `core/trace/__init__.py`, `core/trace/contract.py` - layout
  constants, `TraceDesc`, encode/parse for both endiannesses (encode
  exists for tests and the demo simulator).
- `core/trace/reader.py` - `TraceReader`: discovery, drain, watch
  protocol; talks only to `Engine.read_words`/`Engine.write_word`.
- `core/engine/core.py` - add `write_word` (thin `_exec` wrapper).
- `tests/trace_sim.py` - `FakeTraceFirmware`: a host-side stand-in
  for `ps_trace.c` living inside a MockAdapter's memory dict; used by
  reader tests and by `ui/demo.py`.
- `firmware/ps_trace/ps_trace.h`, `firmware/ps_trace/ps_trace.c` -
  reference sampler; `firmware/blackpill_adc_dma/main.c` + `Makefile`
  integrate it at 1 kHz.
- `ui/trace_store.py` - numpy per-slot rings + gap math.
- `ui/demo.py` - demo engine gains a simulated trace target so
  `--demo`/`--shot` keep working.
- `ui/panels/scope_page.py` - rewritten as the trace page (file name
  and class name `ScopePage` kept; MainWindow wiring untouched).
- `cli.py` - new `bench-read` subcommand (throughput spike).
- `tests/hw/test_trace_hw.py` - hardware E2E suite (agent-drivable
  with a board attached).

---

### Task 1: Trace memory contract (`core/trace/contract.py`)

**Files:**
- Create: `core/trace/__init__.py` (empty), `core/trace/contract.py`
- Test: `tests/test_trace_contract.py`

**Interfaces:**
- Produces (later tasks import these exact names):

```python
MAGIC = 0x50435350          # 'PSCP' read as little-endian u32
VERSION = 1
MAX_CH = 10
RING_COUNT = 256
RECORD_SIZE = 8 + 4 * MAX_CH          # 48
DESC_SIZE = 24 + 4 * MAX_CH + 4       # 68: header 24 + table 40 + count/gen/reserved 4
STATUS_OK, STATUS_BAD_ADDR, STATUS_BAD_COUNT = 0, 1, 2

@dataclass(frozen=True)
class TraceDesc:
    endian: str          # "<" or ">"
    version: int
    max_ch: int
    status: int
    period_us: int
    record_size: int
    ring_count: int
    ring_addr: int
    wr_seq: int
    watch_addrs: tuple   # length max_ch
    watch_count: int
    generation: int

@dataclass(frozen=True)
class TraceRecord:
    seq: int
    gen: int
    slots: tuple         # length max_ch, raw u32

class ContractError(Exception): ...

def parse_desc(words: List[int]) -> TraceDesc          # words = 17 u32 from read_words
def parse_records(words: List[int], desc: TraceDesc) -> List[TraceRecord]
def encode_desc(desc_fields..., endian: str) -> List[int]   # test/sim helper
def encode_record(seq: int, gen: int, slots: List[int], endian: str) -> List[int]
def record_word_addr(desc, seq) -> int   # ring_addr + (seq % ring_count) * record_size
```

Layout is the spec section 3 tables verbatim. `parse_desc` detects
endianness from the magic: the adapter returns little-endian-composed
u32 words (pyOCD semantics), so on a big-endian target every word
arrives byte-swapped - `parse_desc` sees `0x50534350`-style swapped
magic, sets `endian=">"` and byte-swaps every field (including each
record slot in `parse_records`). Wrong magic in both orders raises
`ContractError("no trace descriptor at this address")`; version
mismatch raises `ContractError("unsupported trace version N")`.

- [ ] **Step 1: failing tests** - round-trip both endians, bad magic,
  bad version, ring addressing:

```python
import pytest
from core.trace.contract import (MAGIC, VERSION, MAX_CH, RECORD_SIZE,
                                 ContractError, TraceDesc, encode_desc,
                                 encode_record, parse_desc,
                                 parse_records, record_word_addr)

def _desc_words(endian):
    return encode_desc(period_us=1000, ring_addr=0x20001000,
                       ring_count=256, wr_seq=7,
                       watch_addrs=[0x20000000] * 3 + [0] * 7,
                       watch_count=3, generation=2, status=0,
                       endian=endian)

@pytest.mark.parametrize("endian", ["<", ">"])
def test_desc_round_trip(endian):
    d = parse_desc(_desc_words(endian))
    assert d.endian == endian
    assert (d.version, d.max_ch, d.period_us) == (VERSION, MAX_CH, 1000)
    assert d.watch_count == 3 and d.generation == 2
    assert d.watch_addrs[0] == 0x20000000 and d.watch_addrs[9] == 0

@pytest.mark.parametrize("endian", ["<", ">"])
def test_record_round_trip(endian):
    d = parse_desc(_desc_words(endian))
    words = encode_record(seq=41, gen=2,
                          slots=list(range(100, 110)), endian=endian)
    assert len(words) * 4 == RECORD_SIZE
    (r,) = parse_records(words, d)
    assert (r.seq, r.gen) == (41, 2)
    assert r.slots == tuple(range(100, 110))

def test_bad_magic_and_version_raise():
    words = _desc_words("<")
    with pytest.raises(ContractError):
        parse_desc([0xDEADBEEF] + words[1:])
    bad = encode_desc(period_us=1000, ring_addr=0, ring_count=256,
                      wr_seq=0, watch_addrs=[0] * 10, watch_count=0,
                      generation=0, status=0, endian="<", version=9)
    with pytest.raises(ContractError):
        parse_desc(bad)

def test_record_word_addr_wraps():
    d = parse_desc(_desc_words("<"))
    assert record_word_addr(d, 0) == 0x20001000
    assert record_word_addr(d, 256) == 0x20001000
    assert record_word_addr(d, 5) == 0x20001000 + 5 * RECORD_SIZE
```

- [ ] **Step 2:** run `pytest tests/test_trace_contract.py -q` -> FAIL
  (module missing).
- [ ] **Step 3:** implement `contract.py`. Encode/parse via
  `struct.pack`/`struct.unpack` on the byte level: convert the u32
  word list to bytes with `struct.pack("<%dI" % n, *words)` (adapter
  word order), then unpack fields with the detected `endian` format
  string. `encode_*` reverses the same path so a big-endian encode
  fed through a little-endian word transport reproduces firmware
  behavior exactly.
- [ ] **Step 4:** tests pass; full suite still green.
- [ ] **Step 5:** commit `feat(core): trace memory contract with endian detection`.

---

### Task 2: `Engine.write_word`

**Files:**
- Modify: `core/engine/core.py` (next to `read_words`, ~line 260)
- Test: `tests/test_engine.py` (append)

**Interfaces:**
- Consumes: `Engine._exec`, `TargetAdapter.write32` (both exist).
- Produces: `Engine.write_word(addr: int, value: int) -> None` -
  executed on the poller thread via the command queue, raising
  `EngineError` on failure exactly like `read_words`.

- [ ] **Step 1: failing test** (append to `tests/test_engine.py`,
  reusing its existing engine/MockAdapter fixture helpers):

```python
def test_write_word_lands_in_target_memory_via_poller():
    adapter = MockAdapter({0x20000100: 0})
    engine = Engine.load("targets/f411", adapter, interval_s=0.01)
    engine.start()
    try:
        engine.write_word(0x20000100, 0xCAFEF00D)
        assert adapter.mem[0x20000100] == 0xCAFEF00D
    finally:
        engine.stop()
```

- [ ] **Step 2:** run -> FAIL (`write_word` missing).
- [ ] **Step 3:** implement, mirroring `read_words`'s `_exec` pattern:

```python
    def write_word(self, addr: int, value: int) -> None:
        """Single-word write on the poller thread (command queue) -
        the trace watch-table protocol's building block. Single-word
        writes are atomic with respect to the target's ISR."""
        self._exec(lambda: self._adapter.write32(addr, value))
```

(match the exact attribute `read_words` uses for the adapter).
- [ ] **Step 4:** tests pass. **Step 5:** commit
  `feat(core): engine write_word via poller command queue`.

---

### Task 3: Firmware simulator + `TraceReader`

**Files:**
- Create: `tests/trace_sim.py`, `core/trace/reader.py`
- Test: `tests/test_trace_reader.py`

**Interfaces:**
- Consumes: Task 1 contract (all names), Task 2 `Engine.write_word`,
  `Engine.read_words`, `MockAdapter` (mem dict, `set_word`).
- Produces:

```python
# tests/trace_sim.py
class FakeTraceFirmware:
    def __init__(self, adapter: MockAdapter, desc_addr: int = 0x20002000,
                 ring_addr: int = 0x20001000, period_us: int = 1000,
                 whitelist=((0x20000000, 0x20020000),), endian: str = "<")
    def step(self, n: int = 1) -> None   # emit n records honoring the gate
    # reacts to adapter writes: watch_count==0 -> gate closed (records
    # still emitted, slots untouched=0); count 0->N -> validate against
    # whitelist; ok: generation += 1, sample *addr for each entry (reads
    # adapter.mem, missing keys -> BAD_ADDR too); bad: status=BAD_ADDR/
    # BAD_COUNT, count stays 0.

# core/trace/reader.py
class TraceError(Exception): ...

class TraceReader:
    def __init__(self, engine: Engine)
    def discover(self, desc_addr: int) -> TraceDesc      # read+parse, ContractError -> TraceError
    def refresh(self) -> List[TraceRecord]               # new records since last, seq-deduped
    lost: int                                            # records lost to overwrite, cumulative
    def set_watch(self, addrs: List[int]) -> None        # full gate protocol, blocks < 1 s
    def status(self) -> TraceDesc                        # re-read descriptor
```

`refresh()` reads the descriptor (17 words), then the new record
range `[last_seq+1, wr_seq)` in at most TWO block reads (the range is
contiguous in the ring except across the wrap point - split there).
If `wr_seq - last_seq > ring_count`, the overwritten span is added to
`self.lost` and reading starts at `wr_seq - ring_count`. `set_watch`
raises `TraceError` on `len(addrs) > desc.max_ch`, writes count=0,
each address, count=N, then polls `status()` (up to 10 x 50 ms) until
generation increments or a nonzero status appears; nonzero ->
`TraceError("firmware rejected table: BAD_ADDR")` etc.

- [ ] **Step 1: failing tests:**

```python
import pytest
from core.adapter.mock import MockAdapter
from core.engine.core import Engine
from core.trace.reader import TraceError, TraceReader
from tests.trace_sim import FakeTraceFirmware

def _rig():
    adapter = MockAdapter({0x20000000: 111, 0x20000004: 222})
    fw = FakeTraceFirmware(adapter)
    engine = Engine.load("targets/f411", adapter, interval_s=0.01)
    engine.start()
    return adapter, fw, engine

def test_discover_and_drain_records():
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        desc = r.discover(0x20002000)
        assert desc.period_us == 1000
        r.set_watch([0x20000000, 0x20000004])
        fw.step(5)
        recs = r.refresh()
        assert len(recs) >= 5
        assert recs[-1].slots[0] == 111 and recs[-1].slots[1] == 222
        assert r.refresh() == []          # nothing new, no re-delivery
    finally:
        engine.stop()

def test_overflow_counts_lost_and_resumes():
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        r.discover(0x20002000)
        r.set_watch([0x20000000])
        fw.step(3); r.refresh()
        fw.step(300)                      # > ring_count: oldest overwritten
        recs = r.refresh()
        assert r.lost > 0
        assert recs[0].seq > 3            # resumed past the hole
    finally:
        engine.stop()

def test_set_watch_rejected_address_raises():
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        r.discover(0x20002000)
        with pytest.raises(TraceError):
            r.set_watch([0x99999990])     # outside sim whitelist
        assert r.status().watch_count == 0
    finally:
        engine.stop()

def test_set_watch_too_many_raises_before_touching_target():
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        r.discover(0x20002000)
        with pytest.raises(TraceError):
            r.set_watch([0x20000000] * 11)
    finally:
        engine.stop()

def test_generation_marks_table_change():
    adapter, fw, engine = _rig()
    try:
        r = TraceReader(engine)
        r.discover(0x20002000)
        r.set_watch([0x20000000]); fw.step(2)
        first = r.refresh()
        r.set_watch([0x20000004]); fw.step(2)
        second = r.refresh()
        assert second[-1].gen == first[-1].gen + 1
    finally:
        engine.stop()
```

- [ ] **Step 2:** run -> FAIL. **Step 3:** implement `trace_sim.py`
  first (hook `MockAdapter.write32` by subclassing or wrapping - the
  sim owns the adapter it is given: monkey-patch its `write32` to
  intercept writes into the descriptor range, mirror everything else
  to the original), then `reader.py`. The sim keeps the descriptor
  and ring serialized in `adapter.mem` via `encode_desc`/
  `encode_record` on every mutation, so the reader exercises the real
  parse path. **Step 4:** green. **Step 5:** commit
  `feat(core): trace reader with drain, gate protocol and loss accounting`.

---

### Task 4: Reference firmware module + Blackpill integration

**Files:**
- Create: `firmware/ps_trace/ps_trace.h`, `firmware/ps_trace/ps_trace.c`
- Modify: `firmware/blackpill_adc_dma/main.c`,
  `firmware/blackpill_adc_dma/Makefile`
- Test: `tests/test_ps_trace_build.py`

**Interfaces:**
- Produces (C, all Doxygen-commented):

```c
/* ps_trace.h - configuration the integrator edits: */
#define PS_TRACE_MAX_CH     10u
#define PS_TRACE_RING_COUNT 256u
#define PS_TRACE_PERIOD_US  1000u
/* whitelist: integrator-defined readable ranges, checked on table accept */
typedef struct { uint32_t lo; uint32_t hi; } ps_trace_range_t;
extern const ps_trace_range_t ps_trace_whitelist[];
extern const uint32_t ps_trace_whitelist_len;

void ps_trace_init(void);
void ps_trace_sample(void);   /* call from ONE periodic timer ISR */
extern volatile ps_trace_desc_t ps_trace_desc;  /* the ELF symbol the tool finds */
```

`ps_trace_desc_t` mirrors the spec section 3.1/3.3 layout exactly
(same field order, `__attribute__((packed, aligned(4)))`), the ring
is `static volatile uint8_t ps_trace_ring[PS_TRACE_RING_COUNT * 48]`.
`ps_trace_sample()`: if a table-change is pending (count transitioned
0 -> N since last tick), validate every address against the
whitelist; accept (generation++) or reject (status, count back to 0).
Then write one record at `wr_seq % RING_COUNT` (seq, gen, slots via
`*(volatile uint32_t *)addr`; slots beyond count = 0) and increment
`wr_seq` LAST (the tool reads `wr_seq` as the publish barrier).

Blackpill integration: TIM2 at 1 kHz (APB1 timer clock 48 MHz, PSC
47, ARR 999), ISR calls `ps_trace_sample()`; whitelist = SRAM
`0x20000000-0x20020000` plus `0x40026400-0x40026500` (DMA2) and
`0x40012000-0x40012100` (ADC1) so register channels work; NVIC enable
+ handler in the vector table follow the existing `main.c` style
(bare-metal, no HAL - read the file first and match it).

- [ ] **Step 1: build-guard test** (same skip pattern as the existing
  ELF test - copy its guard):

```python
import shutil, subprocess, pytest

@pytest.mark.skipif(shutil.which("arm-none-eabi-gcc") is None,
                    reason="cross toolchain not installed")
def test_firmware_builds_with_ps_trace():
    subprocess.run(["make", "-C", "firmware/blackpill_adc_dma", "clean"],
                   check=True, capture_output=True)
    r = subprocess.run(["make", "-C", "firmware/blackpill_adc_dma"],
                       check=True, capture_output=True, text=True)
    nm = subprocess.run(["arm-none-eabi-nm", "firmware/blackpill_adc_dma/fw.elf"],
                        check=True, capture_output=True, text=True)
    assert "ps_trace_desc" in nm.stdout
```

- [ ] **Step 2:** FAIL (module absent). **Step 3:** implement
  `ps_trace.h/.c` + integration. **Step 4:** build test passes
  locally (skips on CI). **Step 5:** commit
  `feat(firmware): ps_trace reference sampler, blackpill 1 kHz integration`.

---

### Task 5: numpy `TraceStore` (`ui/trace_store.py`)

**Files:**
- Create: `ui/trace_store.py`
- Test: `tests/ui/test_trace_store.py`

**Interfaces:**
- Consumes: `TraceRecord`, `TraceDesc` (Task 1).
- Produces:

```python
class TraceStore:
    def __init__(self, desc: TraceDesc, window_s: float = 10.0)
    def append(self, records: List[TraceRecord]) -> None
    def series(self, slot: int) -> Tuple[np.ndarray, np.ndarray]
        # (t_seconds, raw_u32) inside the window; a seq gap or a
        # generation change inserts one NaN sample pair so
        # connect="finite" breaks the line there (M6 gap honesty)
    def newest(self, slot: int) -> Optional[int]
    def latest_gen(self) -> int
    def clear(self) -> None
```

Internally: one growing numpy structured buffer (seq u64, t f64,
gen u16, slots (10,) u32) trimmed to the window on append;
`t = seq * desc.period_us * 1e-6`. NaN break rows carry slots as the
sentinel handled in `series` (store f64 columns for t and per-slot
f64 view for plotting - decode to display types happens in the page,
not here).

- [ ] **Step 1: failing tests:**

```python
import numpy as np
from core.trace.contract import TraceRecord, parse_desc
from tests.test_trace_contract import _desc_words
from ui.trace_store import TraceStore

def _rec(seq, gen=1, v=0):
    return TraceRecord(seq=seq, gen=gen, slots=tuple([v] * 10))

def test_series_time_axis_from_period():
    store = TraceStore(parse_desc(_desc_words("<")))
    store.append([_rec(0, v=5), _rec(1, v=6), _rec(2, v=7)])
    t, y = store.series(0)
    assert np.allclose(t[:3], [0.0, 0.001, 0.002])
    assert y[0] == 5 and store.newest(0) == 7

def test_seq_gap_and_gen_change_insert_nan_break():
    store = TraceStore(parse_desc(_desc_words("<")))
    store.append([_rec(0), _rec(1), _rec(5)])          # gap 2..4
    _t, y = store.series(0)
    assert np.isnan(y).any()
    store2 = TraceStore(parse_desc(_desc_words("<")))
    store2.append([_rec(0, gen=1), _rec(1, gen=2)])    # table change
    _t2, y2 = store2.series(0)
    assert np.isnan(y2).any()

def test_window_trims_old_samples():
    store = TraceStore(parse_desc(_desc_words("<")), window_s=0.01)
    store.append([_rec(i) for i in range(100)])        # 100 ms of data
    t, _y = store.series(0)
    assert t[-1] - t[0] <= 0.011
```

- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** green.
- [ ] **Step 5:** commit `feat(ui): numpy trace store with gap-honest series`.

---

### Task 6: Demo-mode trace target (`ui/demo.py`)

**Files:**
- Modify: `ui/demo.py` (animate thread, `make_demo_engine`)
- Test: `tests/ui/test_demo_trace.py`

**Interfaces:**
- Consumes: `FakeTraceFirmware` (Task 3 - import from `tests.trace_sim`
  is not allowed in shipped code: MOVE the class to
  `core/trace/sim.py` in this task, re-export from `tests/trace_sim.py`
  for the Task 3 tests, and note the move in the commit).
- Produces: `make_demo_engine()` return gains `.trace_desc_addr`
  attribute (int) so the UI can discover without an ELF in demo mode;
  the animate thread calls `fw.step()` at ~200 Hz emitting a sine on
  slot addresses `0x20000000` (u16 ADC-like) and the NDTR sawtooth
  address already animated.

- [ ] **Step 1: failing test:**

```python
from core.trace.reader import TraceReader
from ui.demo import make_demo_engine

def test_demo_engine_exposes_live_trace(qtbot=None):
    engine = make_demo_engine("targets/f411")
    engine.start()
    try:
        r = TraceReader(engine)
        desc = r.discover(engine.trace_desc_addr)
        assert desc.period_us > 0
        r.set_watch([0x20000000])
        import time; time.sleep(0.2)
        assert len(r.refresh()) > 0
    finally:
        engine.stop()
```

- [ ] **Step 2:** FAIL. **Step 3:** implement (sim rate scaled: demo
  period_us=5000, step batches in the existing animate loop).
- [ ] **Step 4:** green. **Step 5:** commit
  `feat(ui): demo engine carries a simulated trace target`.

---

### Task 7: Scope page rework A - trace plumbing

**Files:**
- Modify: `ui/panels/scope_page.py`, `ui/main_window.py` (scope
  activation passes the engine only - unchanged call), `ui/app.py`
  (`--shot-scope` path: demo discovery via `engine.trace_desc_addr`)
- Test: `tests/ui/test_scope_page.py` (rework in place)

**Scope of this task (reviewable on its own):** the page discovers,
shows state, and renders the fixed table skeleton - channels land in
Task 8.

- Page states, all inline (no dialogs): NO_SOURCE ("Load ELF with a
  ps_trace-instrumented firmware to use the scope"), BAD_MAGIC /
  BAD_VERSION (specific text from `TraceError`), READY.
- Discovery: `load_elf()` keeps the existing symbol loader; after
  loading, look up symbol `ps_trace_desc`, call
  `TraceReader.discover`; demo mode (`engine.trace_desc_addr`
  attribute present) discovers automatically at construction.
- Header row: `t-now`, inline error label, `"%d Hz (firmware)" %
  round(1e6 / desc.period_us)`, drain-health label (hidden unless
  `reader.lost` grew within the last window: "lost N samples"),
  Auto-lane, big Run/Stop. DELETE: budget label, skew label, window
  label remnants, per-channel Hz column, the register/address add
  rows and their handlers, `add_addr_watch` usage, `_polled`-based
  channel add - the ELF group remains the only source control.
- Channel table: ALWAYS `MAX_CH` rows, row i = slot i; palette
  binds by row (`CURVE_COLORS[i]`); empty slot renders a grey
  placeholder row (name "-", other cells blank, disabled).
- Repaint timer, splitter layout, crosshair machinery, y-axis
  follow, Run/Stop button + spacebar all survive verbatim.
- Adapt/delete tests referencing removed widgets in the same commit;
  keep every cursor/y-axis/Run-Stop test compiling against the new
  construction (they add channels via a Task 8 API - in THIS task
  mark those with `pytest.mark.skip(reason="channels land in T8")`,
  and Task 8 MUST unskip all of them; the review gate checks no skip
  survives).

- [ ] Steps: failing tests for the three states + fixed row count +
  rate header (concrete asserts below), implement, unskip nothing
  yet, full suite green, commit
  `feat(ui): scope page trace discovery, states and fixed slot table`.

```python
def test_scope_shows_no_source_state_without_trace(qtbot):
    engine_no_trace = ...  # Engine.load with MockAdapter, no sim
    page = ScopePage(engine_no_trace)
    qtbot.addWidget(page)
    assert "ps_trace" in page.error_label.text()

def test_scope_discovers_demo_trace_and_shows_rate(qtbot):
    page = ScopePage(make_demo_engine(TARGET))
    qtbot.addWidget(page)
    assert page.channel_table.rowCount() == 10
    assert "Hz (firmware)" in page.rate_label_text()   # accessor
```

---

### Task 8: Scope page rework B - channels, protocol, render

**Files:**
- Modify: `ui/panels/scope_page.py`
- Test: `tests/ui/test_scope_page.py` (unskip + extend)

**Interfaces:**
- Consumes: `TraceReader.set_watch/refresh/lost`, `TraceStore`,
  Task 7 page states.
- Produces (test-support API): `add_symbol_channel(name)`,
  `add_register_channel(reg_key)` (resolves the SVD address, then
  the same slot path), `remove_channel(slot: int)`,
  `channel_slots() -> List[Optional[ChannelCfg]]`.

Behavior to implement and pin with tests:
- Adding: first free slot gets the address; the page calls
  `reader.set_watch([every occupied slot's addr])`; a full table ->
  inline "table full". A `TraceError` from firmware rejection renders
  inline verbatim.
- Guarded refusal BEFORE any write: address matches SVD readAction /
  flows guarded -> inline refusal (reuse the engine's guarded set the
  polling scope used).
- Repaint tick: `records = reader.refresh(); store.append(records)`;
  per occupied slot decode display-side (existing `decode_value`/
  `format_value`, 11 types) from `store.series(slot)` float arrays;
  curves get `setDownsampling(auto=True, method="peak")` and
  `setClipToView(True)` once at construction.
- Run/Stop stops ONLY the repaint/refresh of the plot; a hidden or
  stopped page still calls `reader.refresh()+store.append()` from
  the timer kept alive at a slow cadence (500 ms) so the ring never
  overflows because of UI state (assert: stop, simulate > ring_count
  records, run -> `reader.lost` did not grow).
- Unskip every Task 7-skipped test; adapt cursor/value tests to feed
  the store (`store.append([...])`) instead of `history.record`.

- [ ] Steps: failing tests first (slot assignment, full-table error,
  guarded refusal, stop-does-not-lose-data, decode-on-store), then
  implementation, full suite green, no skips left, commit
  `feat(ui): trace channels, watch protocol and downsampled render`.

---

### Task 9: `bench-read` CLI (throughput spike tool)

**Files:**
- Modify: `cli.py`
- Test: `tests/test_cli.py` (append)

**Interfaces:** `pathscope bench-read [--seconds 5] [--block-words
1024] [--addr 0x20000000]` - connects like `monitor`, loops
`read_block32(addr, block_words)`, prints
`blocks=N bytes=M throughput=K KB/s ceiling@48B=R Hz` where
`R = throughput / 48`. Parser test asserts defaults; a MockAdapter
run (monkeypatched like the existing monitor test) asserts the
printed ceiling math.

- [ ] Steps: failing parser+math tests, implement, green, commit
  `feat(cli): bench-read block throughput measurement`.

---

### Task 10: Hardware E2E suite (`tests/hw/`)

**Files:**
- Create: `tests/hw/__init__.py`, `tests/hw/test_trace_hw.py`
- Modify: pytest marker config only if `hardware` is not already
  registered (check `pyproject.toml`/`conftest.py` first - the
  existing deselected hardware test shows where).

Marked `@pytest.mark.hardware`, driven with a board attached (agent
runs them; CI deselects). Uses the REAL adapter + real firmware from
Task 4, pytest-qt driving the real `MainWindow`:

```python
@pytest.mark.hardware
def test_live_trace_end_to_end(qtbot):
    engine = ...  # Engine.load with PyOCDAdapter, real target dir
    win = MainWindow(engine, EngineBridge(engine)); win.show()
    win.tabs.setCurrentIndex(1)
    page = win.scope_page
    page.load_elf("firmware/blackpill_adc_dma/fw.elf")
    page.add_symbol_channel("adc_buf")
    page.add_register_channel("DMA2.S0NDTR")
    qtbot.waitUntil(lambda: page.store_sample_count(0) > 500, timeout=5000)
    # coherence by construction: both slots come from the same records
    assert page.channel_slots()[0] is not None
```

plus: table-edit-under-load (add/remove while streaming, generation
gap visible), unplug/replug recovery, Run/Stop no-data-loss, and a
`--shot`-equivalent `win.grab().save(...)` at the end of each test
for the agent's visual pass.

- [ ] Steps: write suite, verify it DESELECTS cleanly offscreen
  without hardware, commit
  `test: hardware e2e suite for the trace scope`.

---

### Task 11: Docs + validation sweep (controller + user/agent with board)

- Run Task 9's bench on real hardware; write the measured ceiling
  into README Probe notes; set the demo firmware period accordingly
  if 1 kHz is not sustainable.
- Run the Task 10 suite with the board; fix findings via the SDD
  loop; record results in README Hardware validation (M7 section).
- README feature/Quick-start rewrite: scope section describes the
  trace workflow (instrument firmware with ps_trace -> Load ELF ->
  channels), removes polling-scope wording; run /sepia:sepia over the
  changed README sections per the user's standing instruction.
- Spec section 11 backlog: add trigger capture, variable-width
  records, manual descriptor address, double-buffered tables,
  window selector (from the M7 spec's out-of-scope list).

---

## Self-review notes

- Spec coverage: sections 2 (T4), 3 (T1), 3.3 protocol (T3 sim+reader,
  T8 UI), 4 (T9+T11), 5 (T2/T3/T5/T8), 6 (T3 sim whitelist, T4
  firmware, T8 guarded refusal), 7 (T1 endian, T7 discovery), 8
  (T7/T8), 9 backlog (T11), 10 validation (T10/T11). No gaps.
- `FakeTraceFirmware` lives in `core/trace/sim.py` from Task 6 onward
  (moved there because `ui/demo.py` may not import from `tests/`);
  Task 3 builds it under `tests/trace_sim.py` first and Task 6
  performs the move - executors of Task 6 must update Task 3's test
  imports in the same commit.
- Type consistency: `TraceReader.set_watch(List[int])`,
  `TraceStore.series(slot) -> (np.ndarray, np.ndarray)` used
  identically in Tasks 3/5/8/10.
