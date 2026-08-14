"""PyInstaller entry point for the Windows panel.

main.py uses relative imports (with a run-as-script bootstrap), which
PyInstaller's static analysis can't follow when main.py is the entry
script. This launcher imports the package with a plain absolute import
so the dependency walker sees every vibecenter.* module.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vibecenter.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
