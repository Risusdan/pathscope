"""The single crossing point between the engine's poller thread and the
Qt main thread. Signals emitted from a non-Qt thread are delivered as
queued connections - UI slots always run on the main thread."""
from PySide6.QtCore import QObject, Signal

from core.engine.core import Engine


class EngineBridge(QObject):
    update = Signal(object)   # EngineUpdate
    state = Signal(str)

    def __init__(self, engine: Engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        engine.on_update(self.update.emit)
        engine.on_state(self.state.emit)
