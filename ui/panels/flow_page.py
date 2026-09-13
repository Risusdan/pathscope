"""Flow inspector panel: shows which activities (data-path "flows")
pass through a clicked edge, live ACTIVE/idle state, progress and
per-rule anomaly dots.

Ported from prototype/ui_proto.py's FlowPage, with one structural
change: the prototype's hand-written `FLOWS` stub dict (edges/blocks
per flow, hardcoded by hand) is gone. `build_flow_edge_map()` below
derives the same "activity -> edge id set" mapping live from
topology + flowspec - this is the exact algorithm task-9 already used
for `MainWindow._flow_edges` (T9's docstring on that method explains
the "longest straight segment"-free part of the rule: an activity's
edge set is every edge whose (src, dst) is a consecutive pair in the
activity's `path`). `MainWindow` and `FlowPage` each hold their own
copy of this map (built from the same immutable topology/flowspec, so
they never disagree) rather than one handing the other a private
attribute - `FlowPage(engine)`'s constructor signature only takes the
engine, with no other constructor arguments.

The prototype's three-argument `show_edge(eid, flow_ids, auto_pick)`
(Main.select_edge computed flow_ids/auto_pick itself and passed them
in) becomes the two-argument `show_edge(edge_id, update)` the brief
specifies - FlowPage now computes both itself, using its own
`flow_edges` map for membership and `update.flows[fid].active` for the
auto-pick ("the single active one") vote.
"""
from typing import Dict, List, Optional, Set, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QLabel, QListWidget, QListWidgetItem,
                               QVBoxLayout, QWidget)

from core.engine.core import Engine
from core.engine.rules import EngineUpdate
from core.target.flows import Activity, FlowSpec
from core.target.topology import Topology

COL_ACTIVE = QColor("#1565C0")
COL_GREY = QColor("#B0B0B0")


def _noop(_id: Optional[str]) -> None:
    pass


def build_flow_edge_map(topology: Topology, flowspec: FlowSpec
                        ) -> Dict[str, Set[str]]:
    """An activity's edge set = edges whose (src, dst) are consecutive
    blocks in the activity's `path` (order-sensitive pairs). Shared by
    `MainWindow` (T9's `_flow_edges`, used for active-edge highlight and
    progress-label placement) and `FlowPage` (edge-click membership,
    here) so the rule lives in exactly one place."""
    pair_to_edges: Dict[Tuple[str, str], List[str]] = {}
    for edge in topology.edges:
        pair_to_edges.setdefault((edge.src, edge.dst), []).append(edge.id)
    result: Dict[str, Set[str]] = {}
    for act in flowspec.activities:
        edges: Set[str] = set()
        for a, b in zip(act.path, act.path[1:]):
            edges.update(pair_to_edges.get((a, b), []))
        result[act.name] = edges
    return result


class FlowPage(QWidget):
    def __init__(self, engine: Engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.flow_edges = build_flow_edge_map(engine.topology,
                                              engine.flowspec)
        self.activities: Dict[str, Activity] = {
            act.name: act for act in engine.flowspec.activities}
        self.edge_id: Optional[str] = None
        self.flow_id: Optional[str] = None
        self.on_pick = _noop      # set by MainWindow: re-highlight on pick
        self._last_update: Optional[EngineUpdate] = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.head = QLabel()
        self.head.setWordWrap(True)
        self.listw = QListWidget()
        self.listw.setMaximumHeight(90)
        self.listw.itemClicked.connect(self._pick)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.RichText)
        self.detail.setAlignment(Qt.AlignTop)
        lay.addWidget(self.head)
        lay.addWidget(self.listw)
        lay.addWidget(self.detail, 1)

    # -- selection ---------------------------------------------------------

    def show_edge(self, edge_id: str, update: Optional[EngineUpdate]
                 ) -> None:
        self.edge_id = edge_id
        flow_ids = [name for name, edges in self.flow_edges.items()
                   if edge_id in edges]
        active = [fid for fid in flow_ids
                 if update is not None and update.flows.get(fid) is not None
                 and update.flows[fid].active]
        self.flow_id = (active[0] if len(active) == 1
                        else (flow_ids[0] if flow_ids else None))
        self.head.setText("<b>edge %s</b> - flows through this edge:"
                          % edge_id)
        self.listw.clear()
        for fid in flow_ids:
            it = QListWidgetItem(fid)
            it.setData(Qt.UserRole, fid)
            self.listw.addItem(it)
            if fid == self.flow_id:
                it.setSelected(True)
        self.refresh(update)

    def _pick(self, item: QListWidgetItem) -> None:
        self.flow_id = item.data(Qt.UserRole)
        self.on_pick(self.flow_id)
        self.refresh(self._last_update)

    # -- live data -----------------------------------------------------------

    def refresh(self, update: Optional[EngineUpdate] = None) -> None:
        if update is not None:
            self._last_update = update
        update = self._last_update
        if update is None:
            return
        for i in range(self.listw.count()):
            it = self.listw.item(i)
            fid = it.data(Qt.UserRole)
            fs = update.flows.get(fid)
            active = fs.active if fs is not None else False
            it.setText("%s %s" % ("[ACTIVE]" if active else "[idle]  ", fid))
            it.setForeground(QBrush(COL_ACTIVE if active else COL_GREY))
        if self.flow_id is None:
            self.detail.setText("")
            return
        act = self.activities.get(self.flow_id)
        fs = update.flows.get(self.flow_id)
        if act is None or fs is None:
            self.detail.setText("")
            return
        rows = ["<b>%s</b> - %s" % (
                    act.name,
                    "<span style='color:#1565C0'>ACTIVE</span>" if fs.active
                    else "<span style='color:#888'>idle</span>"),
                "<code>active_when: %s</code>" % act.active_when]
        if act.progress is not None and fs.progress is not None:
            rows.append("<code>progress %s = 0x%04X</code>"
                        % (act.progress, fs.progress))
        if fs.rules:
            rows.append("<br><b>anomaly rules</b>")
            for rule in fs.rules:
                dot = "&#9679;"
                color = "#C62828" if rule.firing else "#2E7D32"
                rows.append(
                    "<span style='color:%s'>%s</span> <code>%s</code>"
                    "<br>&nbsp;&nbsp;&nbsp;-> %s"
                    % (color, dot, rule.expr, rule.msg))
        self.detail.setText("<br>".join(rows))
