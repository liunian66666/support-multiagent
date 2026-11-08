"""四 Agent 主图所依赖的**共用能力**接口（稳定面）。

主图（``app/agent/support``）只通过这里取能力，不直接碰 ``app.agent.tools`` /
``retrieval`` / ``llm_config`` / ``faq_agent`` 的实现细节。好处不是好看，而是可替换：
换检索、换模型网关、换业务接口都只改这一层，评审规则与图结构不动。

四个模块各管一件事：

* :mod:`~app.agent.capabilities.models` —— 租户已验证模型 / 平台模型（含评测档换 id）
* :mod:`~app.agent.capabilities.faq` —— FAQ 专职 Agent、已发布证据过滤
* :mod:`~app.agent.capabilities.orders` —— 订单只读（归属由业务层判定）
* :mod:`~app.agent.capabilities.evidence` —— 已发布证据的游标与 ID 收集

这里**不做**的事：编排（在 ``app.agent.support``）、HTTP/SSE 契约（在 ``app/api``）、
以及任何写操作（退款、支付、建单、改单）。
"""
