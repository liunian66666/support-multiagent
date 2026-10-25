# qwen 接口下的 RSI 对照（2026-10-25）

本次使用用户提供的 OpenAI 兼容接口 `/v1/chat/completions`，请求模型名 `qwen`；接口响应的模型元数据为 `qwen3.8-flash-next`。只调整 `t_demo` 的对话模型，原知识库、Embedding 和其他租户不变。真模型直接调用与工具调用协议探针通过。

## 原始结果

同一 50 题金标，经公开网关各跑两次关闭经验（off）与注入经验（inject），共 200 次请求。每题创建独立的演示会话，限制约 6.2 秒的请求间隔。所有请求 HTTP 200、业务码 0，回答非空，200 条 trace 均可查询。原始答复、耗时和 trace ID 在四个 `qwen_*.jsonl` 文件中；`run_metadata.json` 记录顺序与模型配置，`trace_summary.json` 记录检索命中和时延。

| 轮次 | off 字面规则分数 | inject 字面规则分数 | 经验命中 | off p50 / inject p50 |
|---|---:|---:|---:|---:|
| 1 | 41/50 | 43/50 | 12/50 | 3102 / 3085 ms |
| 2 | 43/50 | 43/50 | 12/50 | 3051 / 2996 ms |

关闭经验组自身从 41/50 波动到 43/50，与首轮表面增益同为两题。`diff_r1.json`、`diff_r2.json` 保存逐题翻转与阻断项；两轮均无规则定义的 blocker。**不能据此宣称 RSI 提高答案质量或响应速度。**

首轮两个“改善”经人工核查均为字面评分误判：

- `ans-fee-display`：off 答“应付的运费金额”，inject 答“应付运费”；两者都正确，但金标要求连续子串“应付运费”，错判了 off。
- `ans-na-insurance`：off 明确说资料不足，不能确认运费险；结尾征询“是否有运费险吗？”含金标禁词“有运费险”，因而错判。inject 也没有确认运费险，却额外声称登录后系统能查实际投保情况；当前订单接口没有这个字段，属于**未经证实的能力承诺**，应单独修正。

因此首轮两个自动评分翻正中，**人工确认的实质改善为 0**。现有金标曾参与开发，不是独立留出集；评分器的简单子串规则不能代表忠实度。要证明 RSI 质量提升，需冻结新留出题、盲审业务正确性与证据支持、重复配对实验，并把能力承诺列为阻断项。

## 复算与运行状态

用现有 `python_agent/evaluation/paired_diff.py` 和 `score.py` 对四个 JSONL 与 `gold_answers.jsonl` 重算，得到上述 `diff_r1.json` 与 `diff_r2.json`。`run_qwen_ablation.py` 是采集脚本；`probe_qwen.py` 是协议探针；`configure_demo_qwen.py` 可在 business 容器内查看、切换和恢复 `t_demo` 对话模型。脚本不会把客户令牌写入产物。

采集期间模型和经验策略临时切换，采集结束时恢复。之后按照用户的模型切换要求，单独将 `t_demo` 对话模型设为该 qwen 接口，经验策略保持 v2/off。旧模型配置（含加密密钥）备份在宿主 `data/model-backups/t_demo_before_qwen_20261025.json`，属于 Git 忽略的持久化目录。切换后公开网关访客 FAQ 冒烟成功，trace `3d076c2defdf97125078d097b531d942` 的日志确认请求到新 `/v1/chat/completions` 返回 200，Embedding 仍走原配置。`post_switch_smoke.py` 可重跑只读 FAQ 冒烟。

该接口为 **HTTP**，协议探针所用的占位 Bearer 值也获接受；目前仅供演示租户使用。接入真实客户数据前须取得 HTTPS 和有效鉴权契约。供应方计费价格未知，演示租户的对话价格字段保持 0，不能据此估算真实费用。
