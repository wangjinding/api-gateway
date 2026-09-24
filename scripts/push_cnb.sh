#!/usr/bin/env bash
# 推送到 CNB（2026-09-24 老板规则：凡重启上线生产的改动都要推）
#
# 用法：
#   bash scripts/push_cnb.sh                 # 推当前分支到 CNB 同名分支
#   CNB_TOKEN=<token> bash scripts/push_cnb.sh
#
# 凭据：默认从 MEMORY.md 取不到（那是小银的记忆文件），所以按下面顺序找：
#   1) 环境变量 CNB_TOKEN
#   2) 命令行第一个参数
# 找不到就直接报错退出 —— **绝不把 token 写进 .git/config、也绝不落盘到任何文件**。
# 约定：每次推送临时把 token 内嵌进 URL，pushed 完即走（本脚本不写 .git/config）。
set -euo pipefail

REPO_URL="https://cnb.cool/Jidekin/api-gateway.git"
USER="cnb"

TOKEN="${CNB_TOKEN:-${1:-}}"
if [ -z "$TOKEN" ]; then
  echo "缺 CNB token：请用 CNB_TOKEN=<token> bash scripts/push_cnb.sh 或作为第一个参数传入。" >&2
  echo "（token 从 MEMORY.md 的 CNB 推送规则取；脚本故意不内置 token，免得落盘。）" >&2
  exit 2
fi

cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain)" ]; then
  echo "工作区不干净，先提交（上线前也应先 commit）：" >&2
  git status --short >&2
  exit 3
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "== 推送 $BRANCH → CNB（$REPO_URL）=="
echo "   本地 HEAD = $(git rev-parse --short HEAD)"

# 一次性内嵌 token；输出里把 token 打码，避免它进日志/终端回滚缓冲
git push "https://${USER}:${TOKEN}@cnb.cool/Jidekin/api-gateway.git" "${BRANCH}:${BRANCH}" 2>&1 \
  | sed "s/${TOKEN}/[TOKEN]/g"

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git ls-remote "https://${USER}:${TOKEN}@cnb.cool/Jidekin/api-gateway.git" "refs/heads/${BRANCH}" 2>/dev/null \
  | awk '{print $1}')"
echo "   远端 HEAD = ${REMOTE:-（取不到）}"
[ "$LOCAL" = "$REMOTE" ] && echo "== ✓ 远端 = 本地，推送完成 ==" || { echo "== ✗ 远端与本地不一致 ==" >&2; exit 1; }

# 保险：确证 token 没被 git 记进配置
if grep -q "$TOKEN" .git/config 2>/dev/null; then
  echo "== ✗ 警告：token 出现在 .git/config 里，请清理 ==" >&2
  exit 1
fi
echo "   .git/config 无 token ✓"
