# Data Path Explorer Core Engine (M0-M2) Implementation Plan

> **Precedence note:** specs under `docs/superpowers/specs/` are living
> and binding; plans under `docs/superpowers/plans/` (this file
> included) are point-in-time execution documents and are not updated
> after the fact - where a plan and a spec disagree, the spec wins.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the headless polling/rule engine, the pyOCD SWD adapter, the
STM32F411 target description, a CLI proving milestones M0-M2 on real
Blackpill hardware, and the test firmware.

**Architecture:** A `core` package with zero UI imports: `TargetAdapter`
abstracts the probe, a register model is parsed from CMSIS-SVD, topology and
flow YAML files describe the data paths, an ast-sandboxed evaluator runs
rule expressions over polled snapshots, and a single poller thread owns the
adapter (batched block reads, priority command queue, reconnect). A CLI
drives it all. UI (M3-M4) and packaging (M5) are separate follow-up plans
gated on M1's measured poll rate.

**Tech Stack:** Python 3.9 (system 3.9.6), pyocd, PyYAML, pytest.
Firmware: bare-metal C, arm-none-eabi-gcc, flashed with pyocd.

**Spec:** `docs/superpowers/specs/2026-09-12-data-path-explorer-design.md`

## Global Constraints

- Python 3.9 compatible syntax only (no `match`, no `X | Y` type unions; use
  `typing.Optional/List/Dict/Tuple/Set`).
- `core/` must never import Qt or anything from a future `ui/` package.
- Pure ASCII in all source files (no box-drawing or unicode arrows).
- C sources carry basic Doxygen comments (`@file`, `@brief`, `@param`,
  `@return`) on file header and every function.
- Registers whose SVD entry has `readAction` are NEVER polled unless listed
  in the flow file's `poll.force_poll`.
- Callbacks from the engine fire on the poller thread; consumers must not
  assume the main thread.
- Commit after every task with a Conventional Commits message.
- Run all commands from the repo root. Python is `.venv/bin/python`,
  pytest is `.venv/bin/pytest`.
- Hardware-dependent tests are marked `@pytest.mark.hw` and are excluded by
  default (see Task 1); plain `pytest` must pass with no hardware attached.

---

### Task 1: Project scaffold and dependencies

**Files:**
- Create: `pyproject.toml`
- Create: `pytest.ini`
- Create: `core/__init__.py`, `core/adapter/__init__.py`,
  `core/target/__init__.py`, `core/engine/__init__.py`
- Create: `tests/__init__.py`

**Interfaces:**
- Produces: importable `core` package; `pytest` configured so `hw`-marked
  tests are skipped by default and selected with `-m hw`.

- [ ] **Step 1: Write pyproject.toml**

```toml
[project]
name = "data-path-explorer"
version = "0.0.1"
description = "Visual data path debug tool - core engine"
requires-python = ">=3.9"
dependencies = [
    "pyocd>=0.36",
    "PyYAML>=6.0",
]

[project.optional-dependencies]
dev = ["pytest>=7.0"]
```

- [ ] **Step 2: Write pytest.ini**

```ini
[pytest]
testpaths = tests
addopts = -m "not hw"
markers =
    hw: requires a connected Blackpill via ST-Link (deselected by default)
```

- [ ] **Step 3: Create the empty packages**

Create `core/__init__.py`, `core/adapter/__init__.py`,
`core/target/__init__.py`, `core/engine/__init__.py`, `tests/__init__.py`
(all empty files).

- [ ] **Step 4: Install and verify**

Run: `.venv/bin/pip install -e ".[dev]"`
Then: `.venv/bin/python -c "import core, pyocd, yaml; print('ok')"`
Expected: `ok`
Then: `.venv/bin/pytest`
Expected: `no tests ran` (exit code 5 is fine at this point)

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml pytest.ini core tests
git commit -m "chore: scaffold core package and test config"
```

---

### Task 2: Adapter interface and MockAdapter

**Files:**
- Create: `core/adapter/base.py`
- Create: `core/adapter/mock.py`
- Test: `tests/test_adapter_mock.py`

**Interfaces:**
- Produces:
  - `core.adapter.base.AdapterError(Exception)`,
    `TargetLostError(AdapterError)`
  - `core.adapter.base.TargetInfo` dataclass: `name: str`, `idcode: int`
  - `core.adapter.base.TargetAdapter` ABC with abstract methods:
    `connect() -> TargetInfo`, `disconnect() -> None`,
    `read_block32(addr: int, count: int) -> List[int]`,
    `write32(addr: int, value: int) -> None`,
    `halt() -> None`, `resume() -> None`, `is_running() -> bool`
  - `core.adapter.mock.MockAdapter(TargetAdapter)`: constructed with
    `mem: Dict[int, int]` (word-addressed), extra methods
    `set_word(addr, value)`, `fail_next(n)` (next n reads raise
    `TargetLostError`), attribute `read_log: List[Tuple[int, int]]`
    recording each `(addr, count)` block read.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_adapter_mock.py
import pytest
from core.adapter.base import TargetAdapter, TargetInfo, TargetLostError
from core.adapter.mock import MockAdapter


def test_mock_is_an_adapter():
    assert issubclass(MockAdapter, TargetAdapter)


def test_block_read_returns_words_and_logs():
    a = MockAdapter({0x40000000: 0x11, 0x40000004: 0x22, 0x40000008: 0x33})
    info = a.connect()
    assert isinstance(info, TargetInfo)
    assert a.read_block32(0x40000000, 3) == [0x11, 0x22, 0x33]
    assert a.read_log == [(0x40000000, 3)]


def test_unbacked_addresses_read_zero():
    a = MockAdapter({})
    a.connect()
    assert a.read_block32(0x20000000, 2) == [0, 0]


def test_set_word_and_write32():
    a = MockAdapter({})
    a.connect()
    a.set_word(0x40000000, 5)
    a.write32(0x40000004, 7)
    assert a.read_block32(0x40000000, 2) == [5, 7]


def test_fail_next_raises_target_lost_then_recovers():
    a = MockAdapter({0x0: 1})
    a.connect()
    a.fail_next(2)
    with pytest.raises(TargetLostError):
        a.read_block32(0x0, 1)
    with pytest.raises(TargetLostError):
        a.read_block32(0x0, 1)
    assert a.read_block32(0x0, 1) == [1]


def test_halt_resume_state():
    a = MockAdapter({})
    a.connect()
    assert a.is_running() is True
    a.halt()
    assert a.is_running() is False
    a.resume()
    assert a.is_running() is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_adapter_mock.py -v`
Expected: FAIL (ModuleNotFoundError: core.adapter.base)

- [ ] **Step 3: Implement base.py and mock.py**

```python
# core/adapter/base.py
"""Probe/transport abstraction. Everything above this layer sees only
addresses and 32-bit words."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List


class AdapterError(Exception):
    """Any adapter-level failure."""


class TargetLostError(AdapterError):
    """Target unreachable (unplugged, reset, powered off)."""


@dataclass
class TargetInfo:
    name: str
    idcode: int


class TargetAdapter(ABC):
    @abstractmethod
    def connect(self) -> TargetInfo: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def read_block32(self, addr: int, count: int) -> List[int]: ...

    @abstractmethod
    def write32(self, addr: int, value: int) -> None: ...

    @abstractmethod
    def halt(self) -> None: ...

    @abstractmethod
    def resume(self) -> None: ...

    @abstractmethod
    def is_running(self) -> bool: ...
```

```python
# core/adapter/mock.py
"""In-memory adapter for tests: word-addressed dict, scripted failures."""
from typing import Dict, List, Tuple

from .base import TargetAdapter, TargetInfo, TargetLostError


class MockAdapter(TargetAdapter):
    def __init__(self, mem: Dict[int, int]):
        self.mem = dict(mem)
        self.read_log: List[Tuple[int, int]] = []
        self._fail = 0
        self._running = True

    def connect(self) -> TargetInfo:
        return TargetInfo(name="mock", idcode=0x0)

    def disconnect(self) -> None:
        pass

    def read_block32(self, addr: int, count: int) -> List[int]:
        if self._fail > 0:
            self._fail -= 1
            raise TargetLostError("scripted failure")
        self.read_log.append((addr, count))
        return [self.mem.get(addr + 4 * i, 0) for i in range(count)]

    def write32(self, addr: int, value: int) -> None:
        self.mem[addr] = value

    def halt(self) -> None:
        self._running = False

    def resume(self) -> None:
        self._running = True

    def is_running(self) -> bool:
        return self._running

    def set_word(self, addr: int, value: int) -> None:
        self.mem[addr] = value

    def fail_next(self, n: int) -> None:
        self._fail = n
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_adapter_mock.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add core/adapter tests/test_adapter_mock.py
git commit -m "feat(core): adapter interface and mock adapter"
```

---

### Task 3: SVD register model

**Files:**
- Create: `core/target/registers.py`
- Test: `tests/test_registers.py`

**Interfaces:**
- Produces:
  - `core.target.registers.SvdError(Exception)`
  - `Field` frozen dataclass: `name: str`, `lsb: int`, `msb: int`
  - `Register` dataclass: `name: str`, `address: int`,
    `read_action: Optional[str]`, `fields: Dict[str, Field]`
  - `Peripheral` dataclass: `name: str`, `base: int`,
    `registers: Dict[str, Register]`
  - `RegRef` frozen dataclass: `ref: str` (as written),
    `reg_key: str` ("PERIPH.REG"), `address: int`, `lsb: int`, `msb: int`,
    `read_action: Optional[str]`
  - `RegisterModel`: attribute `peripherals: Dict[str, Peripheral]`;
    `RegisterModel.from_svd(path: str) -> RegisterModel`;
    `resolve(ref: str) -> RegRef` accepting "PERIPH.REG" (lsb=0, msb=31)
    or "PERIPH.REG.FIELD"; raises `SvdError` on unknown names.
- Handles SVD: `derivedFrom` on peripherals (copies base peripheral's
  registers), `<dim>` register arrays with `%s` expansion, `readAction`
  on registers.

- [ ] **Step 1: Write the failing test with an inline SVD fixture**

