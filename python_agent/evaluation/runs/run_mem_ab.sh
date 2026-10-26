#!/bin/sh
# S5 双臂驱动器（宿主机 Git Bash 运行，后台）：
#   sh run_mem_ab.sh <arm:on|off> <round:1|2> [case_limit]
# 每用例：chat(11轮,埋针) -> 触发本会话摘要(两臂都触发,信息量一致) ->
# 容器内 probe(等16s缓存 -> 探针一次成型 -> 直接追加写 OUT)。
# 臂间唯一差异 = Redis cs:memory:inject（每用例前 set，覆盖读缓存15s已被等待覆盖）。
set -e
ARM="$1"; ROUND="$2"; LIMIT="${3:-10}"
OUT="/app/evaluation/runs/mem_ab_${ARM}_r${ROUND}.jsonl"
ENV="-e ARM=$ARM -e ROUND=$ROUND -e EVAL_PACING=1.2"
i=0
while [ "$i" -lt "$LIMIT" ]; do
  i=$((i+1))
  CID=$(printf 'mem-%02d' "$i")
  docker exec cs_redis redis-cli set cs:memory:inject "$ARM" >/dev/null
  CHAT=$(docker exec $ENV cs_agent sh -c "PYTHONPATH=/app python /app/evaluation/memory_ab_live.py chat $CID")
  THREAD=$(echo "$CHAT" | grep '^THREAD=' | cut -d= -f2)
  echo "[$ARM r$ROUND $CID] chatting done thread=$THREAD"
  docker exec -e SESSION_SUMMARY_IDLE_MINUTES=0 -e SESSION_SUMMARY_LOCK_TTL=5 cs_agent sh -c \
    "PYTHONPATH=/app python /app/evaluation/summary_once.py --thread '$THREAD'" >/dev/null 2>&1 || true
  docker exec $ENV cs_agent sh -c "PYTHONPATH=/app python /app/evaluation/memory_ab_live.py probe $CID '$THREAD' '$OUT'" \
    | grep -E 'RESULT_JSON|auto_pass' | sed 's/^RESULT_JSON=.*"auto_pass": \([a-z]*\).*/PROBE_PASS \1/;s/^RESULT_JSON=.*/PROBE_PASS ?/' || true
  echo "[$ARM r$ROUND $CID] probe appended"
done
echo "ALL_DONE $ARM r$ROUND -> $OUT"
