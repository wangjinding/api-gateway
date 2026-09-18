#!/usr/bin/env bash
# 一条命令跑完所有门禁（第三十轮新增）。
#
# 为什么要有它：这套检查原先散在几个地方（Makefile、frontend/package.json、README 的说明），
# 提交前"该跑哪些"靠脑子记 —— 结果就是漏（本轮就补上了 make lint 少覆盖 6 个文件、
# 测试污染真实 data/.env 这类问题）。这里把它们串成一条，本地和 CI 都能直接用。
#
# 用法：bash scripts/ci_check.sh          # 全部跑
#       bash scripts/ci_check.sh --fast   # 跳过前端 build（只做体检+类型检查）
#
# 退出码非 0 = 有门禁没过（CI 直接照这个判）。任何一步失败都继续跑完，最后汇总 ——
# 一次把问题都暴露出来，而不是修一个跑一次。
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

FAST=0
[ "${1:-}" = "--fast" ] && FAST=1

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

failed=0
run() {                    # run <说明> <命令...>
  local name="$1"; shift
  echo ""
  echo "──────── $name ────────"
  if "$@"; then
    echo "✓ $name"
  else
    echo "✗ $name（退出码 $?）"
    failed=$((failed + 1))
  fi
}

echo "api-gateway CI 门禁（$(date '+%F %T')）"

# 1) 后端语法编译（覆盖整个 app 包 + run.py；不再维护手写文件清单）
run "后端语法编译" "$PY" -m compileall -q app run.py

# 2) 单元测试（mock 上游，不依赖真实 key/网络）
run "单元测试" "$PY" tests/test_gateway.py

# 3) 前端：SVG 注释 + i18n 文案体检（中英对称 / 无硬编码中文 / 占位符语法）
if [ -d frontend/node_modules ]; then
  run "前端文案体检" npm --prefix frontend run check:i18n
  run "SVG 体检"     npm --prefix frontend run check:svg
  # 4) 前端类型检查
  run "前端类型检查" npm --prefix frontend run typecheck
  # 5) 前端构建（已含上面两项体检作为前置；--fast 时跳过，省 ~20s）
  if [ "$FAST" = "0" ]; then
    run "前端构建" npm --prefix frontend run build
  fi
else
  echo ""
  echo "！ frontend/node_modules 不存在 —— 跳过前端三项（先 cd frontend && npm ci）"
fi

echo ""
if [ "$failed" = "0" ]; then
  echo "全部通过 ✓"
  exit 0
fi
echo "$failed 项未通过 ✗"
exit 1