```python
# tests/test_registers.py
import pytest
from core.target.registers import RegisterModel, SvdError

FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<device><name>TESTCHIP</name>
  <peripherals>
    <peripheral>
      <name>ADC1</name>
      <baseAddress>0x40012000</baseAddress>
      <registers>
        <register>
          <name>SR</name><addressOffset>0x0</addressOffset>
          <fields>
            <field><name>OVR</name><bitOffset>5</bitOffset><bitWidth>1</bitWidth></field>
            <field><name>EOC</name><bitOffset>1</bitOffset><bitWidth>1</bitWidth></field>
          </fields>
        </register>
        <register>
          <name>DR</name><addressOffset>0x4C</addressOffset>
          <readAction>clear</readAction>
        </register>
      </registers>
    </peripheral>
    <peripheral derivedFrom="ADC1">
      <name>ADC2</name>
      <baseAddress>0x40012100</baseAddress>
    </peripheral>
    <peripheral>
      <name>DMA2</name>
      <baseAddress>0x40026400</baseAddress>
      <registers>
        <register>
          <dim>2</dim><dimIncrement>0x18</dimIncrement>
          <name>S%sCR</name><addressOffset>0x10</addressOffset>
          <fields>
            <field><name>EN</name><bitOffset>0</bitOffset><bitWidth>1</bitWidth></field>
            <field><name>CHSEL</name><bitOffset>25</bitOffset><bitWidth>3</bitWidth></field>
          </fields>
        </register>
      </registers>
    </peripheral>
  </peripherals>
</device>
"""


@pytest.fixture
def model(tmp_path):
    p = tmp_path / "test.svd"
    p.write_text(FIXTURE)
    return RegisterModel.from_svd(str(p))


def test_register_address(model):
    r = model.resolve("ADC1.SR")
    assert r.address == 0x40012000
    assert (r.lsb, r.msb) == (0, 31)
    assert r.reg_key == "ADC1.SR"
    assert r.read_action is None


def test_field_resolution(model):
    f = model.resolve("ADC1.SR.OVR")
    assert (f.lsb, f.msb) == (5, 5)
    assert f.reg_key == "ADC1.SR"           # field refs point at parent reg
    assert f.address == 0x40012000


def test_read_action_surfaces(model):
    assert model.resolve("ADC1.DR").read_action == "clear"


def test_derived_from_copies_registers(model):
    r = model.resolve("ADC2.SR.OVR")
    assert r.address == 0x40012100          # base of ADC2, not ADC1


def test_dim_expansion(model):
    s0 = model.resolve("DMA2.S0CR")
    s1 = model.resolve("DMA2.S1CR.CHSEL")
    assert s0.address == 0x40026410
    assert s1.address == 0x40026410 + 0x18
    assert (s1.lsb, s1.msb) == (25, 27)


def test_unknown_names_raise(model):
    with pytest.raises(SvdError):
        model.resolve("NOPE.REG")
    with pytest.raises(SvdError):
        model.resolve("ADC1.NOPE")
    with pytest.raises(SvdError):
        model.resolve("ADC1.SR.NOPE")
    with pytest.raises(SvdError):
        model.resolve("justoneword")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_registers.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement registers.py**

```python
# core/target/registers.py
"""CMSIS-SVD subset parser: peripherals, registers, fields, readAction,
derivedFrom, dim arrays. Deliberately minimal - extend only on need."""
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field as dfield
from typing import Dict, Optional


class SvdError(Exception):
    pass


@dataclass(frozen=True)
class Field:
    name: str
    lsb: int
    msb: int


@dataclass
class Register:
    name: str
    address: int
    read_action: Optional[str] = None
    fields: Dict[str, Field] = dfield(default_factory=dict)


@dataclass
class Peripheral:
    name: str
    base: int
    registers: Dict[str, Register] = dfield(default_factory=dict)


@dataclass(frozen=True)
class RegRef:
    ref: str
    reg_key: str
    address: int
    lsb: int
    msb: int
    read_action: Optional[str]


def _int(text: str) -> int:
    return int(text, 0)


def _parse_register(rnode, base: int, out: Dict[str, Register]) -> None:
    name_t = rnode.findtext("name")
    offset = _int(rnode.findtext("addressOffset"))
    read_action = rnode.findtext("readAction")
    fields: Dict[str, Field] = {}
    for fnode in rnode.iter("field"):
        fname = fnode.findtext("name")
        lsb = _int(fnode.findtext("bitOffset"))
        width = _int(fnode.findtext("bitWidth"))
        fields[fname] = Field(fname, lsb, lsb + width - 1)
    dim = rnode.findtext("dim")
    if dim is None:
        out[name_t] = Register(name_t, base + offset, read_action, fields)
        return
    inc = _int(rnode.findtext("dimIncrement"))
    for i in range(_int(dim)):
        n = name_t.replace("%s", str(i))
        out[n] = Register(n, base + offset + i * inc, read_action, fields)


class RegisterModel:
    def __init__(self, peripherals: Dict[str, Peripheral]):
        self.peripherals = peripherals

    @classmethod
    def from_svd(cls, path: str) -> "RegisterModel":
        root = ET.parse(path).getroot()
        peripherals: Dict[str, Peripheral] = {}
        deferred = []
        for pnode in root.iter("peripheral"):
            name = pnode.findtext("name")
            base = _int(pnode.findtext("baseAddress"))
            parent = pnode.get("derivedFrom")
            if parent is not None:
                deferred.append((name, base, parent))
                continue
            regs: Dict[str, Register] = {}
            regs_node = pnode.find("registers")
            if regs_node is not None:
                for rnode in regs_node.findall("register"):
                    _parse_register(rnode, base, regs)
            peripherals[name] = Peripheral(name, base, regs)
        for name, base, parent in deferred:
            if parent not in peripherals:
                raise SvdError("derivedFrom unknown peripheral: %s" % parent)
            src = peripherals[parent]
            regs = {
                rn: Register(rn, base + (r.address - src.base),
                             r.read_action, r.fields)
                for rn, r in src.registers.items()
            }
            peripherals[name] = Peripheral(name, base, regs)
        return cls(peripherals)

    def resolve(self, ref: str) -> RegRef:
        parts = ref.split(".")
        if len(parts) not in (2, 3):
            raise SvdError("bad register reference: %r" % ref)
        pname, rname = parts[0], parts[1]
        periph = self.peripherals.get(pname)
        if periph is None:
            raise SvdError("unknown peripheral: %s" % pname)
        reg = periph.registers.get(rname)
        if reg is None:
            raise SvdError("unknown register: %s.%s" % (pname, rname))
        reg_key = "%s.%s" % (pname, rname)
        if len(parts) == 2:
            return RegRef(ref, reg_key, reg.address, 0, 31, reg.read_action)
        f = reg.fields.get(parts[2])
        if f is None:
            raise SvdError("unknown field: %s" % ref)
        return RegRef(ref, reg_key, reg.address, f.lsb, f.msb,
                      reg.read_action)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_registers.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add core/target/registers.py tests/test_registers.py
git commit -m "feat(core): SVD register model with derivedFrom and dim support"
```

---

### Task 4: Fetch the real STM32F411 SVD

**Files:**
- Create: `targets/f411/STM32F411.svd` (downloaded, committed)
- Test: `tests/test_f411_svd.py`

**Interfaces:**
- Produces: `targets/f411/STM32F411.svd` parseable by `RegisterModel`;
  later tasks rely on register keys `DMA2.S0CR`, `DMA2.S0NDTR`,
  `DMA2.LISR`, `ADC1.SR`, `ADC1.CR2`, `TIM1.CNT` existing in it.

- [ ] **Step 1: Download the SVD**

```bash
mkdir -p targets/f411
curl -L -o targets/f411/STM32F411.svd \
  https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/main/data/STMicro/STM32F411.svd
```

If the URL 404s (repo layout moved): `pip download cmsis-svd-data` is the
fallback -- the file lives at `data/STMicro/STM32F411.svd` inside that
wheel; copy it out. Verify size is roughly 500 KB - 1 MB.

- [ ] **Step 2: Write the smoke test**

```python
# tests/test_f411_svd.py
import pytest
from core.target.registers import RegisterModel

SVD = "targets/f411/STM32F411.svd"


@pytest.fixture(scope="module")
def model():
    return RegisterModel.from_svd(SVD)


def test_keys_needed_by_flows_exist(model):
    for ref in ["DMA2.S0CR.EN", "DMA2.S0CR.CHSEL", "DMA2.S0NDTR",
                "DMA2.LISR.TEIF0", "ADC1.SR.OVR", "ADC1.CR2.ADON",
                "TIM1.CNT"]:
        model.resolve(ref)


def test_known_addresses(model):
    assert model.resolve("DMA2.S0CR").address == 0x40026410
    assert model.resolve("ADC1.SR").address == 0x40012000


def test_adc_dr_is_flagged(model):
    # ADC1.DR must exist; if ST marks readAction it must survive parsing.
    r = model.resolve("ADC1.DR")
    assert r.address == 0x4001204C
```

- [ ] **Step 3: Run test**

Run: `.venv/bin/pytest tests/test_f411_svd.py -v`
Expected: 3 passed. If `resolve` fails on a name, inspect the SVD for the
actual spelling (e.g. some ST SVDs name stream registers `S0CR` vs
`S0CR`) and fix the TEST refs to the real names -- then propagate the real
names into Task 12/15 files. Do not rename things in the SVD.

- [ ] **Step 4: Commit**

```bash
git add targets/f411/STM32F411.svd tests/test_f411_svd.py
git commit -m "feat(targets): STM32F411 SVD and smoke test"
```

---

### Task 5: Topology loader

**Files:**
- Create: `core/target/topology.py`
- Test: `tests/test_topology.py`

**Interfaces:**
- Consumes: `RegisterModel` (Task 3) for validating `svd:` names.
- Produces:
  - `core.target.topology.TopologyError(Exception)`
  - `Block` dataclass: `id: str`, `kind: str`, `title: str`,
    `svd: Optional[str]`, `select: Optional[str]`,
    `base: Optional[int]`, `size: Optional[int]`,
    `ports: Dict[str, Tuple[str, float]]`
  - `Edge` dataclass: `id: str` (auto "src->dst#N"), `src: str`,
    `dst: str`, `via: Optional[str]`, `label: Optional[str]`,
    `when_select: Optional[int]`, `src_port: Optional[str]`,
    `dst_port: Optional[str]`
  - `Topology`: `blocks: Dict[str, Block]`, `edges: List[Edge]`
  - `load_topology(path: str, model: RegisterModel) -> Topology`
- Valid kinds: `peripheral dma memory cpu interconnect mux pin`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_topology.py
import pytest
from core.target.registers import RegisterModel
from core.target.topology import load_topology, TopologyError

# reuse the SVD fixture from test_registers
from tests.test_registers import FIXTURE

GOOD = """
blocks:
  - id: adc1
    kind: peripheral
    svd: ADC1
  - id: mux0
    kind: mux
    select: "DMA2.S0CR.CHSEL"
  - id: dma2
    kind: dma
    svd: DMA2
    ports: {periph: [l, 0.5], mem: [r, 0.5]}
  - id: sram1
    kind: memory
    base: 0x20000000
    size: 0x20000
edges:
  - {from: adc1, to: mux0, when_select: 0}
  - {from: mux0, to: dma2, to_port: periph}
  - {from: dma2, to: sram1, label: M0AR}
"""


@pytest.fixture
def model(tmp_path):
    p = tmp_path / "t.svd"
    p.write_text(FIXTURE)
    return RegisterModel.from_svd(str(p))


def _load(tmp_path, model, text):
    p = tmp_path / "t.topology.yaml"
    p.write_text(text)
    return load_topology(str(p), model)


def test_loads_blocks_and_edges(tmp_path, model):
    t = _load(tmp_path, model, GOOD)
    assert set(t.blocks) == {"adc1", "mux0", "dma2", "sram1"}
    assert t.blocks["sram1"].base == 0x20000000
    assert t.blocks["dma2"].ports["mem"] == ("r", 0.5)
    assert len(t.edges) == 3
    assert t.edges[0].when_select == 0
    assert t.edges[1].dst_port == "periph"
    assert t.edges[0].id == "adc1->mux0#0"


def test_unknown_edge_endpoint_rejected(tmp_path, model):
    bad = GOOD + "  - {from: adc1, to: ghost}\n"
    with pytest.raises(TopologyError):
        _load(tmp_path, model, bad)


def test_unknown_svd_name_rejected(tmp_path, model):
    with pytest.raises(TopologyError):
        _load(tmp_path, model, GOOD.replace("svd: ADC1", "svd: GHOST"))


def test_bad_kind_rejected(tmp_path, model):
    with pytest.raises(TopologyError):
        _load(tmp_path, model, GOOD.replace("kind: dma", "kind: banana"))


def test_duplicate_id_rejected(tmp_path, model):
    with pytest.raises(TopologyError):
        _load(tmp_path, model, GOOD.replace("id: sram1", "id: adc1"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_topology.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement topology.py**

```python
# core/target/topology.py
"""Loads <target>.topology.yaml: the blocks and directed edges of the
data-path diagram. Pure structure - no live semantics here."""
from dataclasses import dataclass, field as dfield
from typing import Dict, List, Optional, Tuple

