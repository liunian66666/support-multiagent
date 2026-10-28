# ruff: noqa: RUF001
"""Generate the (unapplied) cutover patches and verify them with ``git apply --check``.

Line endings are the trap here. ``pathlib.write_text`` on Windows silently rewrites
``\\n`` to ``\\r\\n``, and this repo mixes both: ``chat.py`` is CRLF while
``docker-compose.yaml`` is LF. A patch whose context newlines differ from its target
fails ``git apply`` for a reason that looks like a content conflict. So every read and
write below pins ``newline=""`` and lets the target's own endings flow through.

The patches are written, checked, and deliberately **not applied**.
"""

from __future__ import annotations

import difflib
import pathlib
import subprocess
import sys

CRLF = b"\r\n"
CRLF_S = "\r\n"

CHANGES = [
    {
        "rel": "python_agent/app/api/chat.py",
        "out": "agent/deploy/cutover_chat.patch",
        "old": "from app.services import four_agent\n",
        "new": (
            "try:  # [cutover] 新链路在仓库根 agent/；镜像里缺它必须退回旧实现，不能让服务起不来\n"
            "    from agent import cutover as four_agent\n"
            "except ImportError:  # pragma: no cover - 部署期兜底\n"
            "    from app.services import four_agent\n"
        ),
    },
    {
        "rel": "docker-compose.yaml",
        "out": "agent/deploy/cutover_compose.patch",
        "old": "  agent:\n    build: ./python_agent\n    container_name: cs_agent\n",
        "new": (
            "  agent:\n    build: ./python_agent\n    container_name: cs_agent\n"
            "    volumes:  # [cutover] WORKDIR=/app，挂进来的 /app/agent 即成为可 import 的包\n"
            "      - ../agent:/app/agent:ro\n"
        ),
    },
]


def _adopt(text: str, newline: str) -> str:
    """Rewrite an LF-written anchor into whatever the target file actually uses.

    Anchors here are written with ``\\n`` for readability, but ``chat.py`` is CRLF while
    ``docker-compose.yaml`` is LF. Matching the target prevents a phantom "anchor not
    found" that is really a newline disagreement.
    """
    return text.replace("\n", newline) if newline == "\r\n" else text


def build(change: dict) -> bool:
    rel, out = change["rel"], change["out"]
    # newline="" preserves whatever the file actually uses instead of universalising it.
    with open(rel, encoding="utf-8", newline="") as handle:
        text = handle.read()
    newline = CRLF_S if text.count(CRLF_S) else "\n"
    old, new = (_adopt(change[key], newline) for key in ("old", "new"))
    if text.count(old) != 1:
        print(f"  SKIP {rel}: anchor appears {text.count(old)}x (newline={newline!r})")
        return False

    diff = "".join(difflib.unified_diff(
        text.splitlines(keepends=True),
        text.replace(old, new).splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3))
    pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as handle:
        handle.write(diff)

    with open(out, "rb") as handle:
        patch_crlf = handle.read().count(CRLF)
    with open(rel, "rb") as handle:
        target_crlf = handle.read().count(CRLF)

    result = subprocess.run(["git", "apply", "--check", out], capture_output=True, text=True)
    verdict = "APPLIES" if result.returncode == 0 else f"FAILS: {result.stderr.strip()[:90]}"
    print(f"  {out}\n    target={rel} crlf={target_crlf} patch_crlf={patch_crlf} -> {verdict}")

    # A reverse check must fail: that is the proof the working tree is still untouched.
    back = subprocess.run(["git", "apply", "--check", "--reverse", out],
                          capture_output=True, text=True)
    print(f"    reverse check: {'still unapplied (good)' if back.returncode else 'ALREADY APPLIED (!)'}")
    return result.returncode == 0


def main() -> int:
    print("cutover patch generation:")
    ok = all([build(change) for change in CHANGES])
    if not ok:
        print("\n至少一个补丁未通过 git apply --check")
        return 1
    print("\n两个补丁均可干净应用，且当前工作树未被修改（反向校验失败即为证据）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
