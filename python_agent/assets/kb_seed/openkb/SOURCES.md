# 开源电商知识库语料说明

本目录的 4 份文档由公开资料整理而成，用于演示租户 `t_demo` 的知识库检索与回答。

## 来源与许可

| 来源 | 许可 | 本目录用法 |
|---|---|---|
| [crazyants/customer-service-scripts](https://github.com/crazyants/customer-service-scripts)（电商客服话术大全，1370 条模板，覆盖 11 平台 / 34 行业） | MIT | 提取退换货、物流、售前场景的**流程与条件**，改写为问答条目 |
| 《消费者权益保护法》第二十五条 | 公开法律条文 | `openkb-return-law-v1.md` 的期限与例外清单 |
| 《网络购买商品七日无理由退货暂行办法》（工商总局令第 90 号） | 公开法规条文 | 商品完好判定、赠品退回、退款时限、部分退货结算 |

## 改写原则

- 不使用话术原文：原库是给人工客服复制粘贴的模板，含 `[变量]`、表情符号与英文段落，直接入库会让模型把占位符当事实。
- 每条一个事实：单条 80–180 字，对应检索侧 200 字分片，一个分片就是一个完整问答。
- 不承诺原库没有的内容：原库中“赠送补偿”“优先发货”等营销承诺未收录。
- 与本店政策冲突时以 `demo-store-policy-v1.md` 为准；本目录条目只补充政策未覆盖的场景。

## 导入

```bash
# 容器内执行：上传 → 索引 → 发布（published / all），按标题去重，可重复执行
python scripts/import_kb.py --tenant t_demo --dir assets/kb_seed/openkb \
    --category 通用问答 --publish
```

知识库有四级门禁，缺一步检索都拿不到内容：上传只登记 `status=0`、必须显式 `reindex`、`policy_state` 默认 `unreviewed` 不放行、`product_scope` 默认 `unknown` 不能发布。脚本把这四步串起来。
