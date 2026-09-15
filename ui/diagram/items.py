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
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QPainter, QPainterPath,
                           QPainterPathStroker, QPen, QPolygonF)
from PySide6.QtWidgets import (QApplication, QGraphicsItem,
                               QGraphicsSceneMouseEvent)

from core.target.topology import Block, Edge

from ..style import (COL_ACTIVE, COL_ANOM, COL_EDIT_SELECT, COL_GREY,
                    COL_WARN, MONO)

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

# M8 task 5 manual-gate fix (finding 2): grab/click affordance. A
# WaypointHandle is bigger than the block resize handle (_HANDLE_SIZE
# stays 8, unchanged - this is a separate constant on purpose) and a
# wire's own clickable shape() widens in edit mode, since that is
# where double-click-to-insert precision actually matters; normal-mode
# click-to-select keeps the original width.
_WAYPOINT_HANDLE_SIZE = 12
_WIRE_HIT_WIDTH = 12
_WIRE_HIT_WIDTH_EDIT = 20
_HANDLE_HOVER_FILL = QColor("#FFE0B2")
# Endpoint re-anchor magnet (finding 2d): releasing an ENDPOINT
# waypoint handle within this many scene units of its own block's
# boundary snaps it onto that boundary (then grid-snaps). A block
# MOVE now glues an explicit path's endpoints to it automatically
# (BlockItem.itemChange -> DiagramState.on_block_live_moved ->
# WireItem.translate_endpoint, M8 wave 2) - the magnet's remaining job
# is the cases glue does not cover: a block RESIZE (only a position
# change fires the glue callback) and any manual fine-tune drag of the
# endpoint itself.
_ENDPOINT_MAGNET_PX = 20

# M8 wave B1: arrow-key nudge. Shared by BlockItem/WaypointHandle/
# LegendItem's keyPressEvent - one grid step per press, one unit with
# Shift, matching the drag-time grid/fine step sizes exactly. Reads
# the SHIFT state off the key event's own modifiers() rather than
# _fine_snap()'s QApplication.keyboardModifiers() (used for live drags,
# where polling global state each mouse-move is the natural fit) - a
# discrete keypress already carries its own modifiers on the event,
# which is both the more direct source and the only one an offscreen
# test can drive without a real keyboard.
_ARROW_DELTAS = {
    Qt.Key_Left: (-1, 0),
    Qt.Key_Right: (1, 0),
    Qt.Key_Up: (0, -1),
    Qt.Key_Down: (0, 1),
}


def _nudge_step(ev) -> int:
    fine = bool(ev.modifiers() & Qt.ShiftModifier)
    return _FINE_STEP if fine else _GRID_STEP


# M8 wave B4: dynamic alignment guides. 6px (scene units) - deliberately
# tighter than the 20px endpoint magnet (finding 2d): a block-alignment
# guide should only bite once the user is clearly lining edges up, not
# every time two blocks happen to pass near each other while dragging.
_ALIGN_THRESHOLD = 6


def _compute_alignment_snap(x: float, y: float, w: float, h: float,
                            others: List[Tuple[float, float, float, float]],
                            threshold: float = _ALIGN_THRESHOLD
                            ) -> Tuple[float, float, Optional[float],
                                      Optional[float]]:
    """Given a candidate top-left (x, y) and size (w, h) for a dragged
    block, and the (x, y, w, h) of every OTHER block, finds the
    CLOSEST same-type alignment - left-to-left, hcenter-to-hcenter, or
    right-to-right for the x axis; top-to-top, vcenter-to-vcenter, or
    bottom-to-bottom for y - within `threshold`, independently per
    axis (an x match and a y match can come from two different other
    blocks). Returns (snapped_x, snapped_y, guide_x, guide_y):
    snapped_x/y is x/y shifted so the matched line lands EXACTLY on
    the other block's line (unchanged from the input on an axis with
    no match); guide_x/guide_y is the scene coordinate of the matched
    line itself, for a caller to draw a reference line at - None on an
    axis with no match. Pure (plain floats, no Qt/item dependency), so
    it is unit-testable directly and reusable by BOTH the live snap
    decision (BlockItem.itemChange) and, if a caller wants to
    recompute after the fact, guide rendering.

    M8 wave B fix round 3 (structural bound, user-acceptance finding
    1): the return value is clamped to within `threshold` of the
    INPUT (x, y) on each axis BY CONSTRUCTION (max/min below), not
    merely as a consequence of the best_x/best_y search only ever
    accepting candidates with abs(d) <= threshold. The search's own
    gating already enforced this in practice, but the caller (an
    itemChange handler that only ever calls this once per proposed
    position) has no independent way to verify the bound holds - a
    future edit to the search loop that widened or dropped that gate
    would silently regress this function back into an unbounded
    teleport. Clamping the OUTPUT here, unconditionally, makes the
    bound hold no matter what the search above does."""
    cx_lines = (x, x + w / 2.0, x + w)
    cy_lines = (y, y + h / 2.0, y + h)
    best_x: Optional[Tuple[float, float, float]] = None   # (|d|, d, line)
    best_y: Optional[Tuple[float, float, float]] = None
    for (ox, oy, ow, oh) in others:
        ox_lines = (ox, ox + ow / 2.0, ox + ow)
        oy_lines = (oy, oy + oh / 2.0, oy + oh)
        for c, o in zip(cx_lines, ox_lines):
            d = o - c
            if abs(d) <= threshold and (best_x is None
                                        or abs(d) < best_x[0]):
                best_x = (abs(d), d, o)
        for c, o in zip(cy_lines, oy_lines):
            d = o - c
            if abs(d) <= threshold and (best_y is None
                                        or abs(d) < best_y[0]):
                best_y = (abs(d), d, o)
    raw_snapped_x = x + best_x[1] if best_x is not None else x
    raw_snapped_y = y + best_y[1] if best_y is not None else y
    snapped_x = max(x - threshold, min(x + threshold, raw_snapped_x))
    snapped_y = max(y - threshold, min(y + threshold, raw_snapped_y))
    guide_x = best_x[2] if best_x is not None else None
    guide_y = best_y[2] if best_y is not None else None
    return snapped_x, snapped_y, guide_x, guide_y


def snap(value: float, fine: bool) -> int:
    """Grid-snap `value`: 10-unit steps normally, 1-unit when `fine`
    (Shift held). Pure so tests can hit it directly."""
    step = _FINE_STEP if fine else _GRID_STEP
    return int(round(value / step)) * step


def _fine_snap() -> bool:
    return bool(QApplication.keyboardModifiers() & Qt.ShiftModifier)


