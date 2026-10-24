#!/usr/bin/env bash
# 在容器内跑回归测试（与 .gitlab-ci.yml 的 business_tests/agent_tests 等价）。
# 整个仓库根按原层级只读挂载到 /work，让 app/infra/config.py 能按既有机制
# 读取仓库根 .env（只读，不写回宿主）。
# 首次使用先构建镜像：
#   docker build -f scripts/docker/Dockerfile.test --target agent    -t cs-test:agent    .
#   docker build -f scripts/docker/Dockerfile.test --target business -t cs-test:business .
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MOUNT="$ROOT:/work:ro"

run_side() {
  local side="$1" image="$2"
  echo "===== $side tests ====="
  MSYS_NO_PATHCONV=1 docker run --rm \
    -e PYTHONPYCACHEPREFIX=/tmp/pyc \
    -v "$MOUNT" -w "/work/$side" "$image" \
    sh -c "python -m compileall -q . && python -m unittest discover -s tests -q"
}

case "${1:-all}" in
  agent)    run_side python_agent    cs-test:agent ;;
  business) run_side python_business cs-test:business ;;
  all)      run_side python_agent    cs-test:agent && run_side python_business cs-test:business ;;
  *) echo "usage: $0 [agent|business|all]"; exit 2 ;;
esac
