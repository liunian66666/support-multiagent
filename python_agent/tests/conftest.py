# 中文文案必然包含全角标点
"""整套测试的引擎开关：``SUPPORT_TEST_ENGINE=nodes`` 让**默认装配**的图跑 node 引擎。

为什么需要它：``DEFAULT_ENGINE`` 是模块常量，凡是「不显式传 engine」的用例（HTTP 信封、
持久化、记忆水位、线程锁、进度回调）都跟着那条默认值走。要在**不动默认值**的前提下把
整棵树跑在另一套引擎上，只能在这里改一次——逐个用例传参既容易漏，也会把「默认值是哪套」
这件事从测试里抹掉。

用法：

    python -m pytest tests -q                                  # 默认引擎（tools）
    SUPPORT_TEST_ENGINE=nodes python -m pytest tests -q        # 同一批用例，node 引擎

显式传 ``engine="tools"`` 的文件（``test_graph.py`` / ``test_answer_shape.py``）不受影响：
它们钉的是遗留引擎的回归，跟随默认值就等于放弃这条回归。
"""

from __future__ import annotations

import os

from app.agent.support import graph as support_graph

_REQUESTED = os.getenv("SUPPORT_TEST_ENGINE", "").strip()
if _REQUESTED:
    if _REQUESTED not in support_graph.ENGINES:
        raise RuntimeError(f"SUPPORT_TEST_ENGINE must be one of {support_graph.ENGINES}")
    support_graph.DEFAULT_ENGINE = _REQUESTED
