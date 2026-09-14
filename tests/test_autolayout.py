from core.target.autolayout import auto_layout
from core.target.registers import RegisterModel
from core.target.topology import Block, Edge, load_topology


def _f411():
    model = RegisterModel.from_svd("targets/f411/STM32F411.svd")
    return load_topology("targets/f411/f411.topology.yaml", model)


def _rects(topo, pos):
    rects = []
    for b in topo.blocks.values():
        x, y = pos[b.id]
        w, h = (b.w or 140), (b.h or 80)
        rects.append((x, y, w, h))
    return rects


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


def test_deterministic_across_runs():
    topo = _f411()
    blocks = list(topo.blocks.values())
    pos1 = auto_layout(blocks, topo.edges)
    pos2 = auto_layout(blocks, topo.edges)
    assert pos1 == pos2


def test_never_mutates_inputs():
    topo = _f411()
    blocks = list(topo.blocks.values())
    before = [(b.id, b.x, b.y, b.w, b.h) for b in blocks]
    auto_layout(blocks, topo.edges)
    after = [(b.id, b.x, b.y, b.w, b.h) for b in blocks]
    assert before == after


def test_default_size_used_when_missing():
    # No w/h set on either block: falls back to 140x80 for spacing
    # purposes, and the two must not overlap.
    a = Block(id="a", kind="peripheral", title="A")
    b = Block(id="b", kind="peripheral", title="B")
    edges = [Edge(id="a->b#0", src="a", dst="b")]
    pos = auto_layout([a, b], edges)
    ax, ay = pos["a"]
    bx, by = pos["b"]
    assert ax % 10 == 0 and ay % 10 == 0
    assert bx % 10 == 0 and by % 10 == 0
    # a feeds b, so b must land in a later (strictly greater x) column.
    assert bx > ax


def test_cycle_is_broken_without_crashing():
    # A tight 3-cycle plus a tail hanging off it: the DFS back-edge
    # break must still produce a full, grid-aligned, non-overlapping
    # layout for every block - no infinite recursion, no KeyError.
    a = Block(id="a", kind="peripheral", title="A")
    b = Block(id="b", kind="mux", title="B")
    c = Block(id="c", kind="dma", title="C")
    d = Block(id="d", kind="memory", title="D")
    edges = [
        Edge(id="a->b#0", src="a", dst="b"),
        Edge(id="b->c#1", src="b", dst="c"),
        Edge(id="c->a#2", src="c", dst="a"),   # closes the cycle
        Edge(id="c->d#3", src="c", dst="d"),
    ]
    pos = auto_layout([a, b, c, d], edges)
    assert set(pos) == {"a", "b", "c", "d"}
    for (x, y) in pos.values():
        assert x % 10 == 0 and y % 10 == 0


def test_pin_and_peripheral_sources_land_in_first_column():
    topo = _f411()
    pos = auto_layout(list(topo.blocks.values()), topo.edges)
    # pa1 (pin) and tim1 (peripheral) have no incoming edges in the
    # f411 fixture, so the kind bias should pin both to column 0.
    assert pos["pa1"][0] == 0
    assert pos["tim1"][0] == 0
