#!/usr/bin/env bash
# 新链路（agent/）的可复现验证入口。全部走项目自带的只读挂载通道：
# 生产镜像不被重建、python_agent/ 与 python_business/ 一行不改。
#
# 前置（一次）：
#   docker build -f scripts/docker/Dockerfile.test --target agent -t cs-test:agent .
#
# 用法：
#   bash agent/evaluation/run_all.sh test      # 83 项隔离测试（镜像内 py3.13 + 生产依赖）
#   bash agent/evaluation/run_all.sh demo      # 复合咨询 + 长对话 + 重启续聊（脚本化模型）
#   bash agent/evaluation/run_all.sh demo-pg   # 同一套演示落到真实 AsyncPostgresSaver（自建线程，跑完删除）
#   bash agent/evaluation/run_all.sh pg        # 真实 AsyncPostgresSaver 检查点验证（自建线程，跑完删除）
#   bash agent/evaluation/run_all.sh pg-restart # 两个独立容器进程之间的续聊（先 --keep 再 --resume-thread）
#   bash agent/evaluation/run_all.sh xproc [worker数] [每worker轮数]  # 跨进程共写同一线程：先 --no-guard 复现丢轮，再 --guard（pg advisory 锁）要求零丢轮
#   bash agent/evaluation/run_all.sh retrieval [limit] [repeat] [out]   # 只读检索稳定性探针（单批）
#   bash agent/evaluation/run_all.sh retrieval-batches [批数] [limit] [repeat]  # 连跑多批独立进程并汇总翻动率（§7）
#   bash agent/evaluation/run_all.sh embedding-drift  # 分层定位波动：embedding 返回的向量 vs Qdrant 返回的邻居（只读）
#   bash agent/evaluation/run_all.sh gateway    # 模型网关可达性诊断（只读，零推理消耗）
#   bash agent/evaluation/run_all.sh completion [模型名,逗号分隔] # 各模型源发一次最小补全（TCP 可达≠能出词）；带模型名可分清账号级封禁/单模型没额度
#   bash agent/evaluation/run_all.sh models [N]  # 先 GET /models 列这把 key 看得见哪些模型（只读零消耗），再对前 N 个各发一次最小补全
#   bash agent/evaluation/run_all.sh http       # 真 ASGI 入口 + 真 PG：JSON/SSE、重放、开关、越权、降级
#   bash agent/evaluation/run_all.sh overhead [turns]      # 编排开销与上下文规模（内存 saver，不需模型）
#   bash agent/evaluation/run_all.sh overhead-pg [turns]   # 同上但用真实 PG，跑完自删线程
#   bash agent/evaluation/run_all.sh guards      # Reviewer 对抗语料：改写后的违规是否仍被拦（0 泄漏 0 误拦）
#   bash agent/evaluation/run_all.sh memory     # 四臂长对话记忆对照（确定性，宿主 venv 即可）
#   bash agent/evaluation/run_all.sh mounted    # 已挂载 app 侧图实测账本（脚本化模型，零真实数据）
#   bash agent/evaluation/run_all.sh mounted-guards # 同一份 30 例对抗语料打在挂载图的 Reviewer 上（两遍）
#   bash agent/evaluation/run_all.sh demo-mounted # 开关那条路的演示记录（复合咨询+长对话续聊+重启）
#   bash agent/evaluation/run_all.sh mounted-memory # 挂载图并入逐题记忆协议，产出两张五臂表（§6e）
#   bash agent/evaluation/run_all.sh trace-baseline # 旧路径每轮耗时分布（只读聚合 agent_trace_runs）
#   bash agent/evaluation/run_all.sh image      # 候选镜像离线自检（挂载后能否 import、开关缺省值、审批回旧图）
#   bash agent/evaluation/run_all.sh frontend    # 前端与网络边界：客户能触达到哪一层（只读，不发消息）
#   bash agent/evaluation/run_all.sh compare [候选answer.jsonl]  # 留出集逐题差异闸门（回退=拦截）
#   bash agent/evaluation/run_all.sh live [limit]  # 阶段6 真实模型采集（需模型可用；默认 shadow FAQ）
#   bash agent/evaluation/run_all.sh live-mock [arm] [limit]  # 同一套采集器打到本地 OpenAI 兼容替身：真 HTTP 往返、零可比性
#   bash agent/evaluation/run_all.sh all     # 一次跑完上面所有"不需模型、不需额外授权"的项，逐条给退出码/耗时/摘要
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
NET=cs-customer-service_cs-network
RUNS="$ROOT/agent/evaluation/runs"
# --env-file 由 Windows 侧的 docker 客户端读取，必须是 Windows 路径；-v 反而要 msys 路径
ENV_WIN="$(cd "$ROOT" && pwd -W)/.env"
mkdir -p "$RUNS"

PG_ENV='export POSTGRES_DSN="postgresql://${POSTGRES_USER:-cs_user}:${POSTGRES_PASSWORD:-cs_pass123}@postgres:5432/${POSTGRES_DB:-cs_agent_db}"; '