def _select_exclusively(item: QGraphicsItem) -> None:
    """Select ONLY `item`, clearing any other currently-selected item
    first (M8 wave B fix round 4, user-acceptance finding: clicking
    three items in a row left all three showing the selection
    outline). QGraphicsItem.setSelected(True) alone is ADDITIVE -
    normally a plain click's base-class mousePressEvent handling is
    what clears the scene's previous selection, but every edit-mode
    press site below fully consumes its own event (ev.accept(), no
    super().mousePressEvent(ev) call), so that base-class clearing
    never runs. Multi-select (Ctrl/Shift-click) is an explicit backlog
    exclusion - single, exclusive selection is the spec'd model.

    `item.scene()` is None for an item under test construction that
    was never added to a QGraphicsScene (several tests build a
    WireItem/LegendItem standalone) - guarded rather than assumed,
    since clicking such an item is still a valid, scene-independent
    way to exercise its own selection/nudge behavior directly."""
    scene = item.scene()
    if scene is not None:
        scene.clearSelection()
    item.setSelected(True)


def _run_base_mouse_handler(base_handler, ev) -> None:
    """Deliver `ev` to a QGraphicsItem BASE-CLASS mouse handler (pass
    the bound super() method), skipping non-Qt stand-in events.

    M8 wave B fix round 6 (user-acceptance finding, round 3 - the
    drag-teleport that survived rounds 1 AND 2): every edit-mode press/
    release override below used to fully consume its event (ev.accept()
    with no super() call). That starves Qt's own drag bookkeeping - in
    particular QGraphicsScenePrivate::movingItemsInitialPositions, the
    map the default ItemIsMovable mouse-move handler computes every
    drag step from:

        item->setPos(initialPositions.value(item)
                     + currentParentPos - buttonDownParentPos)

    That map is populated on the FIRST move of a drag ONLY IF EMPTY,
    and cleared in exactly ONE place in all of Qt: the base
    QGraphicsItem::mouseReleaseEvent. Skip that super() call and the
    first completed drag leaves the map permanently holding {dragged
    item: its pre-drag position}; the NEXT drag of any OTHER movable
    item then finds the map non-empty (refill skipped), reads
    .value(item) == default-constructed QPointF(0, 0) for itself, and
    teleports to (0, 0) + the slight drag delta - the view's top-left
    corner - while a second drag of the SAME item jumps back to its
    stale pre-first-drag position instead. (buttonDownScenePos is NOT
    part of this: the scene stores that at press-accept time whether
    or not super() runs.)

    So BlockItem/LegendItem - the two ItemIsMovable items that ride
    Qt's default drag handler - must ALWAYS let the base press/release
    run alongside their custom logic. The isinstance gate exists for
    the item-level tests that drive these overrides directly with
    plain-Python _FakeEvent stand-ins: the C++ base implementation
    cannot accept those, and the base bookkeeping this call exists for
    only matters on the real-event chain, which always delivers real
    QGraphicsSceneMouseEvents."""
    if isinstance(ev, QGraphicsSceneMouseEvent):
        base_handler(ev)


def _scene_view(item: QGraphicsItem):
    """The QGraphicsView currently displaying `item`'s scene, or None
    if it has no scene, or that scene has no view (an item under test
    construction that was never added to a scene, or a scene never
    shown in a view - build_scene()'s own return value, before
    MainWindow wraps it in a _DiagramView)."""
    scene = item.scene()
    if scene is None:
        return None
    views = scene.views()
    return views[0] if views else None


def _view_mapping_fingerprint(view) -> QPointF:
    """The scene point currently shown at the view's own (0, 0) -
    encodes the view's ENTIRE widget-pixel-to-scene mapping (transform
    scale/rotation AND scroll position AND viewport size all at once)
    in one comparable value, for detecting a mid-gesture mapping shift
    (see _GestureMappingGuard's docstring)."""
    return view.mapToScene(QPoint(0, 0))


