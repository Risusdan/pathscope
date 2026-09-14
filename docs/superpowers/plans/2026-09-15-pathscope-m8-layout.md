# PathScope M8 Layout Edit Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Data Path edit mode: drag blocks/waypoints/legend, snap
to grid, undo, and save the geometry back to topology.yaml as a
surgical text patch.

**Architecture:** Two pure modules carry the hard logic - a yaml
write-back patcher (`core/target/layout_io.py`) and a layered
auto-layout heuristic (`core/target/autolayout.py`) - both fully
unit-testable without Qt. The UI side adds an input-routing mode to
the existing diagram items (drag/snap/handles) and a MainWindow
toolbar toggle with an edit-local undo stack.

**Tech Stack:** Python 3.9+, PySide6 QGraphicsScene machinery
already in ui/diagram/, PyYAML (already a dependency) for the
validation re-parse only - the patcher itself is pure text.

**Spec:** docs/superpowers/specs/2026-09-15-pathscope-m8-layout-design.md

## Global Constraints

- Geometry ONLY: never add/remove blocks, edges, ports or rename
  anything in the yaml (spec section 1).
- Grid = 10 scene units; Shift = 1-unit free placement; snap applies
  only to elements the user moves (spec section 3).
- Write-back preserves every byte outside the regenerated `layout:`
  section and the individually rewritten `points:` lines (spec
  section 5); atomic write; re-parse with the normal loader before
  replacing the file; failure leaves the original intact.
- Inline feedback only - never a QMessageBox/dialog.
- Undo: edit-local snapshot stack, Cmd/Ctrl+Z, no redo (spec 4).
- Auto-layout: built-in heuristic, no new dependency, button-only,
  never implicit (spec 6).
- Edit mode exists on the Data Path page only; switching tabs exits
  it; unsaved changes survive in memory with an inline marker.
- core/ stays stdlib+PyYAML (both already there); pure ASCII; light
  theme; conventional commits, no AI attribution.
- Out of scope (do not build): structural editing, alignment guides,
  multi-select, draggable ports, redo (spec 7).

## File Structure

- Create `core/target/layout_io.py` - write-back patcher +
  save-with-validation. Pure text in, text out.
- Create `core/target/autolayout.py` - layered heuristic. Pure
  data in, positions out.
- Modify `ui/diagram/scene.py` - DiagramState gains edit-mode fields.
- Modify `ui/diagram/items.py` - BlockItem drag/resize, WireItem
  waypoint handles, LegendItem drag; all gated on the state flag.
- Modify `ui/main_window.py` - toolbar toggle, Save/Revert/Auto
  buttons, undo stack, unsaved marker, tab-exit hook.
- Tests: `tests/test_layout_io.py`, `tests/test_autolayout.py`,
  `tests/ui/test_layout_edit.py`; fixture
  `tests/fixtures/layout_roundtrip.topology.yaml`.

---

### Task 1: yaml write-back patcher (`core/target/layout_io.py`)

**Files:**
- Create: `core/target/layout_io.py`
- Create: `tests/fixtures/layout_roundtrip.topology.yaml`
- Test: `tests/test_layout_io.py`

**Interfaces:**
- Consumes: nothing from other tasks. `core/target/topology.py`'s
  `load_topology(path, model)` is used only inside `save_layout` for
  validation - via a caller-supplied `validate` callable to keep this
  module model-free (see signature).
- Produces (later tasks rely on these exact names):

```python
class LayoutPatchError(Exception): ...

def patch_layout_text(original: str,
                      blocks: "OrderedDict[str, Tuple[int, int, int, int]]",
                      edge_points: "Dict[Tuple[str, str, str], Optional[List[Tuple[int, int]]]]",
                      legend: "Optional[Tuple[int, int]]") -> str
    # blocks: id -> (x, y, w, h), COMPLETE set, in desired emission order.
    # edge_points key: (from_id, to_id, label or "") identifying the edge
    #   in the file; value None means "do not touch this edge's line",
    #   a list means rewrite that edge's points: value in place
    #   (the edge MUST already have a points: entry or the edge's line
    #   is rewritten to include one - see step 5 rules).
    # legend: None = emit no legend key (and remove an existing one? NO -
    #   None means leave any existing legend line untouched);
    #   (x, y) = emit/replace layout.legend.

def save_layout(path: str, blocks, edge_points, legend,
                validate: "Callable[[str], None]") -> None
    # patch, run validate(tmp_path) (raises on bad output), atomic
    # os.replace; any failure -> LayoutPatchError, original untouched.
```

