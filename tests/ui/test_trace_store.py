"""TraceStore (Task 5): the numpy-backed windowed buffer the UI scope
page will plot from. Records arrive already stability-filtered by
TraceReader (Task 3) - the store does not re-validate them, but a seq
gap or a generation change in what it receives is a real loss and must
become a NaN break in series(), matching M6's gap-honest
connect="finite" plotting (_gapped_xy in ui/panels/scope_page.py)."""
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