import yaml

from .registers import RegisterModel, SvdError

KINDS = {"peripheral", "dma", "memory", "cpu", "interconnect", "mux", "pin"}


class TopologyError(Exception):
    pass


@dataclass
class Block:
    id: str
    kind: str
    title: str
    svd: Optional[str] = None
    select: Optional[str] = None
    base: Optional[int] = None
    size: Optional[int] = None
    ports: Dict[str, Tuple[str, float]] = dfield(default_factory=dict)


@dataclass
class Edge:
    id: str
    src: str
    dst: str
    via: Optional[str] = None
    label: Optional[str] = None
    when_select: Optional[int] = None
    src_port: Optional[str] = None
    dst_port: Optional[str] = None


@dataclass
class Topology:
    blocks: Dict[str, Block]
    edges: List[Edge]


def load_topology(path: str, model: RegisterModel) -> Topology:
    with open(path) as f:
        doc = yaml.safe_load(f)
    blocks: Dict[str, Block] = {}
    for raw in doc.get("blocks", []):
        bid = raw["id"]
        if bid in blocks:
            raise TopologyError("duplicate block id: %s" % bid)
        kind = raw["kind"]
        if kind not in KINDS:
            raise TopologyError("block %s: unknown kind %r" % (bid, kind))
        svd = raw.get("svd")
        if svd is not None and svd not in model.peripherals:
            raise TopologyError("block %s: unknown svd peripheral %r"
                                % (bid, svd))
        select = raw.get("select")
        if select is not None:
            try:
                model.resolve(select)
            except SvdError as e:
                raise TopologyError("block %s: bad select: %s" % (bid, e))
        ports = {name: (side_frac[0], float(side_frac[1]))
                 for name, side_frac in (raw.get("ports") or {}).items()}
        blocks[bid] = Block(
            id=bid, kind=kind, title=raw.get("title", bid.upper()),
            svd=svd, select=select, base=raw.get("base"),
            size=raw.get("size"), ports=ports)
    edges: List[Edge] = []
    for i, raw in enumerate(doc.get("edges", [])):
        src, dst = raw["from"], raw["to"]
        for end in (src, dst):
            if end not in blocks:
                raise TopologyError("edge %d: unknown block %r" % (i, end))
        via = raw.get("via")
        if via is not None and via not in blocks:
            raise TopologyError("edge %d: unknown via %r" % (i, via))
        edges.append(Edge(
            id="%s->%s#%d" % (src, dst, i), src=src, dst=dst, via=via,
            label=raw.get("label"), when_select=raw.get("when_select"),
            src_port=raw.get("from_port"), dst_port=raw.get("to_port")))
    return Topology(blocks=blocks, edges=edges)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_topology.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add core/target/topology.py tests/test_topology.py
git commit -m "feat(core): topology yaml loader with validation"
```

---

### Task 6: Snapshot model and history buffer

**Files:**
- Create: `core/engine/snapshot.py`
- Create: `core/engine/history.py`
- Test: `tests/test_history.py`

**Interfaces:**
- Produces:
  - `core.engine.snapshot.Sample` frozen dataclass: `value: int`, `t: float`
  - `core.engine.snapshot.Snapshot` dataclass:
    `values: Dict[str, Sample]` (keyed by reg_key), `t: float`,
    `rate_hz: float`; method `value(reg_key: str) -> Optional[int]`
  - `core.engine.history.History(window_s: float = 10.0)`:
    `record(reg_key: str, t: float, value: int) -> None`,
    `last_change_age(reg_key: str, now: float) -> Optional[float]`
    (None until two samples exist; else seconds since the value last
    changed, 0.0 if it changed on the newest sample),
    `changed_on_last(reg_key: str) -> bool` (True if the two most recent
    samples differ), `series(reg_key: str) -> List[Tuple[float, int]]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_history.py
from core.engine.history import History
from core.engine.snapshot import Sample, Snapshot


def test_snapshot_value_lookup():
    s = Snapshot(values={"A.B": Sample(7, 1.0)}, t=1.0, rate_hz=10.0)
    assert s.value("A.B") == 7
    assert s.value("A.MISSING") is None


def test_last_change_age_needs_two_samples():
    h = History()
    h.record("A.B", 0.0, 5)
    assert h.last_change_age("A.B", 1.0) is None
    h.record("A.B", 1.0, 5)
    assert h.last_change_age("A.B", 2.0) == 2.0   # never seen a change yet:
    # age counted from the FIRST sample when no change observed


def test_change_resets_age():
    h = History()
    h.record("A.B", 0.0, 5)
    h.record("A.B", 1.0, 5)
    h.record("A.B", 2.0, 9)
    assert h.last_change_age("A.B", 2.5) == 0.5
    assert h.changed_on_last("A.B") is True
    h.record("A.B", 3.0, 9)
    assert h.changed_on_last("A.B") is False


def test_window_trim():
    h = History(window_s=1.0)
    for i in range(50):
        h.record("A.B", i * 0.1, i)
    ts = [t for t, _ in h.series("A.B")]
    assert min(ts) >= 4.9 - 1.0 - 1e-9


def test_unknown_key():
    h = History()
    assert h.last_change_age("X.Y", 1.0) is None
    assert h.changed_on_last("X.Y") is False
    assert h.series("X.Y") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_history.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement snapshot.py and history.py**

```python
# core/engine/snapshot.py
"""Data model passed from engine to consumers. Values carry their own
timestamps because a sweep is not atomic (spec 6.4)."""
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class Sample:
    value: int
    t: float


@dataclass
class Snapshot:
    values: Dict[str, Sample]
    t: float
    rate_hz: float

    def value(self, reg_key: str) -> Optional[int]:
        s = self.values.get(reg_key)
        return None if s is None else s.value
```

```python
# core/engine/history.py
"""Per-register ring history: powers stalled()/changed() and sparklines."""
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple


class History:
    def __init__(self, window_s: float = 10.0):
        self.window_s = window_s
        self._buf: Dict[str, Deque[Tuple[float, int]]] = {}
        self._last_change_t: Dict[str, float] = {}
        self._first_t: Dict[str, float] = {}

    def record(self, reg_key: str, t: float, value: int) -> None:
        buf = self._buf.setdefault(reg_key, deque())
        if buf and buf[-1][1] != value:
            self._last_change_t[reg_key] = t
        if reg_key not in self._first_t:
            self._first_t[reg_key] = t
        buf.append((t, value))
        while buf and buf[0][0] < t - self.window_s:
            buf.popleft()

    def last_change_age(self, reg_key: str, now: float) -> Optional[float]:
        buf = self._buf.get(reg_key)
        if not buf or len(buf) < 2:
            return None
        anchor = self._last_change_t.get(reg_key, self._first_t[reg_key])
        return now - anchor

    def changed_on_last(self, reg_key: str) -> bool:
        buf = self._buf.get(reg_key)
        if not buf or len(buf) < 2:
            return False
        return buf[-1][1] != buf[-2][1]

    def series(self, reg_key: str) -> List[Tuple[float, int]]:
        return list(self._buf.get(reg_key, []))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_history.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add core/engine/snapshot.py core/engine/history.py tests/test_history.py
git commit -m "feat(core): snapshot model and register history buffer"
```

---

### Task 7: Sandboxed expression evaluator

**Files:**
- Create: `core/engine/evaluator.py`
- Test: `tests/test_evaluator.py`

**Interfaces:**
- Consumes: `RegisterModel.resolve` (Task 3), `History` (Task 6),
  `Snapshot` (Task 6).
- Produces:
  - `core.engine.evaluator.ExprError(Exception)`
  - `Evaluator(model: RegisterModel, history: History)` with
    `compile(expr: str) -> CompiledExpr` (raises `ExprError` on syntax
    errors, unknown refs, or disallowed constructs).
  - `CompiledExpr`: attribute `refs: Set[str]` (reg_keys the expression
    reads), method `eval(snap: Snapshot)` returning the value, or `None`
    if any needed register is missing from the snapshot.
- Expression language: Python subset -- int/bool constants, `and or not`,
  comparisons, `+ - * & | ^ << >>`, parenthesization, dotted register
  refs (`PERIPH.REG[.FIELD]`, field refs auto-extract bits), and calls
  `stalled(REF, ms)` / `changed(REF)`. Nothing else parses.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_evaluator.py
import pytest
from core.engine.evaluator import Evaluator, ExprError
from core.engine.history import History
from core.engine.snapshot import Sample, Snapshot
from core.target.registers import RegisterModel
from tests.test_registers import FIXTURE


@pytest.fixture
def ev(tmp_path):
    p = tmp_path / "t.svd"
    p.write_text(FIXTURE)
    return Evaluator(RegisterModel.from_svd(str(p)), History())


def snap(t=1.0, **kv):
    return Snapshot(values={k.replace("_", "."): Sample(v, t)
                            for k, v in kv.items()}, t=t, rate_hz=10.0)


def test_field_extraction_and_compare(ev):
    c = ev.compile("ADC1.SR.OVR == 1")
    assert c.refs == {"ADC1.SR"}
    assert c.eval(snap(ADC1_SR=1 << 5)) is True
    assert c.eval(snap(ADC1_SR=0)) is False


def test_boolean_and_arith(ev):
    c = ev.compile("DMA2.S0CR.EN == 1 and DMA2.S0CR.CHSEL == 6")
    v = (6 << 25) | 1
    assert c.eval(snap(DMA2_S0CR=v)) is True
    assert c.eval(snap(DMA2_S0CR=1)) is False


def test_missing_value_yields_none(ev):
    c = ev.compile("ADC1.SR.OVR == 1")
    assert c.eval(snap()) is None


def test_stalled_uses_history(ev):
    c = ev.compile("stalled(DMA2.S0NDTR, 500)")
    h = ev.history
    h.record("DMA2.S0NDTR", 0.0, 100)
    h.record("DMA2.S0NDTR", 0.1, 99)     # changing
    assert c.eval(snap(t=0.2, DMA2_S0NDTR=99)) is False
    h.record("DMA2.S0NDTR", 0.2, 99)
    h.record("DMA2.S0NDTR", 0.9, 99)     # frozen for 800 ms
    assert c.eval(snap(t=0.9, DMA2_S0NDTR=99)) is True


def test_changed_builtin(ev):
    c = ev.compile("changed(ADC1.SR)")
    ev.history.record("ADC1.SR", 0.0, 0)
    ev.history.record("ADC1.SR", 0.1, 2)
    assert c.eval(snap(t=0.1, ADC1_SR=2)) is True


@pytest.mark.parametrize("bad", [
    "__import__('os')",
    "[x for x in range(9)]",
    "ADC1.SR.OVR.__class__",
    "open('/etc/passwd')",
    "lambda: 1",
    "GHOST.REG == 1",
    "stalled(123, 500)",
    "ADC1.SR ==",
])
def test_rejects_disallowed(ev, bad):
    with pytest.raises(ExprError):
        ev.compile(bad)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_evaluator.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement evaluator.py**

