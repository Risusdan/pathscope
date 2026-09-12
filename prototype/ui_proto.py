#!/usr/bin/env python3
"""UI/UX prototype for Data Path Explorer. THROWAWAY - stub data only.

Run:            python prototype/ui_proto.py
Self-check:     python prototype/ui_proto.py --shot out.png   (offscreen render)

Style: textbook / reference-manual block diagram. White background, thin
black borders, orthogonal wires, optional kind-tint fill toggle.
"""

import os
import sys
import time

if "--shot" in sys.argv:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QBrush, QColor, QFont, QPainter,
                           QPainterPath, QPainterPathStroker, QPen,
                           QPolygonF)
from PySide6.QtWidgets import (QApplication, QDockWidget, QGraphicsItem,
                               QGraphicsScene, QGraphicsView, QLabel,
                               QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QSizePolicy, QStackedWidget, QToolBar,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

# ---------------------------------------------------------------------------
# Stub target model (real version: SVD + topology.yaml + flows.yaml)
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

BLOCKS = {
    "cm4":   dict(title="Cortex-M4", kind="cpu", x=70, y=50, w=160, h=90),
    "flash": dict(title="Flash 512KB", kind="memory", x=780, y=50, w=170, h=80),
    "busmx": dict(title="AHB Bus\nMatrix", kind="interconnect", x=580, y=40, w=110, h=440),
    "sram1": dict(title="SRAM1 128KB", kind="memory", x=780, y=230, w=170, h=90),
    "dma2":  dict(title="DMA2", kind="dma", x=330, y=320, w=160, h=100,
                  ports={"periph": ("l", 0.5), "mem": ("r", 0.5)}),
    "mux0":  dict(title="MUX", kind="mux", x=255, y=330, w=45, h=90,
                  select="DMA2.S0CR.CHSEL"),
    "adc1":  dict(title="ADC1", kind="peripheral", x=70, y=280, w=140, h=80,
                  ports={"DR": ("r", 0.5)}),
    "tim1":  dict(title="TIM1", kind="peripheral", x=70, y=430, w=130, h=80),
    "pa0":   dict(title="PA0", kind="pin", x=8, y=300, w=34, h=34),
}

# waypoints are absolute scene coords, orthogonal
EDGES = {
    "e_pin_adc":  dict(pts=[(42, 317), (70, 317)], label="AIN0"),
    "e_adc_mux":  dict(pts=[(210, 320), (232, 320), (232, 350), (255, 350)],
                       label="DR", mux_input=0),
    "e_tim_mux":  dict(pts=[(200, 470), (232, 470), (232, 400), (255, 400)],
                       label="TRGO", label_seg=0, mux_input=6),
    "e_mux_dma":  dict(pts=[(300, 375), (330, 375)], label="S0"),
    "e_dma_bus":  dict(pts=[(490, 370), (580, 370)], label="AHB", progress=True),
    "e_bus_sram": dict(pts=[(690, 275), (780, 275)], label="M0AR"),
    "e_flash_bus": dict(pts=[(780, 90), (690, 90)], label="I-bus"),
    "e_bus_cm4":  dict(pts=[(580, 95), (230, 95)], label="fetch"),
}

FLOWS = {
    "f_adc": dict(
        name="adc_to_sram",
        blocks=["pa0", "adc1", "mux0", "dma2", "busmx", "sram1"],
        edges=["e_pin_adc", "e_adc_mux", "e_mux_dma", "e_dma_bus", "e_bus_sram"],
        active_when="DMA2.S0CR.EN==1 and DMA2.S0CR.CHSEL==0",
        progress="DMA2.S0NDTR",
        rules=[
            ("ADC1.SR.OVR == 1", "ADC overrun, data lost", "adc1"),
            ("stalled(DMA2.S0NDTR, 500)", "DMA stalled", "dma2"),
            ("DMA2.LISR.TEIF0 == 1", "DMA transfer error", "dma2"),
        ],
    ),
    "f_tim": dict(
        name="tim_to_sram",
        blocks=["tim1", "mux0", "dma2", "busmx", "sram1"],
        edges=["e_tim_mux", "e_mux_dma", "e_dma_bus", "e_bus_sram"],
        active_when="DMA2.S0CR.EN==1 and DMA2.S0CR.CHSEL==6",
        progress="DMA2.S0NDTR",
        rules=[
            ("stalled(DMA2.S0NDTR, 500)", "DMA stalled", "dma2"),
        ],
    ),
    "f_fetch": dict(
        name="cpu_fetch",
        blocks=["flash", "busmx", "cm4"],
        edges=["e_flash_bus", "e_bus_cm4"],
        active_when="(always)",
        progress=None,
        rules=[],
    ),
}

# registers per peripheral block: (name, mode, fields)
# mode: "watched" | "cold" | "guarded";  fields: [(name, lo, hi)]
REG_DEFS = {
    "dma2": [
        ("S0CR",   "watched", [("EN", 0, 0), ("CHSEL", 25, 27)]),
        ("S0NDTR", "watched", []),
        ("S0PAR",  "cold",    []),
        ("S0M0AR", "cold",    []),
        ("LISR",   "watched", [("TEIF0", 3, 3)]),
        ("HISR",   "cold",    []),
    ],
    "adc1": [
        ("SR",   "watched", [("OVR", 5, 5), ("STRT", 4, 4), ("EOC", 1, 1)]),
        ("CR2",  "watched", [("DMA", 8, 8), ("CONT", 1, 1), ("ADON", 0, 0)]),
        ("SQR3", "cold",    []),
        ("DR",   "guarded", []),
    ],
    "tim1": [
        ("CR1", "watched", [("CEN", 0, 0)]),
        ("CNT", "watched", []),
        ("PSC", "cold",    []),
        ("ARR", "cold",    []),
    ],
}


# ---------------------------------------------------------------------------
# Stub engine: fakes polling snapshots, rule evaluation, anomaly latching
# ---------------------------------------------------------------------------

class StubEngine(QObject):
    snapshot = Signal(dict)          # {"regs":..,"flows":..,"badges":..,"rate":..}
    event = Signal(str, str, str)    # severity, text, focus_id (block or flow)

    def __init__(self):
        super().__init__()
        self.chsel = 0
        self.en = 1
        self.ndtr = 1000
        self.tim_cnt = 0
        self.ovr_ticks = 0           # >0: overrun condition held
        self.stall_ticks = 0         # >0: NDTR frozen
        self.ndtr_last_change = time.monotonic()
        self.badges = {}             # block_id -> {"count": n, "active": bool}
        self.prev_rule_state = {}    # (flow, idx) -> bool
        self.regs = {
            "DMA2.S0PAR": 0x4001204C, "DMA2.S0M0AR": 0x20000400,
            "DMA2.HISR": 0, "ADC1.SQR3": 0, "ADC1.DR": None,
            "TIM1.PSC": 83, "TIM1.ARR": 0xFFFF, "TIM1.CR1": 1,
        }
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.tick)

    # -- fault injection -----------------------------------------------------
    def inject_overrun(self):
        self.ovr_ticks = 20          # firmware "clears" it after 2 s

    def inject_stall(self):
        self.stall_ticks = 30

    def set_chsel(self, v):
        self.chsel = v
        self.event.emit("info", "S0CR.CHSEL written -> %d" % v, "mux0")

    def clear_badge(self, block_id):
        if block_id in self.badges:
            self.badges[block_id]["count"] = 0

    # -- helpers -------------------------------------------------------------
    def field(self, reg, lo, hi):
        v = self.reg_value(reg)
        if v is None:
            return None
        mask = (1 << (hi - lo + 1)) - 1
        return (v >> lo) & mask

    def reg_value(self, path):
        if path == "DMA2.S0CR":
            return (self.chsel << 25) | self.en
        if path == "DMA2.S0NDTR":
            return self.ndtr
        if path == "DMA2.LISR":
            return 0
        if path == "ADC1.SR":
            return (0x30 if self.ovr_ticks > 0 else 0x10) | 0x2
        if path == "ADC1.CR2":
            return 0x103
        if path == "TIM1.CNT":
            return self.tim_cnt
        return self.regs.get(path, 0)

    def stalled(self, ms):
        return (time.monotonic() - self.ndtr_last_change) * 1000 > ms

    def eval_rule(self, expr):
        # hardcoded stub evaluation of the three rule shapes we ship
        if expr.startswith("ADC1.SR.OVR"):
            return self.ovr_ticks > 0
        if expr.startswith("stalled"):
            return self.stalled(500)
        if expr.startswith("DMA2.LISR.TEIF0"):
            return False
        return False

    def flow_active(self, fid):
        if fid == "f_fetch":
            return True
        if fid == "f_adc":
            return self.en == 1 and self.chsel == 0
        if fid == "f_tim":
            return self.en == 1 and self.chsel == 6
        return False

    # -- main tick -----------------------------------------------------------
    def tick(self):
        if self.stall_ticks > 0:
            self.stall_ticks -= 1
        else:
            self.ndtr -= 41
            if self.ndtr <= 0:
                self.ndtr = 1000
            self.ndtr_last_change = time.monotonic()
        self.tim_cnt = (self.tim_cnt + 137) & 0xFFFF
        if self.ovr_ticks > 0:
            self.ovr_ticks -= 1

        flows = {}
        for fid, f in FLOWS.items():
            active = self.flow_active(fid)
            rules = []
            for idx, (expr, msg, target) in enumerate(f["rules"]):
                state = self.eval_rule(expr)
                key = (fid, idx)
                if state and not self.prev_rule_state.get(key, False):
                    b = self.badges.setdefault(target, {"count": 0, "active": False})
                    b["count"] += 1
                    self.event.emit("anomaly", "%s: %s" % (f["name"], msg), target)
                self.prev_rule_state[key] = state
                rules.append((expr, msg, target, state))
            flows[fid] = dict(active=active, rules=rules)

        for b in self.badges.values():
            b["active"] = False
        for fid, f in FLOWS.items():
            for expr, msg, target, state in flows[fid]["rules"]:
                if state:
                    self.badges.setdefault(target, {"count": 1})["active"] = True

        regs = {}
        for blk, defs in REG_DEFS.items():
            for name, mode, fields in defs:
                path = "%s.%s" % (blk.upper(), name)
                regs[path] = self.reg_value(path) if mode != "guarded" else None
        snap = dict(
            regs=regs, flows=flows, badges=dict(self.badges),
            chsel=self.chsel, rate=29.5 + (self.tim_cnt % 40) / 10.0,
            ndtr=self.ndtr,
        )
        self.snapshot.emit(snap)