Patch rules (the whole trick - encode exactly):
- The `layout:` section (from the line matching `^layout:` to the
  next top-level key or EOF) is REPLACED by a regenerated section:
  one line per block id in the given order, aligned columns in the
  existing house style (`  adc1:  {x: 70,  y: 280, w: 140, h: 80}`),
  plus `  legend: {x: N, y: N}` last when legend is not None (or
  carrying over the previous legend line's values when legend is
  None and one existed).
- An edge line is located by regex-matching its `from:`/`to:`/
  `label:` scalars inside the `edges:` block (single line or the
  two-line wrapped form the file uses - see the fixture); only the
  `points: [...]` bracket payload on that entry is replaced (or
  appended before the closing `}` if the entry had none). Everything
  else on those lines - key order, spacing, `when_select`, ports -
  stays byte-identical.
- Every byte outside those regions is copied through verbatim.

- [ ] **Step 1: build the fixture** - a topology.yaml with: a header
  comment, an inline comment on a block line, a blocks section with
  odd spacing, two edges WITH points (one single-line, one wrapped
  across two lines), one edge WITHOUT points, a layout section, no
  legend key. Copy targets/f411/f411.topology.yaml as the base and
  add the comment/spacing traps by hand.

- [ ] **Step 2: failing tests**

```python
import pathlib
import pytest
from collections import OrderedDict
from core.target.layout_io import (LayoutPatchError, patch_layout_text,
                                   save_layout)

FIX = pathlib.Path("tests/fixtures/layout_roundtrip.topology.yaml")

def _orig():
    return FIX.read_text()

def _blocks_from(text):
    # helper: parse the current layout section values with PyYAML so
    # tests express "same as before except adc1 moved"
    import yaml
    doc = yaml.safe_load(text)
    return OrderedDict((k, (v["x"], v["y"], v["w"], v["h"]))
                       for k, v in doc["layout"].items())

def test_untouched_file_regions_are_byte_identical():
    orig = _orig()
    out = patch_layout_text(orig, _blocks_from(orig), {}, None)
    head_orig = orig.split("layout:")[0]
    head_out = out.split("layout:")[0]
    assert head_out == head_orig          # every byte before layout:

def test_moved_block_changes_only_its_layout_line():
    orig = _orig()
    blocks = _blocks_from(orig)
    x, y, w, h = blocks["adc1"]
    blocks["adc1"] = (x + 10, y, w, h)
    out = patch_layout_text(orig, blocks, {}, None)
    changed = [l for l in out.splitlines() if "adc1" in l and "x:" in l]
    assert len(changed) == 1 and ("x: %d" % (x + 10)) in changed[0]
    import yaml
    assert yaml.safe_load(out)["layout"]["adc1"]["x"] == x + 10

def test_edge_points_rewritten_in_place_preserving_rest_of_line():
    orig = _orig()
    key = ("adc1", "mux0", "DR")          # match the fixture's edge
    out = patch_layout_text(orig, _blocks_from(orig),
                            {key: [(1, 2), (3, 4)]}, None)
    line = next(l for l in out.splitlines()
                if "adc1" in l and "points" in l)
    assert "[[1, 2], [3, 4]]" in line
    # when_select/label spacing on the same entry untouched:
    assert out.count("when_select") == orig.count("when_select")

def test_edge_without_points_gains_entry_only_when_asked():
    orig = _orig()
    key = ("mux0", "dma2", "S0")          # fixture's pointless edge
    out = patch_layout_text(orig, _blocks_from(orig),
                            {key: [(5, 5)]}, None)
    import yaml
    doc = yaml.safe_load(out)
    edge = next(e for e in doc["edges"]
                if e["from"] == "mux0" and e["to"] == "dma2")
    assert edge["points"] == [[5, 5]]

def test_legend_key_emitted_and_replaced():
    orig = _orig()
    out = patch_layout_text(orig, _blocks_from(orig), {}, (700, 400))
    assert "legend:" in out.split("layout:")[1]
    out2 = patch_layout_text(out, _blocks_from(out), {}, (100, 100))
    assert "x: 100" in [l for l in out2.splitlines()
                        if "legend" in l][0]

def test_save_layout_atomic_and_validated(tmp_path):
    p = tmp_path / "t.topology.yaml"
    p.write_text(_orig())
    def bad_validate(path):
        raise ValueError("boom")
    with pytest.raises(LayoutPatchError):
        save_layout(str(p), _blocks_from(_orig()), {}, None,
                    validate=bad_validate)
    assert p.read_text() == _orig()       # original intact
```

