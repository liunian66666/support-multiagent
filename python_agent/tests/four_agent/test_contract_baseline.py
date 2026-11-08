"""阶段0 的回归闸门本身要有测试：打码之后**还剩什么**才是判据。

``capture_contract_baseline --compare`` 靠 ``normalize_row`` 把每次都会变的随机尾巴
（6~8 位 hex 后缀、时间戳）打码，再逐行 diff。打码打过头（比如把整个 value 换成占位符），
闸门就永远说"契约未变"——那和没有闸门一样。所以这里钉三件事：

1. 随机 id 与时间戳确实被打掉；
2. **只有随机部分不同**的两行归一化后逐字相同（否则闸门天天误报）；
3. **契约关系**（字段是否出现、``turn_id == answer_id``、message id 带 turn 前缀）
   在打码后依然能区分开（否则闸门永远放行）——这一项逼出了"随机 id 不能统一打成一个
   占位符"这个细节：统一打码时 ``answer_id != turn_id`` 看不出来，闸门形同虚设。
"""

from __future__ import annotations

import json

from evaluation.four_agent.capture_contract_baseline import normalize_row

NORMAL = {
    "mode": "offline",
    "captured_at": "2026-11-07T17:01:19",
    "scenario": "faq",
    "request": {"thread_id": "t_demo:baseline-faq-f600aa", "user_message": "退货期限是多久？",
                "turn_id": "faq-e24711"},
    "json": {"turn_id": "faq-e24711", "answer_id": "faq-e24711", "outcome": "answered",
             "agents": ["faq"]},
    "history": [{"role": "user", "content": "退货期限是多久？", "message_id": "faq-e24711:u"},
                {"role": "assistant", "content": "7 天内", "message_id": "faq-e24711:a"}],
}


def drifted(volatile_only: bool = True) -> dict:
    """同一次采集换个随机后缀；``volatile_only=False`` 时顺带改一处真的契约事实。"""
    row = json.loads(json.dumps(NORMAL, ensure_ascii=False))
    row["captured_at"] = "2026-11-08T09:30:00"
    row["request"]["thread_id"] = "t_demo:baseline-faq-1a2b3c"
    row["request"]["turn_id"] = "faq-998877"
    row["json"]["turn_id"] = "faq-998877"
    row["json"]["answer_id"] = "faq-998877" if volatile_only else "faq-000000"
    row["history"][0]["message_id"] = "faq-998877:u"
    row["history"][1]["message_id"] = "faq-998877:a"
    return row


def test_random_suffixes_and_timestamps_are_masked() -> None:
    text = normalize_row(NORMAL)
    assert "e24711" not in text and "f600aa" not in text
    assert "2026-11-07T17:01:19" not in text
    # 同一个 raw id 拿到同一个编号（这是"关系仍可见"的前提）
    assert text.count("<id1>") == 5          # 4 处 turn/answer + 2 条 message id 里的同一个串
    assert "<id2>" in text and "<ts>" in text


def test_only_volatile_parts_differ_means_same_contract() -> None:
    assert normalize_row(NORMAL) == normalize_row(drifted())


def test_real_contract_change_is_still_visible() -> None:
    """``answer_id`` 一旦不等于 ``turn_id``，闸门必须报差异（这正是幂等契约）。"""
    assert normalize_row(NORMAL) != normalize_row(drifted(volatile_only=False))