# ---------------------------------------------------------------------------
# Graphics items
# ---------------------------------------------------------------------------

COL_ACTIVE = QColor("#1565C0")
COL_SELECT = QColor("#E65100")
COL_ANOM = QColor("#C62828")
COL_GREY = QColor("#B0B0B0")

FONT_TITLE = QFont("Helvetica", 11, QFont.Bold)
FONT_PORT = QFont("Helvetica", 8)
FONT_LABEL = QFont("Helvetica", 9, QFont.Normal, italic=True)


class BlockItem(QGraphicsItem):
    def __init__(self, bid, spec, win):
        super().__init__()
        self.bid, self.spec, self.win = bid, spec, win
        self.setPos(spec["x"], spec["y"])
        self.badge = None            # {"count","active"} or None
        self.selected_ = False
        self.in_flow = False

    def boundingRect(self):
        return QRectF(-12, -12, self.spec["w"] + 24, self.spec["h"] + 24)

    def badge_rect(self):
        return QRectF(self.spec["w"] - 10, -10, 22, 22)

    def paint(self, p, opt, widget=None):
        s = self.spec
        p.setRenderHint(QPainter.Antialiasing)
        tint = KIND_TINT[s["kind"]] if self.win.tinted else "#FFFFFF"
        p.setBrush(QBrush(QColor(tint)))
        if self.selected_:
            p.setPen(QPen(COL_ACTIVE, 2.6))
        elif self.in_flow:
            p.setPen(QPen(COL_SELECT, 2.2))
        else:
            p.setPen(QPen(Qt.black, 1.5))
        body = QRectF(0, 0, s["w"], s["h"])
        if s["kind"] == "mux":
            poly = QPolygonF([QPointF(0, 0), QPointF(s["w"], 20),
                              QPointF(s["w"], s["h"] - 20), QPointF(0, s["h"])])
            p.drawPolygon(poly)
        else:
            p.drawRect(body)

        p.setPen(QPen(Qt.black))
        p.setFont(FONT_TITLE)
        if s["kind"] == "mux":
            p.save()
            p.translate(s["w"] / 2 - 6, s["h"] / 2 + 14)
            p.rotate(-90)
            p.drawText(0, 0, "MUX")
            p.restore()
        elif s["kind"] == "pin":
            p.drawText(body, Qt.AlignCenter, s["title"])
        else:
            p.drawText(QRectF(0, 4, s["w"], 40), Qt.AlignHCenter | Qt.AlignTop,
                       s["title"])

        p.setFont(FONT_PORT)
        for name, (side, frac) in s.get("ports", {}).items():
            y = s["h"] * frac
            if side == "l":
                p.drawText(QRectF(4, y - 8, 60, 16),
                           Qt.AlignLeft | Qt.AlignVCenter, name)
            else:
                p.drawText(QRectF(s["w"] - 64, y - 8, 60, 16),
                           Qt.AlignRight | Qt.AlignVCenter, name)

        if s["kind"] == "mux" and self.win.snap:
            p.setFont(FONT_PORT)
            p.setPen(QPen(QColor("#555555")))
            p.drawText(QRectF(-30, s["h"] + 2, 110, 14), Qt.AlignLeft,
                       "sel=CHSEL(%d)" % self.win.snap.get("chsel", 0))

        if self.badge and self.badge.get("count", 0) > 0:
            r = self.badge_rect()
            if self.badge.get("active"):
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
            p.drawText(r, Qt.AlignCenter, str(self.badge["count"]))

    def mousePressEvent(self, ev):
        if self.badge and self.badge.get("count", 0) > 0 \
                and self.badge_rect().contains(ev.pos()):
            self.win.engine.clear_badge(self.bid)
            self.win.log("info", "badge cleared on %s" % self.bid, self.bid)
        else:
            self.win.select_block(self.bid)
        ev.accept()