case "${1:-help}" in
  test)
    MSYS_NO_PATHCONV=1 docker run --rm -v "$ROOT:/work:ro" -e PYTHONPATH=/work:/work/python_agent \
      -e PYTHONPYCACHEPREFIX=/tmp/pyc -e PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
      -w /work cs-test:agent \
      sh -c 'pip install -q -r /work/python_agent/requirements.txt >/dev/null && pip install -q pytest pytest-asyncio >/dev/null && python -m pytest agent/tests -q' ;;
  demo)
    MSYS_NO_PATHCONV=1 docker run --rm -v "$ROOT:/work:ro" -v "$RUNS:/out" \
      -e PYTHONPATH=/work:/work/python_agent -e PYTHONPYCACHEPREFIX=/tmp/pyc -w /work cs-test:agent \
      python -m agent.demo --turns 8 --dump /out/demo_offline.jsonl ;;
  demo-pg)
    # 同一套演示落到真实 AsyncPostgresSaver：自建 demo-* 线程，报写入前后行数，收尾删干净
    MSYS_NO_PATHCONV=1 docker run --rm --env-file "$ENV_WIN" --network "$NET" \
      -v "$ROOT:/work:ro" -v "$RUNS:/out" -e PYTHONPATH=/work:/work/python_agent \
      -e PYTHONPYCACHEPREFIX=/tmp/pyc -w /work cs-test:agent \
      sh -c "${PG_ENV}python -m agent.demo --turns 8 --dump /out/demo_pg.jsonl --postgres \"\$POSTGRES_DSN\"" ;;
  pg)
    MSYS_NO_PATHCONV=1 docker run --rm --env-file "$ENV_WIN" --network "$NET" \
      -v "$ROOT:/work:ro" -e PYTHONPATH=/work:/work/python_agent -e PYTHONPYCACHEPREFIX=/tmp/pyc \
      -w /work/python_agent cs-test:agent \
      sh -c "${PG_ENV}python /work/agent/evaluation/verify_checkpoint_postgres.py" ;;
  pg-restart)
    # 两个独立容器进程之间的续聊：第二个进程必须从真实 PG 里恢复第一个写的线程
    THR="t_demo:pgcheck-crossproc-$$"
    pg() { MSYS_NO_PATHCONV=1 docker run --rm --env-file "$ENV_WIN" --network "$NET" \
        -v "$ROOT:/work:ro" -e PYTHONPATH=/work:/work/python_agent -e PYTHONPYCACHEPREFIX=/tmp/pyc \
        -w /work/python_agent cs-test:agent sh -c "${PG_ENV}$1"; }
    pg "python /work/agent/evaluation/verify_checkpoint_postgres.py --thread $THR --keep"
    echo "--- 第二个进程恢复 $THR ---"
    pg "python /work/agent/evaluation/verify_checkpoint_postgres.py --resume-thread $THR" ;;
  xproc)
    # 跨进程并发写同一线程（§5e M10/M11 的那条平台层缺口）——两遍，只比"丢了几轮"：
    #   1) --no-guard：今天的行为，asyncio.Lock 管不到别的进程 ⇒ 预期会丢
    #   2) --guard   ：agent/thread_guard.py 的 pg advisory 锁 ⇒ 要求 lost_turns=0
    # 探针只写自己那条 support_v2:t_demo:xproc-* 线程并在最后删除（复点行数 0）。
    # 退出码只看 guard 那遍：没锁那遍丢轮是**已知为负的证据**，不是回归。
    W="${2:-3}"; T="${3:-4}"
    # 账本**累加**而不是每次清空：丢轮率是非确定数，要连着几批看区间（陷阱 #6）。
    # 读侧按 thread 过滤，所以历史批次不会污染本次判定。
    mkdir -p "$RUNS"
    xp() { MSYS_NO_PATHCONV=1 docker run --rm --env-file "$ENV_WIN" --network "$NET" \
        -v "$ROOT:/work:ro" -v "$RUNS:/out" -e PYTHONPATH=/work:/work/python_agent \
        -e PYTHONPYCACHEPREFIX=/tmp/pyc -w /work/python_agent cs-test:agent \
        sh -c "${PG_ENV}python /work/agent/evaluation/probe_cross_process_lock.py --role run --workers $W --turns $T --out /out/cross_process_lock.jsonl $1"; }
    echo "--- 不加跨进程锁（复现丢轮）---"; xp --no-guard || true
    echo "--- 加 pg advisory 锁 ---"; if xp --guard; then XRC=0; else XRC=1; fi
    .venv/Scripts/python.exe - <<'PY'
import json, pathlib
for line in pathlib.Path("agent/evaluation/runs/cross_process_lock.jsonl").read_text(encoding="utf-8").splitlines():
    row = json.loads(line)
    if row.get("role") == "summary":
        print(f"guard={str(row['guard']):<5} submitted={row['submitted']} refused={row['refused']} "
              f"written={row['distinct_human_ids']} ledger={row['ledger_turns']} "
              f"lost={row['lost_turns']} 最慢worker={row.get('slowest_worker_ms')}ms "
              f"残留行数={row['rows_after_cleanup']}")
