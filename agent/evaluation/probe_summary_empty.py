# -*- coding: utf-8 -*-
"""为什么摘要会拿到空答复（``empty_summary``）——把摘要那一次补全的原始响应拆开看。

背景（报告 §6g 发现三）：给 mem 臂补上 ``compact_ledger`` 之后，串行重采的 r4 里摘要失败的
原因**全是 ``empty_summary``**（不是 ``summary_model_failed``，也不是限流）：前 5 条对话里 4 条
各失败 2~6 次，mem-03 连着 6 次全空 ⇒ 水位线一直是 0。``empty_summary`` 在代码里只有一个入口：
``message_text(response).strip() == ""``，也就是**这次补全 200 成功、文字是空的**。空文本有三种
完全不同的成因，修法也不同：

1. 内容在 ``reasoning_content``（思考块）里、``content`` 真是空串 ⇒ 我们这边的**读取形状**；
2. 模型确实返回空 completion（``max_tokens`` 被思考吃光 / 服务端截断）⇒ **参数**；
3. 网关把这次调用变成 0-token 200（配额、路由）⇒ **链路**。

所以这个探针把响应的每个相关字段都原样打出来，而不是只打"成功/失败"。
只做补全、不写任何业务数据，跑完自己退出；**必须排在真实模型臂的串行队列之后**
（同一把 key，并发时先掉队的总是这类辅助调用，见陷阱 #23）。

跑法（与 mem 臂同一个容器、同一套环境变量）：
    MSYS_NO_PATHCONV=1 docker run --rm --env-file ./.env --network cs-customer-service_cs-network \\
      -v "$PWD:/work:ro" -e PYTHONPATH=/work:/work/python_agent -e PYTHONPYCACHEPREFIX=/tmp/pyc \\
      -e QDRANT_URL=http://qdrant:6333 -e BUSINESS_API=http://business:8001 \\
      -e ORDER_BUSINESS_API=http://business:8001 -w /work/python_agent cs-test:agent \\
      sh -c 'export POSTGRES_DSN="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}"; \\
             python /work/agent/evaluation/probe_summary_empty.py --repeat 6 \\
                   --model-name deepseek-v4.1-flash --out /out/summary_empty_probe.jsonl'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for entry in (str(ROOT), str(ROOT / "python_agent")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from langchain_core.messages import HumanMessage  # noqa: E402

from agent.evaluation.run_ab_live import scaffold  # noqa: E402
from agent.legacy_bindings import deps_for_app  # noqa: E402
from agent.memory import SUMMARY_LIMIT, SUMMARY_PROMPT, message_text  # noqa: E402

TENANT = "t_demo"
# 与 memory_ab_live.cases() 的 mem-03 同形（3 轮埋针的话术）；摘要器在 compact() 里收到的
# 就是这种"用户/助手"交替的转写，这里连格式一起照抄，否则测的就不是那条路。
CHUNK = "\n".join([
    "用户：我的订单ORD-2024-703下单好几天了一直没发货，能帮我催一下吗",
    "助手：我这边没核到这笔订单，麻烦您再确认一下单号。",
    "用户：这一单我当时实付了330元，到货之后如果我要退货，退货运费是谁出的？",
    "助手：退货运费按退货原因区分，具体以店铺公示为准。",
    "用户：对了，那笔单子的发票抬头我填的是澜山食品，别给我开错了",
    "助手：好的，抬头以订单页为准，我不能替您改。",
])


def summariser_prompt(long: bool = False) -> str:
    """照 ``memory.compact()`` 的拼法走一遍：同一个 SUMMARY_PROMPT、同一个 limit。

    ``long=True`` 把输入拉长到臂里那种量级（11 轮问答、约 2-4k 字）。这一步不是可选的：
    短输入下 16 次补全只出 1 次空正文，而臂里是 34/41 次空 ⇒ 怀疑点是"思考长度随输入变长、
    把 ``max_tokens=1024`` 吃光"，那就必须把输入长度这个变量拉进来才能判。
    """
    newly = CHUNK
    if long:
        pairs = ["你们包邮门槛是多少|满 199 包邮，偏远地区除外。",
                 "退款一般几个工作日到账|验收通过后 1 个工作日内发起，到账看支付渠道。",
                 "下单后多久能发货|现货一般 48 小时内发出。",
                 "支持七天无理由退货吗|支持，商品需保持完好。",
                 "会员积分怎么获得|下单支付后按比例累计。",
                 "客服工作时间是几点到几点|9:00-18:00。",
                 "尺码不合适可以换货吗|可以，七天内申请换货。",
                 "优惠券能和满减一起用吗|一般不可叠加，以结算页为准。"]
        newly = CHUNK + "\n" + "\n".join(
            f"用户：{line.split('|', 1)[0]}\n助手：{line.split('|', 1)[1]}" for line in pairs * 2)
    return SUMMARY_PROMPT.format(limit=SUMMARY_LIMIT, previous="（无）", newly=newly[:5000])


def dump(response: Any) -> dict:
    """把"文字到底在哪个字段"变成可核对的字段。"""
    content = getattr(response, "content", None)
    additional = dict(getattr(response, "additional_kwargs", {}) or {})
    metadata = dict(getattr(response, "response_metadata", {}) or {})
    reasoning = additional.get("reasoning_content") or additional.get("reasoning") or ""
    usage = getattr(response, "usage_metadata", None) or {}
    text = message_text(response) if response is not None else ""
    return {"content_type": type(content).__name__,
            "content_chars": len(text.strip()),
            "content_head": text[:120].replace("\n", " ⏎ "),
            "content_raw": json.dumps(content, ensure_ascii=False)[:200] if not isinstance(content, str) else "",
            "reasoning_chars": len(str(reasoning)),
            "reasoning_head": str(reasoning)[:120].replace("\n", " ⏎ "),
            "finish_reason": additional.get("finish_reason") or metadata.get("finish_reason") or "",
            "output_tokens": (dict(usage) or {}).get("output_tokens"),
            "total_tokens": (dict(usage) or {}).get("total_tokens"),
            "additional_keys": sorted(additional)}


async def run(args: argparse.Namespace) -> list[dict]:
    app, http, pool = await scaffold()
    out: list[dict] = []
    try:
        deps = await deps_for_app(app, TENANT, model_source="platform",
                                  model_name=args.model_name)
        model = await deps.model()          # 与图里摘要用的是同一个 provider 调用
        if args.max_tokens:
            # 只在探针里改这一次调用的预算，用来量"空正文到底是预算被思考吃光还是别的"，
            # 不动 agent/ 的任何生产配置。
            model = model.bind(max_tokens=args.max_tokens)
        prompt = summariser_prompt(long=bool(args.long_input))
        for index in range(args.repeat):
            try:
                response = await model.ainvoke([HumanMessage(content=prompt)])
                record = {"i": index, "max_tokens": args.max_tokens or "inherit",
                          "prompt_chars": len(prompt), "long_input": bool(args.long_input),
                          **dump(response)}
            except Exception as exc:  # noqa: BLE001 - 探针要的是"这次到底怎么了"，不能中断
                record = {"i": index, "max_tokens": args.max_tokens or "inherit",
                          "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            out.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
    finally:
        await pool.close()
        await http.aclose()
    return out


def main(args: argparse.Namespace) -> int:
    records = asyncio.run(run(args))
    empty = sum(1 for r in records if r.get("content_chars", -1) == 0)
    with_reasoning = sum(1 for r in records if r.get("reasoning_chars", 0) > 0)
    print(f"\n{len(records)} 次摘要补全：空正文 {empty} 次 · 带 reasoning {with_reasoning} 次"
          f" · 结论看逐行字段（content 空 + reasoning 非空 ⇒ 是读取形状；两者都空且 "
          f"output_tokens≈0 ⇒ 是参数/链路）")
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"原始行 → {path}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=6)
    parser.add_argument("--long", dest="long_input", action="store_true",
                        help="把输入拉长到臂里那种量级（思考长度随输入变长是这个探针要判的变量）")
    parser.add_argument("--max-tokens", dest="max_tokens", type=int, default=0,
                        help="只在这一次探针调用上覆盖预算（0=继承生产配置）；"
                             "用来验证『空正文』是不是思考把 max_tokens 吃光了")
    parser.add_argument("--model-name", dest="model_name", default="deepseek-v4.1-flash")
    parser.add_argument("--out", default="")
    raise SystemExit(main(parser.parse_args()))