```python
# core/engine/evaluator.py
"""ast-based sandboxed evaluation of rule expressions. The AST whitelist
IS the language definition - anything not matched below is rejected at
compile time, so no runtime surprise is possible."""
import ast
import operator
from typing import Any, Callable, Dict, Optional, Set

from ..target.registers import RegisterModel, RegRef, SvdError
from .history import History
from .snapshot import Snapshot


class ExprError(Exception):
    pass


_BIN = {ast.Add: operator.add, ast.Sub: operator.sub,
        ast.Mult: operator.mul, ast.BitAnd: operator.and_,
        ast.BitOr: operator.or_, ast.BitXor: operator.xor,
        ast.LShift: operator.lshift, ast.RShift: operator.rshift}
_CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne,
        ast.Lt: operator.lt, ast.LtE: operator.le,
        ast.Gt: operator.gt, ast.GtE: operator.ge}
_BUILTINS = ("stalled", "changed")


class _Missing(Exception):
    """A referenced register has no value in this snapshot."""


def _attr_chain(node: ast.AST) -> Optional[str]:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


class CompiledExpr:
    def __init__(self, fn: Callable[[Snapshot], Any], refs: Set[str]):
        self._fn = fn
        self.refs = refs

    def eval(self, snap: Snapshot):
        try:
            return self._fn(snap)
        except _Missing:
            return None


class Evaluator:
    def __init__(self, model: RegisterModel, history: History):
        self.model = model
        self.history = history

    def compile(self, expr: str) -> CompiledExpr:
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError as e:
            raise ExprError("syntax error in %r: %s" % (expr, e))
        refs: Set[str] = set()
        fn = self._build(tree.body, refs)
        return CompiledExpr(fn, refs)

    def _reg(self, node: ast.AST, refs: Set[str]) -> RegRef:
        chain = _attr_chain(node)
        if chain is None:
            raise ExprError("expected a register reference")
        try:
            rr = self.model.resolve(chain)
        except SvdError as e:
            raise ExprError(str(e))
        refs.add(rr.reg_key)
        return rr

    def _build(self, node: ast.AST, refs: Set[str]):
        if isinstance(node, ast.Constant) and isinstance(node.value,
                                                         (int, bool)):
            v = node.value
            return lambda s: v
        if isinstance(node, ast.Attribute):
            rr = self._reg(node, refs)
            mask = (1 << (rr.msb - rr.lsb + 1)) - 1
            lsb = rr.lsb
            key = rr.reg_key

            def read(s: Snapshot):
                v = s.value(key)
                if v is None:
                    raise _Missing()
                return (v >> lsb) & mask
            return read
        if isinstance(node, ast.BoolOp):
            subs = [self._build(x, refs) for x in node.values]
            if isinstance(node.op, ast.And):
                return lambda s: all(f(s) for f in subs)
            return lambda s: any(f(s) for f in subs)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            sub = self._build(node.operand, refs)
            return lambda s: not sub(s)
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1 or type(node.ops[0]) not in _CMP:
                raise ExprError("unsupported comparison")
            op = _CMP[type(node.ops[0])]
            left = self._build(node.left, refs)
            right = self._build(node.comparators[0], refs)
            return lambda s: op(left(s), right(s))
        if isinstance(node, ast.BinOp):
            if type(node.op) not in _BIN:
                raise ExprError("unsupported operator")
            op = _BIN[type(node.op)]
            left = self._build(node.left, refs)
            right = self._build(node.right, refs)
            return lambda s: op(left(s), right(s))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) \
                    or node.func.id not in _BUILTINS or node.keywords:
                raise ExprError("unknown function call")
            name = node.func.id
            rr = self._reg(node.args[0] if node.args else ast.Constant(0),
                           refs)
            hist = self.history
            if name == "stalled":
                if len(node.args) != 2 \
                        or not isinstance(node.args[1], ast.Constant):
                    raise ExprError("stalled(REF, ms) expects a constant ms")
                ms = float(node.args[1].value)

                def stalled(s: Snapshot):
                    age = hist.last_change_age(rr.reg_key, s.t)
                    return age is not None and age * 1000.0 > ms
                return stalled
            if len(node.args) != 1:
                raise ExprError("changed(REF) takes one argument")
            return lambda s: hist.changed_on_last(rr.reg_key)
        raise ExprError("disallowed construct: %s"
                        % type(node).__name__)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_evaluator.py -v`
Expected: all passed (13 items)

- [ ] **Step 5: Commit**

```bash
git add core/engine/evaluator.py tests/test_evaluator.py
git commit -m "feat(core): ast-sandboxed rule expression evaluator"
```

---

### Task 8: Flows loader

**Files:**
- Create: `core/target/flows.py`
- Test: `tests/test_flows.py`

**Interfaces:**
- Consumes: `Evaluator.compile` (Task 7) for validation and ref
  extraction; `Topology` (Task 5) for path/target block validation.
- Produces:
  - `core.target.flows.FlowError(Exception)`
  - `Rule` dataclass: `expr: str`, `msg: str`, `target: str`,
    `compiled: CompiledExpr`
  - `Activity` dataclass: `name: str`, `path: List[str]`,
    `active_when: str`, `active_compiled: CompiledExpr`,
    `progress: Optional[str]` (a reg_key after normalization),
    `rules: List[Rule]`
  - `FlowSpec` dataclass: `activities: List[Activity]`,
    `force_poll: List[str]` (reg_keys)
  - `load_flows(path, evaluator, topology) -> FlowSpec`
  - `needed_registers(spec: FlowSpec) -> Set[str]` -- union of reg_keys
    from every `active_when`, `progress`, and rule expression.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_flows.py
import pytest
from core.engine.evaluator import Evaluator
from core.engine.history import History
from core.target.flows import load_flows, needed_registers, FlowError
from core.target.registers import RegisterModel
from core.target.topology import load_topology
from tests.test_registers import FIXTURE
from tests.test_topology import GOOD as TOPO

FLOWS = """
activities:
  - name: adc_to_sram
    path: [adc1, mux0, dma2, sram1]
    active_when: "DMA2.S0CR.EN == 1 and DMA2.S0CR.CHSEL == 0"
    progress: "DMA2.S0CR"
    anomalies:
      - {rule: "ADC1.SR.OVR == 1", msg: "ADC overrun", target: adc1}
poll:
  force_poll: []
"""


@pytest.fixture
def env(tmp_path):
    (tmp_path / "t.svd").write_text(FIXTURE)
    (tmp_path / "t.topology.yaml").write_text(TOPO)
    model = RegisterModel.from_svd(str(tmp_path / "t.svd"))
    topo = load_topology(str(tmp_path / "t.topology.yaml"), model)
    return tmp_path, Evaluator(model, History()), topo


def _load(env, text):
    tmp_path, ev, topo = env
    p = tmp_path / "t.flows.yaml"
    p.write_text(text)
    return load_flows(str(p), ev, topo)


def test_loads_and_compiles(env):
    spec = _load(env, FLOWS)
    a = spec.activities[0]
    assert a.name == "adc_to_sram"
    assert a.progress == "DMA2.S0CR"
    assert a.rules[0].target == "adc1"
    assert needed_registers(spec) == {"DMA2.S0CR", "ADC1.SR"}


def test_unknown_path_block_rejected(env):
    with pytest.raises(FlowError):
        _load(env, FLOWS.replace("mux0", "ghost"))


def test_unknown_rule_target_rejected(env):
    with pytest.raises(FlowError):
        _load(env, FLOWS.replace("target: adc1", "target: ghost"))