- [ ] **Step 3:** run `pytest tests/test_layout_io.py -q` -> FAIL
  (module missing).
- [ ] **Step 4:** implement. Pure string processing: split lines,
  find the layout region by regex `^layout:\s*$`, regenerate with
  column alignment computed from the longest id; edge lines located
  by scanning the `edges:` list items (a `- {` opener through its
  balanced closing `}` - the fixture's wrapped form spans two lines;
  handle both by joining an item's physical lines for matching but
  editing only the physical line containing `points:` or the one
  with the closing brace).
- [ ] **Step 5:** run the file's tests, then the FULL suite.
- [ ] **Step 6:** commit
  `feat(core): surgical layout write-back for topology yaml`.

---

### Task 2: auto-layout heuristic (`core/target/autolayout.py`)

**Files:**
- Create: `core/target/autolayout.py`
- Test: `tests/test_autolayout.py`

**Interfaces:**
- Consumes: `core/target/topology.py` dataclasses `Block` (fields
  id, kind, w, h) and `Edge` (from_id/to_id via the existing field
  names - READ topology.py first and use its real attribute names).
- Produces:

```python
def auto_layout(blocks: "List[Block]", edges: "List[Edge]",
                grid: int = 10,
                col_gap: int = 120, row_gap: int = 60
                ) -> "Dict[str, Tuple[int, int]]"
    # returns id -> (x, y); w/h come from each block (default
    # 140x80 when None). Never mutates inputs.
```

Algorithm (encode as written):
1. Longest-path topological layering along edge direction (cycles
   broken by ignoring back-edges found via DFS).
2. Kind bias: blocks of kind "cpu" and "memory" get pulled to the
   outermost columns (cpu to the last column, memory alongside it;
   pin/peripheral sources to column 0) by +/- layer adjustment after
   step 1.
3. Within a column, order by barycenter of already-placed neighbor
   rows; two passes (left-to-right then right-to-left).
4. Positions: x = col * (max_block_w_in_col + col_gap), y = running
   offset + row_gap, everything rounded to the grid.

- [ ] **Step 1: failing tests**

```python
from core.target.autolayout import auto_layout
from core.target.registers import RegisterModel
from core.target.topology import load_topology

def _f411():
    model = RegisterModel.from_svd("targets/f411/STM32F411.svd")
    return load_topology("targets/f411/f411.topology.yaml", model)

def test_no_overlaps_and_grid_aligned():
    topo = _f411()
    pos = auto_layout(list(topo.blocks.values()), topo.edges)
    rects = []
    for b in topo.blocks.values():
        x, y = pos[b.id]
        assert x % 10 == 0 and y % 10 == 0
        w, h = (b.w or 140), (b.h or 80)
        for (ox, oy, ow, oh) in rects:
            assert (x >= ox + ow or ox >= x + w or
                    y >= oy + oh or oy >= y + h), "overlap"
        rects.append((x, y, w, h))

def test_kind_columns_plausible():
    topo = _f411()
    pos = auto_layout(list(topo.blocks.values()), topo.edges)
    xs = {b.id: pos[b.id][0] for b in topo.blocks.values()}
    kinds = {b.id: b.kind for b in topo.blocks.values()}
    mem_x = [x for i, x in xs.items() if kinds[i] == "memory"]
    periph_x = [x for i, x in xs.items() if kinds[i] == "peripheral"]
    assert min(mem_x) > max(periph_x)   # memories right of peripherals
```

(Adjust `RegisterModel.from_svd`/`load_topology` call shapes to the
real signatures in core/target/ - read them first; the assertion
content is the requirement.)

- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** file
  tests + full suite green. **Step 5:** commit
  `feat(core): layered kind-aware auto-layout heuristic`.

---

### Task 3: edit-mode input routing on diagram items

**Files:**
- Modify: `ui/diagram/scene.py` (DiagramState), `ui/diagram/items.py`
- Test: `tests/ui/test_layout_edit.py` (new)

