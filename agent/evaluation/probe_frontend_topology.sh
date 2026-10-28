#!/usr/bin/env bash
# 阶段6「验证前端」里不需要模型、也不需要发一条消息的那一半：客户能触达到哪一层。
#
# 为什么单独测这件事：报告 §5c 是从代码读出「浏览器不直接调 /agent/chat」的，而开关那条路一旦
# 生效，最要紧的安全问题就是「客户能不能绕过 business 的身份注入直接打到 agent」。这是网络事实，
# 可以用 HTTP 码与容器端口表钉死，不必发一轮对话（发了就会往 business MySQL 的 messages 写数据）。
#
# 全部只读：docker 的 ps / exec-GET、nginx 的 return 404、psql 的 SELECT，以及宿主对 8000 的一次拒连。
# 不发消息、不建会话、不写任何表；控制项（/py-api/health 与 /chat/login.html 应为 200）用来证明
# 那些 404 是路由决策，不是整站挂了。
#
#   bash agent/evaluation/run_all.sh frontend [base_url]      # 默认 http://127.0.0.1:8080
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BASE="${1:-http://127.0.0.1:8080}"
PASS=0
FAIL=0
ROWS=""
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

chk() { # id  expected  actual  note
  local id="$1" want="$2" got="$3" note="$4" v
  if [ "$got" = "$want" ]; then PASS=$((PASS + 1)); v=PASS; else FAIL=$((FAIL + 1)); v=FINDING; fi
  ROWS="$ROWS$(printf '{"check":"%s","want":"%s","got":"%s","note":"%s","verdict":"%s"}\n' \
    "$id" "$want" "$got" "$note" "$v")"$'\n'
  printf '[%-7s] %-27s want=%-14s got=%-14s %s\n' "$v" "$id" "$want" "$got" "$note"
}
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$1" || true; }
agent_ps() { docker compose -f "$ROOT/docker-compose.yaml" ps --format '{{.Name}}\t{{.Ports}}'; }

echo "== 1. 谁发布了宿主端口（agent 应当只有容器网内部端口）=="
agent_ps | sed 's/^/  /'
AGENT_PORTS="$(agent_ps | awk -F'\t' '$1=="cs_agent"{print $2}')"
if printf '%s' "$AGENT_PORTS" | grep -q -- '->'; then
  AGENT_PUBLISHED=Published
else
  AGENT_PUBLISHED=internal-only
fi
chk "agent_host_port" "internal-only" "$AGENT_PUBLISHED" "compose 里 cs_agent 的端口列＝「${AGENT_PORTS:-（空）}」"
HOST_DIRECT="$(code http://127.0.0.1:8000/agent/chat)"
chk "agent_direct_refused" "000" "$HOST_DIRECT" "宿主直连 agent:8000 应被拒（curl 给 000）"

echo "== 2. 边缘配置是活的，不是只躺在 deploy/ 里 =="
# ⚠ 宿主是 Git Bash 时必须走 sh -c：`docker exec cs_nginx grep -c api/agent /etc/nginx/...` 会被
#    MSYS 把容器内路径改写成 D:/git/Git/etc/nginx/...，grep 报 No such file → 探针给出假发现。
#    （实测踩过：第一版直接 exec 得到 got=none，同一条命令套进 sh -c 得到 2。）
NGINX_HITS="$(docker exec cs_nginx sh -c 'grep -c api/agent /etc/nginx/conf.d/default.conf' 2>/dev/null | tr -d '\r')"
chk "nginx_edge_rules" "2" "${NGINX_HITS:-none}" "容器里 conf.d/default.conf 中 api/agent 出现次数（两条 return 404）"
EDGE_PATHS=(/py-api/api/agent/chat /api/agent/chat /agent/chat)
for i in "${!EDGE_PATHS[@]}"; do
  EDGE="${EDGE_PATHS[$i]}"
  GOT="$(code "$BASE$EDGE")"
  chk "edge_404_$((i + 1))" "404" "$GOT" "$BASE$EDGE"
done
HEALTH="$(code "$BASE/py-api/health")"
LOGIN="$(code "$BASE/chat/login.html")"
chk "control_business_health" "200" "$HEALTH" "同一台 nginx 上的业务健康检查（证明 404 是路由决策不是整站挂了）"
chk "control_login_page" "200" "$LOGIN" "前端壳可达；SDK 拼的是 <server>/chat/pc.html 的 iframe，聊天走 /ws/chat"