def test_bad_expression_rejected(env):
    with pytest.raises(FlowError):
        _load(env, FLOWS.replace("ADC1.SR.OVR == 1", "GHOST.X == 1"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_flows.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement flows.py**

```python
# core/target/flows.py
"""Loads <target>.flows.yaml: activities (data paths with liveness
conditions) and anomaly rules. Everything compiles at load time so a
typo dies at startup, not mid-debug-session."""
from dataclasses import dataclass, field as dfield
from typing import List, Optional, Set

import yaml

from ..engine.evaluator import CompiledExpr, Evaluator, ExprError
from .registers import SvdError
from .topology import Topology


class FlowError(Exception):
    pass


@dataclass
class Rule:
    expr: str
    msg: str
    target: str
    compiled: CompiledExpr


@dataclass
class Activity:
    name: str
    path: List[str]
    active_when: str
    active_compiled: CompiledExpr
    progress: Optional[str]
    rules: List[Rule] = dfield(default_factory=list)


@dataclass
class FlowSpec:
    activities: List[Activity]
    force_poll: List[str] = dfield(default_factory=list)


def _compile(ev: Evaluator, expr: str, where: str) -> CompiledExpr:
    try:
        return ev.compile(expr)
    except ExprError as e:
        raise FlowError("%s: %s" % (where, e))


def load_flows(path: str, evaluator: Evaluator,
               topology: Topology) -> FlowSpec:
    with open(path) as f:
        doc = yaml.safe_load(f)
    activities: List[Activity] = []
    for raw in doc.get("activities", []):
        name = raw["name"]
        act_path = raw["path"]
        for bid in act_path:
            if bid not in topology.blocks:
                raise FlowError("activity %s: unknown block %r"
                                % (name, bid))
        active = _compile(evaluator, raw["active_when"],
                          "activity %s active_when" % name)
        progress = raw.get("progress")
        if progress is not None:
            try:
                progress = evaluator.model.resolve(progress).reg_key
            except SvdError as e:
                raise FlowError("activity %s progress: %s" % (name, e))
        rules: List[Rule] = []
        for r in raw.get("anomalies", []):
            target = r["target"]
            if target not in topology.blocks:
                raise FlowError("activity %s rule target unknown: %r"
                                % (name, target))
            rules.append(Rule(
                expr=r["rule"], msg=r["msg"], target=target,
                compiled=_compile(evaluator, r["rule"],
                                  "activity %s rule" % name)))
        activities.append(Activity(
            name=name, path=act_path, active_when=raw["active_when"],
            active_compiled=active, progress=progress, rules=rules))
    force = []
    for ref in (doc.get("poll", {}) or {}).get("force_poll", []) or []:
        try:
            force.append(evaluator.model.resolve(ref).reg_key)
        except SvdError as e:
            raise FlowError("force_poll: %s" % e)
    return FlowSpec(activities=activities, force_poll=force)


def needed_registers(spec: FlowSpec) -> Set[str]:
    keys: Set[str] = set()
    for a in spec.activities:
        keys |= a.active_compiled.refs
        if a.progress is not None:
            keys.add(a.progress)
        for r in a.rules:
            keys |= r.compiled.refs
    return keys
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_flows.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add core/target/flows.py tests/test_flows.py
git commit -m "feat(core): flows yaml loader with compile-time validation"
```

---

### Task 9: Rule engine (flow states, anomaly latching)

**Files:**
- Create: `core/engine/rules.py`
- Test: `tests/test_rules.py`

**Interfaces:**
- Consumes: `FlowSpec` (Task 8), `Snapshot` (Task 6).
- Produces:
  - `RuleState` dataclass: `expr: str`, `msg: str`, `target: str`,
    `firing: bool`
  - `FlowState` dataclass: `name: str`, `active: bool`,
    `progress: Optional[int]`, `rules: List[RuleState]`
  - `AnomalyEvent` dataclass: `t: float`, `flow: str`, `msg: str`,
    `target: str`
  - `BadgeState` dataclass: `count: int`, `active: bool`
  - `EngineUpdate` dataclass: `snapshot: Snapshot`,
    `flows: Dict[str, FlowState]`, `events: List[AnomalyEvent]`,
    `badges: Dict[str, BadgeState]`
  - `RuleEngine(spec: FlowSpec)`:
    `process(snap: Snapshot) -> EngineUpdate` (events only on rising
    edges; badges latch counts per target block),
    `clear_badge(block_id: str) -> None`.
- Semantics: an expression evaluating to `None` (missing data) counts as
  not-firing and not-active. `badges[block].active` is True while any
  rule targeting that block currently fires; `count` increments on each
  rising edge and only `clear_badge` resets it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rules.py
import pytest
from core.engine.evaluator import Evaluator
from core.engine.history import History
from core.engine.rules import RuleEngine
from core.engine.snapshot import Sample, Snapshot
from core.target.flows import load_flows
from core.target.registers import RegisterModel
from core.target.topology import load_topology
from tests.test_registers import FIXTURE
from tests.test_topology import GOOD as TOPO
from tests.test_flows import FLOWS


@pytest.fixture
def engine(tmp_path):
    (tmp_path / "t.svd").write_text(FIXTURE)
    (tmp_path / "t.topology.yaml").write_text(TOPO)
    (tmp_path / "t.flows.yaml").write_text(FLOWS)
    model = RegisterModel.from_svd(str(tmp_path / "t.svd"))
    topo = load_topology(str(tmp_path / "t.topology.yaml"), model)
    ev = Evaluator(model, History())
    return RuleEngine(load_flows(str(tmp_path / "t.flows.yaml"), ev, topo))


def snap(t, sr=0, s0cr=1):
    return Snapshot(values={"ADC1.SR": Sample(sr, t),
                            "DMA2.S0CR": Sample(s0cr, t)},
                    t=t, rate_hz=30.0)


def test_flow_active_and_progress(engine):
    u = engine.process(snap(1.0))
    f = u.flows["adc_to_sram"]
    assert f.active is True
    assert f.progress == 1                    # progress reg is DMA2.S0CR
    assert u.events == []


def test_rising_edge_emits_once_and_latches(engine):
    engine.process(snap(1.0, sr=0))
    u2 = engine.process(snap(1.1, sr=1 << 5))     # OVR fires
    assert len(u2.events) == 1
    assert u2.events[0].msg == "ADC overrun"
    assert u2.badges["adc1"].count == 1
    assert u2.badges["adc1"].active is True
    u3 = engine.process(snap(1.2, sr=1 << 5))     # still true: no new event
    assert u3.events == []
    assert u3.badges["adc1"].count == 1
    u4 = engine.process(snap(1.3, sr=0))          # condition gone: latched
    assert u4.badges["adc1"].count == 1
    assert u4.badges["adc1"].active is False


def test_clear_badge_and_refire(engine):
    engine.process(snap(1.0, sr=1 << 5))
    engine.clear_badge("adc1")
    u = engine.process(snap(1.1, sr=1 << 5))      # no rising edge
    assert u.badges["adc1"].count == 0
    assert u.badges["adc1"].active is True        # honest: still firing
    engine.process(snap(1.2, sr=0))
    u2 = engine.process(snap(1.3, sr=1 << 5))     # new rising edge
    assert u2.badges["adc1"].count == 1


def test_missing_data_is_inactive_not_firing(engine):
    u = engine.process(Snapshot(values={}, t=1.0, rate_hz=0.0))
    assert u.flows["adc_to_sram"].active is False
    assert u.events == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_rules.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement rules.py**

```python
# core/engine/rules.py
"""Turns snapshots into flow states and latched anomaly events."""
from dataclasses import dataclass, field as dfield
from typing import Dict, List, Optional

from ..target.flows import FlowSpec
from .snapshot import Snapshot


@dataclass
class RuleState:
    expr: str
    msg: str
    target: str
    firing: bool


@dataclass
class FlowState:
    name: str
    active: bool
    progress: Optional[int]
    rules: List[RuleState] = dfield(default_factory=list)


@dataclass
class AnomalyEvent:
    t: float
    flow: str
    msg: str
    target: str


@dataclass
class BadgeState:
    count: int = 0
    active: bool = False


@dataclass
class EngineUpdate:
    snapshot: Snapshot
    flows: Dict[str, FlowState]
    events: List[AnomalyEvent]
    badges: Dict[str, BadgeState]


class RuleEngine:
    def __init__(self, spec: FlowSpec):
        self.spec = spec
        self._prev: Dict[str, bool] = {}
        self._badges: Dict[str, BadgeState] = {}

    def clear_badge(self, block_id: str) -> None:
        if block_id in self._badges:
            self._badges[block_id].count = 0

    def process(self, snap: Snapshot) -> EngineUpdate:
        flows: Dict[str, FlowState] = {}
        events: List[AnomalyEvent] = []
        for b in self._badges.values():
            b.active = False
        for act in self.spec.activities:
            active = act.active_compiled.eval(snap) is True
            progress = None
            if act.progress is not None:
                progress = snap.value(act.progress)
            states: List[RuleState] = []
            for i, rule in enumerate(act.rules):
                firing = rule.compiled.eval(snap) is True
                key = "%s#%d" % (act.name, i)
                if firing and not self._prev.get(key, False):
                    badge = self._badges.setdefault(rule.target,
                                                    BadgeState())
                    badge.count += 1
                    events.append(AnomalyEvent(
                        t=snap.t, flow=act.name, msg=rule.msg,
                        target=rule.target))
                if firing:
                    self._badges.setdefault(rule.target,
                                            BadgeState()).active = True
                self._prev[key] = firing
                states.append(RuleState(rule.expr, rule.msg, rule.target,
                                        firing))
            flows[act.name] = FlowState(act.name, active, progress, states)
        return EngineUpdate(snapshot=snap, flows=flows, events=events,
                            badges={k: BadgeState(v.count, v.active)
                                    for k, v in self._badges.items()})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_rules.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add core/engine/rules.py tests/test_rules.py
git commit -m "feat(core): rule engine with flow states and anomaly latching"
```

---

### Task 10: Read-plan builder

**Files:**
- Create: `core/engine/readplan.py`
- Test: `tests/test_readplan.py`

**Interfaces:**
- Consumes: `RegisterModel.resolve` (Task 3).
- Produces:
  - `ReadOp` dataclass: `addr: int`, `count: int`,
    `targets: List[Tuple[str, int]]` (reg_key, word index into the block)
  - `build_read_plan(reg_keys: Set[str], model: RegisterModel,
    merge_gap_words: int = 8) -> List[ReadOp]` -- sorted by address;
    registers whose gap is <= merge_gap_words words are merged into one
    block read (USB latency dominates, spec 6.2).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_readplan.py
import pytest
from core.engine.readplan import build_read_plan
from core.target.registers import RegisterModel
from tests.test_registers import FIXTURE


@pytest.fixture
def model(tmp_path):
    (tmp_path / "t.svd").write_text(FIXTURE)
    return RegisterModel.from_svd(str(tmp_path / "t.svd"))


def test_adjacent_registers_merge(model):
    # DMA2.S0CR @ +0x10 and DMA2.S1CR @ +0x28: gap 5 words -> one op
    plan = build_read_plan({"DMA2.S0CR", "DMA2.S1CR"}, model)
    assert len(plan) == 1
    op = plan[0]
    assert op.addr == 0x40026410
    assert op.count == 7                      # 0x10..0x28 inclusive
    assert ("DMA2.S0CR", 0) in op.targets
    assert ("DMA2.S1CR", 6) in op.targets


def test_distant_registers_split(model):
    plan = build_read_plan({"ADC1.SR", "DMA2.S0CR"}, model)
    assert len(plan) == 2
    assert plan[0].addr == 0x40012000         # sorted by address


def test_merge_gap_zero_never_merges(model):
    plan = build_read_plan({"DMA2.S0CR", "DMA2.S1CR"}, model,
                           merge_gap_words=0)
    assert len(plan) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_readplan.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement readplan.py**

```python
# core/engine/readplan.py
"""Groups the registers a sweep needs into as few block reads as the
address map allows."""
from dataclasses import dataclass
from typing import List, Set, Tuple

from ..target.registers import RegisterModel


@dataclass
class ReadOp:
    addr: int
    count: int
    targets: List[Tuple[str, int]]


def build_read_plan(reg_keys: Set[str], model: RegisterModel,
                    merge_gap_words: int = 8) -> List[ReadOp]:
    entries = sorted((model.resolve(k).address, k) for k in reg_keys)
    plan: List[ReadOp] = []
    for addr, key in entries:
        if plan:
            cur = plan[-1]
            gap = (addr - (cur.addr + 4 * cur.count)) // 4
            if 0 <= gap <= merge_gap_words:
                cur.count = (addr - cur.addr) // 4 + 1
                cur.targets.append((key, (addr - cur.addr) // 4))
                continue
        plan.append(ReadOp(addr=addr, count=1, targets=[(key, 0)]))
    return plan
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_readplan.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add core/engine/readplan.py tests/test_readplan.py
git commit -m "feat(core): batched read-plan builder"
```

---

### Task 11: Poller thread

**Files:**
- Create: `core/engine/poller.py`
- Test: `tests/test_poller.py`

**Interfaces:**
- Consumes: `TargetAdapter` (Task 2), `ReadOp` list (Task 10),
  `Sample`/`Snapshot` (Task 6).
- Produces:
  - `PollerState` str constants: `RUNNING = "running"`,
    `TARGET_LOST = "target_lost"`, `STOPPED = "stopped"`
  - `Poller(adapter, plan: List[ReadOp], interval_s: float = 0.02,
    reconnect_s: float = 0.5)` -- a `threading.Thread` subclass:
    - `on_snapshot(cb: Callable[[Snapshot], None])`,
      `on_state(cb: Callable[[str], None])` -- callbacks fire on the
      poller thread.
    - `submit(fn: Callable[[TargetAdapter], Any]) -> "queue.Queue"` --
      run `fn` with the adapter between sweeps (halt, memory dumps);
      the returned queue delivers one `(ok: bool, result)` tuple.
    - `stop()` -- joins the thread.
    - Adapter exceptions flip state to `TARGET_LOST`; the poller then
      retries `adapter.connect()` every `reconnect_s` until it succeeds
      and resumes (state back to `RUNNING`).
    - `rate_hz` measured over a 2 s sliding window, carried in each
      snapshot.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_poller.py
import time

import pytest
from core.adapter.mock import MockAdapter
from core.engine.poller import Poller, PollerState
from core.engine.readplan import ReadOp


def make_poller(adapter, snaps, states, interval=0.005):
    plan = [ReadOp(addr=0x40000000, count=2,
                   targets=[("P.A", 0), ("P.B", 1)])]
    p = Poller(adapter, plan, interval_s=interval, reconnect_s=0.02)
    p.on_snapshot(snaps.append)
    p.on_state(states.append)
    return p


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


def test_snapshots_flow_and_carry_values():
    a = MockAdapter({0x40000000: 7, 0x40000004: 9})
    snaps, states = [], []
    p = make_poller(a, snaps, states)
    p.start()
    assert wait_for(lambda: len(snaps) >= 3)
    p.stop()
    s = snaps[-1]
    assert s.value("P.A") == 7
    assert s.value("P.B") == 9
    assert s.rate_hz > 0


def test_command_queue_runs_between_sweeps():
    a = MockAdapter({})
    snaps, states = [], []
    p = make_poller(a, snaps, states)
    p.start()
    q = p.submit(lambda ad: ad.read_block32(0x0, 1))
    ok, result = q.get(timeout=2.0)
    p.stop()
    assert ok is True
    assert result == [0]


def test_command_error_reported_not_fatal():
    a = MockAdapter({})
    snaps, states = [], []
    p = make_poller(a, snaps, states)
    p.start()

    def boom(ad):
        raise ValueError("nope")
    ok, result = p.submit(boom).get(timeout=2.0)
    n = len(snaps)
    assert wait_for(lambda: len(snaps) > n)   # still polling
    p.stop()
    assert ok is False
    assert isinstance(result, ValueError)


def test_target_lost_and_reconnect():
    a = MockAdapter({0x40000000: 1})
    snaps, states = [], []
    p = make_poller(a, snaps, states)
    p.start()
    assert wait_for(lambda: len(snaps) >= 1)
    a.fail_next(3)
    assert wait_for(lambda: PollerState.TARGET_LOST in states)
    assert wait_for(lambda: states[-1] == PollerState.RUNNING)
    n = len(snaps)
    assert wait_for(lambda: len(snaps) > n)
    p.stop()
    assert states[-1] == PollerState.STOPPED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_poller.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement poller.py**

```python
# core/engine/poller.py
"""The single thread that owns the adapter. Everything hardware goes
through here: periodic sweeps, user commands, reconnection."""
import queue
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, List

from ..adapter.base import AdapterError, TargetAdapter
from .readplan import ReadOp
from .snapshot import Sample, Snapshot


class PollerState:
    RUNNING = "running"
    TARGET_LOST = "target_lost"
    STOPPED = "stopped"


class Poller(threading.Thread):
    def __init__(self, adapter: TargetAdapter, plan: List[ReadOp],
                 interval_s: float = 0.02, reconnect_s: float = 0.5):
        super().__init__(daemon=True)
        self.adapter = adapter
        self.plan = plan
        self.interval_s = interval_s
        self.reconnect_s = reconnect_s
        self._commands: "queue.Queue" = queue.Queue()
        self._snapshot_cbs: List[Callable[[Snapshot], None]] = []
        self._state_cbs: List[Callable[[str], None]] = []
        self._stop = threading.Event()
        self._times: Deque[float] = deque(maxlen=600)

    def on_snapshot(self, cb: Callable[[Snapshot], None]) -> None:
        self._snapshot_cbs.append(cb)

    def on_state(self, cb: Callable[[str], None]) -> None:
        self._state_cbs.append(cb)

    def submit(self, fn: Callable[[TargetAdapter], Any]) -> "queue.Queue":
        result: "queue.Queue" = queue.Queue(maxsize=1)
        self._commands.put((fn, result))
        return result

    def stop(self) -> None:
        self._stop.set()
        self.join(timeout=5.0)

    def _emit_state(self, state: str) -> None:
        for cb in self._state_cbs:
            cb(state)

    def _drain_commands(self) -> None:
        while True:
            try:
                fn, result = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                result.put((True, fn(self.adapter)))
            except AdapterError:
                result.put((False, None))
                raise
            except Exception as e:  # command bug: report, keep polling
                result.put((False, e))

    def _sweep(self) -> Snapshot:
        values = {}
        for op in self.plan:
            words = self.adapter.read_block32(op.addr, op.count)
            t = time.monotonic()
            for key, idx in op.targets:
                values[key] = Sample(words[idx], t)
        now = time.monotonic()
        self._times.append(now)
        cutoff = now - 2.0
        recent = [t for t in self._times if t >= cutoff]
        rate = len(recent) / 2.0 if len(recent) > 1 else 0.0
        return Snapshot(values=values, t=now, rate_hz=rate)

    def run(self) -> None:
        self._emit_state(PollerState.RUNNING)
        while not self._stop.is_set():
            try:
                self._drain_commands()
                snap = self._sweep()
                for cb in self._snapshot_cbs:
                    cb(snap)
            except AdapterError:
                self._emit_state(PollerState.TARGET_LOST)
                while not self._stop.is_set():
                    time.sleep(self.reconnect_s)
                    try:
                        self.adapter.connect()
                        self._emit_state(PollerState.RUNNING)
                        break
                    except AdapterError:
                        continue
                continue
            self._stop.wait(self.interval_s)
        self._emit_state(PollerState.STOPPED)
```

Note: `MockAdapter.fail_next(3)` makes the reconnect `connect()` succeed
immediately (mock connect never fails); the two extra scripted failures
are consumed by subsequent sweeps, which re-enter TARGET_LOST and
reconnect again -- the test only requires that the final state settles
back to RUNNING and snapshots resume.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_poller.py -v`
Expected: 4 passed (allow a few seconds; these are timing tests)

- [ ] **Step 5: Commit**

```bash
git add core/engine/poller.py tests/test_poller.py
git commit -m "feat(core): poller thread with command queue and reconnect"
```

---

### Task 12: Engine facade

**Files:**
- Create: `core/engine/core.py`
- Create: `tests/fixtures/minitarget/mini.svd`,
  `tests/fixtures/minitarget/mini.topology.yaml`,
  `tests/fixtures/minitarget/mini.flows.yaml`
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `core.engine.core.EngineError(Exception)`
  - `Engine.load(target_dir: str, adapter: TargetAdapter,
    interval_s: float = 0.02) -> Engine` -- finds exactly one `*.svd`,
    `*.topology.yaml`, `*.flows.yaml` in `target_dir`; builds model,
    evaluator, flows, read plan; applies the readAction whitelist
    (needed registers with a `read_action` and not in `force_poll` are
    DROPPED from polling; their reg_keys land in
    `engine.excluded: List[str]`).
  - Attributes: `topology`, `flowspec`, `model`, `history`, `excluded`.
  - Methods: `start()`, `stop()`,
    `on_update(cb: Callable[[EngineUpdate], None])`,
    `on_state(cb: Callable[[str], None])`,
    `clear_badge(block_id)`, `submit(fn)` (delegates to poller).
  - Internally: poller snapshot -> history.record for every value ->
    RuleEngine.process -> on_update callbacks.

- [ ] **Step 1: Write the fixture target**

`tests/fixtures/minitarget/mini.svd` -- copy the FIXTURE XML from
`tests/test_registers.py` verbatim into this file.

`tests/fixtures/minitarget/mini.topology.yaml`:

```yaml
blocks:
  - id: adc1
    kind: peripheral
    svd: ADC1
  - id: dma2
    kind: dma
    svd: DMA2
  - id: sram1
    kind: memory
    base: 0x20000000
    size: 0x20000
edges:
  - {from: adc1, to: dma2}
  - {from: dma2, to: sram1}
```

`tests/fixtures/minitarget/mini.flows.yaml`:

```yaml
activities:
  - name: adc_to_sram
    path: [adc1, dma2, sram1]
    active_when: "DMA2.S0CR.EN == 1"
    progress: "DMA2.S0CR"
    anomalies:
      - {rule: "ADC1.SR.OVR == 1", msg: "ADC overrun", target: adc1}
      - {rule: "ADC1.DR == 0", msg: "never fires", target: adc1}
poll:
  force_poll: []
```

(`ADC1.DR` has `readAction: clear` in the fixture SVD -- it exists to
prove the whitelist drops it.)

- [ ] **Step 2: Write the failing test**

```python
# tests/test_engine.py
import time

import pytest
from core.adapter.mock import MockAdapter
from core.engine.core import Engine

TARGET = "tests/fixtures/minitarget"
S0CR = 0x40026410
ADC_SR = 0x40012000


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def rig():
    a = MockAdapter({S0CR: 1, ADC_SR: 0})
    e = Engine.load(TARGET, a, interval_s=0.005)
    updates = []
    e.on_update(updates.append)
    e.start()
    yield a, e, updates
    e.stop()


def test_guarded_register_excluded(rig):
    a, e, updates = rig
    assert "ADC1.DR" in e.excluded
    assert wait_for(lambda: len(updates) >= 1)
    assert "ADC1.DR" not in updates[-1].snapshot.values


def test_flow_state_and_anomaly_end_to_end(rig):
    a, e, updates = rig
    assert wait_for(lambda: len(updates) >= 2)
    assert updates[-1].flows["adc_to_sram"].active is True
    a.set_word(ADC_SR, 1 << 5)                 # inject overrun
    assert wait_for(lambda: any(u.events for u in updates))
    assert wait_for(
        lambda: updates[-1].badges.get("adc1") is not None
        and updates[-1].badges["adc1"].count >= 1)


def test_history_populated(rig):
    a, e, updates = rig
    assert wait_for(lambda: len(e.history.series("DMA2.S0CR")) >= 2)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_engine.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 4: Implement core.py**

```python
# core/engine/core.py
"""Engine facade: loads a target directory, wires poller -> history ->
rules -> callbacks. The only class UI or CLI code needs to touch."""
import glob
import os
from typing import Any, Callable, List

from ..adapter.base import TargetAdapter
from ..target.flows import FlowSpec, load_flows, needed_registers
from ..target.registers import RegisterModel
from ..target.topology import Topology, load_topology
from .evaluator import Evaluator
from .history import History
from .poller import Poller
from .readplan import build_read_plan
from .rules import EngineUpdate, RuleEngine
from .snapshot import Snapshot


class EngineError(Exception):
    pass


def _one(target_dir: str, pattern: str) -> str:
    hits = sorted(glob.glob(os.path.join(target_dir, pattern)))
    if len(hits) != 1:
        raise EngineError("expected exactly one %s in %s, found %d"
                          % (pattern, target_dir, len(hits)))
    return hits[0]


class Engine:
    def __init__(self, model: RegisterModel, topology: Topology,
                 flowspec: FlowSpec, history: History, poller: Poller,
                 rules: RuleEngine, excluded: List[str]):
        self.model = model
        self.topology = topology
        self.flowspec = flowspec
        self.history = history
        self.excluded = excluded
        self._poller = poller
        self._rules = rules
        self._update_cbs: List[Callable[[EngineUpdate], None]] = []
        poller.on_snapshot(self._handle_snapshot)

    @classmethod
    def load(cls, target_dir: str, adapter: TargetAdapter,
             interval_s: float = 0.02) -> "Engine":
        model = RegisterModel.from_svd(_one(target_dir, "*.svd"))
        history = History()
        evaluator = Evaluator(model, history)
        topology = load_topology(_one(target_dir, "*.topology.yaml"), model)
        flowspec = load_flows(_one(target_dir, "*.flows.yaml"), evaluator,
                              topology)
        needed = needed_registers(flowspec)
        for b in topology.blocks.values():
            if b.select is not None:
                needed.add(model.resolve(b.select).reg_key)
        excluded = []
        polled = set()
        for key in needed:
            rr = model.resolve(key)
            if rr.read_action is not None and key not in flowspec.force_poll:
                excluded.append(key)
            else:
                polled.add(key)
        plan = build_read_plan(polled, model)
        poller = Poller(adapter, plan, interval_s=interval_s)
        rules = RuleEngine(flowspec)
        return cls(model, topology, flowspec, history, poller, rules,
                   sorted(excluded))

    def start(self) -> None:
        self._poller.start()

    def stop(self) -> None:
        self._poller.stop()

    def on_update(self, cb: Callable[[EngineUpdate], None]) -> None:
        self._update_cbs.append(cb)

    def on_state(self, cb: Callable[[str], None]) -> None:
        self._poller.on_state(cb)

    def clear_badge(self, block_id: str) -> None:
        self._rules.clear_badge(block_id)

    def submit(self, fn: Callable[[TargetAdapter], Any]) -> Any:
        return self._poller.submit(fn)

    def _handle_snapshot(self, snap: Snapshot) -> None:
        for key, sample in snap.values.items():
            self.history.record(key, sample.t, sample.value)
        update = self._rules.process(snap)
        for cb in self._update_cbs:
            cb(update)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_engine.py -v`
Expected: 3 passed
Then run the whole suite: `.venv/bin/pytest`
Expected: all green

- [ ] **Step 6: Commit**

```bash
git add core/engine/core.py tests/fixtures tests/test_engine.py
git commit -m "feat(core): engine facade wiring poller, history, and rules"
```

---

### Task 13: pyOCD SWD adapter

**Files:**
- Create: `core/adapter/pyocd_swd.py`
- Test: `tests/test_pyocd_adapter.py` (hw-marked)

**Interfaces:**
- Consumes: `TargetAdapter` contract (Task 2).
- Produces: `core.adapter.pyocd_swd.PyOCDAdapter(target: str =
  "stm32f411ce")` implementing `TargetAdapter`. `connect()` attaches
  without reset (`connect_mode="attach"`) so it can observe running
  firmware. All pyOCD exceptions are re-raised as `TargetLostError`.

- [ ] **Step 1: Implement pyocd_swd.py**

```python
# core/adapter/pyocd_swd.py
"""SWD transport via pyOCD + ST-Link. Attach-only: never resets the
target on connect, we are an observer."""
from typing import List, Optional

from pyocd.core.helpers import ConnectHelper

from .base import TargetAdapter, TargetInfo, TargetLostError

DBGMCU_IDCODE = 0xE0042000


class PyOCDAdapter(TargetAdapter):
    def __init__(self, target: str = "stm32f411ce"):
        self.target_name = target
        self._session = None

    def connect(self) -> TargetInfo:
        try:
            if self._session is not None:
                self.disconnect()
            self._session = ConnectHelper.session_with_chosen_probe(
                target_override=self.target_name,
                connect_mode="attach",
                blocking=False)
            if self._session is None:
                raise TargetLostError("no debug probe found")
            self._session.open()
            idcode = self._session.target.read32(DBGMCU_IDCODE)
            return TargetInfo(name=self.target_name, idcode=idcode)
        except TargetLostError:
            raise
        except Exception as e:
            self._session = None
            raise TargetLostError(str(e))

    def disconnect(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None

    def _target(self):
        if self._session is None:
            raise TargetLostError("not connected")
        return self._session.target

    def read_block32(self, addr: int, count: int) -> List[int]:
        try:
            return self._target().read_memory_block32(addr, count)
        except TargetLostError:
            raise
        except Exception as e:
            raise TargetLostError(str(e))

    def write32(self, addr: int, value: int) -> None:
        try:
            self._target().write32(addr, value)
        except TargetLostError:
            raise
        except Exception as e:
            raise TargetLostError(str(e))

    def halt(self) -> None:
        try:
            self._target().halt()
        except Exception as e:
            raise TargetLostError(str(e))

    def resume(self) -> None:
        try:
            self._target().resume()
        except Exception as e:
            raise TargetLostError(str(e))

    def is_running(self) -> bool:
        try:
            from pyocd.core.target import Target
            return self._target().get_state() == Target.State.RUNNING
        except Exception as e:
            raise TargetLostError(str(e))
```

- [ ] **Step 2: Write the hw-marked test**

```python
# tests/test_pyocd_adapter.py
import pytest
from core.adapter.pyocd_swd import PyOCDAdapter

pytestmark = pytest.mark.hw


def test_connect_and_read_cpuid():
    a = PyOCDAdapter()
    info = a.connect()
    try:
        assert info.idcode & 0xFFF == 0x431          # F411 device id
        cpuid = a.read_block32(0xE000ED00, 1)[0]     # SCB->CPUID
        assert (cpuid >> 4) & 0xFFF == 0xC24         # Cortex-M4 part
        assert a.is_running() in (True, False)
    finally:
        a.disconnect()
```

- [ ] **Step 3: Verify default suite still green (hw test deselected)**

Run: `.venv/bin/pytest`
Expected: all passed, hw test shown as deselected.
With the Blackpill + ST-Link attached, run:
`.venv/bin/pytest -m hw -v` -- Expected: 1 passed. If the probe is
missing the WinUSB/libusb driver or permissions, pyOCD's error text
says so; record any fix needed in README later (Task 16 step).

- [ ] **Step 4: Commit**

```bash
git add core/adapter/pyocd_swd.py tests/test_pyocd_adapter.py
git commit -m "feat(core): pyOCD SWD adapter (attach mode)"
```

---

### Task 14: CLI probe command -- MILESTONE M0

**Files:**
- Create: `cli.py`
- Test: manual hardware check (this is the M0 gate)

**Interfaces:**
- Produces: `python -m cli probe` and the argparse skeleton that Task 16
  extends with `monitor`. `cli.build_parser()` returns the parser
  (import-tested later).

- [ ] **Step 1: Implement cli.py**

```python
# cli.py
"""Command-line driver for the data path explorer core engine."""
import argparse
import sys

from core.adapter.pyocd_swd import PyOCDAdapter


def cmd_probe(args: argparse.Namespace) -> int:
    a = PyOCDAdapter(target=args.target)
    info = a.connect()
    try:
        cpuid = a.read_block32(0xE000ED00, 1)[0]
        print("target   : %s" % info.name)
        print("idcode   : 0x%08X" % info.idcode)
        print("cpuid    : 0x%08X" % cpuid)
        print("running  : %s" % a.is_running())
    finally:
        a.disconnect()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dpe")
    p.add_argument("--target", default="stm32f411ce")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe", help="connect and print target identity")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "probe":
        return cmd_probe(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: M0 gate -- run on hardware**

Connect ST-Link to Blackpill (SWDIO/SWCLK/GND/3V3), plug in, then:
Run: `.venv/bin/python -m cli probe`
Expected output shape:

```
target   : stm32f411ce
idcode   : 0x100xx431
cpuid    : 0x410FC241
running  : True
```

If pyOCD cannot find the probe: `.venv/bin/pyocd list` to check
enumeration; on macOS no driver is needed, on Windows install the
ST-Link driver. Do not proceed to M1 until probe works.

- [ ] **Step 3: Commit**

```bash
git add cli.py
git commit -m "feat(cli): probe command - M0 hardware bring-up"
```

---

### Task 15: F411 target description files

**Files:**
- Create: `targets/f411/f411.topology.yaml`
- Create: `targets/f411/f411.flows.yaml`
- Test: `tests/test_f411_target.py`

**Interfaces:**
- Consumes: loaders (Tasks 5, 8), F411 SVD (Task 4).
- Produces: the target directory `targets/f411/` loadable by
  `Engine.load`. Register names must match Task 4's verified names.
- Note: firmware (Task 17) uses ADC channel 1 on pin PA1 (PA0 is the
  Blackpill user button, used for fault injection).

- [ ] **Step 1: Write the topology file**

```yaml
# targets/f411/f411.topology.yaml
# STM32F411 "Blackpill" ADC->DMA->SRAM demo scenario.
blocks:
  - id: pa1
    kind: pin
    title: PA1
  - id: adc1
    kind: peripheral
    title: ADC1
    svd: ADC1
    ports: {DR: [r, 0.5]}
  - id: mux0
    kind: mux
    title: MUX
    select: "DMA2.S0CR.CHSEL"
  - id: tim1
    kind: peripheral
    title: TIM1
    svd: TIM1
  - id: dma2
    kind: dma
    title: DMA2
    svd: DMA2
    ports: {periph: [l, 0.5], mem: [r, 0.5]}
  - id: busmx
    kind: interconnect
    title: AHB Bus Matrix
  - id: sram1
    kind: memory
    title: SRAM1 128KB
    base: 0x20000000
    size: 0x20000
  - id: cm4
    kind: cpu
    title: Cortex-M4
  - id: flash
    kind: memory
    title: Flash 512KB
    base: 0x08000000
    size: 0x80000
edges:
  - {from: pa1, to: adc1, label: AIN1}
  - {from: adc1, to: mux0, label: DR, when_select: 0}
  - {from: tim1, to: mux0, label: TRGO, when_select: 6}
  - {from: mux0, to: dma2, to_port: periph, label: S0}
  - {from: dma2, from_port: mem, to: busmx, label: AHB}
  - {from: busmx, to: sram1, label: M0AR}
  - {from: flash, to: busmx, label: I-bus}
  - {from: busmx, to: cm4, label: fetch}
```

- [ ] **Step 2: Write the flows file**

```yaml
# targets/f411/f411.flows.yaml
activities:
  - name: adc_to_sram
    path: [pa1, adc1, mux0, dma2, busmx, sram1]
    active_when: "DMA2.S0CR.EN == 1 and DMA2.S0CR.CHSEL == 0"
    progress: "DMA2.S0NDTR"
    anomalies:
      - {rule: "ADC1.SR.OVR == 1", msg: "ADC overrun, data lost",
         target: adc1}
      - {rule: "stalled(DMA2.S0NDTR, 500)", msg: "DMA stalled",
         target: dma2}
      - {rule: "DMA2.LISR.TEIF0 == 1", msg: "DMA transfer error",
         target: dma2}
poll:
  force_poll: []
```

- [ ] **Step 3: Write the loader test**

```python
# tests/test_f411_target.py
from core.adapter.mock import MockAdapter
from core.engine.core import Engine


def test_f411_target_loads_with_mock_adapter():
    e = Engine.load("targets/f411", MockAdapter({}))
    assert "adc_to_sram" in [a.name for a in e.flowspec.activities]
    assert "mux0" in e.topology.blocks
    # whitelist policy: nothing in this flow file needs a guarded reg
    assert e.excluded == []
```

- [ ] **Step 4: Run test**

Run: `.venv/bin/pytest tests/test_f411_target.py -v`
Expected: 1 passed. If SVD names differ (Task 4 step 3 note), fix the
YAML refs to the verified names.

- [ ] **Step 5: Commit**

```bash
git add targets/f411 tests/test_f411_target.py
git commit -m "feat(targets): f411 topology and flows description"
```

---

### Task 16: CLI monitor command -- MILESTONE M1

**Files:**
- Modify: `cli.py`
- Create: `README.md`
- Test: `tests/test_cli.py` plus manual hardware gate

**Interfaces:**
- Produces: `python -m cli monitor --target-dir targets/f411
  [--seconds N] [--interval MS]` -- runs the Engine on the real
  adapter, prints one status line per second and every event; on exit
  prints the measured average snapshot rate. THIS NUMBER IS THE M1
  GATE.

- [ ] **Step 1: Add monitor to cli.py**

Add to `cli.py` (new imports at top, new function, parser wiring):

```python
import threading
import time

from core.engine.core import Engine


def cmd_monitor(args: argparse.Namespace) -> int:
    adapter = PyOCDAdapter(target=args.target)
    engine = Engine.load(args.target_dir, adapter,
                         interval_s=args.interval / 1000.0)
    if engine.excluded:
        print("note: not polled (readAction): %s"
              % ", ".join(engine.excluded))
    lock = threading.Lock()
    latest = {"update": None, "rates": []}

    def on_update(u):
        with lock:
            latest["update"] = u
            latest["rates"].append(u.snapshot.rate_hz)
        for ev in u.events:
            print("[%8.3f] ANOMALY %s: %s (block %s)"
                  % (ev.t, ev.flow, ev.msg, ev.target))

    def on_state(state):
        print("state -> %s" % state)

    engine.on_update(on_update)
    engine.on_state(on_state)
    engine.start()
    end = time.monotonic() + args.seconds
    try:
        while time.monotonic() < end:
            time.sleep(1.0)
            with lock:
                u = latest["update"]
            if u is None:
                print("(no data yet)")
                continue
            parts = ["rate %5.1f Hz" % u.snapshot.rate_hz]
            for name, f in sorted(u.flows.items()):
                p = "" if f.progress is None else " prog=0x%04X" % f.progress
                parts.append("%s %s%s"
                             % (name, "ACTIVE" if f.active else "idle", p))
            print(" | ".join(parts))
    finally:
        engine.stop()
        with lock:
            rates = [r for r in latest["rates"] if r > 0]
        if rates:
            print("M1 RESULT: avg %.1f Hz, min %.1f Hz over %d snapshots"
                  % (sum(rates) / len(rates), min(rates), len(rates)))
    return 0
```

Parser wiring inside `build_parser()` (after the probe subparser):

```python
    mon = sub.add_parser("monitor", help="poll flows and print states")
    mon.add_argument("--target-dir", default="targets/f411")
    mon.add_argument("--seconds", type=float, default=10.0)
    mon.add_argument("--interval", type=float, default=20.0,
                     help="sweep interval in ms")
```

And in `main()`:

```python
    if args.cmd == "monitor":
        return cmd_monitor(args)
```

- [ ] **Step 2: Write the no-hardware CLI test**

```python
# tests/test_cli.py
import cli


def test_parser_has_both_commands():
    p = cli.build_parser()
    a = p.parse_args(["probe"])
    assert a.cmd == "probe"
    a = p.parse_args(["monitor", "--seconds", "3", "--interval", "10"])
    assert a.cmd == "monitor"
    assert a.seconds == 3.0
```

Run: `.venv/bin/pytest tests/test_cli.py -v` -- Expected: 1 passed

- [ ] **Step 3: M1 gate -- measure on hardware**

Firmware is not needed for the rate measurement (registers read as
whatever reset state holds). With Blackpill attached:
Run: `.venv/bin/python -m cli monitor --seconds 15`
Record the `M1 RESULT` line.

Decision rule from the spec: the flow set polls 4 registers
(`DMA2.S0CR`, `DMA2.S0NDTR`, `DMA2.LISR`, `ADC1.SR`).
- avg >= 20 Hz: PASS -- proceed.
- 5-20 Hz: marginal -- try `--interval 0` and USB port changes; proceed
  but note the ceiling in README.
- < 5 Hz: FAIL -- stop; reassess probe (J-Link) before Tasks 17+ and
  before planning M3.

- [ ] **Step 4: Write README.md recording the result**

```markdown
# Data Path Explorer

Visual data-path debug tool. Core engine + CLI (M0-M2); UI follows.

## Quick start (dev)

    python3 -m venv .venv
    .venv/bin/pip install -e ".[dev]"
    .venv/bin/pytest              # no hardware needed
    .venv/bin/python -m cli probe # needs Blackpill + ST-Link

## M1 poll-rate measurement

Setup: Blackpill (STM32F411CE), clone ST-Link v2, macOS.
Command: `python -m cli monitor --seconds 15`
Result: <PASTE THE M1 RESULT LINE HERE>

## Probe notes

<record any driver/permission fixes discovered during bring-up>
```

Paste the real measured line before committing.

- [ ] **Step 5: Commit**

```bash
git add cli.py tests/test_cli.py README.md
git commit -m "feat(cli): monitor command and M1 poll-rate measurement"
```

---

### Task 17: Blackpill test firmware

**Files:**
- Create: `firmware/blackpill_adc_dma/main.c`
- Create: `firmware/blackpill_adc_dma/stm32f411.ld`
- Create: `firmware/blackpill_adc_dma/Makefile`

**Interfaces:**
- Produces: firmware where ADC1 continuously samples channel 1 (PA1),
  DMA2 stream 0 (channel 0) moves samples into a circular SRAM buffer
  of 1000 halfwords; pressing the KEY button (PA0, active low) toggles
  the DMA stream off/on (off = the monitor should show "DMA stalled"
  then "ADC overrun"); PC13 LED heartbeat proves the loop is alive.
- Requires: `brew install --cask gcc-arm-embedded` (or any
  arm-none-eabi-gcc >= 10 on PATH).

- [ ] **Step 1: Write main.c**

```c
/**
 * @file main.c
 * @brief Blackpill (STM32F411CE) test firmware for Data Path Explorer.
 * @details ADC1 ch1 (PA1) continuous conversion -> DMA2 stream 0
 *          (channel 0) -> circular buffer in SRAM. KEY button (PA0,
 *          active low) toggles the DMA stream to inject the "stalled"
 *          and "overrun" anomalies the tool detects. PC13 LED blinks
 *          as a heartbeat. Bare metal, HSI 16 MHz, no HAL.
 */
#include <stdint.h>

#define REG(a)          (*(volatile uint32_t *)(a))

#define RCC_AHB1ENR     REG(0x40023830u)
#define RCC_APB2ENR     REG(0x40023844u)
#define GPIOA_MODER     REG(0x40020000u)
#define GPIOA_PUPDR     REG(0x4002000Cu)
#define GPIOA_IDR       REG(0x40020010u)
#define GPIOC_MODER     REG(0x40020800u)
#define GPIOC_ODR       REG(0x40020814u)
#define ADC1_SR         REG(0x40012000u)
#define ADC1_CR2        REG(0x40012008u)
#define ADC1_SMPR2      REG(0x40012010u)
#define ADC1_SQR3       REG(0x40012034u)
#define ADC1_DR_ADDR    (0x4001204Cu)
#define DMA2_S0CR       REG(0x40026410u)
#define DMA2_S0NDTR     REG(0x40026414u)
#define DMA2_S0PAR      REG(0x40026418u)
#define DMA2_S0M0AR     REG(0x4002641Cu)
#define DMA2_LIFCR      REG(0x40026408u)

#define BUF_LEN 1000u

static volatile uint16_t adc_buf[BUF_LEN];

/**
 * @brief Crude busy-wait delay.
 * @param n Loop iterations (about n/16M seconds at HSI).
 * @return None.
 */
static void delay(volatile uint32_t n) { while (n--) { } }

/**
 * @brief Start (or restart) DMA2 stream 0 for the ADC circular buffer.
 * @return None.
 */
static void dma_start(void)
{
    DMA2_S0CR &= ~1u;                       /* EN = 0 */
    while (DMA2_S0CR & 1u) { }
    DMA2_LIFCR = 0x3Du;                     /* clear stream 0 flags */
    DMA2_S0PAR = ADC1_DR_ADDR;
    DMA2_S0M0AR = (uint32_t)adc_buf;
    DMA2_S0NDTR = BUF_LEN;
    /* CHSEL=0, MSIZE=16, PSIZE=16, MINC, CIRC */
    DMA2_S0CR = (1u << 13) | (1u << 11) | (1u << 10) | (1u << 8);
    DMA2_S0CR |= 1u;                        /* EN */
    ADC1_SR &= ~(1u << 5);                  /* clear OVR */
    ADC1_CR2 |= (1u << 30);                 /* SWSTART again */
}

/**
 * @brief Firmware entry point: clocks, GPIO, ADC, DMA, button loop.
 * @return Never returns.
 */
int main(void)
{
    RCC_AHB1ENR |= (1u << 0) | (1u << 2) | (1u << 22); /* A, C, DMA2 */
    RCC_APB2ENR |= (1u << 8);                          /* ADC1 */

    GPIOA_MODER |= (3u << 2);               /* PA1 analog */
    GPIOA_PUPDR |= (1u << 0);               /* PA0 pull-up (KEY) */
    GPIOC_MODER |= (1u << 26);              /* PC13 output */

    ADC1_SMPR2 = (7u << 3);                 /* ch1: 480 cycles */
    ADC1_SQR3 = 1u;                         /* SQ1 = channel 1 */
    /* DDS + DMA + CONT + ADON */
    ADC1_CR2 = (1u << 9) | (1u << 8) | (1u << 1) | 1u;
    delay(1000);
    dma_start();

    uint32_t dma_on = 1u;
    for (;;) {
        GPIOC_ODR ^= (1u << 13);            /* heartbeat */
        if ((GPIOA_IDR & 1u) == 0u) {       /* KEY pressed */
            if (dma_on) {
                DMA2_S0CR &= ~1u;           /* stall: EN=0, OVR follows */
            } else {
                dma_start();
            }
            dma_on ^= 1u;
            delay(2000000);                 /* debounce + release wait */
        }
        delay(400000);
    }
}

/** @brief Initial stack pointer + reset vector table. */
__attribute__((section(".vectors")))
const uint32_t vectors[] = {
    0x20020000u,                            /* MSP top of 128K SRAM */
    (uint32_t)main,
};
```

- [ ] **Step 2: Write stm32f411.ld**

```
/* Minimal linker script: code in flash, data/bss zero-init skipped
   (firmware uses only zero-initialized statics and stack). */
MEMORY
{
  FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 512K
  RAM (rwx)  : ORIGIN = 0x20000000, LENGTH = 128K
}
SECTIONS
{
  .text : { KEEP(*(.vectors)) *(.text*) *(.rodata*) } > FLASH
  .bss (NOLOAD) : { *(.bss*) *(COMMON) } > RAM
}
```

- [ ] **Step 3: Write Makefile**

```makefile
CC = arm-none-eabi-gcc
CFLAGS = -mcpu=cortex-m4 -mthumb -O2 -Wall -Wextra -ffreestanding \
         -nostdlib -T stm32f411.ld

all: fw.elf

fw.elf: main.c stm32f411.ld
	$(CC) $(CFLAGS) main.c -o $@

flash: fw.elf
	../../.venv/bin/pyocd flash --target stm32f411ce fw.elf

clean:
	rm -f fw.elf
```

Note: the firmware never initializes .data (no initialized statics --
`adc_buf` is bss and the code only ever writes it via DMA), which is
what keeps the startup this small. If you add an initialized global,
add proper startup code.

- [ ] **Step 4: Build and flash**

```bash
cd firmware/blackpill_adc_dma
make            # needs arm-none-eabi-gcc; brew install --cask gcc-arm-embedded
make flash
```

Expected: pyocd reports programming done. On the board: PC13 LED
blinking. Sanity check from repo root:
`.venv/bin/python -m cli monitor --seconds 10`
Expected: `adc_to_sram ACTIVE prog=0x...` with the prog value changing
between lines (DMA moving), fetch/others per flow file.

- [ ] **Step 5: Commit**

```bash
git add firmware
git commit -m "feat(firmware): blackpill adc-dma test firmware with fault button"
```

---

### Task 18: End-to-end anomaly validation -- MILESTONE M2

**Files:**
- Modify: `README.md` (append the M2 checklist results)

- [ ] **Step 1: Run the M2 checklist on hardware**

With firmware running and `python -m cli monitor --seconds 60` active:

1. Baseline: status lines show `adc_to_sram ACTIVE`, `prog` changing.
2. Press KEY once (DMA off). Expected within ~1 s:
   `ANOMALY adc_to_sram: ADC overrun, data lost (block adc1)` and
   shortly after (~0.5 s) `ANOMALY adc_to_sram: DMA stalled (block dma2)`.
3. Status lines: `adc_to_sram idle` (EN==0 makes active_when false),
   `prog` frozen.
4. Press KEY again (DMA restarts). Expected: `ACTIVE` returns, `prog`
   moves, no new anomaly events.
5. Unplug the ST-Link USB mid-run. Expected: `state -> target_lost`.
   Replug: `state -> running` and status lines resume, no crash.

- [ ] **Step 2: Record results in README**

Append to README.md:

```markdown
## M2 validation (date, setup)

- [ ] baseline ACTIVE + moving progress
- [ ] KEY press -> DMA stalled + ADC overrun events
- [ ] flow goes idle while stalled, prog frozen
- [ ] KEY press -> recovery, no spurious events
- [ ] USB unplug/replug -> target_lost -> running, no crash
```

Check every box that passed, note deviations under the list.

- [ ] **Step 3: Full suite + commit**

Run: `.venv/bin/pytest`
Expected: all green.

```bash
git add README.md
git commit -m "docs: record M2 end-to-end validation results"
```

---

## After this plan

- M1 gate result decides pacing: PASS -> write the M3-M4 UI plan (port
  `prototype/ui_proto.py` onto `Engine`), then the M5 packaging plan.
- The prototype stays untouched as the approved visual reference.
