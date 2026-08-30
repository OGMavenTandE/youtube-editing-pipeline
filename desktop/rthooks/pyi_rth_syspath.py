"""Put the onedir bundle dir on sys.path so desktop.* and pipeline.* import.

Does not change install_root(): that stays the folder next to the exe
(see desktop/paths.py and pipeline/config.py).
"""

from __future__ import annotations

import sys

if getattr(sys, "frozen", False):
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and meipass not in sys.path:
        sys.path.insert(0, meipass)
