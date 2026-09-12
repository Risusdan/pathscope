from core.adapter.mock import MockAdapter
from core.engine.core import Engine
from ui.diagram.scene import DiagramState, build_scene


def test_scene_builds_from_f411_topology(qtbot):
    engine = Engine.load("targets/f411", MockAdapter({}))
    state = DiagramState()
    scene, blocks, wires = build_scene(engine.topology, state)
    assert set(blocks) == set(engine.topology.blocks)
    assert len(wires) == len(engine.topology.edges)
    # laid-out block honors yaml coordinates
    assert blocks["adc1"].pos().x() == 70
    # edge with explicit points uses them
    bent = [w for w in wires.values() if w.edge.points]
    assert bent and bent[0].pts[0].x() == bent[0].edge.points[0][0]
    # straight fallback produced two-point routes for the rest
    straight = [w for w in wires.values() if not w.edge.points]
    assert all(len(w.pts) == 2 for w in straight)


def test_badge_access_is_sparse_safe(qtbot):
    engine = Engine.load("targets/f411", MockAdapter({}))
    state = DiagramState()
    scene, blocks, wires = build_scene(engine.topology, state)
    for item in blocks.values():
        item.update()          # paint path must not KeyError on badges
