#!/usr/bin/env bash
# 长对话两臂 r3 重采（带逐轮转写 + thread 键 + 语料指纹）。
# 两臂必须**串行**：并发打同一把平台 key 时，先被限流的是摘要那一次辅助调用，
# 于是 watermark=0 一片（陷阱 #23/#25），采到的数就没法解释了。
#
# 用法（前台或后台都行，路径全部绝对化，不依赖调用方的 cwd）：
#   bash agent/evaluation/run_mem_r3.sh [条数=10] [批次标签=r3]
#   标签写 r4 就产出 legacy_mem_dsv41_r4.jsonl / live_mem_dsv41_r4.jsonl 与对应对照表。
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
ROOT="$(cd "$HERE/../.." 2>/dev/null && pwd)"
N="${1:-10}"
TAG="${2:-r3}"
RUNS="$ROOT/agent/evaluation/runs"
LOG="$RUNS/mem_${TAG}_collect.log"
RUNNER="$ROOT/agent/evaluation/run_all.sh"
mkdir -p "$RUNS"
cd "$ROOT" || { echo "[r3] cd $ROOT 失败（run_all.sh 里的 .venv 相对路径要用仓库根当 cwd）" >> "$LOG"; exit 1; }
echo "[${TAG}] $(date -Is) ROOT=$ROOT N=$N 日志=$LOG" >> "$LOG"

{
  echo "[${TAG}] $(date -Is) 起：旧路径臂 $N 条"
  bash "$RUNNER" legacy "$N" deepseek-v4.1-flash \
       "legacy_mem_dsv41_${TAG}.jsonl" mem
  echo "[${TAG}] $(date -Is) 旧路径臂退出码 $?"
  echo "[${TAG}] $(date -Is) 起：新图臂 $N 条"
  bash "$RUNNER" live-mem "$N" deepseek-v4.1-flash \
       "live_mem_dsv41_${TAG}.jsonl"
  echo "[${TAG}] $(date -Is) 新图臂退出码 $?"
  echo "[${TAG}] $(date -Is) 起：strict 对照表"
  bash "$RUNNER" mem-diff \
       "$RUNS/legacy_mem_dsv41_${TAG}.jsonl" "$RUNS/live_mem_dsv41_${TAG}.jsonl" \
       "$RUNS/mem_diff_dsv41_${TAG}_off_vs_on.md" strict
  echo "[${TAG}] $(date -Is) mem-diff 退出码 $?"
  echo "[${TAG}] $(date -Is) 全部结束"
} >> "$LOG" 2>&1