PY
    echo "补丁组退出码=$XRC（0＝加锁后零丢轮、零崩溃、零残留）"
    exit "$XRC" ;;
  retrieval)
    MSYS_NO_PATHCONV=1 docker run --rm --env-file "$ENV_WIN" --network "$NET" \
      -v "$ROOT:/work:ro" -v "$RUNS:/out" -e PYTHONPATH=/work:/work/python_agent \
      -e PYTHONPYCACHEPREFIX=/tmp/pyc -e QDRANT_URL=http://qdrant:6333 \
      -e BUSINESS_API=http://business:8001 -w /work/python_agent cs-test:agent \
      sh -c "${PG_ENV}python /work/agent/evaluation/probe_retrieval_stability.py --limit ${2:-15} --repeat ${3:-5} --out ${4:-/out/retrieval_stability.jsonl}" ;;
  embedding-drift)
    # 分层定位检索波动：同一句问话，embedding 会不会返回不同向量？固定向量喂进 Qdrant，邻居会不会换？
    # 只有后者才是本仓库能改的那一层——先分清，再决定要不要加确定性 tie-break。
    MSYS_NO_PATHCONV=1 docker run --rm --env-file "$ENV_WIN" --network "$NET" \
      -v "$ROOT:/work:ro" -v "$RUNS:/out" -e PYTHONPATH=/work:/work/python_agent \
      -e PYTHONPYCACHEPREFIX=/tmp/pyc -e QDRANT_URL=http://qdrant:6333 \
      -e BUSINESS_API=http://business:8001 -w /work/python_agent cs-test:agent \
      sh -c "${PG_ENV}python /work/agent/evaluation/probe_embedding_determinism.py --out /out/embedding_drift.jsonl" ;;
  retrieval-batches)
    # 同一份固定题集连跑 N 个独立批次（每批一个容器进程）。单批只能回答"这批翻没翻"，
    # 多批才能给出翻动率——§7 的那句"零抖动"就是只看一批才说错的。
    N="${2:-3}"
    for k in $(seq 1 "$N"); do
      bash "$0" retrieval "${3:-15}" "${4:-5}" "/out/retr_batch${k}.jsonl" | tail -6
    done
    .venv/Scripts/python.exe agent/evaluation/summarize_retrieval_batches.py ;;
  overhead)
    # 编排开销与上下文规模：模型即刻返回，剥掉推理时间；三臂各 40 轮
    .venv/Scripts/python.exe agent/evaluation/measure_overhead.py --turns "${2:-40}" ;;
  overhead-pg)
    # 同上，但用真实 AsyncPostgresSaver；脚本跑完自删三个线程并打印行数自检
    MSYS_NO_PATHCONV=1 docker run --rm --env-file "$ENV_WIN" --network "$NET" \
      -v "$ROOT:/work:ro" -v "$RUNS:/out" -e PYTHONPATH=/work:/work/python_agent \
      -e PYTHONPYCACHEPREFIX=/tmp/pyc -w /work/python_agent cs-test:agent \
      sh -c "${PG_ENV}python /work/agent/evaluation/measure_overhead.py --turns ${2:-40} --postgres --out /out/orchestration_overhead_postgres.jsonl" ;;
  guards)
    .venv/Scripts/python.exe agent/evaluation/probe_guards_adversarial.py ;;
  memory)
    # 冻结十例（同构、不同值）+ 结构变体十一例（锚点深度 × 对话长度），逐题四臂对照
    .venv/Scripts/python.exe agent/evaluation/memory_context_ab.py
    .venv/Scripts/python.exe agent/evaluation/diff_arms.py -o agent/evaluation/runs/memory_four_arm_diff.md
    .venv/Scripts/python.exe agent/evaluation/memory_context_ab.py --geometry
    .venv/Scripts/python.exe agent/evaluation/diff_arms.py \
      agent/evaluation/runs/memory_context_geometry.jsonl -o agent/evaluation/runs/memory_geometry_diff.md ;;
  gateway)
    MSYS_NO_PATHCONV=1 docker run --rm --env-file "$ENV_WIN" --network "$NET" \
      -v "$ROOT:/work:ro" -e PYTHONPATH=/work:/work/python_agent -e PYTHONPYCACHEPREFIX=/tmp/pyc \
      -e BUSINESS_API=http://business:8001 -w /work/python_agent cs-test:agent \
      python /work/agent/evaluation/probe_model_gateway.py ;;
  completion)
    # TCP 可达 ≠ 能出词：各发一次最小补全，把配额/密钥层面的拒绝坐实。
    # 第二个参数可选：逗号分隔的备用模型名，用来分清"账号级封禁"和"单个模型没额度"。
    EXTRA=""
    [ -n "${2:-}" ] && EXTRA="--try-model $2"
    MSYS_NO_PATHCONV=1 docker run --rm --env-file "$ENV_WIN" --network "$NET" \
      -v "$ROOT:/work:ro" -v "$RUNS:/out" -e PYTHONPATH=/work:/work/python_agent \
      -e PYTHONPYCACHEPREFIX=/tmp/pyc -e BUSINESS_API=http://business:8001 -w /work/python_agent cs-test:agent \
      python /work/agent/evaluation/probe_platform_completion.py --out /out/model_availability.jsonl $EXTRA ;;
  models)
    # 别再猜模型名：先 GET /models 看这把 key 到底看得见哪些（只读、零推理消耗），
    # 再把列出来的前 N 个逐个发一次最小补全。第二个参数默认 6。
    N="${2:-6}"
    MSYS_NO_PATHCONV=1 docker run --rm --env-file "$ENV_WIN" --network "$NET" \
      -v "$ROOT:/work:ro" -v "$RUNS:/out" -e PYTHONPATH=/work:/work/python_agent \
      -e PYTHONPYCACHEPREFIX=/tmp/pyc -e BUSINESS_API=http://business:8001 -w /work/python_agent cs-test:agent \
      python /work/agent/evaluation/probe_platform_completion.py \
        --list-models --source none --auto-probe-listed "$N" \
        --list-out /out/model_list.jsonl --out /out/model_availability.jsonl ;;
  http)
    # 阶段6 传输层：真 ASGI 入口 + 真 PG 检查点 + 脚本化模型（不依赖模型网关）
    MSYS_NO_PATHCONV=1 docker run --rm --env-file "$ENV_WIN" --network "$NET" \
      -v "$ROOT:/work:ro" -e PYTHONPATH=/work:/work/python_agent -e PYTHONPYCACHEPREFIX=/tmp/pyc \
      -w /work/python_agent cs-test:agent \
      sh -c "${PG_ENV}python /work/agent/evaluation/verify_entrypoint_http.py ${2:-}" ;;
  mounted)
    # 已挂载的 app 侧四 Agent 图（chat.py 的 FOUR_AGENT_GRAPH 分支）实测账本：
    # 走生产入口 four_agent.run_turn，脚本化模型 + InMemorySaver，零模型调用、零真实数据写入。
    MSYS_NO_PATHCONV=1 docker run --rm -v "$ROOT:/work:ro" -v "$RUNS:/out" \
      -e PYTHONPATH=/work/python_agent:/work -e PYTHONPYCACHEPREFIX=/tmp/pyc \
      -w /work/python_agent cs-test:agent \
      python /work/agent/evaluation/probe_mounted_graph.py --out /out/mounted_graph_probe.jsonl ;;
  mounted-guards)
    # 同一份 30 例对抗语料（§5d 用来打 agent/guards.py 的那一份）改打挂载图里的 Reviewer。
    # 两遍：corpus=语料原样（没委派，必触发"必须委派"），backed=先配好正确委派（只考内容级规则）。
    # 带 --gate 时两遍都要 0 漏放 0 误拦才返回 0；今天它返回 1，这就是"开关继续留 off"的证据。
    GATE=""; case "${2:-}" in --gate) GATE="--gate" ;; esac
    MSYS_NO_PATHCONV=1 docker run --rm -v "$ROOT:/work:ro" -v "$RUNS:/out" \
      -e PYTHONPATH=/work/python_agent:/work -e PYTHONPYCACHEPREFIX=/tmp/pyc \
      -w /work/python_agent cs-test:agent \
      python /work/agent/evaluation/probe_mounted_guards.py --out /out/mounted_guards_adversarial.json $GATE ;;
  mounted-fixed|mounted-fixed)
    # 把 §5f（Reviewer 内容规则）、§5e-M2（重放无幂等键）与 §5e-M5b（失败轮留半成品）三份**修法**
    # 在同一份语料/同一套探针上实测一遍——但只在临时副本里跑：工作树的 python_agent/ 一个字节都不动
    # （用户明确要求不修改 app 部分），生产镜像也不动。对照组 = 同一套 stage 流程、被改的两个文件
    # 换回原样，保证前后两组数字是同一把尺子量出来的，而不是"换了台机器重新测"。
    # 暂存目录必须落在 Docker 看得见的盘上（.tmp_eval/ 本来就是干这个的）：
    # /tmp 是 MSYS 自己的路径，docker -v 拿到它会挂出一个空目录，容器起来就报 read-only。
    STAGE_ROOT="$ROOT/.tmp_eval/mounted_fix_$$"
    PATCHED_ROOT="$STAGE_ROOT/patched"; CTRL_ROOT="$STAGE_ROOT/control"
    mkdir -p "$PATCHED_ROOT" "$CTRL_ROOT"
    .venv/Scripts/python.exe agent/deploy/make_mounted_review_fix.py \
      --dest "$PATCHED_ROOT" --scope all >/dev/null || { rm -rf "$STAGE_ROOT"; exit 1; }
    cp -a "$PATCHED_ROOT/repo" "$CTRL_ROOT/repo"
    # 三个 scope 落在同样的两个文件上：guards→graph.py，persistence→four_agent.py + graph.py，
    # recovery→four_agent.py + graph.py（last_error 状态通道）
    cp python_agent/app/multi_agent/graph.py    "$CTRL_ROOT/repo/python_agent/app/multi_agent/graph.py"
    cp python_agent/app/services/four_agent.py  "$CTRL_ROOT/repo/python_agent/app/services/four_agent.py"
    run_on() {   # $1=stage root  $2=探针  $3=out 名  ${@:4}=额外参数
      # 副本里没有 .env（app/infra/config.py 按仓库根加载它），把真 .env 挂进去；
      # 副本可写（:ro 会让 Docker 无法在只读目录下建 .env 挂载点），但它是临时拷贝，
      # 真正的边界是工作树的 python_agent/ 一个字节都不动——由 stage 是拷贝来保证。
      MSYS_NO_PATHCONV=1 docker run --rm -v "$1:/work" -v "$ENV_WIN:/work/.env:ro" \
        -v "$RUNS:/out" \
        -e PYTHONPATH=/work/python_agent:/work -e PYTHONPYCACHEPREFIX=/tmp/pyc \
        -w /work/python_agent cs-test:agent \
        python "/work/agent/evaluation/$2" --out "/out/$3" "${@:4}"
    }
    echo "--- 对照组（当前 app 侧原样）---"
    if run_on "$CTRL_ROOT/repo" probe_mounted_guards.py mounted_guards_control.json --gate \
       > "$RUNS/mounted_guards_control.log" 2>&1; then CTRL_RC=0; else CTRL_RC=1; fi
    run_on "$CTRL_ROOT/repo" probe_mounted_graph.py mounted_graph_control.jsonl \
       > "$RUNS/mounted_graph_control.log" 2>&1 || true
    grep -a "不安全\|按类别\|改判\|^GATE" "$RUNS/mounted_guards_control.log" | tail -6
    echo "--- 补丁组（同一副本 + 两份修法）---"
    if run_on "$PATCHED_ROOT/repo" probe_mounted_guards.py mounted_guards_patched.json --gate \
       > "$RUNS/mounted_guards_patched.log" 2>&1; then PATCH_RC=0; else PATCH_RC=1; fi
    run_on "$PATCHED_ROOT/repo" probe_mounted_graph.py mounted_graph_patched.jsonl \
       > "$RUNS/mounted_graph_patched.log" 2>&1 || true
    grep -a "不安全\|按类别\|改判\|^GATE" "$RUNS/mounted_guards_patched.log" | tail -6
    echo "--- 挂载图逐题差异（control → patched）---"
    # --show 把成本列也对照：Reviewer 每多拦一次就多跑一遍 supervisor，判定相同、成本不同
    # 的行走 "都通过" 会被完全掩盖——attempts/billed_msgs 就是那一层的账。
    .venv/Scripts/python.exe agent/evaluation/summarize_mounted_arms.py \
      "$RUNS/mounted_graph_control.jsonl" "$RUNS/mounted_graph_patched.jsonl" \
      --show attempts,billed_msgs,billed_msgs_per_turn,supervisor_calls,lost_turn_trials,failure_turn_error_events \
      --out "$RUNS/mounted_arms_diff.json" || GRAPH_RC=$?
    echo "guards 闸门：对照组退出码=$CTRL_RC（今天应为 1＝有漏放）  补丁组退出码=$PATCH_RC（0＝两遍都 0 漏放 0 误拦）"
    rm -rf "$STAGE_ROOT"
    exit $(( PATCH_RC + ${GRAPH_RC:-0} )) ;;
  frontend)
    # 阶段6「验证前端」里不需要模型、也不需要发消息的那一半：客户能触达到哪一层、
    # 活进程的路由表里有没有开关那条路、trace 列宽余量。全部只读（ps / GET / SELECT）。
    OUT="$RUNS/frontend_topology.jsonl" bash agent/evaluation/probe_frontend_topology.sh "${2:-http://127.0.0.1:8080}" ;;
  trace-baseline)
    # 旧路径每轮耗时分布：直接聚合现网 agent_trace_runs（只读 SELECT，不写任何数据）。
    # 这是阶段6 延迟对照里"旧版"那一侧最全的一份证据（n=1741 轮，10-21..10-26）。
    docker exec cs_postgres psql -U cs_agent_user -d cs_agent_db -A -F'|' -c "
      select route, count(*) n,
             count(*) filter (where duration_ms < 200) model_free,
             percentile_cont(0.5)  within group (order by duration_ms)::int p50_ms,
             percentile_cont(0.95) within group (order by duration_ms)::int p95_ms,
             min(duration_ms) min_ms, max(duration_ms) max_ms
      from agent_trace_runs group by route order by n desc;" -c "
      select count(*) total_runs,
             count(*) filter (where duration_ms < 200) model_free_runs,
             round(100.0*count(*) filter (where duration_ms < 200)/count(*),1) pct_model_free,
             percentile_cont(0.5) within group (order by duration_ms)::int p50_all,
             percentile_cont(0.95) within group (order by duration_ms)::int p95_all,
             min(created_at)::date first_day, max(created_at)::date last_day
      from agent_trace_runs;" \
      | tee "$RUNS/legacy_trace_latency.txt" ;;
  image)
    # 候选镜像离线自检：单独 build 一个 tag，绝不 compose build agent（那会替换运行中的服务）。
    # 只 import、只问开关语义；DSN/URL 全指向不存在的地址，本脚本不会发起任何连接或写入。
    # 构建上下文由 Windows 侧的 docker 客户端读取，必须给 Windows 路径（同 ENV_WIN 的处理）。
    ROOT_WIN="$(cd "$ROOT" && pwd -W)"
    MSYS_NO_PATHCONV=1 docker build -q -t cs-agent-candidate "$ROOT_WIN/python_agent" >/dev/null
    MSYS_NO_PATHCONV=1 docker run --rm --entrypoint python -w /app \
      -v "$ROOT:/work:ro" -e PYTHONPATH=/app -e PYTHONPYCACHEPREFIX=/tmp/pyc \
      -e POSTGRES_DSN='postgresql://nobody:none@127.0.0.1:1/nonexistent' \
      -e QDRANT_URL=http://127.0.0.1:1 -e BUSINESS_API=http://127.0.0.1:1 \
      -e ORDER_BUSINESS_API=http://127.0.0.1:1 -e INTERNAL_API_TOKEN=probe-token \
      -e REDIS_URL=redis://127.0.0.1:1/0 \
      -e MODEL_KEY_ENCRYPT_KEY=0000000000000000000000000000000000000000000000000000000000000000 \
      cs-agent-candidate /work/agent/evaluation/verify_mounted_image.py ;;
  mounted-memory)
    # 挂载图的记忆逐题实测：同一批转写、同一个"会改写没锚点"的最差摘要器，
    # 把 mounted_v2 臂并进已有的四臂文件，再用 diff_arms 出五臂逐题表。
    MSYS_NO_PATHCONV=1 docker run --rm -v "$ROOT:/work:ro" -v "$RUNS:/out" \
      -e PYTHONPATH=/work/python_agent:/work -e PYTHONPYCACHEPREFIX=/tmp/pyc \
      -w /work/python_agent cs-test:agent sh -c '
        python /work/agent/evaluation/memory_context_mounted.py --out /out/mounted_v2.jsonl
        python /work/agent/evaluation/memory_context_mounted.py --geometry --out /out/mounted_v2_geometry.jsonl
        cp /out/memory_context_ab.jsonl /out/_f.jsonl && cat /out/mounted_v2.jsonl >> /out/_f.jsonl
        cp /out/memory_context_geometry.jsonl /out/_g.jsonl && cat /out/mounted_v2_geometry.jsonl >> /out/_g.jsonl
        python /work/agent/evaluation/diff_arms.py /out/_f.jsonl -o /out/memory_five_arm_frozen.md
        python /work/agent/evaluation/diff_arms.py /out/_g.jsonl -o /out/memory_five_arm_geometry.md
        rm /out/_f.jsonl /out/_g.jsonl'
    wc -l "$RUNS/mounted_v2.jsonl" "$RUNS/mounted_v2_geometry.jsonl" ;;
  compare)
    # 逐题差异闸门：直接吃 answer jsonl（不必先转 checklist），判定复用官方 score_heldout.norm。
    #   不带参数 = 自检：基线自比应逐题零翻转，再与另一条已采臂比，证明闸门看得见翻正/保持失败。
    #   带一个参数 = 模型恢复后新臂的对照：bash agent/evaluation/run_all.sh compare agent/evaluation/runs/heldout_v2_r1.jsonl
    GOLD="$ROOT/python_agent/evaluation/gold_heldout_v1.jsonl"
    BASE="${3:-$ROOT/python_agent/evaluation/runs/ho_off_r4.jsonl}"
    if [ -n "${2:-}" ]; then
      .venv/Scripts/python.exe agent/evaluation/heldout_compare.py "$GOLD" --baseline "$BASE" \
        --candidate "$2" -o "$ROOT/agent/evaluation/runs/heldout_diff_new_vs_off.md"
    else
      .venv/Scripts/python.exe agent/evaluation/heldout_compare.py "$GOLD" --baseline "$BASE" \
        --candidate "$BASE" --candidate "$ROOT/python_agent/evaluation/runs/ho_mem_r2_answers.jsonl" \
        -o "$ROOT/agent/evaluation/runs/heldout_diff_sample.md"
    fi ;;
  mem-diff)
    # §6f 那两张真实模型长对话臂的逐题对照表：判据在这里**重算**（官方 norm），不采信行内
    # containment（陷阱 #2/#21），并把 watermark=0 标成"并发污染"而不是"记忆失败"（陷阱 #23）。
    # 回退>0 或未采集>0 ⇒ 退出码 1。两个 jsonl 都是已采到的原始数据，跑它不需要模型、不需要授权。
    # 第 4 个参数=输出 md（逐轮转写附录自动跟着叫 *_transcripts.md）；第 5 个参数写 strict 时，
    # 数据自证性也进门禁（缺语料指纹/缺逐轮转写/两臂指纹冲突 ⇒ 退 1）。
    # 默认臂换成 r4 旧臂 vs r5 新臂：这是唯一"两臂都可自证 + 摘要输出预算已修"的一对（§6g 发现三），
    # 读数 旧 0/10 vs 新 9/10、回退 0。r1/r2/r3 那几批仍在 runs/ 下，按参数指名即可复算。
    OFF="${2:-$ROOT/agent/evaluation/runs/legacy_mem_dsv41_r4.jsonl}"
    ON="${3:-$ROOT/agent/evaluation/runs/live_mem_dsv41_r5.jsonl}"
    OUT_MD="${4:-$ROOT/agent/evaluation/runs/mem_diff_dsv41_r4_off_vs_r5_on.md}"
    EXTRA=""
    [ "${5:-}" = "strict" ] && EXTRA="--require-selfproof"
    .venv/Scripts/python.exe agent/evaluation/mem_arms_diff.py --off "$OFF" --on "$ON" \
      -o "$OUT_MD" --transcripts "${OUT_MD%.md}_transcripts.md" $EXTRA ;;
  demo-mounted)
    # 开关那条路（app 侧图）的现场演示记录：复合咨询 + 长对话续聊 + 重启续聊，逐轮留原始行。
    MSYS_NO_PATHCONV=1 docker run --rm -v "$ROOT:/work:ro" -v "$RUNS:/out" \
      -e PYTHONPATH=/work/python_agent:/work -e PYTHONPYCACHEPREFIX=/tmp/pyc \
      -w /work/python_agent cs-test:agent \
      python /work/agent/evaluation/demo_mounted.py --out /out/demo_mounted.jsonl ;;
  live)
    # 新路径真实模型采集：run_all.sh live [题数] [tenant|platform|custom] [模型名] [输出文件名]
    # 第四个参数只在 model=platform 时有效——GET /models 证实这把 key 活着且能看见 182 个模型，
    # 而配置的那个是 403 免费额度耗尽，所以按模型名换是"有没有活臂"的差别，不是措辞偏好。
    # 换过名字的行一律 comparable=false：同一模型两个臂之间可比，与冻结基线不可比。
    LIM="${2:-36}"; SRC="${3:-tenant}"; NAME="${4:-}"
    OUT="${5:-heldout_v2_r1.jsonl}"
    MODEL_ARG="--model $SRC"
    [ -n "$NAME" ] && MODEL_ARG="$MODEL_ARG --model-name $NAME"
    [ -n "${6:-}" ] && MODEL_ARG="$MODEL_ARG --only $6"
    MSYS_NO_PATHCONV=1 docker run --rm --env-file "$ENV_WIN" --network "$NET" \
      -v "$ROOT:/work:ro" -v "$RUNS:/out" -e PYTHONPATH=/work:/work/python_agent \
      -e PYTHONPYCACHEPREFIX=/tmp/pyc -e QDRANT_URL=http://qdrant:6333 \
      -e BUSINESS_API=http://business:8001 -e ORDER_BUSINESS_API=http://business:8001 \
      -w /work/python_agent cs-test:agent \
      sh -c "${PG_ENV}python /work/agent/evaluation/run_ab_live.py heldout --limit $LIM --out /out/$OUT $MODEL_ARG" ;;
  legacy)
    # 旧路径（开关 off 那条）的对照臂：同一个尺子、同一个模型名，进程内直接调
    # app.api.chat.agent_chat——这是"新旧对照"里一直缺的那一侧（冻结基线是走网关采的，
    # 而租户自己的模型端点今天拒连，所以那一侧从来没有同模型可比的数）。
    # 模型按 --model-name 换平台 key 下的另一个 id（配置的那个 403 免费额度耗尽），
    # 所以每行 comparable=false：只与 live 同模型那臂互比，绝不与 qwen3.8-flash-next 基线互比。
    # 用完自清理自建线程（checkpoints / session_meta / kb_hits / token_usage / agent_trace_runs），
    # 并打印残留行数——不为 0 就是没擦干净，会被看见。
    # 第 5 个参数=臂：heldout（默认，36 题留出集）或 mem（10 条长对话 × 11 轮 + 探针，
    # 这时第 2 个参数是"条数"不是"题数"）。§6f 那一版旧路径长对话臂就是 mem。
    ARM="${5:-heldout}"
    .venv/Scripts/python.exe -c "import ast; ast.parse(open('agent/evaluation/run_legacy_heldout.py',encoding='utf-8').read())" || exit 1
    MSYS_NO_PATHCONV=1 docker run --rm --env-file "$ENV_WIN" --network "$NET" \
      -v "$ROOT:/work:ro" -v "$RUNS:/out" -e PYTHONPATH=/work:/work/python_agent \
      -e PYTHONPYCACHEPREFIX=/tmp/pyc -e QDRANT_URL=http://qdrant:6333 \
      -e BUSINESS_API=http://business:8001 -e ORDER_BUSINESS_API=http://business:8001 \
      -w /work/python_agent cs-test:agent \
      sh -c "${PG_ENV}python /work/agent/evaluation/run_legacy_heldout.py --arm $ARM --limit ${2:-36} --model-name ${3:-deepseek-v4.1-flash} --out /out/${4:-legacy_off.jsonl}" ;;
  live-mem)
    # 长对话真实模型臂（阶段4 的逐题对照在真模型上跑一遍）：10 条 × (11 轮 + 探针)。
    # 模型同样只能按 --model-name 换平台 key 下的可用 id，所以每行 comparable=false——
    # 这一臂只与同模型的另一臂（或脚本化模型臂的机制结论）并列读，不与四臂冻结表互比分数。
    N="${2:-10}"; NAME="${3:-deepseek-v4.1-flash}"; OUT="${4:-live_mem.jsonl}"
    MSYS_NO_PATHCONV=1 docker run --rm --env-file "$ENV_WIN" --network "$NET" \
      -v "$ROOT:/work:ro" -v "$RUNS:/out" -e PYTHONPATH=/work:/work/python_agent \
      -e PYTHONPYCACHEPREFIX=/tmp/pyc -e QDRANT_URL=http://qdrant:6333 \
      -e BUSINESS_API=http://business:8001 -e ORDER_BUSINESS_API=http://business:8001 \
      -w /work/python_agent cs-test:agent \
      sh -c "${PG_ENV}python /work/agent/evaluation/run_ab_live.py mem --limit $N --out /out/$OUT --model platform --model-name $NAME" ;;
  live-mock)
    # 同一套采集器，但 chat 模型换成容器内的确定性 OpenAI 兼容替身（真 HTTP 往返、真工具调用 JSON、
    # 真 usage 记账）。它测的是传输与记账这一段，不测措辞——每行都带 comparable=false，
    # 也绝不允许和 33/36 那条冻结基线互比。检索侧仍走真 Qdrant + 真 embedding（今天只有 chat 被堵）。
    ARM="${2:-heldout}"; N="${3:-4}"
    MSYS_NO_PATHCONV=1 docker run --rm --env-file "$ENV_WIN" --network "$NET" \
      -v "$ROOT:/work:ro" -v "$RUNS:/out" -e PYTHONPATH=/work:/work/python_agent \
      -e PYTHONPYCACHEPREFIX=/tmp/pyc -e QDRANT_URL=http://qdrant:6333 \
      -e BUSINESS_API=http://business:8001 -e ORDER_BUSINESS_API=http://business:8001 \
      -w /work/python_agent cs-test:agent \
      sh -c "${PG_ENV}python /work/agent/evaluation/run_ab_live.py $ARM --model custom --mock --limit $N --out /out/mock_${ARM}.jsonl" ;;
  summary-probe)
    # §6g 发现三：摘要那一次补全为什么会"200 成功、正文为空"。把 content / reasoning_content /
    # finish_reason / output_tokens 全打出来，并可用第 2 个参数覆盖输出预算（0=继承生产的 1024）。
    # 只做补全、不写业务数据；真实模型臂在跑时别插进来（同一把 key，辅助调用会先掉队）。
    N="${2:-4}"; MT="${3:-0}"; NAME="${4:-deepseek-v4.1-flash}"
    MSYS_NO_PATHCONV=1 docker run --rm --env-file "$ENV_WIN" --network "$NET" \
      -v "$ROOT:/work:ro" -v "$RUNS:/out" -e PYTHONPATH=/work:/work/python_agent \
      -e PYTHONPYCACHEPREFIX=/tmp/pyc -e QDRANT_URL=http://qdrant:6333 \
      -e BUSINESS_API=http://business:8001 -e ORDER_BUSINESS_API=http://business:8001 \
      -w /work/python_agent cs-test:agent \
      sh -c "${PG_ENV}python /work/agent/evaluation/probe_summary_empty.py --long --repeat $N \
             --max-tokens $MT --model-name $NAME --out /out/summary_empty_probe_mt$MT.jsonl" ;;
  all)
    # 一条命令跑完今天"不需真实模型、不需额外授权"的全部验收项，逐条记退出码与耗时。
    # 期望为负的那一项单列（§5f：挂载 Reviewer 今天必漏放），它进账本是为了别丢，不拖垮总退出码。
    # mounted-fixed 是硬项：它的退出码 0 是**有内容的**——补丁组两遍 0 漏放 0 误拦 + 两臂逐题零回退零缺题（§5h/§5i），
    # 反常地非 0 说明候选修法不再成立（app 侧代码变了或锚点被改动），必须让人看见。
    HARD="test demo demo-pg pg pg-restart xproc http guards memory mounted-memory demo-mounted mounted mounted-fixed frontend retrieval overhead overhead-pg trace-baseline image compare mem-diff"
    # 带参数 = 只跑指定子集（CI/现场只想复查两三项时用）：bash agent/evaluation/run_all.sh all demo compare
    SOFT=1
    if [ "$#" -gt 1 ]; then HARD="${@:2}"; SOFT=0; case " $HARD " in *" mounted-guards "*) SOFT=1 ;; esac; fi
    LOG="$(mktemp)"; fails=0; total=0; TABLE=""
    emit_line() {   # 同一份表既给终端看，也进账本——以前要人手 `| tee`，忘了就出现"文档说有账本、文件却是旧的"
      local line; line=$(printf '%-16s %5s %6ss  %s\n' "$1" "$2" "$3" "$4")
      echo "$line"; TABLE="$TABLE$line"$'\n'
    }
    printf '%-16s %5s %7s  %s\n' 项 退出 耗时 摘要
    TABLE=$(printf '%-16s %5s %7s  %s\n' 项 退出 耗时 摘要)$'\n'
    for c in $HARD; do
      start=$SECONDS
      if bash "$0" "$c" >"$LOG" 2>&1; then rc=0; else rc=$?; fi
      el=$((SECONDS - start)); total=$((total + 1))
      last=$(grep -av '^[[:space:]]*$' "$LOG" | tail -1 | cut -c1-84)
      if [ "$rc" != 0 ]; then fails=$((fails + 1)); printf '>> %s 退出 %s，末尾输出已截进摘要\n' "$c" "$rc"; fi
      emit_line "$c" "$rc" "$el" "$last"
    done
    # 软项：今天正确的结果就是"闸门拦住"（退出码 1）。反常地放行才是问题。
    if [ "$SOFT" = 1 ]; then
      start=$SECONDS
      if bash "$0" mounted-guards --gate >"$LOG" 2>&1; then src=0; else src=$?; fi
      total=$((total + 1))
      if [ "$src" = 1 ]; then note="已知为负（§5f 漏放），按预期被闸门拦住"
      else note="反常：闸门今天放行了，§5f 的结论要重看"; fails=$((fails + 1)); fi
      emit_line "mounted-guards" "$src" "$((SECONDS - start))" "$note"
    fi
    rm -f "$LOG"
    echo "--- $total 项：计入失败的硬项 $fails ---"
    [ "$fails" = 0 ] || echo "有项反常：见上面带 >> 的行，或单独重跑该项"
    { if [ "$SOFT" = 1 ]; then SCOPE="全量清单（软项 mounted-guards 期望为负，已跑）"; else SCOPE="子集（带参数跑的，软项未进；实际项见下表）"; fi
      printf '=== %s  run_all.sh all：%s 项，失败硬项 %s ｜ %s\n' \
              "$(date -Iseconds)" "$total" "$fails" "$SCOPE"
      printf '%s' "$TABLE"
      printf -- '--- %s 项：计入失败的硬项 %s ---\n' "$total" "$fails"
    } >> "$RUNS/acceptance_ledger.txt"
    exit "$fails" ;;
  *) grep '^#   bash' "$0" | sed 's/^#  //' ;;
esac
