#!/bin/bash
# 统一测试入口：Python 业务层 + Agent + 系统健康冒烟
# 用法: bash scripts/run_p0_tests.sh [--skip-smoke]
set -e
# 支持 CS_ROOT 环境变量覆盖（开发环境 CRLF 拷贝到 /tmp 执行时使用）
if [ -n "$CS_ROOT" ]; then
  ROOT="$CS_ROOT"
else
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
FAIL=0

echo "==================== 1/3 Python business 单测 ===================="
if docker compose -f "$ROOT/docker-compose.yaml" run --rm --no-deps business \
    python -m unittest discover -s tests -q; then
  echo "[PASS] Python business 单测"
else
  echo "[FAIL] Python business 单测"; FAIL=1
fi

echo "==================== 2/3 Agent 单测 ===================="
if docker compose -f "$ROOT/docker-compose.yaml" run --rm --no-deps \
    -v "$ROOT/python_agent/tests:/app/tests:ro" agent \
    python -m unittest discover -s tests -q; then
  echo "[PASS] Agent 单测"
else
  echo "[FAIL] Agent 单测"; FAIL=1
fi

echo "==================== 3/3 系统健康冒烟 ===================="
if [ "$1" = "--skip-smoke" ]; then
  echo "[SKIP] 冒烟跳过"
else
  if bash "$ROOT/scripts/run_healthcheck.sh" >/dev/null 2>&1; then
    echo "[PASS] 健康冒烟 14 项断言"
  else
    echo "[FAIL] 健康冒烟"; FAIL=1
  fi
fi

echo "==================== 结果: $([ $FAIL -eq 0 ] && echo ALL_PASS || echo HAS_FAIL) ===================="
exit $FAIL
