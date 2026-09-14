"""QGraphicsItems for the block diagram: BlockItem, WireItem, LegendItem.

Ported from prototype/ui_proto.py's BlockItem/WireItem/LegendItem. Paint
code (trapezoid mux body, rotated MUX caption, port labels, arrowheads,
longest-segment label placement, dash animation, badge solid/outline
states, two-column legend) is copied verbatim; the only structural
changes are per task-8's porting instructions:

  - every prototype `self.win` becomes `self.state` (a DiagramState,
    see ui.diagram.scene) - items never touch the engine, only
    DiagramState and the Block/Edge topology dataclasses.
  - BlockItem/WireItem construct from Block/Edge dataclasses instead of
    the prototype's stub dicts (`spec["x"]` -> `block.x`, etc).
  - badge lookups go through the sparse `state.badges` dict via .get().
"""
from typing import List, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QPainter, QPainterPath,
                           QPainterPathStroker, QPen, QPolygonF)
from PySide6.QtWidgets import QApplication, QGraphicsItem

from core.target.topology import Block, Edge

from ..style import COL_ACTIVE, COL_ANOM, COL_GREY, COL_WARN, MONO

# ---------------------------------------------------------------------------
# Visual constants - copied verbatim from prototype/ui_proto.py
# ---------------------------------------------------------------------------

KIND_TINT = {
    "peripheral": "#E9F3E7",
    "dma": "#E4EDF7",
    "memory": "#FBF3DC",
    "cpu": "#ECECEC",
    "interconnect": "#F1EAF3",
    "mux": "#FFFFFF",
    "pin": "#FFFFFF",
}

COL_SELECT = COL_WARN

FONT_TITLE = QFont("Helvetica", 11, QFont.Bold)
FONT_PORT = QFont("Helvetica", 8)
FONT_LABEL = QFont("Helvetica", 9, QFont.Normal, italic=True)

# Default (w, h) in pixels for a block kind that has no explicit layout
# size (targets/f411 always supplies w/h, but a target without a full
# layout section should still render something sane).
_DEFAULT_WH = {
    "pin": (34, 34),
    "mux": (45, 90),
    "interconnect": (110, 440),
}
_DEFAULT_WH_FALLBACK = (150, 90)


def default_wh(kind: str):
    """(w, h) fallback for a block of `kind` with no layout dims."""
    return _DEFAULT_WH.get(kind, _DEFAULT_WH_FALLBACK)


# ---------------------------------------------------------------------------
# M8 layout edit mode (task 3): drag/resize snapping shared by BlockItem
# and LegendItem.
# ---------------------------------------------------------------------------

_GRID_STEP = 10
_FINE_STEP = 1
_HANDLE_SIZE = 8
_MIN_BLOCK_W = 30
_MIN_BLOCK_H = 24


def snap(value: float, fine: bool) -> int:
    """Grid-snap `value`: 10-unit steps normally, 1-unit when `fine`
    (Shift held). Pure so tests can hit it directly."""
    step = _FINE_STEP if fine else _GRID_STEP
    return int(round(value / step)) * step


def _fine_snap() -> bool:
    return bool(QApplication.keyboardModifiers() & Qt.ShiftModifier)


