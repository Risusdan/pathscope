"""UI tests run headless: force the offscreen Qt platform before any
QApplication exists."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
