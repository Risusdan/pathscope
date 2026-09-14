"""Single home for the UI style constants shared across the diagram and
panels: the monospace font used for register/address/hex display, and
the named palette colors from the textbook-diagram visual language
(design doc section 7). Every module that used to define its own copy
of these imports them from here instead - values are unchanged, this
only removes the multi-file duplication (architecture review R7)."""
from PySide6.QtGui import QColor, QFont

MONO = QFont()
MONO.setFamilies(["Menlo", "Consolas", "Courier New"])
MONO.setPointSize(10)

# Hex strings, for contexts that want a plain color string (a pyqtgraph
# pen argument, a Qt style sheet) rather than a QColor.
ANOM_HEX = "#C62828"      # anomaly red: badges, event log, error text
ACTIVE_HEX = "#1565C0"    # active-edge / cursor blue
GREY_HEX = "#B0B0B0"      # stale/inactive grey
WARN_HEX = "#E65100"      # warning orange: guarded selection, warn badge

COL_ANOM = QColor(ANOM_HEX)
COL_ACTIVE = QColor(ACTIVE_HEX)
COL_GREY = QColor(GREY_HEX)
COL_WARN = QColor(WARN_HEX)