class BlockItem(QGraphicsItem):
    def __init__(self, block: Block, state):
        super().__init__()
        self.block = block
        self.state = state
        dw, dh = default_wh(block.kind)
        self.w = block.w if block.w is not None else dw
        self.h = block.h if block.h is not None else dh
        self.setPos(block.x, block.y)
        self._editable = False
        # _applying suppresses the itemChange grid-snap for
        # apply_geometry's own programmatic setPos (undo restore must
        # land the item at the exact requested x/y, snap-aligned or
        # not, even while the item is editable). _geom_at_press is the
        # gesture-start baseline (captured in mousePressEvent, and
        # re-synced on every commit) so mouseReleaseEvent can skip a
        # no-op click-only press/release instead of always firing
        # on_geometry_changed.
        self._applying = False
        self._geom_at_press = (int(block.x), int(block.y))
        self.handle = _ResizeHandle(self)
        self.handle.setVisible(False)
        self._position_handle()

    def _position_handle(self) -> None:
        self.handle.setPos(self.w - _HANDLE_SIZE, self.h - _HANDLE_SIZE)

    def set_editable(self, on: bool) -> None:
        self._editable = bool(on)
        self.setFlag(QGraphicsItem.ItemIsMovable, on)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, on)
        self.handle.setVisible(on)

    def geometry(self) -> Tuple[int, int, int, int]:
        pos = self.pos()
        return (int(pos.x()), int(pos.y()), int(self.w), int(self.h))

    def apply_geometry(self, x: int, y: int, w: int, h: int) -> None:
        """Undo restore path: moves the item AND updates self.block.
        setPos is wrapped in _applying so itemChange's live grid-snap
        does not intercept this programmatic move - the restored
        position must land exactly, snap-aligned or not."""
        self.prepareGeometryChange()
        self.w, self.h = w, h
        self.block.w, self.block.h = w, h
        self._applying = True
        try:
            self.setPos(x, y)
        finally:
            self._applying = False
        self.block.x, self.block.y = int(x), int(y)
        self._geom_at_press = (int(x), int(y))
        self._position_handle()
        self.update()

    def itemChange(self, change, value):
        if (change == QGraphicsItem.ItemPositionChange and self._editable
                and not self._applying):
            return QPointF(snap(value.x(), _fine_snap()),
                           snap(value.y(), _fine_snap()))
        return super().itemChange(change, value)

    def boundingRect(self):
        return QRectF(-12, -12, self.w + 24, self.h + 24)

    def badge_rect(self):
        return QRectF(self.w - 10, -10, 22, 22)

    def paint(self, p, opt, widget=None):
        w, h, kind = self.w, self.h, self.block.kind
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(QColor(KIND_TINT[kind])))
        selected_ = self.block.id == self.state.selected_block
        in_flow = self.block.id in self.state.flow_blocks
        if selected_:
            p.setPen(QPen(COL_ACTIVE, 2.6))
        elif in_flow:
            p.setPen(QPen(COL_SELECT, 2.2))
        else:
            p.setPen(QPen(Qt.black, 1.5))
        body = QRectF(0, 0, w, h)
        if kind == "mux":
            poly = QPolygonF([QPointF(0, 0), QPointF(w, 20),
                              QPointF(w, h - 20), QPointF(0, h)])
            p.drawPolygon(poly)
        else:
            p.drawRect(body)

        p.setPen(QPen(Qt.black))
        p.setFont(FONT_TITLE)
        if kind == "mux":
            p.save()
            p.translate(w / 2 - 6, h / 2 + 14)
            p.rotate(-90)
            p.drawText(0, 0, "MUX")
            p.restore()
        elif kind == "pin":
            p.drawText(body, Qt.AlignCenter, self.block.title)
        else:
            p.drawText(QRectF(0, 4, w, 40),
                       Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
                       self.block.title)

        p.setFont(FONT_PORT)
        for name, (side, frac) in self.block.ports.items():
            y = h * frac
            if side == "l":
                p.drawText(QRectF(4, y - 8, 60, 16),
                           Qt.AlignLeft | Qt.AlignVCenter, name)
            else:
                p.drawText(QRectF(w - 64, y - 8, 60, 16),
                           Qt.AlignRight | Qt.AlignVCenter, name)

        if kind == "mux" and self.state.chsel_value is not None:
            p.setFont(FONT_PORT)
            p.setPen(QPen(QColor("#555555")))
            p.drawText(QRectF(-30, h + 2, 110, 14), Qt.AlignLeft,
                       "sel=CHSEL(%d)" % self.state.chsel_value)

        badge = self.state.badges.get(self.block.id)
        if badge and badge.count > 0:
            r = self.badge_rect()
            if badge.active:
                p.setBrush(QBrush(COL_ANOM))
                p.setPen(QPen(COL_ANOM, 1.5))
                p.drawEllipse(r)
                p.setPen(QPen(Qt.white))
            else:
                p.setBrush(QBrush(Qt.white))
                p.setPen(QPen(COL_ANOM, 2))
                p.drawEllipse(r)
                p.setPen(QPen(COL_ANOM))
            p.setFont(FONT_PORT)
            p.drawText(r, Qt.AlignCenter, str(badge.count))

    def mousePressEvent(self, ev):
        if self._editable:
            pos = self.pos()
            self._geom_at_press = (int(pos.x()), int(pos.y()))
        if self.state.edit_mode:
            ev.accept()
            return
        badge = self.state.badges.get(self.block.id)
        if badge and badge.count > 0 and self.badge_rect().contains(ev.pos()):
            self.state.on_badge_clicked(self.block.id)
        else:
            self.state.on_block_clicked(self.block.id)
        ev.accept()

    def mouseReleaseEvent(self, ev):
        if self._editable:
            pos = self.pos()
            new_xy = (int(pos.x()), int(pos.y()))
            if new_xy != self._geom_at_press:
                self.block.x, self.block.y = new_xy
                self.state.on_geometry_changed()
            self._geom_at_press = new_xy
        ev.accept()


