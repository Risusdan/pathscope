"""Demo mode: a MockAdapter animated by a background thread so the full
UI runs, and anomalies fire, with no hardware attached."""
import threading
import time

from core.adapter.mock import MockAdapter
from core.engine.core import Engine

S0CR = 0x40026410
S0NDTR = 0x40026414
ADC_SR = 0x40012000


def make_demo_engine(target_dir: str = "targets/f411") -> Engine:
    mem = {S0CR: 1, S0NDTR: 1000, ADC_SR: 0x10}
    adapter = MockAdapter(mem)
    engine = Engine.load(target_dir, adapter, interval_s=0.03)

    def animate():
        t0 = time.monotonic()
        while True:
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
            time.sleep(0.05)

    threading.Thread(target=animate, daemon=True).start()
    return engine
