"""
Backend package for PyMcLauncher.
Exposes all downloaders, launch executor, utilities, and authentication handlers.
"""

import sys
import os
from pathlib import Path

# Ensure backend directory is in sys.path
_pkg_dir = str(Path(__file__).resolve().parent)
if _pkg_dir not in sys.path:
    sys.path.insert(0, _pkg_dir)

_root_dir = str(Path(__file__).resolve().parent.parent)
if _root_dir not in sys.path:
    sys.path.insert(0, _root_dir)
