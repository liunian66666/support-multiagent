# RSI 四轮复测审计与检索定位（2026-10-25）

## 结论

现有四轮只能证明经验召回链路可运行，不能证明注入提高了答案质量。原始字面评分为 off1/inject1 = 29/31、off2/inject2 = 31/31；off 自身波动 2 题。加入 Markdown 与空白归一化后，历史数据的字面评分变为 30/32、32/32，结论仍是“注入收益未证实”。两套分数使用不同评分版本，不应混报成同一个前后对比。

`ans-fee-99` 的旧版第二轮发布阻断是评分误判：off2 仅把“满 99 元包邮”作为不敢向顾客确认的示例提到，却因字面包含金标短语被算通过；inject2 没提数值，被误判为丢失已正确回答的政策事实。新 `paired_diff.py` 对这类“基线含数值且伴随拒答”给出 `manual_review`、退出码 3，禁止自动放行，不把它写成经验导致的事实丢失。两份原始报告仍保留；修订版为 `diff_r1_rechecked.json` 和 `diff_r2_rechecked.json`。

## 根因分层

- 四轮原始 JSONL 均为 50 条，无缺项；原始数据在 `python_agent/evaluation/runs/2026-10-25-s10/`。
- 从 `agent_trace_runs` 按 trace_id 对齐 15 个字面评分波动题：4 题四轮证据 ID 相同，11 题证据 ID 有变化。部分题没有 `search_kb` 事件，不能把所有证据差异都叫作 Qdrant 波动。
- 从 `kb_hits` 关联实际工具查询，多个问题的 FAQ Agent 检索词在不同轮次被改写。`ans-fee-99` 四轮使用四种改写，但 off2/inject2 的三个证据 ID 完全相同，且没有经验命中事件；这题的第二轮回退不是经验注入或知识库漏检导致。
- 对 15 个抖动题固定原始问题，直接执行“当前 embedding → Qdrant top-3 → PostgreSQL 政策过滤”三轮：14/15 的证据 ID 序列稳定，1/15（`ans-ref-channel`）有片段变化，所有题三轮均有可用证据。原始文件：`wobble_fixed_queries.jsonl`、`wobble_fixed_r1/r2/r3.jsonl`。
- 对 `ans-fee-99` 的原问和四个历史改写各跑三轮：15/15 均含 `99 元` 条款，证据片段序列一致。分数有小幅变化，但未造成证据丢失。原始文件：`retrieval_probe_gold.jsonl`、`retrieval_probe_r1/r2/r3.jsonl`。
- 当前两份 t_demo 文档均为 `policy_state=demo`。FAQ 系统提示要求“不把演示条款当本店承诺”，但此前没有说明可以怎样安全转述演示数值；这使“说明示例 99 元”和“拒绝说明示例数字”两种回复交替出现。已补充：可转述明确的演示数值，必须标注仅供测试，不能称真实商家承诺。

## 已执行的调整与验收

1. 通过项目现有策略回滚接口将 t_demo 从 v3 inject 切回 v2 off。数据库核验：v2=published/off，v3=superseded/inject；`cs_agent` 的 active-strategy 接口返回 off。经验 #16/#18 保持 suspended。
2. Compose 中评测期 `EXPERIENCE_DISTILL_INTERVAL=60`、`MAX_PER_ROUND=10` 恢复为注释。仅重建 Agent，容器健康；运行环境未设置这两个覆盖值，使用代码默认 1800 秒和每轮 5 条。数据库及数据卷未重建。
3. FAQ 提示补充演示数值边界。通过真实网关对 4 题冒烟：揽收 24 小时、99 元包邮、退款 1 个工作日均以“演示示例”口径回答；运费险仍明确表示资料未说明。输出：`demo_policy_smoke_after.jsonl`。这是单次小样本，不能证明稳定改善。
4. `score.py` 对 required 短语忽略 Markdown 强调符与空白，避免“**99 元** 包邮”被算缺失；`paired_diff.py` 对基线假设引用或拒答增加人工复核。相关 10 个单测通过，Agent 全量 163 个 unittest 通过。

## 未解决的评测与产品边界

字面 `required/forbidden` 仍不是答案忠实度评测。“比如满 99 元包邮，但不能作为本店承诺”即使含数值也不能自动算作完成回答；演示政策的正确口径需要独立人工或更严格的结构化判断。当前新的第二轮配对报告含 1 个 `manual_review`，发布闸门退出码 3，不能宣布 inject v3 获准发布。

下一步先补查询改写、候选分数、过滤原因的离线诊断，并按“原始问题固定检索 / Agent 改写检索 / 同证据生成”分层复测。只有在演示政策的正确回答标准确定、噪声与误评分剔除之后，才考虑调整 top-k/阈值或扩充金标。不要把 14/15 固定检索稳定推断为所有租户或所有查询都稳定。
