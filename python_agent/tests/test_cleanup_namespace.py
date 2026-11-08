"""清理用的检查点前缀必须与新版图的 NAMESPACE 一致。

``app/infra/db/session.py`` 里的 ``_SUPPORT_NAMESPACE`` 是硬编码的：清理任务只用一个
字符串常量，不 import ``app.agent.support.service``（那会拖进整条 langchain 依赖链）。
硬编码的风险是两边改一处漏一处——前缀写错时 ``adelete_thread`` 对不存在的键是 no-op，
清理会"成功"返回却什么也没删。这里把等式钉成测试。
"""

from app.agent.support.service import NAMESPACE
from app.infra.db.session import _SUPPORT_NAMESPACE


def test_cleanup_prefix_matches_graph_namespace():
    assert NAMESPACE == "support_v2"
    assert _SUPPORT_NAMESPACE == NAMESPACE