class WireItem(QGraphicsItem):
    def __init__(self, eid, spec, win):
        super().__init__()
        self.eid, self.spec, self.win = eid, spec, win
        self.pts = [QPointF(x, y) for x, y in spec["pts"]]
        self.active = False
        self.in_flow = False
        self.mux_off = False         # de-selected mux input -> grey
        self.setZValue(-1)

    def path(self):
        pp = QPainterPath(self.pts[0])
        for pt in self.pts[1:]:
            pp.lineTo(pt)
        return pp

    def _longest_segment(self):
        seg = self.spec.get("label_seg")
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
        if self.in_flow:
            glow = QPen(QColor(255, 179, 0, 110), 8, Qt.SolidLine, Qt.RoundCap)
            p.setPen(glow)
            p.drawPath(pp)
        if self.active:
            pen = QPen(COL_ACTIVE, 2.6)
            pen.setDashPattern([5, 4])
            pen.setDashOffset(-self.win.dash_phase)
        elif self.mux_off:
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
        if self.spec.get("label"):
            p.setFont(FONT_LABEL)
            p.setPen(QPen(pen.color() if self.active else QColor("#444444")))
            a, b, vertical = self._longest_segment()
            w = p.fontMetrics().horizontalAdvance(self.spec["label"])
            mx, my = (a.x() + b.x()) / 2, (a.y() + b.y()) / 2
            if vertical:
                p.drawText(QPointF(a.x() + 5, my + 4), self.spec["label"])
            else:
                p.drawText(QPointF(mx - w / 2, my - 5), self.spec["label"])

        # live progress value rides below the marked edge
        if self.spec.get("progress") and self.win.snap:
            f = QFont(MONO)
            f.setPointSize(8)
            p.setFont(f)
            p.setPen(QPen(COL_ACTIVE if self.active else QColor("#666666")))
            text = "NDTR 0x%04X" % self.win.snap["ndtr"]
            w = p.fontMetrics().horizontalAdvance(text)
            a, b, _ = self._longest_segment()
            mx = (a.x() + b.x()) / 2
            p.drawText(QPointF(mx - w / 2, (a.y() + b.y()) / 2 + 16), text)

    def mousePressEvent(self, ev):
        self.win.select_edge(self.eid)
        ev.accept()


