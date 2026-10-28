# 中文文案必然包含全角标点
"""pytest bootstrap: make the repo root importable so ``import agent`` resolves.

The scripted doubles live in :mod:`agent.fakes` — visible outside tests on purpose —
so the demo harness and the tests share exactly one implementation.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
