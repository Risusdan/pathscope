# PathScope M8: layout edit mode

Status: approved design (requirements interview 2026-09-15).

## 1. Problem and decision

Authoring a target's diagram means hand-editing absolute coordinates
in `topology.yaml` and restarting to see the result - the single
worst friction in the "add your own target" story. M8 adds an edit
mode to the Data Path page: drag the geometry, save it back to the
yaml. **Geometry only** - structure (adding blocks/edges, names,
kinds, ports) stays in hand-written yaml, which was never the
painful part.

## 2. Edit mode

- Explicit toolbar toggle "Edit layout", checkable, Data Path page
  only. Entering shows a clear visual cue (button highlight plus a
  dashed border around the diagram viewport). Switching to the Scope
  page exits edit mode automatically.
- In edit mode, mouse gestures route to layout editing exclusively:
  inspector block/edge clicks are disabled. Flow animation and live
  values keep rendering - only input routing changes.
- Normal mode is completely unchanged; accidental layout damage
  during ordinary debugging is impossible by construction.

## 3. Editable geometry

| element | gestures |
|---|---|
| block | drag to move; corner handle to resize |
| edge waypoint | drag to move; double-click an edge segment to insert; select + Delete to remove |
| legend | drag to move |

- Grid snap: 10 scene units on every drag/resize; hold Shift for
  1-unit free placement. Snap applies only to elements the user
  moves - untouched yaml values are preserved exactly.
- Edges without a `points:` list show only their endpoints; the
  first double-click converts the edge to an explicit waypoint path.
  Untouched edges never gain a `points:` entry.
- No orthogonality enforcement and no implicit collinear-point
  merging - the grid keeps lines straight in practice, Delete
  removes points deliberately.
- Ports are not separately draggable in v1: anchors follow their
  block with auto-computed positions.
- Single-element drag only; multi-select is backlog.

## 4. Undo

- Lightweight undo stack, edit-mode-local: every completed action
  (block move/resize, waypoint move/insert/delete, legend move)
  pushes a geometry snapshot; Cmd/Ctrl+Z pops. No redo.
- "Revert to saved" button reloads geometry from the yaml on disk.
- The stack clears on save and on mode exit.

## 5. Saving (yaml write-back)

- Explicit "Save layout" button in edit mode; no save-on-drag.
  Leaving edit mode with unsaved changes keeps them in memory and
  shows an inline unsaved marker (never a dialog); re-entering the
  mode continues where the user left off.
- Write-back is a surgical text patch, not a re-serialization:
  - the `layout:` section is regenerated wholesale (it is
    machine-shaped; regeneration keeps the aligned pretty-printed
    column format), including the new optional `legend: {x, y}` key
    once the legend has been moved;
  - a moved edge's `points:` value is rewritten in place on its own
    line, located by the edge's from/to/label identity;
  - every other byte of the file - comments, blocks section, field
    order, hand formatting - is preserved untouched.
- Atomic write (temp file + rename). Before replacing the file, the
  generated content is re-parsed with the normal topology loader; a
  parse failure aborts the save and reports inline, leaving the
  original file intact.
- Result: a layout change produces a git diff that shows only real
  coordinate changes.

## 6. Auto-layout (first-draft seed only)

- "Auto-layout" button, available in edit mode at any time. It
  replaces all block positions and clears explicit edge paths -
  destructive to hand tuning, but a single undo restores everything,
  so no confirmation ceremony.
- Implementation is a built-in layered heuristic, no new dependency:
  topological layering along edge direction into columns, kind-aware
  placement (cpu and memory kinds toward the outside, interconnect
  in the middle), barycenter ordering within a column to reduce
  crossings, all positions grid-aligned. Quality bar: a readable
  first draft the user immediately hand-tunes - which is the whole
  premise of this milestone.
- Auto-layout never runs implicitly. Loading a target with no
  `layout:` section renders at defaults and the user chooses to
  seed.

## 7. Out of scope (backlog)

- Structural editing (add/remove blocks and edges) in the GUI.
- Smart alignment guides (Figma-style neighbor snapping).
- Multi-select group drag.
- Draggable port anchors.
- Redo.

## 8. Validation

- Unit: snap arithmetic; undo stack semantics; write-back on a
  fixture yaml rich in comments and odd formatting - asserting
  byte-identical preservation outside the touched regions, and exact
  expected bytes within them; atomic-write failure path leaves the
  original intact; auto-layout produces non-overlapping,
  grid-aligned, kind-plausible columns on the f411 topology.
- UI (offscreen): mode toggle routes gestures and suppresses
  inspector clicks; drag moves a block and marks unsaved; waypoint
  insert/delete; legend position round-trips through save and
  reload; mode exits on tab switch with changes retained.
- Manual gate: hand-tune the f411 diagram end to end, save, restart,
  confirm the diagram reproduces exactly and the git diff is clean.