class _ResizeHandle(QGraphicsItem):
    """8x8 bottom-right corner handle, child of a BlockItem, visible
    only while its parent is editable. Dragging it live-resizes the
    parent (prepareGeometryChange + repaint, min-clamped so the block
    never paints below 30x24); release clamps+snaps once more and
    writes block.w/h, firing state.on_geometry_changed() once."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setZValue(10)
        self._drag_from = None
        self._start_w = 0.0
        self._start_h = 0.0

    def boundingRect(self):
        return QRectF(0, 0, _HANDLE_SIZE, _HANDLE_SIZE)

    def paint(self, p, opt, widget=None):
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(Qt.white))
        p.setPen(QPen(Qt.black, 1))
        p.drawRect(self.boundingRect())

    def mousePressEvent(self, ev):
        block_item = self.parentItem()
        self._drag_from = ev.scenePos()
        self._start_w, self._start_h = block_item.w, block_item.h
        ev.accept()

    def mouseMoveEvent(self, ev):
        block_item = self.parentItem()
        if self._drag_from is None:
            ev.accept()
            return
        dx = ev.scenePos().x() - self._drag_from.x()
        dy = ev.scenePos().y() - self._drag_from.y()
        block_item.prepareGeometryChange()
        block_item.w = max(_MIN_BLOCK_W, self._start_w + dx)
        block_item.h = max(_MIN_BLOCK_H, self._start_h + dy)
        block_item._position_handle()
        block_item.update()
        ev.accept()

    def mouseReleaseEvent(self, ev):
        block_item = self.parentItem()
        fine = _fine_snap()
        w = max(_MIN_BLOCK_W, snap(block_item.w, fine))
        h = max(_MIN_BLOCK_H, snap(block_item.h, fine))
        changed = (w, h) != (int(self._start_w), int(self._start_h))
        block_item.prepareGeometryChange()
        block_item.w, block_item.h = w, h
        block_item._position_handle()
        block_item.update()
        if changed:
            block_item.block.w, block_item.block.h = w, h
            block_item.state.on_geometry_changed()
        self._drag_from = None
        ev.accept()


def _point_segment_dist2(p: QPointF, a: QPointF, b: QPointF) -> float:
    """Squared distance from `p` to the segment a-b (clamped
    projection onto the segment) - used to find which rendered
    segment a double-click landed nearest to, so an inserted waypoint
    lands in the right spot in the path."""
    dx, dy = b.x() - a.x(), b.y() - a.y()
    length2 = dx * dx + dy * dy
    if length2 <= 1e-9:
        t = 0.0
    else:
        t = ((p.x() - a.x()) * dx + (p.y() - a.y()) * dy) / length2
        t = max(0.0, min(1.0, t))
    cx, cy = a.x() + t * dx, a.y() + t * dy
    return (p.x() - cx) ** 2 + (p.y() - cy) ** 2


class WireItem(QGraphicsItem):
    def __init__(self, edge: Edge, pts, state):
        super().__init__()
        self.edge = edge
        self.state = state
        self.pts = pts
        self.setZValue(-1)
        # M8 task 4 (waypoint editing): _editable gates whether
        # WaypointHandle children exist at all - they are created
        # lazily by set_editable(True) and torn down by
        # set_editable(False), never merely hidden. _auto_pts is the
        # path this WireItem was FIRST constructed with, cached
        # unconditionally: for an edge that started pointless (no
        # edge.points) this is exactly the auto-routed 2-point line
        # from build_scene's _straight_route, so it is the correct
        # fallback when the user deletes every explicit waypoint back
        # to edge.points == [] (see remove_point). WireItem has no
        # reference to the src/dst BlockItems (only DiagramState,
        # which carries no block geometry), so it cannot recompute a
        # fresh auto-route on demand if the blocks have since moved -
        # this cached snapshot is the best available fallback without
        # widening this task's touched files.
        self._editable = False
        self._handles: List["WaypointHandle"] = []
        self._auto_pts = list(pts)

    def set_editable(self, on: bool) -> None:
        self._editable = bool(on)
        if self._editable:
            self._rebuild_handles()
        else:
            self._clear_handles()

    def edge_key(self) -> Tuple[str, str, str]:
        """Identity tuple matching core/target/layout_io.py's
        _edge_identity(): (src, dst, label-or-empty-string) - Task 5
        uses this to key layout_io.patch_layout_text's edge_points
        dict."""
        return (self.edge.src, self.edge.dst, self.edge.label or "")

    def _clear_handles(self) -> None:
        for h in self._handles:
            h.setParentItem(None)
            sc = h.scene()
            if sc is not None:
                sc.removeItem(h)
        self._handles = []

    def _rebuild_handles(self) -> None:
        self._clear_handles()
        for i in range(len(self.edge.points)):
            self._handles.append(WaypointHandle(self, i))

    def apply_points(self, points: List[Tuple[int, int]]) -> None:
        """Undo restore path (mirrors BlockItem.apply_geometry /
        LegendItem.apply_geometry's _applying discipline): replaces
        edge.points with an exact, UNSNAPPED copy of `points` - an
        empty list reverts to this wire's auto-routed fallback - then
        rebuilds self.pts and (if editable) the waypoint handles to
        match, and repaints. Must NEVER call snap() on the restored
        coordinates and must NEVER fire on_geometry_changed: the undo
        stack is popping an already-applied snapshot, not a new user
        gesture."""
        self.prepareGeometryChange()
        self.edge.points = list(points)
        if self.edge.points:
            self.pts = [QPointF(x, y) for x, y in self.edge.points]
        else:
            self.pts = list(self._auto_pts)
        if self._editable:
            self._rebuild_handles()
        self.update()

    def remove_point(self, index: int) -> None:
        """Delete key on a selected WaypointHandle: removes
        edge.points[index] and fires on_geometry_changed once - no
        implicit collinear merging is ever applied (spec section 3).
        Edge case (controller resolution): deleting the LAST
        remaining point leaves edge.points == [] and the wire reverts
        to its auto-routed fallback path - that is correct and
        intended, matching "edges without a points: list show only
        their endpoints" (design spec section 3): a fully-emptied
        explicit path is indistinguishable from one that was never
        made explicit, so the edge simply reverts to auto-routing."""
        self.prepareGeometryChange()
        del self.edge.points[index]
        del self.pts[index]
        if not self.edge.points:
            self.pts = list(self._auto_pts)
        if self._editable:
            self._rebuild_handles()
        self.update()
        self.state.on_geometry_changed()

    def mouseDoubleClickEvent(self, ev) -> None:
        """Edit-mode double-click on the wire: inserts a waypoint at
        the clicked position (snapped) into the segment of the
        CURRENTLY RENDERED path (self.pts) closest to the click -
        this is the same operation whether the edge was pointless
        (self.pts is the 2-point auto route) or already pointed
        (self.pts == edge.points): the resulting full path, endpoints
        included, is written back to edge.points, converting a
        pointless edge to an explicit path on first insert. Fires
        on_geometry_changed once."""
        if not self.state.edit_mode:
            ev.accept()
            return
        pos = ev.pos()
        seg = 0
        best_d = None
        for i in range(len(self.pts) - 1):
            d = _point_segment_dist2(pos, self.pts[i], self.pts[i + 1])
            if best_d is None or d < best_d:
                best_d, seg = d, i
        fine = _fine_snap()
        new_pt = QPointF(snap(pos.x(), fine), snap(pos.y(), fine))
        new_pts = list(self.pts)
        new_pts.insert(seg + 1, new_pt)
        self.prepareGeometryChange()
        self.pts = new_pts
        self.edge.points = [(int(p.x()), int(p.y())) for p in new_pts]
        if self._editable:
            self._rebuild_handles()
        self.update()
        self.state.on_geometry_changed()
        ev.accept()

    def path(self):
        pp = QPainterPath(self.pts[0])
        for pt in self.pts[1:]:
            pp.lineTo(pt)
        return pp

    def _longest_segment(self):
        # label_seg is not part of the current Edge schema, but future
        # topologies may carry it - honor it defensively if present.
        seg = getattr(self.edge, "label_seg", None)
        if seg is not None:
            a, b = self.pts[seg], self.pts[seg + 1]
            return a, b, abs(b.y() - a.y()) > abs(b.x() - a.x())
        best, blen, vert = (self.pts[0], self.pts[1]), 0, False
        for a, b in zip(self.pts, self.pts[1:]):
            ln = abs(b.x() - a.x()) + abs(b.y() - a.y())
            if ln > blen:
                best, blen = (a, b), ln
                vert = abs(b.y() - a.y()) > abs(b.x() - a.x())
        return best[0], best[1], vert

    def boundingRect(self):
        return self.path().boundingRect().adjusted(-14, -20, 14, 20)

    def shape(self):
        st = QPainterPathStroker()
        st.setWidth(12)
        return st.createStroke(self.path())

    def paint(self, p, opt, widget=None):
        p.setRenderHint(QPainter.Antialiasing)
        pp = self.path()
        active = self.edge.id in self.state.active_edges
        in_flow = self.edge.id in self.state.flow_edges
        mux_off = (self.edge.when_select is not None
                  and self.edge.when_select != self.state.chsel_value)
        if in_flow:
            glow = QPen(QColor(255, 179, 0, 110), 8, Qt.SolidLine, Qt.RoundCap)
            p.setPen(glow)
            p.drawPath(pp)
        if active:
            pen = QPen(COL_ACTIVE, 2.6)
            pen.setDashPattern([5, 4])
            pen.setDashOffset(-self.state.dash_phase)
        elif mux_off:
            pen = QPen(COL_GREY, 1.2)
        else:
            pen = QPen(Qt.black, 1.4)
        p.setPen(pen)
        p.drawPath(pp)

        # arrowhead
        a, b = self.pts[-2], self.pts[-1]
        dx, dy = b.x() - a.x(), b.y() - a.y()
        n = max((dx * dx + dy * dy) ** 0.5, 1e-6)
        ux, uy = dx / n, dy / n
        base = QPointF(b.x() - 8 * ux, b.y() - 8 * uy)
        left = QPointF(base.x() - 4 * uy, base.y() + 4 * ux)
        right = QPointF(base.x() + 4 * uy, base.y() - 4 * ux)
        p.setBrush(QBrush(pen.color()))
        p.setPen(QPen(pen.color(), 1))
        p.drawPolygon(QPolygonF([b, left, right]))

        # label centered on the longest segment
        if self.edge.label:
            p.setFont(FONT_LABEL)
            p.setPen(QPen(pen.color() if active else QColor("#444444")))
            a, b, vertical = self._longest_segment()
            tw = p.fontMetrics().horizontalAdvance(self.edge.label)
            mx, my = (a.x() + b.x()) / 2, (a.y() + b.y()) / 2
            if vertical:
                p.drawText(QPointF(a.x() + 5, my + 4), self.edge.label)
            else:
                p.drawText(QPointF(mx - tw / 2, my - 5), self.edge.label)

        # generic progress label rides below the marked edge
        text = self.state.progress_text.get(self.edge.id)
        if text:
            f = QFont(MONO)
            f.setPointSize(8)
            p.setFont(f)
            p.setPen(QPen(COL_ACTIVE if active else QColor("#666666")))
            tw = p.fontMetrics().horizontalAdvance(text)
            a, b, _ = self._longest_segment()
            mx = (a.x() + b.x()) / 2
            p.drawText(QPointF(mx - tw / 2, (a.y() + b.y()) / 2 + 16), text)

    def mousePressEvent(self, ev):
        if self.state.edit_mode:
            ev.accept()
            return
        self.state.on_edge_clicked(self.edge.id)
        ev.accept()


class WaypointHandle(QGraphicsItem):
    """8x8 square handle, child of a WireItem, one per edge.points
    entry - shown only while the wire is editable (WireItem.
    set_editable(True) creates one per point; set_editable(False)
    destroys them). Dragging live-moves its point (prepareGeometry-
    Change + repaint, snap 10 / Shift 1 on release); release writes
    edge.points[index] and fires state.on_geometry_changed() exactly
    once IF the point actually moved - a click-only press/release
    fires nothing, mirroring BlockItem/_ResizeHandle's gesture-start-
    baseline discipline. Clicking a handle selects it; Delete then
    removes its point via the parent WireItem.remove_point (no
    implicit collinear merging)."""

    def __init__(self, wire: "WireItem", index: int):
        super().__init__(wire)
        self.setZValue(10)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemIsFocusable, True)
        self.index = index
        self._drag_from = None
        self._start_point = (0.0, 0.0)
        pt = wire.pts[index]
        self.setPos(pt)
        self._geom_at_press = (int(pt.x()), int(pt.y()))

    def boundingRect(self):
        half = _HANDLE_SIZE / 2
        return QRectF(-half, -half, _HANDLE_SIZE, _HANDLE_SIZE)

    def paint(self, p, opt, widget=None):
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(COL_SELECT if self.isSelected() else Qt.white))
        p.setPen(QPen(Qt.black, 1))
        p.drawRect(self.boundingRect())

    def mousePressEvent(self, ev):
        self.setSelected(True)
        self.setFocus(Qt.MouseFocusReason)
        pos = self.pos()
        self._geom_at_press = (int(pos.x()), int(pos.y()))
        self._drag_from = ev.scenePos()
        self._start_point = (pos.x(), pos.y())
        ev.accept()

    def mouseMoveEvent(self, ev):
        if self._drag_from is None:
            ev.accept()
            return
        wire = self.parentItem()
        dx = ev.scenePos().x() - self._drag_from.x()
        dy = ev.scenePos().y() - self._drag_from.y()
        nx, ny = self._start_point[0] + dx, self._start_point[1] + dy
        wire.prepareGeometryChange()
        self.setPos(nx, ny)
        wire.pts[self.index] = QPointF(nx, ny)
        wire.update()
        ev.accept()

    def mouseReleaseEvent(self, ev):
        wire = self.parentItem()
        fine = _fine_snap()
        pos = self.pos()
        new_xy = (snap(pos.x(), fine), snap(pos.y(), fine))
        wire.prepareGeometryChange()
        self.setPos(new_xy[0], new_xy[1])
        wire.pts[self.index] = QPointF(new_xy[0], new_xy[1])
        wire.update()
        if new_xy != self._geom_at_press:
            wire.edge.points[self.index] = new_xy
            wire.state.on_geometry_changed()
        self._geom_at_press = new_xy
        self._drag_from = None
        ev.accept()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.parentItem().remove_point(self.index)
            ev.accept()
        else:
            ev.accept()


class LegendItem(QGraphicsItem):
    KINDS = [("cpu", "CPU"), ("peripheral", "Peripheral"), ("dma", "DMA"),
             ("memory", "Memory"), ("interconnect", "Bus / interconnect")]
    LINES = [("active", "active flow"), ("idle", "idle path"),
             ("muxoff", "mux input not selected"),
             ("selected", "selected flow"),
             ("badge", "anomaly (click clears)")]

    def __init__(self, state):
        super().__init__()
        self.state = state
        self._editable = False
        # See BlockItem's matching fields: _applying suppresses the
        # itemChange grid-snap for apply_geometry's own setPos;
        # _geom_at_press is the gesture-start baseline that lets
        # mouseReleaseEvent skip firing on_geometry_changed for a
        # no-op click-only press/release.
        self._applying = False
        x, y = state.legend_pos or (700, 402)
        self.setPos(x, y)
        self._geom_at_press = (int(x), int(y))
        self.setZValue(5)

    def set_editable(self, on: bool) -> None:
        self._editable = bool(on)
        self.setFlag(QGraphicsItem.ItemIsMovable, on)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, on)

    def geometry(self) -> Tuple[int, int, int, int]:
        pos = self.pos()
        return (int(pos.x()), int(pos.y()), 0, 0)

    def apply_geometry(self, x: int, y: int, w: int, h: int) -> None:
        """Undo restore path; the legend has no w/h so those are
        ignored, matching geometry()'s (x, y, 0, 0). setPos is wrapped
        in _applying so itemChange's live grid-snap does not
        intercept this programmatic move."""
        self._applying = True
        try:
            self.setPos(x, y)
        finally:
            self._applying = False
        self._geom_at_press = (int(x), int(y))

    def itemChange(self, change, value):
        if (change == QGraphicsItem.ItemPositionChange and self._editable
                and not self._applying):
            return QPointF(snap(value.x(), _fine_snap()),
                           snap(value.y(), _fine_snap()))
        return super().itemChange(change, value)

    def mousePressEvent(self, ev):
        if self._editable:
            pos = self.pos()
            self._geom_at_press = (int(pos.x()), int(pos.y()))
        ev.accept()

    def mouseReleaseEvent(self, ev):
        if self._editable:
            pos = self.pos()
            new_xy = (int(pos.x()), int(pos.y()))
            if new_xy != self._geom_at_press:
                self.state.on_geometry_changed()
            self._geom_at_press = new_xy
        ev.accept()

    def boundingRect(self):
        return QRectF(0, 0, 272, 142)

    def paint(self, p, opt, widget=None):
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(QColor(255, 255, 255, 235)))
        p.setPen(QPen(QColor("#888888"), 1))
        p.drawRect(self.boundingRect())
        f = QFont(FONT_PORT)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QPen(Qt.black))
        p.drawText(QRectF(0, 3, 272, 14), Qt.AlignHCenter, "Legend")
        p.setFont(FONT_PORT)

        y = 30
        for kind, name in self.KINDS:
            p.setBrush(QBrush(QColor(KIND_TINT[kind])))
            p.setPen(QPen(Qt.black, 1))
            p.drawRect(QRectF(8, y - 8, 14, 10))
            p.drawText(QPointF(28, y + 1), name)
            y += 21

        y = 30
        for key, name in self.LINES:
            if key == "active":
                pen = QPen(COL_ACTIVE, 2.4)
                pen.setDashPattern([5, 4])
            elif key == "idle":
                pen = QPen(Qt.black, 1.4)
            elif key == "muxoff":
                pen = QPen(COL_GREY, 1.2)
            elif key == "selected":
                p.setPen(QPen(QColor(255, 179, 0, 140), 7))
                p.drawLine(QPointF(122, y - 3), QPointF(148, y - 3))
                pen = QPen(Qt.black, 1.4)
            else:
                p.setBrush(QBrush(COL_ANOM))
                p.setPen(QPen(COL_ANOM, 1))
                p.drawEllipse(QRectF(128, y - 9, 13, 13))
                p.setPen(QPen(Qt.white))
                p.drawText(QRectF(128, y - 9, 13, 13), Qt.AlignCenter, "2")
                p.setPen(QPen(Qt.black))
                p.drawText(QPointF(152, y + 1), name)
                y += 21
                continue
            p.setPen(pen)
            p.drawLine(QPointF(122, y - 3), QPointF(148, y - 3))
            p.setPen(QPen(Qt.black))
            p.drawText(QPointF(152, y + 1), name)
            y += 21