class _GestureMappingGuard:
    """M8 wave B fix round 5 (user-acceptance finding, round 2: the
    round-1 fix - gating _apply_zoom while a gesture is in flight -
    was NOT the whole story. A window/dock resize mid-drag reproduces
    the identical "teleport" with NO zoom call anywhere involved:
    resizing the view's VIEWPORT shifts its scrollbar range/position
    even with the transform's scale left untouched, which is enough by
    itself to corrupt Qt's default ItemIsMovable drag tracking the
    same way a scale change does - see BlockItem.itemChange's matching
    comment for the mechanism. Rather than keep chasing individual
    triggers (zoom today, resize now, something else next), this
    detects the SYMPTOM directly and generically: has the view's
    widget-to-scene mapping - _view_mapping_fingerprint - moved since
    this gesture started, for ANY reason at all.

    Usage: `arm()` in mousePressEvent (while editable); `drifted()` at
    the top of itemChange's ItemPositionChange branch - if True, the
    incoming `value` is not trustworthy (it reflects the mapping
    shift, not real mouse movement) and must be discarded outright
    (return the item's OWN current position, unchanged) rather than
    fed to snap/alignment logic, which can only ever bound a
    correction RELATIVE TO an already-corrupted input.

    Once drift is detected, EVERY remaining itemChange call for this
    SAME gesture is rejected too (a "poisoned" gesture), not just the
    one call that first noticed it. Qt's own default ItemIsMovable
    tracking computes each step from event->pos() (current mapping)
    minus buttonDownPos() (captured ONCE, at press, under whatever
    mapping was active then) - buttonDownPos() is never recalibrated
    mid-gesture by Qt itself, so once the two mappings disagree they
    stay disagreeing, by roughly the same margin, for every subsequent
    step of this same drag. Resyncing the baseline and trusting the
    very next step (an earlier version of this class did that) still
    let the next step apply a similarly-corrupted value. The item
    simply stays frozen at wherever it was when the drift was first
    caught until mouseReleaseEvent ends this gesture; the next press
    starts a fresh, uncorrupted one."""

    def __init__(self):
        self._baseline: Optional[QPointF] = None
        self._poisoned = False

    def arm(self, item: QGraphicsItem) -> None:
        view = _scene_view(item)
        self._baseline = (_view_mapping_fingerprint(view)
                          if view is not None else None)
        self._poisoned = False

    def drifted(self, item: QGraphicsItem) -> bool:
        if self._poisoned:
            return True   # already corrupted this gesture - stay frozen
        if self._baseline is None:
            return False   # never armed (no view) - nothing to compare
        view = _scene_view(item)
        if view is None:
            return False
        if _view_mapping_fingerprint(view) == self._baseline:
            return False
        self._poisoned = True
        return True


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
        # M8 wave B2: set by a WaypointHandle dragging an endpoint
        # toward THIS block, while within the endpoint magnet's range
        # (see WaypointHandle._update_magnet_highlight) - paint() reads
        # it to brighten the outline as a "drop here to connect" cue.
        self._magnet_highlighted = False
        # M8 wave B4 (dynamic alignment guides): (guide_x, guide_y) as
        # of the LAST itemChange position-snap decision - None on an
        # axis with no active alignment. MainWindow reads this after
        # on_block_live_moved fires to draw/hide the reference lines;
        # cleared (both None) whenever a nudge/apply_geometry/Shift
        # drag bypasses alignment snapping entirely.
        self._active_guides: Tuple[Optional[float], Optional[float]] = (
            None, None)
        # M8 wave B fix round 5: see _GestureMappingGuard's docstring.
        self._gesture_mapping = _GestureMappingGuard()

    def _position_handle(self) -> None:
        self.handle.setPos(self.w - _HANDLE_SIZE, self.h - _HANDLE_SIZE)

    def set_magnet_highlight(self, on: bool) -> None:
        on = bool(on)
        if on != self._magnet_highlighted:
            self._magnet_highlighted = on
            self.update()

    def set_editable(self, on: bool) -> None:
        self._editable = bool(on)
        self.setFlag(QGraphicsItem.ItemIsMovable, on)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, on)
        # M8 wave B1 (arrow-key nudge): selectable/focusable only while
        # editing - matches WaypointHandle, which has carried both
        # unconditionally since T4's Delete flow (a handle only exists
        # at all while editable, so it never needed the on/off dance).
        self.setFlag(QGraphicsItem.ItemIsSelectable, on)
        self.setFlag(QGraphicsItem.ItemIsFocusable, on)
        self.handle.setVisible(on)
        # finding 2c: an open-hand cursor signals "draggable" while
        # editing; cleared (falls back to whatever the view/cursor
        # stack underneath shows) the moment edit mode exits.
        if on:
            self.setCursor(Qt.OpenHandCursor)
        else:
            self.unsetCursor()

    def geometry(self) -> Tuple[int, int, int, int]:
        pos = self.pos()
        return (int(pos.x()), int(pos.y()), int(self.w), int(self.h))

    def apply_geometry(self, x: int, y: int, w: int, h: int,
                       sync_press: bool = True) -> None:
        """Undo restore path: moves the item AND updates self.block.
        setPos is wrapped in _applying so itemChange's live grid-snap
        (and B4's alignment snap) does not intercept this programmatic
        move - the restored position must land exactly, snap-aligned
        or not. Always clears _active_guides - a programmatic move is
        never itself an alignment-snap decision, so a guide line left
        over from an earlier real drag would otherwise render stale.

        sync_press=False (M8 wave B fix: nudge glue) skips resyncing
        _geom_at_press to the new position - used ONLY by
        keyPressEvent's arrow-key nudge, which needs _geom_at_press to
        keep reading the PRE-nudge position for one more call
        (DiagramState.on_block_live_moved, fired right after this) so
        MainWindow's live-move delta computation - gesture_origin() as
        the first-call baseline, since no mouse drag is ever
        concurrently in progress - resolves to the nudge's own exact
        delta instead of zero; see keyPressEvent for the full
        sequence and why."""
        self.prepareGeometryChange()
        self.w, self.h = w, h
        self.block.w, self.block.h = w, h
        self._applying = True
        try:
            self.setPos(x, y)
        finally:
            self._applying = False
        self.block.x, self.block.y = int(x), int(y)
        if sync_press:
            self._geom_at_press = (int(x), int(y))
        self._active_guides = (None, None)
        self._position_handle()
        self.update()

    def itemChange(self, change, value):
        if (change == QGraphicsItem.ItemPositionChange and self._editable
                and not self._applying):
            # M8 wave B fix round 5 (structural + root cause, user-
            # acceptance finding round 2): if the view's widget-to-
            # scene mapping shifted since this gesture started (a
            # zoom, a window/dock resize, anything) `value` reflects
            # that shift, not real mouse movement, and can be
            # arbitrarily far from where the mouse actually is - see
            # _GestureMappingGuard's docstring. Discard it outright
            # (keep the item exactly where it already is) rather than
            # feed it to snap/alignment below, which can only ever
            # bound a correction RELATIVE TO its input - an
            # already-corrupted input stays corrupted no matter how
            # tightly the correction itself is bounded.
            if self._gesture_mapping.drifted(self):
                return self.pos()
            fine = _fine_snap()
            if fine:
                # M8 wave B4: Shift disables alignment snapping
                # entirely, along with grid snapping falling back to
                # its existing 1-unit fine step - the user is asking
                # for unassisted, precise placement.
                self._active_guides = (None, None)
                return QPointF(snap(value.x(), True), snap(value.y(), True))
            others = [item.geometry() for item in self.state.blocks.values()
                     if item is not self]
            sx, sy, gx, gy = _compute_alignment_snap(
                value.x(), value.y(), self.w, self.h, others)
            # alignment wins over grid snap on whichever axis matched;
            # the other axis (or both, with no match anywhere) still
            # falls back to the existing grid snap.
            fx = sx if gx is not None else snap(sx, False)
            fy = sy if gy is not None else snap(sy, False)
            # Structural bound, enforced AGAIN here at the application
            # site (not only inside _compute_alignment_snap's own
            # clamp) - belt and suspenders: neither axis's applied
            # value may differ from what the drag machinery proposed
            # by more than one alignment threshold beyond the grid
            # step's own rounding budget.
            budget = _ALIGN_THRESHOLD + _GRID_STEP / 2.0
            fx = max(value.x() - budget, min(value.x() + budget, fx))
            fy = max(value.y() - budget, min(value.y() + budget, fy))
            self._active_guides = (gx, gy)
            return QPointF(fx, fy)
        if (change == QGraphicsItem.ItemPositionHasChanged
                and self._editable and not self._applying):
            # M8 wave 2 (Visio-style connector glue): fires on every
            # snapped step of a live drag (ItemSendsGeometryChanges is
            # only on while editable, so this never fires outside a
            # drag; _applying excludes apply_geometry's own
            # programmatic setPos - undo/revert/auto-layout notify
            # MainWindow through their own explicit calls instead, not
            # this live-drag path). MainWindow filters to the wires
            # actually attached to this block.
            self.state.on_block_live_moved(self.block.id)
            self._emit_live_status()   # M8 wave B3
        return super().itemChange(change, value)

    def _emit_live_status(self) -> None:
        """M8 wave B3 (live coordinate readout): "id: x, y (w x h)" -
        the same format whether a position drag or a resize is what is
        actually changing (see _ResizeHandle.mouseMoveEvent's matching
        call), since both read the block's current, full geometry."""
        x, y, w, h = self.geometry()
        self.state.on_live_status(
            "%s: %d, %d (%d x %d)" % (self.block.id, x, y, w, h))

    def gesture_origin(self) -> Tuple[int, int]:
        """The (x, y) this block's CURRENT drag/resize gesture started
        from - _geom_at_press, exposed read-only so MainWindow's live-
        move handler (see DiagramState.on_block_live_moved) can compute
        the FULL delta since gesture start on its very first callback,
        rather than only catching up from the second snapped step
        onward."""
        return self._geom_at_press

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
        if self._magnet_highlighted:
            # M8 wave B2: "drop here to connect" cue while a
            # WaypointHandle endpoint drag is within magnet range of
            # this block - takes priority over the live-inspection
            # selected_/in_flow pens, which are only meaningful outside
            # edit mode anyway.
            p.setPen(QPen(COL_ACTIVE, 3.2))
        elif self.state.edit_mode and self.isSelected():
            # M8 wave B fix round 1 (finding 4): visible selection
            # indicator - without it, a block selected for the arrow-
            # key nudge (B1) gave no visual sign anything was
            # selected, so the nudge feature itself was undiscoverable.
            # Distinct COL_EDIT_SELECT (teal), not COL_ACTIVE/COL_SELECT
            # - see that constant's comment in style.py for why.
            p.setPen(QPen(COL_EDIT_SELECT, 2.2))
        elif selected_:
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
            self.setCursor(Qt.ClosedHandCursor)   # finding 2c
            # M8 wave B1: click-to-select/focus, mirroring WaypointHandle's
            # own mousePressEvent - the arrow-key nudge below only ever
            # fires on whichever item currently holds keyboard focus.
            # Exclusive (see _select_exclusively's docstring) as of
            # wave B fix round 4.
            _select_exclusively(self)
            self.setFocus(Qt.MouseFocusReason)
            # M8 wave B fix round 5: arm this gesture's mapping
            # baseline - see _GestureMappingGuard's docstring.
            self._gesture_mapping.arm(self)
        if self.state.edit_mode:
            # M8 wave B fix round 6: the base class MUST see the press
            # too, so Qt's own drag bookkeeping is armed for this
            # gesture - see _run_base_mouse_handler's docstring. Runs
            # AFTER _select_exclusively above, so the base handler's
            # own selection pass (clear-then-select on a not-yet-
            # selected item) finds the item already selected and
            # leaves the selection state exactly as the tests pin it.
            # Normal mode stays super()-free: with every interaction
            # flag off, the base press would just ignore() the event,
            # and the inspector click routing below needs it accepted.
            _run_base_mouse_handler(super().mousePressEvent, ev)
            ev.accept()
            return
        badge = self.state.badges.get(self.block.id)
        if badge and badge.count > 0 and self.badge_rect().contains(ev.pos()):
            self.state.on_badge_clicked(self.block.id)
        else:
            self.state.on_block_clicked(self.block.id)
        ev.accept()

    def mouseReleaseEvent(self, ev):
        # M8 wave B fix round 6 (THE round-3 teleport root cause):
        # the base-class release is the ONE place in Qt that clears
        # QGraphicsScenePrivate::movingItemsInitialPositions - skip it
        # and the next drag of any movable item computes its steps
        # from this gesture's stale map and teleports; see
        # _run_base_mouse_handler's docstring. Unconditional (not
        # gated on _editable): the release of a drag whose edit mode
        # was toggled off mid-gesture still lands here, and must
        # still tear the map down.
        _run_base_mouse_handler(super().mouseReleaseEvent, ev)
        if self._editable:
            pos = self.pos()
            new_xy = (int(pos.x()), int(pos.y()))
            if new_xy != self._geom_at_press:
                self.block.x, self.block.y = new_xy
                self.state.on_geometry_changed()
            self._geom_at_press = new_xy
            self.setCursor(Qt.OpenHandCursor)   # finding 2c: back to open
            # M8 wave B fix round 2 (regression): gated back on
            # self._editable - a mode-exit mid-drag is already covered
            # synchronously by MainWindow._cancel_gesture_visuals
            # (toggle-off calls it BEFORE the set_editable(False) loop
            # below runs, per its own docstring), so this is only
            # end-of-real-gesture cleanup while still editing. Ungated,
            # this fired on every NORMAL-mode click too (this item
            # accepts presses in both modes, for inspector clicks) and
            # clobbered MainWindow.on_state's persistent
            # "poller: ..." status line on ordinary clicks.
            self.state.on_live_status("")
        ev.accept()

    def keyPressEvent(self, ev) -> None:
        """M8 wave B1: arrow-key nudge - one grid step (10) per press,
        one unit with Shift, on the currently-focused/selected block.
        A completed gesture like a drag commit: moves via
        apply_geometry (snap-bypassing - the step is already exact, it
        must land exactly, not get re-snapped/aligned against a
        possibly off-grid starting position), glues any attached
        explicit-path wire endpoint by that SAME delta, and fires
        on_geometry_changed once so undo/dirty ride the same path a
        mouse drag commit uses. Inert outside edit mode (mirrors
        WaypointHandle's Delete handling - this item would not hold
        focus outside edit mode in the real app, but a test may call
        this directly).

        Endpoint glue fix (M8 wave B, post-review): a nudge IS a move,
        and Visio-style connector glue is input-agnostic - it used to
        desync an explicit endpoint from the block by the nudge delta
        while an auto-routed wire still correctly re-routed, exactly
        the mouse-vs-keyboard inconsistency the glue wave had already
        eliminated for drags. Fixed by reusing
        DiagramState.on_block_live_moved - the SAME callback a live
        mouse drag fires per step - rather than duplicating its
        delta/translate/refresh logic: apply_geometry(sync_press=False)
        moves the item without yet resyncing _geom_at_press, so when
        on_block_live_moved runs immediately after, MainWindow's delta
        computation (gesture_origin() as the first-call baseline,
        always correct here since no mouse drag is ever concurrently
        in progress) resolves to exactly this nudge's delta - one
        delta, one translate, one commit. _geom_at_press is then
        resynced by hand before the commit call, matching what
        apply_geometry itself would have done with the default
        sync_press=True."""
        if not self._editable or ev.key() not in _ARROW_DELTAS:
            ev.accept()
            return
        ddx, ddy = _ARROW_DELTAS[ev.key()]
        step = _nudge_step(ev)
        x, y, w, h = self.geometry()
        self.apply_geometry(x + ddx * step, y + ddy * step, w, h,
                            sync_press=False)
        self.state.on_block_live_moved(self.block.id)
        pos = self.pos()
        self._geom_at_press = (int(pos.x()), int(pos.y()))
        self.state.on_geometry_changed()
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
        # finding 2c: diagonal-resize cursor. Set unconditionally - the
        # handle only exists/is interactive while its parent block is
        # editable (BlockItem.set_editable toggles handle.setVisible),
        # so there is no separate on/off to manage here.
        self.setCursor(Qt.SizeFDiagCursor)

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
        block_item._emit_live_status()   # M8 wave B3
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
        block_item.state.on_live_status("")   # M8 wave B3: clear on release
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


