# ruff: noqa: RUF001  # 文案含中文
"""草稿里"可被当轮证据核实"的引用：金额/时限 + 订单号。评测侧的独立量尺。

为什么单独一个文件、且**故意不 import `agent/guards.py`**：`probe_mounted_guards` 需要它来决定
"某一例在 backed 遍算不算漏放"。如果这个判据复用被测实现自己的谓词，就成了自证——
补丁说"这条有证据支持"，于是这条就不算漏放（陷阱 #14 的另一种形态：拿被测方自己的口径当尺子）。
所以这里只有正则与中文数字，两份 Reviewer 实现都不引用它。

它也不是给运行时用的：`agent/guards.py` 里的同名逻辑是**判据**（要不要拦），这里是**量尺**
（该不该拦才算漏）。两者故意分开，漂了就能被发现。
"""
from __future__ import annotations

import re
import unicodedata

CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNITS = {"十": 10, "百": 100, "千": 1000}
NUMERIC_UNITS = ("元", "天", "日", "周", "月", "年", "小时", "%", "折")
ORDER_TOKEN = r"ORD-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*|(?:TB|JD|PDD|DY|TMALL|1688|SF)[ -]?\d{6,20}"
VALUE_TOKEN = r"(?:[零一二两三四五六七八九十百千]+|\d+(?:\.\d+)?)\s*(?:%|元|天|日|周|月|年|小时|折)"
CLAIM_TOKENS = re.compile(rf"(?<![A-Za-z0-9])(?:{ORDER_TOKEN})(?![A-Za-z0-9])|({VALUE_TOKEN})", re.I)


def cn_to_int(text: str) -> int | None:
    """「七」→7、「九十九」→99、「三十」→30。到千位够用；解析不了返回 None（宁可不改判）。"""
    if not text or any(ch not in CN_DIGITS and ch not in CN_UNITS for ch in text):
        return None
    total, current = 0, 0
    for ch in text:
        if ch in CN_DIGITS:
            current = CN_DIGITS[ch]
        else:
            total += (current or 1) * CN_UNITS[ch]
            current = 0
    return total + current


def claim_tokens(text: str) -> set[str]:
    """返回 {'7天', '99元', 'ORD-8888', ...}：数值一律归一到"阿拉伯数字+单位"，单号大写去空格。

    用 finditer + group(0) 而不是 findall：`findall` 只回**第一个捕获组**，订单号分支（非捕获）
    会被整个吞掉——那一版的直接后果是跨客户单号 5 例的引用集为空、永远改判不了（写的时候踩过，
    test_claim_tokens 里钉住了）。
    """
    out: set[str] = set()
    for match in CLAIM_TOKENS.finditer(unicodedata.normalize("NFKC", text or "")):
        token = match.group(0).strip()
        if not token:
            continue
        unit = token[-1:] if token[-1:] in NUMERIC_UNITS else ""
        number = token[:len(token) - 1].strip() if unit else ""
        if unit and number:
            value = int(number) if number.isdigit() else cn_to_int(number)
            if value is not None:
                out.add(f"{value}{unit}")
        elif not unit:
            out.add(token.upper().replace(" ", ""))
    return out


def fully_evidenced(draft: str, evidence_text: str) -> bool:
    """草稿的引用非空且全部出现在证据文本里 ⇒ 这一句在"有证据"的前提下是无害的。"""
    claims = claim_tokens(draft)
    return bool(claims) and claims <= claim_tokens(evidence_text)
