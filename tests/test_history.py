from core.engine.history import History
from core.engine.snapshot import Sample, Snapshot


def test_snapshot_value_lookup():
    s = Snapshot(values={"A.B": Sample(7, 1.0)}, t=1.0, rate_hz=10.0)
    assert s.value("A.B") == 7
    assert s.value("A.MISSING") is None


def test_last_change_age_needs_two_samples():
    h = History()
    h.record("A.B", 0.0, 5)
    assert h.last_change_age("A.B", 1.0) is None
    h.record("A.B", 1.0, 5)
    assert h.last_change_age("A.B", 2.0) == 2.0   # never seen a change yet:
    # age counted from the FIRST sample when no change observed


def test_change_resets_age():
    h = History()
    h.record("A.B", 0.0, 5)
    h.record("A.B", 1.0, 5)
    h.record("A.B", 2.0, 9)
    assert h.last_change_age("A.B", 2.5) == 0.5
    assert h.changed_on_last("A.B") is True
    h.record("A.B", 3.0, 9)
    assert h.changed_on_last("A.B") is False


def test_window_trim():
    h = History(window_s=1.0)
    for i in range(50):
        h.record("A.B", i * 0.1, i)
    ts = [t for t, _ in h.series("A.B")]
    assert min(ts) >= 4.9 - 1.0 - 1e-9


def test_unknown_key():
    h = History()
    assert h.last_change_age("X.Y", 1.0) is None
    assert h.changed_on_last("X.Y") is False
    assert h.series("X.Y") == []


def test_gates_survive_window_trim():
    h = History(window_s=1.0)
    h.record("A.B", 0.0, 5)
    h.record("A.B", 5.0, 5)      # gap > window: buffer trimmed to 1 sample
    assert h.last_change_age("A.B", 5.0) == 5.0   # not None
    h.record("A.B", 10.0, 7)
    assert h.changed_on_last("A.B") is True
    assert h.last_change_age("A.B", 10.5) == 0.5


def test_concurrent_record_and_read():
    import threading
    h = History(window_s=1.0)
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            h.record("A.B", i * 0.001, i)
            i += 1

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        for _ in range(2000):
            h.series("A.B")
            h.last_change_age("A.B", 99.0)
            h.changed_on_last("A.B")
    finally:
        stop.set()
        t.join(timeout=2.0)
    assert h.series("A.B")          # survived without exception
