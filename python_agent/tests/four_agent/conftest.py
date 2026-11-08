# 中文文案必然包含全角标点
"""pytest bootstrap: make ``python_agent/`` importable so ``import app.*`` resolves.

The scripted doubles live in :mod:`app.agent.support.fakes` — visible outside tests on purpose —
so the demo harness and the tests share exactly one implementation. （收敛前它们在顶层的
``agent/fakes.py`` 里；那个包已并入 ``app/agent/support/``。）
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
