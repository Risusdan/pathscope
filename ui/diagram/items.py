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
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QPainter, QPainterPath,
                           QPainterPathStroker, QPen, QPolygonF)
from PySide6.QtWidgets import QGraphicsItem

from core.target.topology import Block, Edge

# ---------------------------------------------------------------------------
# Visual constants - copied verbatim from prototype/ui_proto.py
# ---------------------------------------------------------------------------

MONO = QFont()
MONO.setFamilies(["Menlo", "Consolas", "Courier New"])
MONO.setPointSize(10)

KIND_TINT = {
    "peripheral": "#E9F3E7",
    "dma": "#E4EDF7",
    "memory": "#FBF3DC",
    "cpu": "#ECECEC",
    "interconnect": "#F1EAF3",
    "mux": "#FFFFFF",
    "pin": "#FFFFFF",
}

COL_ACTIVE = QColor("#1565C0")
COL_SELECT = QColor("#E65100")
COL_ANOM = QColor("#C62828")
COL_GREY = QColor("#B0B0B0")

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


class BlockItem(QGraphicsItem):
    def __init__(self, block: Block, state):
        super().__init__()
        self.block = block
        self.state = state
        dw, dh = default_wh(block.kind)
        self.w = block.w if block.w is not None else dw
        self.h = block.h if block.h is not None else dh
        self.setPos(block.x, block.y)

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
        badge = self.state.badges.get(self.block.id)
        if badge and badge.count > 0 and self.badge_rect().contains(ev.pos()):
            self.state.on_badge_clicked(self.block.id)
        else:
            self.state.on_block_clicked(self.block.id)
        ev.accept()


class WireItem(QGraphicsItem):
    def __init__(self, edge: Edge, pts, state):
        super().__init__()
        self.edge = edge
        self.state = state
        self.pts = pts
        self.setZValue(-1)

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
        self.state.on_edge_clicked(self.edge.id)
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
        self.setPos(700, 402)
        self.setZValue(5)

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
