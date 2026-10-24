"""Collect deterministic route/order predictions from the current Agent code."""

import argparse
import json
from pathlib import Path

from app.chat.intent import route_message
from app.chat.plan import plan_compound


def collect(case):
    query = case["query"]
    route = route_message(query, case.get("tenant_id", "t_demo"))
    plan = plan_compound(query)
    return {"id": case["id"], "actual_route": route.specialist,
            "actual_order_id": route.order_id,
            "actual_action": "compound" if plan else route.specialist,
            "public_kb_query": [task.query for task in plan.tasks if task.kind == "kb"] if plan else []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gold", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases = [json.loads(line) for line in args.gold.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    payload = "".join(json.dumps(collect(case), ensure_ascii=False) + "\n" for case in cases)
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