**Interfaces:**
- Consumes: `DiagramState` (scene.py:~30), `BlockItem`
  (items.py:61, positioned via `setPos(block.x, block.y)`),
  `LegendItem` (items.py:253, fixed `setPos(700, 402)`).
- Produces (Task 5 relies on these):

```python
# DiagramState new fields:
state.edit_mode: bool = False
state.on_geometry_changed: Callable[[], None]   # any drag/resize/
    # waypoint change calls this once per completed gesture (mouse
    # release), AFTER applying it - MainWindow uses it for undo
    # snapshots and the unsaved marker.

# BlockItem:
item.set_editable(on: bool)     # toggles ItemIsMovable + handle
item.geometry() -> Tuple[int, int, int, int]      # x, y, w, h now
item.apply_geometry(x, y, w, h)                   # undo restore path

# LegendItem: same set_editable/geometry/apply_geometry contract
# (geometry returns (x, y, 0, 0)).
```

Behavior:
- `set_editable(True)`: `setFlag(ItemIsMovable)`; `itemChange` on
  `ItemPositionChange` snaps to grid 10 (1 when Shift held - query
  `QApplication.keyboardModifiers()`); mouseReleaseEvent writes the
  final snapped pos back into `self.block.x/y` and fires
  `state.on_geometry_changed()`.
- Resize: an 8x8 bottom-right corner handle child item, visible only
  in edit mode; dragging it changes `block.w/h` (min 30x24, snapped),
  triggers `prepareGeometryChange` + repaint, fires the callback on
  release.
- In edit mode `BlockItem.mousePressEvent` does NOT call the
  inspector callback (`state.on_block_clicked`); WireItem clicks do
  not select flows.
- Nothing here touches MainWindow yet - the tests drive items
  directly on a scene built by `build_scene`.

- [ ] **Step 1: failing tests** (offscreen, pytest-qt):

```python
def test_edit_mode_drag_snaps_and_updates_block(qtbot):
    # build_scene on the f411 topology; grab a BlockItem
    ...
    state.edit_mode = True; item.set_editable(True)
    item.setPos(103, 118)          # programmatic move, snap on release
    item.mouseReleaseEvent(_fake_release())
    assert (item.block.x, item.block.y) == (100, 120)

def test_normal_mode_click_hits_inspector_edit_mode_does_not(qtbot):
    clicks = []
    state.on_block_clicked = clicks.append
    # normal: press -> one callback; edit: press -> zero
```

(Write the real versions against the actual build_scene/DiagramState
APIs - read scene.py first; the two behaviors above are the binding
requirements, plus a resize test asserting w/h min-clamp and snap.)

- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** suite
  green. **Step 5:** commit
  `feat(ui): edit-mode drag, snap and resize on diagram items`.

---

### Task 4: waypoint handles and legend drag

**Files:**
- Modify: `ui/diagram/items.py` (WireItem + a new small
  `WaypointHandle(QGraphicsItem)` class), `ui/diagram/scene.py` only
  if a state field is missing.
- Test: `tests/ui/test_layout_edit.py` (extend)

**Interfaces:**
- Consumes: Task 3's `set_editable`/`on_geometry_changed` pattern,
  `Edge.points` (list of (x, y) tuples, topology.py:43).
- Produces: `WireItem.set_editable(on)`, `WireItem.edge_key() ->
  Tuple[str, str, str]` ((from, to, label or "") - Task 5 feeds this
  to layout_io), `WireItem.apply_points(points)` for undo.

Behavior (spec section 3):
- Edit mode shows an 8x8 square `WaypointHandle` per existing point;
  drag = move (snap 10 / Shift 1), release updates `edge.points[i]`
  and fires the callback.
- Double-click on the wire inserts a waypoint at the clicked segment
  position (snapped), converting a pointless edge to an explicit
  path on first insert.
- A selected handle + Delete removes that point (keyPressEvent on
  the handle); no implicit collinear merging.
- LegendItem: movable in edit mode; its position participates in
  geometry()/apply_geometry (Task 3 contract already declares it).

- [ ] Steps: failing tests first - handle drag updates
  edge.points and snaps; double-click on a pointless edge creates
  points == [clicked snapped pos]; Delete removes; legend drag
  updates its geometry and fires the callback. Then implement, full
  suite green, commit
  `feat(ui): waypoint editing and draggable legend`.

