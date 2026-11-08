# -*- coding: utf-8 -*-
"""长对话两臂共用的「逐轮脚本 + 语料指纹 + 逐轮转写」helper（阶段4/6 数据自证性）。

为什么需要这个文件：两个采集器（`run_ab_live.py mem` 与 `run_legacy_heldout.py --arm mem`）
都从 `evaluation.memory_ab_live.cases()` 取语料，所以"两臂跑的是同一段对话"目前只是
**代码结构上的承诺**——落盘的 jsonl 里只有探针原话，11 轮埋针/填充一句都没记，读者无法自证，
也无法在"探针没召回"之后翻回去看某一轮到底说了什么、模型当时答了什么。

这里把三件事定成一处定义，两臂共用，diff 脚本据此逐例核对：
1. ``script(case)`` / ``fingerprint(case)``：这一例**按顺序实际发出去**的 12 句话与其 sha，
   写进行内 `script_sha`——两臂同 sha 才叫同一对话；
2. ``turn_row(...)``：逐轮转写行（原话 + 答复前 120 字 + 该轮墙钟），写进行内 `per_turn`；
3. ``transcript_complete(row)``：这一例的转写是否齐备（行数 = 轮数-1 且每行有原话），
   供 diff 把"旧数据不可证"和"新数据自证齐备"分成两类，而不是混成一个分数。

注意：这里的 sha 只证明"发出去的话一样"，不证明模型看到的东西一样——旧臂与新臂喂给模型的
上下文形状本来就不同（新臂 = 水位线摘要 + 最近原文，旧臂 = memory_messages 全量），那正是被测对象。
"""

from __future__ import annotations

import hashlib

ANSWER_HEAD_CHARS = 120


def _sha(text: str, chars: int = 12) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:chars]


def script(case: dict) -> list[str]:
    """这一例真正发给模型的话，顺序 = 埋针轮 → 填充轮 → 探针轮（最后一句）。"""
    return list(case.get("turns") or []) + [case.get("probe") or ""]


def fingerprint(case: dict) -> str:
    """逐轮脚本指纹；两臂对同一 `id` 必须给出同一个值，否则"同一对话"不成立。"""
    return _sha("\n".join(script(case)))


def turn_row(index: int, user_text: str, answer: str | None, *,
             latency_ms: float | None = None, extra: dict | None = None) -> dict:
    """一行逐轮转写：原话全文 + 答复开头 + 该轮墙钟。

    答复只留前 ``ANSWER_HEAD_CHARS`` 字并另存长度，是为了让 10 例 × 12 轮的原始数据仍然
    可读、可 diff；要看全文按 `thread` 回检查点/消息表取（所以 thread 键必须落盘）。
    """
    text = answer or ""
    row = {"turn": index, "user": user_text, "user_sha": _sha(user_text, 8),
           "answer_chars": len(text), "answer_head": text[:ANSWER_HEAD_CHARS]}
    if latency_ms is not None:
        row["latency_ms"] = round(float(latency_ms), 1)
    for key, value in (extra or {}).items():
        # 只丢"没记"的空串/None（error="" 与"根本没这个字段"不该同形）；
        # 空列表和 False 是**结论**（本轮没委派给任何 specialist / 这一轮没成功），必须留着。
        if value is not None and value != "":
            row[key] = value
    return row


def transcript_complete(row: dict | None) -> tuple[int, int]:
    """(转写齐备的轮数, 应有的轮数)；`per_turn` 里每行都得带原话才算数。

    应有的轮数 = `turns` - 1，因为 `turns` 把探针那一轮也算进去了，而探针答复在行内 `answer`。
    旧数据（只有 `{"turn", "error", "latency_ms"}` 那种形状）会算出 0/n——这不是失败，
    是"这一例没被自证过"，diff 里单列一类。
    """
    if not row:
        return 0, 0
    turns = int(row.get("turns") or 0)
    want = max(turns - 1, 0)
    got = sum(1 for t in (row.get("per_turn") or [])
              if isinstance(t, dict) and (t.get("user") or t.get("user_sha")))
    return got, want