class LegendItem(QGraphicsItem):
    KINDS = [("cpu", "CPU"), ("peripheral", "Peripheral"), ("dma", "DMA"),
             ("memory", "Memory"), ("interconnect", "Bus / interconnect")]
    LINES = [("active", "active flow"), ("idle", "idle path"),
             ("muxoff", "mux input not selected"),
             ("selected", "selected flow"),
             ("badge", "anomaly (click clears)")]

    def __init__(self, win):
        super().__init__()
        self.win = win
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
            color = KIND_TINT[kind] if self.win.tinted else "#FFFFFF"
            p.setBrush(QBrush(QColor(color)))
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


# ---------------------------------------------------------------------------
# Inspector panels
# ---------------------------------------------------------------------------

class RegisterPage(QTreeWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.block_id = None
        self.setColumnCount(2)
        self.setHeaderLabels(["Register / Field", "Value"])
        self.setColumnWidth(0, 170)
        self.itemDoubleClicked.connect(self.on_double)
        self.changed_at = {}         # path -> monotonic ts

    def show_block(self, bid):
        self.block_id = bid
        self.clear()
        for name, mode, fields in REG_DEFS.get(bid, []):
            path = "%s.%s" % (bid.upper(), name)
            it = QTreeWidgetItem([name, "--"])
            it.setData(0, Qt.UserRole, (path, mode))
            it.setFont(1, MONO)
            if mode == "cold":
                it.setForeground(0, QBrush(COL_GREY))
                it.setForeground(1, QBrush(COL_GREY))
                it.setToolTip(0, "not polled - double-click to read once")
            elif mode == "guarded":
                it.setForeground(0, QBrush(COL_ANOM))
                it.setText(1, "[guarded]")
                it.setToolTip(0, "readAction register: reading has side "
                                 "effects. Double-click to force one read.")
            for fname, lo, hi in fields:
                ch = QTreeWidgetItem(["  .%s [%d:%d]" % (fname, hi, lo), "--"])
                ch.setData(0, Qt.UserRole, (path, "field", lo, hi))
                ch.setFont(1, MONO)
                it.addChild(ch)
            self.addTopLevelItem(it)
        self.expandAll()
        self.refresh()

    def refresh(self):
        snap = self.win.snap
        if not snap or self.block_id is None:
            return
        now = time.monotonic()
        for i in range(self.topLevelItemCount()):
            it = self.topLevelItem(i)
            path, mode = it.data(0, Qt.UserRole)[:2]
            if mode == "watched":
                v = snap["regs"].get(path)
                new = "0x%08X" % v if v is not None else "--"
                if it.text(1) != new and it.text(1) != "--":
                    self.changed_at[path] = now
                it.setText(1, new)
                ts = self.changed_at.get(path)
                flash = ts is not None and now - ts < 0.5
                it.setBackground(1, QBrush(QColor("#FFF59D") if flash
                                           else QColor("transparent")))
                for j in range(it.childCount()):
                    ch = it.child(j)
                    _, _, lo, hi = ch.data(0, Qt.UserRole)
                    mask = (1 << (hi - lo + 1)) - 1
                    ch.setText(1, str((v >> lo) & mask) if v is not None else "--")

    def on_double(self, item, col):
        data = item.data(0, Qt.UserRole)
        if data is None or len(data) != 2:
            return
        path, mode = data
        if mode == "cold":
            v = self.win.engine.reg_value(path)
            item.setText(1, "0x%08X (stale)" % v)
            self.win.log("info", "one-shot read %s" % path, self.block_id)
        elif mode == "guarded":
            r = QMessageBox.question(
                self, "Guarded register",
                "%s has read side effects (readAction).\n"
                "Reading it may disturb the running firmware.\n\nRead anyway?"
                % path)
            if r == QMessageBox.Yes:
                item.setText(1, "0x00000ABC (forced)")
                self.win.log("warn", "forced read of guarded %s" % path,
                             self.block_id)


class FlowPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.edge_id = None
        self.flow_id = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.head = QLabel()
        self.head.setWordWrap(True)
        self.listw = QListWidget()
        self.listw.setMaximumHeight(90)
        self.listw.itemClicked.connect(self.pick)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.RichText)
        self.detail.setAlignment(Qt.AlignTop)
        lay.addWidget(self.head)
        lay.addWidget(self.listw)
        lay.addWidget(self.detail, 1)

    def show_edge(self, eid, flow_ids, auto_pick):
        self.edge_id = eid
        self.flow_id = auto_pick
        self.head.setText("<b>edge %s</b> - flows through this edge:" % eid)
        self.listw.clear()
        for fid in flow_ids:
            it = QListWidgetItem(FLOWS[fid]["name"])
            it.setData(Qt.UserRole, fid)
            self.listw.addItem(it)
            if fid == auto_pick:
                it.setSelected(True)
        self.refresh()

    def pick(self, item):
        self.flow_id = item.data(Qt.UserRole)
        self.win.highlight_flow(self.flow_id)
        self.refresh()

    def refresh(self):
        snap = self.win.snap
        if not snap:
            return
        for i in range(self.listw.count()):
            it = self.listw.item(i)
            fid = it.data(Qt.UserRole)
            act = snap["flows"][fid]["active"]
            it.setText("%s %s" % ("[ACTIVE]" if act else "[idle]  ",
                                  FLOWS[fid]["name"]))
            it.setForeground(QBrush(COL_ACTIVE if act else COL_GREY))
        if not self.flow_id:
            self.detail.setText("")
            return
        f = FLOWS[self.flow_id]
        fs = snap["flows"][self.flow_id]
        rows = ["<b>%s</b> - %s" % (f["name"],
                "<span style='color:#1565C0'>ACTIVE</span>" if fs["active"]
                else "<span style='color:#888'>idle</span>"),
                "<code>active_when: %s</code>" % f["active_when"]]
        if f["progress"]:
            rows.append("<code>progress %s = 0x%04X</code>"
                        % (f["progress"], snap["ndtr"]))
        if f["rules"]:
            rows.append("<br><b>anomaly rules</b>")
            for expr, msg, target, state in fs["rules"]:
                dot = "&#9679;"
                color = "#C62828" if state else "#2E7D32"
                rows.append("<span style='color:%s'>%s</span> <code>%s</code>"
                            "<br>&nbsp;&nbsp;&nbsp;-> %s" % (color, dot, expr, msg))
        self.detail.setText("<br>".join(rows))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Data Path Explorer - UI prototype (stub data)")
        self.resize(1280, 800)
        self.engine = StubEngine()
        self.snap = None
        self.frozen = False
        self.tinted = True
        self.dash_phase = 0.0

        self.scene = QGraphicsScene(0, 0, 980, 560)
        self.scene.setBackgroundBrush(QBrush(Qt.white))
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setCentralWidget(self.view)

        self.blocks = {}
        for bid, spec in BLOCKS.items():
            item = BlockItem(bid, spec, self)
            self.scene.addItem(item)
            self.blocks[bid] = item
        self.wires = {}
        for eid, spec in EDGES.items():
            item = WireItem(eid, spec, self)
            self.scene.addItem(item)
            self.wires[eid] = item
        self.legend = LegendItem(self)
        self.scene.addItem(self.legend)

        self._build_toolbar()
        self._build_docks()

        self.engine.snapshot.connect(self.on_snapshot)
        self.engine.event.connect(self.log)
        self.engine.timer.start()

        self.anim = QTimer(self)
        self.anim.setInterval(60)
        self.anim.timeout.connect(self._advance_dash)
        self.anim.start()
        QTimer.singleShot(0, self.fit_view)

    def fit_view(self):
        self.view.fitInView(QRectF(0, 0, 980, 560), Qt.KeepAspectRatio)

    # -- chrome --------------------------------------------------------------
    def _build_toolbar(self):
        tb = QToolBar("main")
        tb.setMovable(False)
        tb.setStyleSheet(
            "QToolBar { spacing: 8px; padding: 4px; }"
            "QToolButton { padding: 7px 16px; font-size: 14px; }"
            "QLabel { font-size: 14px; }")
        self.addToolBar(tb)
        self.status_lbl = QLabel("  STM32F411 (stub)  ")
        tb.addWidget(self.status_lbl)
        tb.addSeparator()
        self.halt_act = QAction("Halt", self)
        self.halt_act.triggered.connect(self.toggle_halt)
        tb.addAction(self.halt_act)
        self.freeze_act = QAction("Freeze", self)
        self.freeze_act.setCheckable(True)
        self.freeze_act.toggled.connect(self.toggle_freeze)
        tb.addAction(self.freeze_act)
        self.tint_act = QAction("Tint", self)
        self.tint_act.setCheckable(True)
        self.tint_act.setChecked(True)
        self.tint_act.toggled.connect(self.toggle_tint)
        tb.addAction(self.tint_act)
        fit = QAction("Fit", self)
        fit.triggered.connect(self.fit_view)
        tb.addAction(fit)
        tb.addSeparator()
        self.chsel_act = QAction("CHSEL 0->6", self)
        self.chsel_act.triggered.connect(self.toggle_chsel)
        tb.addAction(self.chsel_act)
        ovr = QAction("Inject OVR", self)
        ovr.triggered.connect(self.engine.inject_overrun)
        tb.addAction(ovr)
        stall = QAction("Inject stall", self)
        stall.triggered.connect(self.engine.inject_stall)
        tb.addAction(stall)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.rate_lbl = QLabel("poll -- Hz  ")
        self.rate_lbl.setFont(MONO)
        tb.addWidget(self.rate_lbl)

    def _build_docks(self):
        self.reg_page = RegisterPage(self)
        self.flow_page = FlowPage(self)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.reg_page)
        self.stack.addWidget(self.flow_page)
        dock = QDockWidget("Inspector", self)
        dock.setWidget(self.stack)
        dock.setMinimumWidth(300)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)

        self.log_list = QListWidget()
        self.log_list.itemClicked.connect(self.log_clicked)
        dock2 = QDockWidget("Event log", self)
        dock2.setWidget(self.log_list)
        dock2.setMinimumHeight(100)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock2)
        self.resizeDocks([dock2], [140], Qt.Vertical)

    # -- actions -------------------------------------------------------------
    def toggle_halt(self):
        running = self.halt_act.text() == "Halt"
        self.halt_act.setText("Resume" if running else "Halt")
        self.log("info", "target %s (stub)" % ("halted" if running else "resumed"),
                 "cm4")

    def toggle_freeze(self, on):
        self.frozen = on
        self.status_lbl.setText("  STM32F411 (stub)  %s"
                                % ("[FROZEN]" if on else ""))
        if not on and self.snap:
            self._apply(self.snap)

    def toggle_tint(self, on):
        self.tinted = on
        for b in self.blocks.values():
            b.update()
        self.legend.update()

    def toggle_chsel(self):
        new = 6 if self.engine.chsel == 0 else 0
        self.engine.set_chsel(new)
        self.chsel_act.setText("CHSEL %d->%d" % (new, 6 if new == 0 else 0))

    # -- selection -----------------------------------------------------------
    def select_block(self, bid):
        for b in self.blocks.values():
            b.selected_ = (b.bid == bid)
            b.update()
        self.highlight_flow(None)
        self.reg_page.show_block(bid)
        self.stack.setCurrentWidget(self.reg_page)

    def select_edge(self, eid):
        flow_ids = [fid for fid, f in FLOWS.items() if eid in f["edges"]]
        if not flow_ids:
            return
        active = [fid for fid in flow_ids
                  if self.snap and self.snap["flows"][fid]["active"]]
        pick = active[0] if len(active) == 1 else flow_ids[0]
        for b in self.blocks.values():
            b.selected_ = False
            b.update()
        self.highlight_flow(pick)
        self.flow_page.show_edge(eid, flow_ids, pick)
        self.stack.setCurrentWidget(self.flow_page)

    def highlight_flow(self, fid):
        members_e = set(FLOWS[fid]["edges"]) if fid else set()
        members_b = set(FLOWS[fid]["blocks"]) if fid else set()
        for eid, w in self.wires.items():
            w.in_flow = eid in members_e
            w.update()
        for bid, b in self.blocks.items():
            b.in_flow = bid in members_b
            b.update()

    # -- data ----------------------------------------------------------------
    def on_snapshot(self, snap):
        self.snap = snap if not self.frozen else self.snap
        if self.frozen:
            self._pending = snap
            return
        self._apply(snap)

    def _apply(self, snap):
        self.snap = snap
        self.rate_lbl.setText("poll %4.1f Hz  " % snap["rate"])
        for eid, w in self.wires.items():
            act = any(snap["flows"][fid]["active"] and eid in FLOWS[fid]["edges"]
                      for fid in FLOWS)
            w.active = act
            mi = w.spec.get("mux_input")
            w.mux_off = mi is not None and mi != snap["chsel"]
            w.update()
        for bid, b in self.blocks.items():
            b.badge = snap["badges"].get(bid)
            b.update()
        self.reg_page.refresh()
        self.flow_page.refresh()

    def _advance_dash(self):
        if self.frozen:
            return
        self.dash_phase = (self.dash_phase + 1.3) % 100
        for w in self.wires.values():
            if w.active:
                w.update()

    def log(self, severity, text, focus_id):
        ts = time.strftime("%H:%M:%S")
        it = QListWidgetItem("[%s] %-5s %s" % (ts, severity.upper(), text))
        it.setFont(MONO)
        it.setData(Qt.UserRole, focus_id)
        if severity == "anomaly":
            it.setForeground(QBrush(COL_ANOM))
        elif severity == "warn":
            it.setForeground(QBrush(QColor("#E65100")))
        self.log_list.addItem(it)
        sb = self.log_list.verticalScrollBar()
        if sb.value() >= sb.maximum() - 4:
            self.log_list.scrollToBottom()

    def log_clicked(self, item):
        fid = item.data(Qt.UserRole)
        if fid in self.blocks:
            self.select_block(fid)
            self.view.centerOn(self.blocks[fid])

    def wheelEvent(self, ev):
        if ev.angleDelta().y() > 0:
            self.view.scale(1.15, 1.15)
        else:
            self.view.scale(1 / 1.15, 1 / 1.15)


def main():
    app = QApplication(sys.argv)
    # tool is light-theme only by design; never follow the OS dark appearance
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    except AttributeError:
        pass
    win = Main()
    win.show()

    if "--shot" in sys.argv:
        out = sys.argv[sys.argv.index("--shot") + 1]
        # drive the stub into an interesting state: active flow + latched badge
        for _ in range(12):
            win.engine.tick()
            app.processEvents()
        win.engine.inject_overrun()
        for _ in range(4):
            win.engine.tick()
            app.processEvents()
        win.select_edge("e_dma_bus")
        for _ in range(2):
            win.engine.tick()
            app.processEvents()
        win.grab().save(out)
        # second variant: tinted + block selected (register inspector)
        win.tint_act.setChecked(True)
        win.select_block("dma2")
        for _ in range(2):
            win.engine.tick()
            app.processEvents()
        out2 = out.replace(".png", "_b.png")
        win.grab().save(out2)
        print("saved", out, out2)
        return 0
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