def _nearest_rect_boundary_point(px: float, py: float, x: float, y: float,
                                 w: float, h: float) -> Tuple[float, float]:
    """Nearest point ON the boundary (perimeter, not interior) of the
    rect (x, y, w, h) to (px, py) - used by the endpoint re-anchor
    magnet (finding 2d) to snap a released handle onto its block's
    edge. If (px, py) is outside the rect, simple clamping already
    lands exactly on the boundary; if it is inside (or exactly on it),
    clamping alone would land in the interior, so the clamped point is
    pushed out to whichever of the 4 edges is closest."""
    cx = max(x, min(px, x + w))
    cy = max(y, min(py, y + h))
    if cx != px or cy != py:
        return cx, cy
    d_left, d_right = cx - x, (x + w) - cx
    d_top, d_bottom = cy - y, (y + h) - cy
    m = min(d_left, d_right, d_top, d_bottom)
    if m == d_left:
        return x, cy
    if m == d_right:
        return x + w, cy
    if m == d_top:
        return cx, y
    return cx, y + h


# ---------------------------------------------------------------------------
# M8 task 5 fix round 2: live auto-route recompute. This IS the single
# routing implementation for a pointless edge's straight-line path -
# scene.py's build_scene imports _straight_route_points from here for
# an edge's initial route (scene.py already imports BlockItem/WireItem
# from this module, so the direction was always fine); WireItem.
# refresh_auto_route (below) calls it again later to recompute that
# same route once the blocks have moved.
# ---------------------------------------------------------------------------