echo "== 3. 活进程的路由表里有没有开关那条路（不问镜像文件，问进程）=="
docker exec -i cs_business python - >"$TMP" 2>&1 <<'PY' || true
import json, urllib.request
try:
    data = json.load(urllib.request.urlopen("http://agent:8000/openapi.json", timeout=10))
    paths = sorted(data.get("paths", {}))
    flags = [str(x in paths).lower() for x in ("/agent/chat", "/agent/chat/stream", "/agent/chat/v2")]
    print(len(paths), *flags)
except Exception as exc:
    print(0, "false", "false", "err:%s:%s" % (type(exc).__name__, str(exc)[:60]))
PY
read -r N_ROUTES HAS_CHAT HAS_STREAM HAS_V2 _rest < "$TMP"
N_ROUTES="${N_ROUTES:-0}"
if [ "${N_ROUTES}" -gt 0 ] 2>/dev/null; then REACH=ok; else REACH=no; fi
chk "business_can_reach_agent" "ok" "$REACH" "business→agent 取到 OpenAPI：$(tr '\n' ' ' < "$TMP")"
chk "chat_route_in_live_image" "true" "$HAS_CHAT" "旧入口 /agent/chat 确实在跑着的服务里（探针不是对着空气测）"
chk "v2_route_absent_in_live_image" "false" "$HAS_V2" "运行中的服务里没有 /agent/chat/v2 —— §5e「图只在工作树」的第三种独立测法"
CS_AGENT_URL="$(docker exec cs_business printenv CS_AGENT_URL 2>/dev/null | tr -d '\r')"
chk "business_agent_upstream_env" "http://agent:8000" "${CS_AGENT_URL:-none}" "身份头由 business 注入；客户面只到 /ws/chat（nginx 1.1 Upgrade → business:8001）"

echo "== 4. trace 落库列宽：委派名变长会不会把整行吃掉 =="
SPEC_W="$(docker exec cs_postgres psql -U cs_agent_user -d cs_agent_db -A -t -F'|' -c "
select character_maximum_length from information_schema.columns
 where table_name='agent_trace_runs' and column_name='specialist'" 2>/dev/null | tr -d '\r')"
SPEC_MAX="$(docker exec cs_postgres psql -U cs_agent_user -d cs_agent_db -A -t -F'|' -c "
select coalesce(max(length(specialist)),-1) from agent_trace_runs" 2>/dev/null | tr -d '\r')"
N_RUNS="$(docker exec cs_postgres psql -U cs_agent_user -d cs_agent_db -A -t -F'|' -c "
select count(*) from agent_trace_runs" 2>/dev/null | tr -d '\r')"
WORST="$(printf 'faq,order,general' | wc -c | tr -d ' ')"
chk "specialist_column_width" "32" "${SPEC_W:-none}" "agent_trace_runs.specialist 的 varchar 宽度（现网 ${N_RUNS:-?} 行，实际最长 ${SPEC_MAX:-?} 字符）"
if [ "${SPEC_W:-0}" -gt 0 ] 2>/dev/null && [ "$WORST" -le "$SPEC_W" ]; then FIT=yes; else FIT=no; fi
chk "mounted_worst_case_fits" "yes" "$FIT" \
  "三条角色全委派＝${WORST} 字符 ≤ 宽度 ${SPEC_W:-?}：今天不会撞墙。余量 $(( ${SPEC_W:-0} - WORST )) 字符——但 four_agent.py:63 把 specialist 截到 120，而 app/services/trace.py:62 的 try/except 只 logger.exception，将来角色变多或改名一旦超过宽度，代价是整行 trace 静默丢失（不是截断）。"

echo
printf '{"generated_at":"%s","pass":%s,"findings":%s}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PASS" "$FAIL"
echo "前端与网络边界：${PASS} 通过 / ${FAIL} 发现"
if [ -n "${OUT:-}" ]; then
  printf '%s' "$ROWS" > "$OUT"
  echo "原始数据 → $OUT"
fi
if [ "$FAIL" -gt 0 ]; then exit 1; fi
exit 0
