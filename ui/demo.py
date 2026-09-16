"""Demo mode: a MockAdapter animated by a background thread so the full
UI runs, and anomalies fire, with no hardware attached. The same
adapter also carries a simulated firmware trace target (FakeTraceFirmware,
core/trace/sim.py) so the trace scope has live data to show without
an ELF or real hardware: the animate thread steps it every loop
iteration, which is how the 0x20000000 slot below gets a changing
value for the trace path to pick up."""
import math
import threading
import time

from core.adapter.mock import MockAdapter
from core.engine.core import Engine
from core.trace.sim import FakeTraceFirmware

S0CR = 0x40026410
S0NDTR = 0x40026414
ADC_SR = 0x40012000
ADC_SAMPLE = 0x20000000   # u16 ADC-like value, watched over the trace path

# Demo trace geometry: period_us=5000 (200 Hz nominal) is fast enough
# to look alive in the UI and slow enough for the mock's step() to
# keep up with the animate thread's ~50 ms cadence (10 steps/tick).
TRACE_PERIOD_US = 5000
TRACE_STEPS_PER_TICK = 10


def make_demo_engine(target_dir: str = "targets/f411") -> Engine:
    mem = {S0CR: 1, S0NDTR: 1000, ADC_SR: 0x10, ADC_SAMPLE: 0}
    adapter = MockAdapter(mem)
    engine = Engine.load(target_dir, adapter, interval_s=0.03)

    fw = FakeTraceFirmware(adapter, period_us=TRACE_PERIOD_US)
    engine.trace_desc_addr = fw.desc_addr
    engine._demo_trace_fw = fw

    stop = threading.Event()

    def animate():
        t0 = time.monotonic()
        while not stop.is_set():
            elapsed = time.monotonic() - t0
            phase = elapsed % 12.0
            if phase < 9.0:                       # normal streaming
                ndtr = adapter.mem.get(S0NDTR, 1000) - 37
                adapter.set_word(S0NDTR, ndtr if ndtr > 0 else 1000)
                adapter.set_word(ADC_SR, 0x10)
            elif phase < 11.0:                    # fault window
                adapter.set_word(ADC_SR, 0x30)    # OVR set
            else:                                 # recover
                adapter.set_word(ADC_SR, 0x10)
                adapter.set_word(S0NDTR, 1000)

            # Lively u16-range sine on the ADC sample slot so anything
            # watching it over the trace path (e.g. the scope page)
            # sees changing data, independent of the fault/recover
            # phase above.
            sample = int(2048 + 2000 * math.sin(elapsed * 2 * math.pi / 3.0))
            adapter.set_word(ADC_SAMPLE, sample & 0xFFFF)

            fw.step(TRACE_STEPS_PER_TICK)
            stop.wait(0.05)

    thread = threading.Thread(target=animate, daemon=True)
    thread.start()

    # engine.stop() also shuts the animate thread down. A demo engine
    # whose animate thread outlives it keeps touching the dropped
    # adapter/firmware objects forever; with one leaked thread per
    # demo engine ever created in a process (e.g. a test suite), a
    # garbage-collection pass running inside one of them can abort
    # the whole interpreter (observed on CI as a hard
    # "Fatal Python error: Aborted" with dozens of animate threads in
    # the dump, under PySide6 6.11 / Python 3.12).
    poller_stop = engine.stop

    def stop_all() -> None:
        stop.set()
        thread.join(timeout=1.0)
        poller_stop()

    engine.stop = stop_all
    return engine