def _side_point(x: float, y: float, w: float, h: float, side: str,
                frac: float) -> Tuple[float, float]:
    if side == "l":
        return x, y + h * frac
    if side == "r":
        return x + w, y + h * frac
    if side == "t":
        return x + w * frac, y
    return x + w * frac, y + h   # "b"


def _ranges_overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 and b0 < a1


def _facing_sides(sx: float, sy: float, sw: float, sh: float, dx: float,
                  dy: float, dw: float, dh: float) -> Tuple[str, str]:
    if not _ranges_overlap(sx, sx + sw, dx, dx + dw):
        if sx + sw <= dx:
            return "r", "l"
        return "l", "r"
    if sy + sh <= dy:
        return "b", "t"
    return "t", "b"


def _straight_route_points(src_item: "BlockItem", dst_item: "BlockItem",
                           edge: Edge) -> List[QPointF]:
    """Two-point straight route between src_item/dst_item's CURRENT
    geometry, honoring the edge's declared ports if any. Position
    comes from src_item.pos()/dst_item.pos() - the LIVE QGraphicsItem
    position - deliberately NOT from src_item.block.x/y: BlockItem
    only writes a drag's final position back into block.x/y at
    mouseReleaseEvent (see BlockItem.mouseReleaseEvent), so during a
    live drag (M8 wave 2's mid-gesture refresh_auto_route calls)
    block.x/y is still the GESTURE-START position - reading it here
    would silently recompute the OLD route every time, defeating the
    whole point of a live reroute. pos() carries no such lag; at
    construction and at every commit it is exactly block.x/y anyway,
    so this is a strict improvement with no behavior change outside a
    live drag. block.ports is still read off src_item.block/
    dst_item.block - the declared-port table is static, never
    mid-drag state. The single routing implementation: scene.py's
    build_scene imports this directly for a pointless edge's initial
    path, and WireItem.refresh_auto_route (below) calls it again to
    recompute that same route later, after a block has moved."""
    src_b, dst_b = src_item.block, dst_item.block
    sx, sy = src_item.pos().x(), src_item.pos().y()
    dx, dy = dst_item.pos().x(), dst_item.pos().y()
    side_s, side_d = _facing_sides(sx, sy, src_item.w, src_item.h,
                                   dx, dy, dst_item.w, dst_item.h)
    frac_s = frac_d = 0.5
    port = src_b.ports.get(edge.src_port) if edge.src_port else None
    if port:
        side_s, frac_s = port
    port = dst_b.ports.get(edge.dst_port) if edge.dst_port else None
    if port:
        side_d, frac_d = port
    p0 = _side_point(sx, sy, src_item.w, src_item.h, side_s, frac_s)
    p1 = _side_point(dx, dy, dst_item.w, dst_item.h, side_d, frac_d)
    return [QPointF(*p0), QPointF(*p1)]


