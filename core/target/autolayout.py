"""Auto-layout heuristic: turns a topology's blocks/edges into a
readable first-draft (x, y) arrangement for the M8 layout editor to
open with. It is deliberately not trying to be optimal - the whole
premise of M8 is that the user hand-tunes the result afterward, so
this only has to get close enough that hand-tuning starts from a
sane place rather than a pile of stacked boxes.

The algorithm, in the order it runs:

1. Longest-path layering. Each block is assigned to a column (its
   "layer") equal to one more than the deepest layer among its
   predecessors along edges, so data flows left to right. A graph
   with a cycle has no well-defined longest path, so cycles are
   broken first: a DFS from every block (visited in id order, for
   determinism) finds "back edges" - edges into a block still on the
   current DFS stack - and those edges are ignored for layering
   purposes only (they still exist in the input; this module never
   mutates or drops them).

2. Kind bias. Real hardware diagrams read better when the CPU and
   memories sit at the outer edge of the picture and pin/peripheral
   sources sit at the near edge, regardless of what the raw longest
   path says. So after step 1: blocks of kind "cpu" or "memory" are
   pulled out to the last (rightmost) column, and source blocks
   (no incoming edges) of kind "pin" or "peripheral" are pinned to
   column 0. Columns that end up empty after this shuffle are then
   compacted out, so the picture never has a dead gap in the middle.

3. Row ordering within each column. A two-pass barycenter sweep
   (left-to-right using predecessor positions, then right-to-left
   using successor positions) orders blocks within a column near the
   average row of the neighbors they connect to, which is a cheap
   and standard way to cut down edge crossings. Ties, and blocks with
   no already-placed neighbor to average, fall back to sorting by
   block id, which is what keeps the whole function deterministic:
   nothing here ever depends on dict/set iteration order.

4. Positions. Column x-offsets accumulate the widest block in each
   preceding column plus col_gap, so columns never overlap even when
   block widths vary a lot within one topology. Within a column, rows
   stack top to bottom using each block's own height plus row_gap.
   Every coordinate is then snapped to the nearest multiple of grid.

Only core/ stdlib is used here - no third-party imports."""
from typing import Dict, List, Tuple

from .topology import Block, Edge

_DEFAULT_W = 140
_DEFAULT_H = 80


def _size(block: Block) -> Tuple[int, int]:
    return (block.w if block.w is not None else _DEFAULT_W,
            block.h if block.h is not None else _DEFAULT_H)


def _snap(value: float, grid: int) -> int:
    return int(round(value / grid)) * grid


def _find_back_edges(ids: List[str],
                     out_edges: Dict[str, List[Tuple[str, str]]]
                     ) -> set:
    # Standard DFS white/gray/black coloring. out_edges[u] is a list
    # of (v, edge_key) already sorted by v (then edge_key) so the
    # traversal order - and therefore which edges get flagged as back
    # edges when there is a choice - is fixed by block id, not by
    # whatever order the caller happened to pass blocks/edges in.
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {i: WHITE for i in ids}
    back_edges = set()

    def visit(u: str) -> None:
        color[u] = GRAY
        for v, key in out_edges.get(u, ()):
            if color[v] == GRAY:
                back_edges.add(key)
            elif color[v] == WHITE:
                visit(v)
        color[u] = BLACK

    for i in ids:
        if color[i] == WHITE:
            visit(i)
    return back_edges


def _longest_path_layers(ids: List[str],
                         dag_out: Dict[str, List[str]],
                         dag_in: Dict[str, List[str]]) -> Dict[str, int]:
    # Kahn's algorithm over the back-edge-free DAG, processing the
    # frontier in id order each round so the result does not depend
    # on input ordering. layer(v) = 1 + max(layer(u) for u -> v); a
    # block with no (remaining) predecessor is layer 0.
    indegree = {i: len(dag_in.get(i, ())) for i in ids}
    layer = {i: 0 for i in ids}
    frontier = sorted(i for i in ids if indegree[i] == 0)
    seen = 0
    while frontier:
        frontier.sort()
        u = frontier.pop(0)
        seen += 1
        for v in dag_out.get(u, ()):
            layer[v] = max(layer[v], layer[u] + 1)
            indegree[v] -= 1
            if indegree[v] == 0:
                frontier.append(v)
    if seen != len(ids):
        # Defensive only: _find_back_edges should already guarantee
        # dag_out/dag_in are acyclic, so this would mean a bug above
        # rather than a real cyclic input reaching this far.
        raise RuntimeError("autolayout: DAG reduction left a cycle")
    return layer


