"""Reanalyse the four archived RSI runs with the current string scorer."""
import json
import importlib.util
import os
import random
import statistics
import sys
from pathlib import Path

if os.getenv("SCORE_PATH"):
    spec = importlib.util.spec_from_file_location("score_current", os.environ["SCORE_PATH"])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    score_case = module.score_case
else:
    from evaluation.score import score_case

ROOT = Path(sys.argv[1])
GOLD = Path(sys.argv[2])
NAMES = ("base_off", "exp_inject", "off_r2", "inj_r2")
gold = {r["id"]: r for r in map(json.loads, GOLD.read_text(encoding="utf-8").splitlines()) if r}
runs = {}
for name in NAMES:
    path = ROOT / (name + ".jsonl")
    rows = {r["id"]: r for r in map(json.loads, path.read_text(encoding="utf-8").splitlines()) if r}
    assert set(rows) == set(gold), (name, len(rows), len(gold))
    scores = {case_id: bool(score_case({**g, **rows[case_id]})["answer_rules_ok"])
              for case_id, g in gold.items()}
    lat = sorted(r["latency_ms"] for r in rows.values())
    runs[name] = {"rows": rows, "scores": scores,
                  "pass": sum(scores.values()),
                  "latency_p50_ms": statistics.median(lat),
                  "latency_p95_ms": lat[-3]}


def compare(a, b):
    left, right = runs[a]["scores"], runs[b]["scores"]
    return {"improve": sum(not left[i] and right[i] for i in gold),
            "regress": sum(left[i] and not right[i] for i in gold),
            "delta": sum(right.values()) - sum(left.values())}


ids = list(gold)
random.seed(20261025)
samples = []
for _ in range(10000):
    draw = random.choices(ids, k=len(ids))
    delta = sum((int(runs["exp_inject"]["scores"][i]) - int(runs["base_off"]["scores"][i])
                 + int(runs["inj_r2"]["scores"][i]) - int(runs["off_r2"]["scores"][i])) / (2 * len(ids))
                for i in draw)
    samples.append(delta)
samples.sort()
summary = {
    "status": "historical_existing_runs_before_later_prompt_change",
    "cases_per_run": len(gold),
    "run_metrics": {name: {key: value for key, value in item.items() if key not in ("rows", "scores")}
                    for name, item in runs.items()},
    "paired_r1": compare("base_off", "exp_inject"),
    "paired_r2": compare("off_r2", "inj_r2"),
    "off_self_variation": compare("base_off", "off_r2"),
    "inject_self_variation": compare("exp_inject", "inj_r2"),
    "mean_inject_minus_off_pp": round((runs["exp_inject"]["pass"] - runs["base_off"]["pass"]
                                       + runs["inj_r2"]["pass"] - runs["off_r2"]["pass"]), 2),
    "bootstrap_delta_95ci_pp": [round(100 * samples[250], 1), round(100 * samples[9750], 1)],
}

if os.getenv("POSTGRES_DSN"):
    import psycopg
    trace_ids = [row["trace_id"] for item in runs.values() for row in item["rows"].values()]
    with psycopg.connect(os.environ["POSTGRES_DSN"]) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT trace_id, events FROM agent_trace_runs WHERE trace_id = ANY(%s)", (trace_ids,))
            events_by_trace = dict(cursor.fetchall())
    for name, item in runs.items():
        hits = []
        missing_traces = 0
        for case_id, row in item["rows"].items():
            events = events_by_trace.get(row["trace_id"])
            if events is None:
                missing_traces += 1
                continue
            if any(e.get("name") == "experience_hit" for e in events):
                hits.append(case_id)
        summary["run_metrics"][name]["experience_hit_cases"] = len(hits)
        summary["run_metrics"][name]["missing_trace_cases"] = missing_traces
        summary["run_metrics"][name]["hit_case_ids"] = hits
    for base, candidate, label in (("base_off", "exp_inject", "r1"),
                                   ("off_r2", "inj_r2", "r2")):
        hit_ids = set(summary["run_metrics"][candidate]["hit_case_ids"])
        summary["hit_coverage_" + label] = {}
        for group, group_ids in (("hit", hit_ids), ("no_hit", set(gold) - hit_ids)):
            summary["hit_coverage_" + label][group] = {
                "n": len(group_ids),
                "base_pass": sum(runs[base]["scores"][i] for i in group_ids),
                "inject_pass": sum(runs[candidate]["scores"][i] for i in group_ids),
                "improve": sum(not runs[base]["scores"][i] and runs[candidate]["scores"][i]
                               for i in group_ids),
                "regress": sum(runs[base]["scores"][i] and not runs[candidate]["scores"][i]
                               for i in group_ids),
            }

print(json.dumps(summary, ensure_ascii=False, indent=2))