class WireItem(QGraphicsItem):
    """Renders one Edge as a polyline: the explicit full path from
    edge.points when non-empty (index 0 and -1 are the source/dest
    anchors, everything between is an interior waypoint), an
    auto-routed straight line to the connected blocks' ports
    otherwise. A moved block drags BOTH kinds of wire along with it
    (Visio-style connector glue, M8 wave 2): a pointless wire's line
    is recomputed from the block's live position (refresh_auto_route,
    called by MainWindow after every block-geometry change - drag,
    auto-layout, undo, revert); an explicit path's own endpoint
    anchor(s) translate by the block's exact drag delta, live, during
    the drag (translate_endpoint, called from
    DiagramState.on_block_live_moved). The one remaining limitation is
    interior waypoints: they do not re-route around a moved block - a
    dog-leg that used to clear an obstacle may need re-jigging by hand
    (drag the interior WaypointHandle) after the blocks around it
    move."""

    def __init__(self, edge: Edge, pts, state):
        super().__init__()
        self.edge = edge
        self.state = state
        self.pts = pts
        self.setZValue(-1)
        # M8 task 4 (waypoint editing): _editable gates whether
        # WaypointHandle children exist at all - they are created
        # lazily by set_editable(True) and torn down by
        # set_editable(False), never merely hidden.
        #
        # _auto_pts is this wire's cached straight-line auto-route
        # fallback - what edge.points collapses back to on deleting
        # the last waypoint (remove_point) or via apply_points([]).
        # Seeded from `pts` unconditionally here, which is only
        # correct for an edge that started pointless (build_scene
        # passes the true auto-route as `pts` in that case); for an
        # edge that started WITH an explicit path this seed is wrong
        # (it is that original path, not a straight route) until
        # refresh_auto_route() runs at least once - see that method's
        # docstring for who calls it and when. WireItem has no
        # reference to the src/dst BlockItems at construction (only
        # DiagramState, which carries no block geometry) - _src_item/
        # _dst_item start out None and are populated the first time
        # refresh_auto_route(blocks) runs (finding 2d's endpoint
        # magnet reads them off a WaypointHandle release; they stay
        # None for a wire built standalone in a test that never calls
        # refresh_auto_route, which simply disables the magnet for it).
        self._editable = False
        self._handles: List["WaypointHandle"] = []
        self._auto_pts = list(pts)
        self._src_item = None
        self._dst_item = None

    def set_editable(self, on: bool) -> None:
        self._editable = bool(on)
        if self._editable:
            self._rebuild_handles()
        else:
            self._clear_handles()
        # finding 2c: a cross-hair cursor signals the double-click-to-
        # insert affordance while editing; cleared on exit.
        if on:
            self.setCursor(Qt.CrossCursor)
        else:
            self.unsetCursor()

    def edge_key(self) -> Tuple[str, str, str]:
        """Identity tuple matching core/target/layout_io.py's
        _edge_identity(): (src, dst, label-or-empty-string) - Task 5
        uses this to key layout_io.patch_layout_text's edge_points
        dict."""
        return (self.edge.src, self.edge.dst, self.edge.label or "")

    def _clear_handles(self) -> None:
        """Destroys every WaypointHandle - called by set_editable(False)
        (mode exit) and by _rebuild_handles (points-list changes). M8
        wave B fix round 1 (finding 2): a handle mid-drag when this
        runs (mode exit mid-drag, the exact path the review
        reproduced) dies WITHOUT ever reaching its own
        mouseReleaseEvent - clear any magnet highlight it left on its
        target block here, at the source, so destroying the handle can
        never leave a block stuck highlighted with nothing left to
        clear it. (MainWindow._cancel_gesture_visuals sweeps every
        block's highlight too, as a second, independent backstop - the
        two do not depend on each other.)"""
        for h in self._handles:
            if h._magnet_target is not None:
                h._magnet_target.set_magnet_highlight(False)
                h._magnet_target = None
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
        gesture.

        Raises ValueError for a length-1 list: under the full-polyline
        schema (index 0/-1 are the source/dest anchors, everything
        between is an interior waypoint) a single point is neither a
        valid explicit path - which needs at least its 2 endpoint
        anchors - nor the empty auto-route sentinel. This module never
        produces that state itself (remove_point collapses straight to
        [] once only the 2 anchors remain, and mouseDoubleClickEvent
        only ever grows a path), so undo snapshots should never
        contain it either; a length-1 list here means a caller bug."""
        if len(points) == 1:
            raise ValueError(
                "WireItem.apply_points: a single-point path is "
                "invalid - edge.points must be empty (auto-route) or "
                "have >= 2 points (source/dest anchors plus optional "
                "interior waypoints)")
        self.prepareGeometryChange()
        self.edge.points = list(points)
        if self.edge.points:
            self.pts = [QPointF(x, y) for x, y in self.edge.points]
        else:
            self.pts = list(self._auto_pts)
        if self._editable:
            self._rebuild_handles()
        self.update()

    def refresh_auto_route(self, blocks: "Dict[str, BlockItem]") -> None:
        """Recomputes this wire's straight-line auto-route fallback
        (_straight_route_points, above - the same function build_scene
        calls to seed a pointless edge's initial path) from `blocks`'
        CURRENT geometry, and refreshes self._auto_pts to match.

        If this wire is CURRENTLY auto-routed (edge.points is empty -
        no explicit path), the fresh route is also applied as the
        rendered path immediately (prepareGeometryChange + repaint) -
        so a pointless edge's line keeps following its connected
        blocks, per spec section 3's "anchors follow their block".
        An edge with an explicit path only gets its _auto_pts fallback
        refreshed quietly; self.pts (the explicit path itself) is
        untouched HERE - an explicit path's endpoints do still track a
        moved block, just via a different method (translate_endpoint,
        M8 wave 2) called separately by MainWindow's live-move handler,
        not by this one.

        MainWindow - the only owner with a full id -> BlockItem map -
        is responsible for calling this for every wire: once right
        after build_scene (a wire that started with an explicit path
        seeds _auto_pts wrong, from its OWN original path rather than
        a straight route - see __init__'s comment - so this call
        corrects that before any gesture can expose it), and again
        after every block-geometry change - a drag/resize commit,
        auto-layout, undo, or revert - so both a currently-pointless
        wire's rendering and any wire's fallback (for a LATER delete/
        apply_points([]) back to auto-route) stay correct. Never fires
        on_geometry_changed - this is a passive re-derivation, not a
        new user gesture. A no-op if either endpoint block is missing
        from `blocks` (defensive only; MainWindow always passes its
        own complete self.blocks, which contains every block).

        Side effect (finding 2d): also caches src_item/dst_item as
        self._src_item/_dst_item - a live BlockItem reference always
        reflects that block's CURRENT geometry via its own .geometry()
        regardless of when it was cached (blocks are mutated in place,
        never replaced), so caching once, here, is enough for
        WaypointHandle's endpoint magnet to always read fresh block
        rects without needing its own blocks-dict plumbing."""
        src_item = blocks.get(self.edge.src)
        dst_item = blocks.get(self.edge.dst)
        if src_item is None or dst_item is None:
            return
        self._src_item = src_item
        self._dst_item = dst_item
        self._auto_pts = _straight_route_points(src_item, dst_item,
                                                self.edge)
        if not self.edge.points:
            self.prepareGeometryChange()
            self.pts = list(self._auto_pts)
            self.update()

    def translate_endpoint(self, block_id: str, dx: int, dy: int) -> None:
        """Visio-style connector glue (M8 wave 2): translates THIS
        wire's explicit-path endpoint(s) anchored to `block_id` by
        (dx, dy) - edge.points[0] if block_id == self.edge.src,
        edge.points[-1] if block_id == self.edge.dst (both are checked
        independently, so a - never occurring in practice -
        src == dst self-loop edge would translate both ends). A no-op
        if this wire is pointless (edge.points empty - refresh_auto_route
        is the pointless-wire counterpart MainWindow calls alongside
        this) or if block_id matches neither end.

        RAW delta, never re-snapped: dx/dy is simply how far the
        block's own (already-snapped, by BlockItem's itemChange)
        position moved, so an unshifted drag's delta lands on a grid
        multiple whenever the block's STARTING position was itself
        grid-aligned (a fresh drag from an off-grid hand-authored
        position, e.g. an odd x from a hand-edited yaml, produces
        whatever offset the snap needed to land on-grid - still
        exactly the block's own delta, nothing extra). A Shift (fine)
        drag's delta is fine-grained and the endpoint follows at that
        same fine grain - acceptable, documented behavior (finding
        2d/5c), not a bug.

        Called by MainWindow from BOTH DiagramState.on_block_live_moved
        (live, mid-drag - so the wire visibly stays attached instead of
        detaching until release) and, implicitly, by the fact that the
        SAME edge.points mutation this makes is what
        MainWindow._on_layout_geometry_changed's existing world-
        snapshot diff already detects at commit - no separate dirty-
        marking or undo-snapshot logic was needed for this: the commit
        handler's `old` snapshot was captured before this gesture even
        started, so it already holds the PRE-drag endpoint, and its
        diff against `new` (captured at commit, after every live
        translate_endpoint call this gesture made) already flags this
        wire's edge_key() dirty exactly like any other points change.

        Keeps the corresponding WaypointHandle(s) in sync too (position
        and gesture-start baseline) if the wire is currently editable,
        without a full handle rebuild (cheap - this can fire many
        times per drag)."""
        if not self.edge.points:
            return
        n = len(self.edge.points)
        touched: List[int] = []
        if block_id == self.edge.src:
            x, y = self.edge.points[0]
            self.edge.points[0] = (x + dx, y + dy)
            touched.append(0)
        if block_id == self.edge.dst:
            x, y = self.edge.points[-1]
            self.edge.points[-1] = (x + dx, y + dy)
            touched.append(n - 1)
        if not touched:
            return
        self.prepareGeometryChange()
        for i in touched:
            self.pts[i] = QPointF(*self.edge.points[i])
        if self._editable:
            for handle in self._handles:
                if handle.index in touched:
                    new_pt = self.pts[handle.index]
                    handle.setPos(new_pt)
                    handle._geom_at_press = (int(new_pt.x()),
                                             int(new_pt.y()))
        self.update()

    def remove_point(self, index: int) -> None:
        """Delete key on a selected WaypointHandle: removes an
        INTERIOR waypoint (any index other than 0 or -1) and fires
        on_geometry_changed once - no implicit collinear merging is
        ever applied (spec section 3). The two endpoints (index 0 and
        len(edge.points)-1) are anchors, not waypoints, under the
        full-polyline schema - WaypointHandle.keyPressEvent already
        refuses to call this for an endpoint index, so `index` here is
        normally always interior; this method still no-ops
        defensively for an endpoint/out-of-range index rather than
        ever producing an invalid path.

        Edge case (controller ruling): once an interior delete leaves
        only the 2 endpoints, edge.points reverts to [] (auto-
        routing) instead of staying a 2-element explicit path - a
        frozen path down to just its 2 endpoints is indistinguishable
        from an edge that was never made explicit (spec section 3:
        "edges without a points: list show only their endpoints").
        This is also what makes the invalid length-1 state (see
        apply_points) unreachable by construction: an interior delete
        can only ever land on >= 3 points (still explicit) or exactly
        0 (auto-route) - never 1."""
        n = len(self.edge.points)
        if n < 3 or index <= 0 or index >= n - 1:
            return   # endpoint, or nothing interior to remove - no-op
        self.prepareGeometryChange()
        del self.edge.points[index]
        del self.pts[index]
        if len(self.edge.points) <= 2:
            self.edge.points = []
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
        # finding 2a: wider click/double-click hit area while editing
        # (the geometry that matters for placing a waypoint precisely)
        # - normal mode (edge-select-to-inspect) keeps the original
        # width.
        st = QPainterPathStroker()
        st.setWidth(_WIRE_HIT_WIDTH_EDIT if self.state.edit_mode
                   else _WIRE_HIT_WIDTH)
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
    """12x12 square handle, child of a WireItem, one per edge.points
    entry - shown only while the wire is editable (WireItem.
    set_editable(True) creates one per point; set_editable(False)
    destroys them). Dragging live-moves its point (prepareGeometry-
    Change + repaint, snap 10 / Shift 1 on release, endpoint magnet -
    see mouseReleaseEvent - first); release writes edge.points[index]
    and fires state.on_geometry_changed() exactly once IF the point
    actually moved - a click-only press/release fires nothing,
    mirroring BlockItem/_ResizeHandle's gesture-start-baseline
    discipline. Clicking a handle selects it; Delete then removes its
    point via the parent WireItem.remove_point (no implicit collinear
    merging). Hover highlight and an open/closed-hand cursor (finding
    2b) give it the same grab affordance as a block drag."""

    def __init__(self, wire: "WireItem", index: int):
        super().__init__(wire)
        self.setZValue(10)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemIsFocusable, True)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.OpenHandCursor)
        self.index = index
        self._drag_from = None
        self._start_point = (0.0, 0.0)
        self._hovered = False
        # M8 wave B2: the BlockItem currently showing the "drop here to
        # connect" magnet-highlight cue because of THIS handle's live
        # drag, or None - see _update_magnet_highlight.
        self._magnet_target: Optional["BlockItem"] = None
        pt = wire.pts[index]
        self.setPos(pt)
        self._geom_at_press = (int(pt.x()), int(pt.y()))

    def boundingRect(self):
        half = _WAYPOINT_HANDLE_SIZE / 2
        return QRectF(-half, -half, _WAYPOINT_HANDLE_SIZE,
                      _WAYPOINT_HANDLE_SIZE)

    def paint(self, p, opt, widget=None):
        p.setRenderHint(QPainter.Antialiasing)
        if self.isSelected():
            fill = COL_SELECT
        elif self._hovered:
            fill = _HANDLE_HOVER_FILL
        else:
            fill = Qt.white
        p.setBrush(QBrush(fill))
        p.setPen(QPen(Qt.black, 1))
        p.drawRect(self.boundingRect())

    def hoverEnterEvent(self, ev):
        self._hovered = True
        self.update()

    def hoverLeaveEvent(self, ev):
        self._hovered = False
        self.update()

    def mousePressEvent(self, ev):
        # Exclusive (see _select_exclusively's docstring) as of wave B
        # fix round 4.
        _select_exclusively(self)
        self.setFocus(Qt.MouseFocusReason)
        pos = self.pos()
        self._geom_at_press = (int(pos.x()), int(pos.y()))
        self._drag_from = ev.scenePos()
        self._start_point = (pos.x(), pos.y())
        self.setCursor(Qt.ClosedHandCursor)
        ev.accept()

    def _magnet_candidate(self, wire) -> Optional["BlockItem"]:
        """The block this handle's endpoint anchors to, if any - index
        0 is the src anchor, index len(edge.points)-1 is the dst
        anchor; an interior waypoint has no candidate at all.
        wire._src_item/_dst_item are None until refresh_auto_route
        (blocks) has run at least once (real MainWindow sessions
        always do this - see that method's docstring), so the magnet
        (and its highlight) is simply inert until then, same as a
        standalone test-built wire with no real blocks."""
        n = len(wire.edge.points)
        if self.index == 0:
            return wire._src_item
        if self.index == n - 1:
            return wire._dst_item
        return None

    def _update_magnet_highlight(self, wire, px: float, py: float) -> None:
        """M8 wave B2: toggles the candidate block's "drop here to
        connect" cue (BlockItem.set_magnet_highlight) as this handle's
        LIVE drag position enters/leaves the endpoint magnet's range -
        reuses _nearest_rect_boundary_point, the same projected-point
        math mouseReleaseEvent's actual snap uses below, so the cue and
        the eventual snap always agree on what "in range" means."""
        candidate = self._magnet_candidate(wire)
        target = None
        if candidate is not None:
            bx, by, bw, bh = candidate.geometry()
            nx, ny = _nearest_rect_boundary_point(px, py, bx, by, bw, bh)
            dist = ((px - nx) ** 2 + (py - ny) ** 2) ** 0.5
            if dist <= _ENDPOINT_MAGNET_PX:
                target = candidate
        if target is not self._magnet_target:
            if self._magnet_target is not None:
                self._magnet_target.set_magnet_highlight(False)
            if target is not None:
                target.set_magnet_highlight(True)
            self._magnet_target = target

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
        self._update_magnet_highlight(wire, nx, ny)
        wire.state.on_live_status(   # M8 wave B3
            "waypoint: %d, %d" % (int(nx), int(ny)))
        ev.accept()

    def mouseReleaseEvent(self, ev):
        wire = self.parentItem()
        pos = self.pos()
        px, py = pos.x(), pos.y()
        # finding 2d: endpoint re-anchor magnet. Only index 0 (src) and
        # index n-1 (dst) are anchors at all; an interior waypoint is
        # never magnetized (_magnet_candidate returns None for it).
        magnet_item = self._magnet_candidate(wire)
        if magnet_item is not None:
            bx, by, bw, bh = magnet_item.geometry()
            nx, ny = _nearest_rect_boundary_point(px, py, bx, by, bw, bh)
            dist = ((px - nx) ** 2 + (py - ny) ** 2) ** 0.5
            if dist <= _ENDPOINT_MAGNET_PX:
                px, py = nx, ny
        # M8 wave B2: the highlight is a LIVE-drag-only cue - clears on
        # release regardless of whether the magnet actually applied.
        if self._magnet_target is not None:
            self._magnet_target.set_magnet_highlight(False)
            self._magnet_target = None
        fine = _fine_snap()
        new_xy = (snap(px, fine), snap(py, fine))
        wire.prepareGeometryChange()
        self.setPos(new_xy[0], new_xy[1])
        wire.pts[self.index] = QPointF(new_xy[0], new_xy[1])
        wire.update()
        if new_xy != self._geom_at_press:
            wire.edge.points[self.index] = new_xy
            wire.state.on_geometry_changed()
        self._geom_at_press = new_xy
        self._drag_from = None
        self.setCursor(Qt.OpenHandCursor)
        wire.state.on_live_status("")   # M8 wave B3: clear on release
        ev.accept()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            wire = self.parentItem()
            n = len(wire.edge.points)
            if self.index == 0 or self.index == n - 1:
                # Endpoints are anchors, not waypoints (full-polyline
                # schema: edge.points[0]/[-1] are where the wire
                # connects to its blocks) - Delete on an endpoint
                # handle is a documented no-op so a stray keypress can
                # never detach the wire from a block. The endpoint
                # stays draggable (see mouseMoveEvent/
                # mouseReleaseEvent above) for manual re-anchoring;
                # only interior points are deletable, via
                # WireItem.remove_point.
                ev.accept()
                return
            wire.remove_point(self.index)
            ev.accept()
        elif ev.key() in _ARROW_DELTAS:
            # M8 wave B1: arrow-key nudge, on whichever waypoint handle
            # is currently selected/focused (set in mousePressEvent) -
            # one grid step per press, one unit with Shift. No magnet:
            # nudge is exact-by-construction, so re-anchoring only
            # applies to a mouse-dragged release (see
            # mouseReleaseEvent).
            wire = self.parentItem()
            ddx, ddy = _ARROW_DELTAS[ev.key()]
            step = _nudge_step(ev)
            pos = self.pos()
            nx, ny = pos.x() + ddx * step, pos.y() + ddy * step
            wire.prepareGeometryChange()
            self.setPos(nx, ny)
            wire.pts[self.index] = QPointF(nx, ny)
            wire.edge.points[self.index] = (int(nx), int(ny))
            wire.update()
            self._geom_at_press = (int(nx), int(ny))
            wire.state.on_geometry_changed()
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
        # M8 wave B fix round 5: see _GestureMappingGuard's docstring.
        self._gesture_mapping = _GestureMappingGuard()

    def set_editable(self, on: bool) -> None:
        self._editable = bool(on)
        self.setFlag(QGraphicsItem.ItemIsMovable, on)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, on)
        # M8 wave B1 (arrow-key nudge): see BlockItem.set_editable's
        # matching comment.
        self.setFlag(QGraphicsItem.ItemIsSelectable, on)
        self.setFlag(QGraphicsItem.ItemIsFocusable, on)

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
            # M8 wave B fix round 5: see BlockItem.itemChange's
            # matching comment / _GestureMappingGuard's docstring.
            if self._gesture_mapping.drifted(self):
                return self.pos()
            return QPointF(snap(value.x(), _fine_snap()),
                           snap(value.y(), _fine_snap()))
        if (change == QGraphicsItem.ItemPositionHasChanged
                and self._editable and not self._applying):
            # M8 wave B3 (live coordinate readout): mirrors BlockItem's
            # matching branch - ItemSendsGeometryChanges is only on
            # while editable, so this never fires outside a drag;
            # _applying excludes apply_geometry's own programmatic
            # setPos (undo/revert do not want a live-status flicker).
            pos = self.pos()
            self.state.on_live_status(
                "legend: %d, %d" % (int(pos.x()), int(pos.y())))
        return super().itemChange(change, value)

    def mousePressEvent(self, ev):
        if self._editable:
            pos = self.pos()
            self._geom_at_press = (int(pos.x()), int(pos.y()))
            # M8 wave B1: click-to-select/focus - see BlockItem's
            # matching mousePressEvent comment. Exclusive (see
            # _select_exclusively's docstring) as of wave B fix
            # round 4.
            _select_exclusively(self)
            self.setFocus(Qt.MouseFocusReason)
            # M8 wave B fix round 5: see _GestureMappingGuard's
            # docstring.
            self._gesture_mapping.arm(self)
            # M8 wave B fix round 6: arm Qt's own drag bookkeeping
            # too - see BlockItem.mousePressEvent's matching comment /
            # _run_base_mouse_handler's docstring. Editable-only for
            # the same reason as there: with every interaction flag
            # off, the base press would just ignore() the event.
            _run_base_mouse_handler(super().mousePressEvent, ev)
        ev.accept()

    def keyPressEvent(self, ev) -> None:
        """M8 wave B1: arrow-key nudge - see BlockItem.keyPressEvent's
        docstring; identical contract, just legend-shaped (no w/h)."""
        if not self._editable or ev.key() not in _ARROW_DELTAS:
            ev.accept()
            return
        ddx, ddy = _ARROW_DELTAS[ev.key()]
        step = _nudge_step(ev)
        x, y, w, h = self.geometry()
        self.apply_geometry(x + ddx * step, y + ddy * step, w, h)
        self.state.on_geometry_changed()
        ev.accept()

    def mouseReleaseEvent(self, ev):
        # M8 wave B fix round 6: see BlockItem.mouseReleaseEvent's
        # matching comment - the base release is the one place Qt
        # clears movingItemsInitialPositions, and a completed legend
        # drag poisons the map for the next BLOCK drag exactly the
        # same way. Unconditional for the same mid-gesture-mode-exit
        # reason as there.
        _run_base_mouse_handler(super().mouseReleaseEvent, ev)
        if self._editable:
            pos = self.pos()
            new_xy = (int(pos.x()), int(pos.y()))
            if new_xy != self._geom_at_press:
                self.state.on_geometry_changed()
            self._geom_at_press = new_xy
            # M8 wave B fix round 2 (regression, applied here too for
            # the same reason): gated back on self._editable - see
            # BlockItem.mouseReleaseEvent's matching comment.
            self.state.on_live_status("")
        ev.accept()

    def boundingRect(self):
        return QRectF(0, 0, 272, 142)

    def paint(self, p, opt, widget=None):
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(QColor(255, 255, 255, 235)))
        if self.state.edit_mode and self.isSelected():
            # M8 wave B fix round 1 (finding 4): see BlockItem.paint's
            # matching comment.
            p.setPen(QPen(COL_EDIT_SELECT, 2.2))
        else:
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
