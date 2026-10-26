#!/bin/sh
# S6：gold_heldout_v1 有记忆配置回归（在 cs_business 容器内跑采集，宿主调用）。
# 前提：S5 已结束，Redis cs:memory:inject = on（有记忆配置）。
set -e
for R in 1 2; do
  docker exec -e EVAL_PACING=6 cs_business sh -c \
    "python /app/evaluation/collect_answers_live.py /app/evaluation/gold_heldout_v1.jsonl --output /app/evaluation/runs/ho_mem_r${R}.jsonl"
  docker exec cs_business sh -c \
    "python /app/evaluation/score_heldout.py /app/evaluation/gold_heldout_v1.jsonl /app/evaluation/runs/ho_mem_r${R}.jsonl --output /app/evaluation/runs/ho_mem_r${R}_checklist.jsonl" || true
  echo "=== S6 round $R scored"
done