def _apply_kind_bias(blocks: Dict[str, Block], layer: Dict[str, int],
                     indegree: Dict[str, int]) -> Dict[str, int]:
    layer = dict(layer)
    max_layer = max(layer.values()) if layer else 0
    for bid, block in blocks.items():
        if block.kind in ("pin", "peripheral") and indegree[bid] == 0:
            layer[bid] = 0
        elif block.kind in ("cpu", "memory"):
            layer[bid] = max_layer
    # Compact away any column left empty by the shuffle above so the
    # drawing has no dead gap, while preserving relative left-right
    # order (a monotonic remap of the sorted distinct values used).
    used = sorted(set(layer.values()))
    remap = {old: new for new, old in enumerate(used)}
    return {bid: remap[l] for bid, l in layer.items()}


def _barycenter_pass(columns: Dict[int, List[str]],
                     row: Dict[str, int],
                     neighbors: Dict[str, List[str]],
                     col_order: List[int]) -> None:
    for col in col_order:
        members = columns[col]
        if len(members) <= 1:
            continue

        def key(bid: str):
            rows = [row[n] for n in neighbors.get(bid, ())
                   if n in row]
            if rows:
                return (0, sum(rows) / len(rows), bid)
            return (1, 0.0, bid)

        members.sort(key=key)
        for idx, bid in enumerate(members):
            row[bid] = idx


def auto_layout(blocks: "List[Block]", edges: "List[Edge]",
                grid: int = 10, col_gap: int = 120, row_gap: int = 60
                ) -> "Dict[str, Tuple[int, int]]":
    by_id: Dict[str, Block] = {b.id: b for b in blocks}
    ids = sorted(by_id)
    if not ids:
        return {}

    out_edges: Dict[str, List[Tuple[str, str]]] = {i: [] for i in ids}
    for e in edges:
        out_edges[e.src].append((e.dst, e.id))
    for lst in out_edges.values():
        lst.sort()

    back_edges = _find_back_edges(ids, out_edges)

    dag_out: Dict[str, List[str]] = {i: [] for i in ids}
    dag_in: Dict[str, List[str]] = {i: [] for i in ids}
    indegree: Dict[str, int] = {i: 0 for i in ids}
    for e in edges:
        if e.id in back_edges:
            continue
        dag_out[e.src].append(e.dst)
        dag_in[e.dst].append(e.src)
        indegree[e.dst] += 1
    for lst in dag_out.values():
        lst.sort()
    for lst in dag_in.values():
        lst.sort()

    layer = _longest_path_layers(ids, dag_out, dag_in)
    layer = _apply_kind_bias(by_id, layer, indegree)

    columns: Dict[int, List[str]] = {}
    for bid in ids:
        columns.setdefault(layer[bid], []).append(bid)
    num_cols = max(columns) + 1

    # Undirected neighbor lists (predecessors + successors, all
    # edges - not just the DAG ones) drive the barycenter sweep;
    # crossing minimization has no need to respect edge direction or
    # the cycle-breaking done for layering.
    neighbors: Dict[str, List[str]] = {i: [] for i in ids}
    for e in edges:
        neighbors[e.src].append(e.dst)
        neighbors[e.dst].append(e.src)
    for lst in neighbors.values():
        lst.sort()

    row: Dict[str, int] = {}
    for col in range(num_cols):
        for idx, bid in enumerate(sorted(columns.get(col, []))):
            row[bid] = idx

    _barycenter_pass(columns, row, neighbors, list(range(num_cols)))
    _barycenter_pass(columns, row, neighbors,
                     list(range(num_cols - 1, -1, -1)))

    col_x: Dict[int, int] = {0: 0}
    col_width: Dict[int, int] = {}
    for col in range(num_cols):
        members = columns.get(col, [])
        widths = [_size(by_id[bid])[0] for bid in members]
        col_width[col] = max(widths) if widths else _DEFAULT_W
    for col in range(1, num_cols):
        col_x[col] = col_x[col - 1] + col_width[col - 1] + col_gap

    pos: Dict[str, Tuple[int, int]] = {}
    for col, members in columns.items():
        ordered = sorted(members, key=lambda bid: row[bid])
        y = 0
        for bid in ordered:
            _, h = _size(by_id[bid])
            pos[bid] = (_snap(col_x[col], grid), _snap(y, grid))
            y += h + row_gap
    return pos
