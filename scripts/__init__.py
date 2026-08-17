"""Local project scripts package for test imports.

The script modules also run as standalone files and use sibling imports. Keep the
directory importable when tests load modules through the ``scripts`` package.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
