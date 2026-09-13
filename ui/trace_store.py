"""TraceStore: the numpy-backed windowed buffer the M7 scope page
plots trace-buffer channels from (design doc section 6, M7 plan
Task 5).

Records arrive from TraceReader.refresh() (core/trace/reader.py,
Task 3) already stability-filtered - a record only reaches append()
once its ring slot has been re-read unchanged, so this store never
re-validates record contents. What it DOES have to detect itself is
loss between records it receives: TraceReader can only report what it
managed to read, so a seq gap (the next record's seq is not exactly
one more than the previous record's) or a generation change (the
watch table was rebuilt - see core/trace/contract.py's generation
field) both mean real samples are missing or no longer comparable to
what came before. Both are treated identically: a break, surfaced to
series() as one inserted (t, NaN) sample pair so a pyqtgraph curve
drawn with connect="finite" stops interpolating across it - the same
gap-honesty contract M6's _gapped_xy (ui/panels/scope_page.py)
applies, just detected exactly (from seq/gen equality) rather than
statistically (from a median sample interval), since trace records
carry an exact seq instead of M6's jittered wall-clock timestamps. The
break's NaN sample pair is placed at the midpoint between the t of the
last real sample before it and the t of the first real sample after
it - an arbitrary but documented choice; only that it falls strictly
between the two matters, so connect="finite" cuts the line there.

Loss detection also has to see across append() calls, not just within
one batch: a gap or generation change can land exactly on the boundary
between two refresh cycles. self._last_seq/_last_gen/_last_t carry the
tail of the previous batch forward so that boundary is checked exactly
like a mid-batch one.

Storage: one growing numpy structured array (fields: seq u64, t f64,
gen u16, slots (max_ch,) f64) appended to in batches and trimmed to
the trailing window_s on every append. slots are stored as f64 even
though the wire value is u32 (exact for every real u32 value - all
representable below 2**53), so a NaN break row can carry NaN in every
slot column without a second parallel "is this row a break" array.
Decoding those f64 values to their display type (signed/float
reinterpretation, scaling, ...) is the scope page's job, not this
store's - series() and newest() return raw magnitudes, matching
TraceRecord.slots.

t is derived purely from seq: t = seq * desc.period_us * 1e-6, an
absolute (not wall-clock) seconds axis anchored at seq 0. The scope
page performs the roll-mode "now" subtraction itself, exactly as it
already does for M6's History-backed channels.
"""
from typing import List, Optional, Tuple

import numpy as np

from core.trace.contract import TraceDesc, TraceRecord

_DTYPE = np.dtype([
    ("seq", np.uint64),
    ("t", np.float64),
    ("gen", np.uint16),
    ("slots", np.float64, (10,)),
])

_NAN_SLOTS = tuple(float("nan") for _ in range(10))


class TraceStore:
    def __init__(self, desc: TraceDesc, window_s: float = 10.0):
        self._desc = desc
        self._window_s = window_s
        self._buf = np.empty(0, dtype=_DTYPE)
        self._last_seq = None          # type: Optional[int]
        self._last_gen = None          # type: Optional[int]
        self._last_t = None            # type: Optional[float]

    def append(self, records: List[TraceRecord]) -> None:
        if not records:
            return

        rows = []
        for r in records:
            t = r.seq * self._desc.period_us * 1e-6
            if self._last_seq is not None and (
                r.seq != self._last_seq + 1 or r.gen != self._last_gen
            ):
                nan_t = (self._last_t + t) / 2.0
                rows.append((r.seq, nan_t, r.gen, _NAN_SLOTS))
            rows.append((r.seq, t, r.gen, tuple(r.slots)))
            self._last_seq = r.seq
            self._last_gen = r.gen
            self._last_t = t

        new = np.array(rows, dtype=_DTYPE)
        self._buf = np.concatenate([self._buf, new])
        self._trim()

    def _trim(self) -> None:
        if self._buf.size == 0:
            return
        cutoff = self._buf["t"][-1] - self._window_s
        keep = self._buf["t"] >= cutoff
        first = int(np.argmax(keep)) if keep.any() else self._buf.size
        if first > 0:
            self._buf = self._buf[first:]

    def series(self, slot: int) -> Tuple[np.ndarray, np.ndarray]:
        t = self._buf["t"].copy()
        y = self._buf["slots"][:, slot].copy()
        return t, y

    def newest(self, slot: int) -> Optional[int]:
        col = self._buf["slots"][:, slot]
        real = np.flatnonzero(~np.isnan(col))
        if real.size == 0:
            return None
        return int(col[real[-1]])

    def latest_gen(self) -> int:
        return self._last_gen if self._last_gen is not None else 0

    def clear(self) -> None:
        self._buf = np.empty(0, dtype=_DTYPE)
        self._last_seq = None
        self._last_gen = None
        self._last_t = None
