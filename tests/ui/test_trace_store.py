"""TraceStore (Task 5): the numpy-backed windowed buffer the UI scope
page will plot from. Records arrive already stability-filtered by
TraceReader (Task 3) - the store does not re-validate them, but a seq
gap or a generation change in what it receives is a real loss and must
become a NaN break in series(), matching M6's gap-honest
connect="finite" plotting (originally scope_page.py's own statistical
gap detector, since removed in favor of this store's exact detection).
"""
import numpy as np

from core.trace.contract import TraceRecord, parse_desc
from tests.test_trace_contract import _desc_words
from ui.trace_store import TraceStore


def _rec(seq, gen=1, v=0):
    return TraceRecord(seq=seq, gen=gen, slots=tuple([v] * 10))


def test_series_time_axis_from_period():
    store = TraceStore(parse_desc(_desc_words("<")))
    store.append([_rec(0, v=5), _rec(1, v=6), _rec(2, v=7)])
    t, y = store.series(0)
    assert np.allclose(t[:3], [0.0, 0.001, 0.002])
    assert y[0] == 5 and store.newest(0) == 7


def test_seq_gap_and_gen_change_insert_nan_break():
    store = TraceStore(parse_desc(_desc_words("<")))
    store.append([_rec(0), _rec(1), _rec(5)])          # gap 2..4
    _t, y = store.series(0)
    assert np.isnan(y).any()
    store2 = TraceStore(parse_desc(_desc_words("<")))
    store2.append([_rec(0, gen=1), _rec(1, gen=2)])    # table change
    _t2, y2 = store2.series(0)
    assert np.isnan(y2).any()


def test_window_trims_old_samples():
    store = TraceStore(parse_desc(_desc_words("<")), window_s=0.01)
    store.append([_rec(i) for i in range(100)])        # 100 ms of data
    t, _y = store.series(0)
    assert t[-1] - t[0] <= 0.011


def test_seq_gap_split_across_two_appends():
    """A gap that straddles two append() calls must still be caught -
    detection carries _last_seq/_last_gen/_last_t forward on self
    rather than resetting cold at the start of every append()."""
    store = TraceStore(parse_desc(_desc_words("<")))
    store.append([_rec(0), _rec(1)])
    store.append([_rec(5)])          # gap 2..4, spans the two calls
    t, y = store.series(0)
    assert np.sum(np.isnan(y)) == 1
    nan_idx = int(np.flatnonzero(np.isnan(y))[0])
    assert t[nan_idx - 1] < t[nan_idx] < t[nan_idx + 1]


def test_gen_change_split_across_two_appends():
    store = TraceStore(parse_desc(_desc_words("<")))
    store.append([_rec(0, gen=1), _rec(1, gen=1)])
    store.append([_rec(2, gen=2)])   # seq contiguous, gen changed
    _t, y = store.series(0)
    assert np.sum(np.isnan(y)) == 1


def test_newest_skips_nan_break_row():
    """newest() must return the last REAL value even when a NaN break
    row sits immediately before it in the buffer."""
    store = TraceStore(parse_desc(_desc_words("<")))
    store.append([_rec(0, v=1), _rec(1, v=2)])
    store.append([_rec(5, v=99)])    # break row, then the real v=99 row
    assert store.newest(0) == 99


def test_clear_then_series_and_newest_are_empty():
    store = TraceStore(parse_desc(_desc_words("<")))
    store.append([_rec(0, v=5), _rec(1, v=6)])
    store.clear()
    t, y = store.series(0)
    assert t.size == 0 and y.size == 0
    assert store.newest(0) is None


def _multi(seq, values, gen=1):
    """A TraceRecord with a distinct value per slot index given in
    `values` (a dict slot->value), 0 everywhere else - the shape a
    real multi-channel record has (every slot carries SOME value,
    never NaN, regardless of whether the firmware is watching it)."""
    slots = [0] * 10
    for slot, v in values.items():
        slots[slot] = v
    return TraceRecord(seq=seq, gen=gen, slots=tuple(slots))


def test_clear_slot_wipes_column_to_nan():
    """ScopePage.add_address_slot() calls this right when occupying a
    slot - a column's history prior to that moment is NOT blank by
    default (every record carries a real, non-NaN value for every
    slot index, watched or not), so this is what actually makes it
    blank."""
    store = TraceStore(parse_desc(_desc_words("<")))
    store.append([_multi(0, {0: 5, 1: 50}), _multi(1, {0: 6, 1: 51})])

    store.clear_slot(0)

    _t, y0 = store.series(0)
    assert np.isnan(y0).all()
    _t, y1 = store.series(1)
    assert list(y1) == [50.0, 51.0]          # untouched


def test_clear_slot_on_empty_buffer_is_a_no_op():
    store = TraceStore(parse_desc(_desc_words("<")))
    store.clear_slot(0)          # must not raise
    t, y = store.series(0)
    assert t.size == 0 and y.size == 0


def test_remove_slot_shifts_higher_columns_down_and_nans_the_tail():
    """The bug this guards against: TraceStore's columns are purely
    positional (series()/newest() index by slot number) with no
    concept of "which channel used to be there" - a middle-slot
    removal on the PAGE side (ScopePage.remove_channel) must be
    mirrored here, column-for-column, or a channel that compacts into
    a new row keeps reading the REMOVED channel's stale history at
    that column instead of its own."""
    store = TraceStore(parse_desc(_desc_words("<")))
    store.append([_multi(0, {0: 100, 1: 200, 2: 300}),
                 _multi(1, {0: 101, 1: 201, 2: 301})])

    store.remove_slot(1)          # remove the middle slot (was "B")

    _t, y0 = store.series(0)
    assert list(y0) == [100.0, 101.0]        # slot 0 ("A") untouched
    _t, y1 = store.series(1)
    assert list(y1) == [300.0, 301.0]        # slot 2's data ("C") shifted in
    _t, y_tail = store.series(9)
    assert np.isnan(y_tail).all()            # explicit freed-tail NaN


def test_remove_slot_on_empty_buffer_is_a_no_op():
    store = TraceStore(parse_desc(_desc_words("<")))
    store.remove_slot(0)         # must not raise
    t, y = store.series(0)
    assert t.size == 0 and y.size == 0