---

### Task 5: MainWindow integration - mode, undo, save

**Files:**
- Modify: `ui/main_window.py`
- Test: `tests/ui/test_layout_edit.py` (extend),
  `tests/ui/test_main_window.py` (only if toolbar assertions break)

**Interfaces:**
- Consumes: Task 1 `save_layout`/`patch_layout_text`, Task 2
  `auto_layout`, Tasks 3-4 item contracts, the engine's target dir
  (find how Engine exposes the loaded target path - read
  core/engine/core.py's load; if the topology yaml path is not
  retained, add `Engine.topology_path: str` set during load as part
  of THIS task, one attribute assignment plus a docstring line).
- Produces (UI surface, test-support):
  `win.edit_layout_btn` (checkable QToolButton in the toolbar next
  to the page switch), and inside edit mode a small header strip on
  the Data Path page: `win.layout_save_btn`, `win.layout_revert_btn`,
  `win.layout_auto_btn`, `win.layout_dirty_label`.

Behavior:
- Toggle on: sets `diagram_state.edit_mode`, calls
  `set_editable(True)` on every block/wire/legend item, dashed
  border cue on the viewport (a stylesheet border on the view),
  shows the strip. Toggle off: reverse; unsaved edits stay in the
  items/topology objects; `layout_dirty_label` ("unsaved layout
  changes") remains visible OUTSIDE edit mode next to the toolbar
  button until saved.
- Undo: `on_geometry_changed` pushes `(item, old_geometry)` captured
  BEFORE the gesture (capture on gesture start via the same callback
  pattern - implement as pre/post pair or snapshot-the-world: a
  simple world snapshot dict {id: geometry} pushed per gesture is
  acceptable and simpler; state it in the code comment). Cmd/Ctrl+Z
  in edit mode pops and applies via apply_geometry/apply_points.
  Stack clears on save and on mode exit.
- Save: collects every block's geometry (ordered as in
  topology.blocks), every TOUCHED wire's points (track a dirty set
  of edge keys), legend position if moved, and calls `save_layout`
  with `validate=lambda p: load_topology(p, engine.model)`;
  LayoutPatchError renders in the existing inline error surface
  (statusBar message, matching _toggle_halt's precedent).
- Revert: reloads the topology file, re-applies geometry to items
  (apply_geometry/apply_points), clears dirty state.
- Auto-layout: `auto_layout(...)` then apply positions to items and
  CLEAR every edge's explicit points (spec 6), one undo snapshot
  first so a single Z restores.
- Tab switch to Scope while in edit mode: toggle the button off
  programmatically (existing `_on_tab_changed` hook).

- [ ] Steps: failing tests - toggle wires editability + suppresses
  inspector; a drag marks dirty; Z undoes; save writes the yaml
  (tmp copy of the f411 target dir) and a reload reproduces
  positions; revert restores; auto-layout moves blocks and one undo
  restores; tab switch exits mode with dirty label persisting. Then
  implement, full suite green, commit
  `feat(ui): layout edit mode toggle, undo and save`.

---

### Task 6: docs

**Files:**
- Modify: `README.md` ("Adding your own target" section)
- Test: none (prose)

- [ ] Add one short paragraph: coordinates need not be hand-written -
  enter Edit layout, optionally press Auto-layout for a first draft,
  drag to taste, Save layout writes topology.yaml back with a clean
  diff. Match the README's plain tone; pure ASCII.
- [ ] Full suite green (unchanged), commit
  `docs: layout edit mode in the target authoring story`.

---

## Self-review notes

- Spec coverage: section 2 -> T3+T5; 3 -> T3+T4; 4 -> T5; 5 -> T1+T5;
  6 -> T2+T5; 7 respected as out-of-scope; 8's unit surface -> T1/T2
  tests, UI surface -> T3-T5 tests, manual gate -> user after merge.
- Type consistency: geometry tuple (x, y, w, h) uniform across
  BlockItem/LegendItem/undo/layout_io; edge key (from, to, label or
  "") uniform across WireItem.edge_key and patch_layout_text.
- Known slack, deliberate: Tasks 3-5's test snippets are requirement
  sketches (real APIs must be read first) - same M6/M7 precedent;
  reviewers watch the interpretation.
