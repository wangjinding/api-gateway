#!/usr/bin/env bash
# 推送到自建 Gitea（2026-10-12 老板改口径：以后改动只推 Gitea，不再推 CNB）
#
# 用法：
#   bash scripts/push_gitea.sh                 # 推当前分支到 Gitea 同名分支
#   GITEA_PASS=<密码> bash scripts/push_gitea.sh
#   bash scripts/push_gitea.sh <密码>           # 或作为第一个参数
#
# 凭据：按下面顺序找，找不到直接报错退出 ——
#   1) 环境变量 GITEA_PASS
#   2) 命令行第一个参数
# 约定（与已停用的 push_cnb.sh 同一套安全口径）：每次推送临时把密码内嵌进 URL，
#   pushed 完即走（本脚本**不写 .git/config**，也不把密码落盘到任何文件）。
# 为什么改推 Gitea：CNB 那把 token 2026-10-12 实测已失效（Repository Not Found /
#   not logged in），老板同期起了自建 Gitea（https://git.wangjinding.com），
#   于是口径从「上生产必推 CNB」改成「上生产必推 Gitea」。
set -euo pipefail

REPO_URL="https://git.wangjinding.com/Jidekin/api-gateway.git"
USER="Jidekin"

PASS="${GITEA_PASS:-${1:-}}"
if [ -z "$PASS" ]; then
  echo "缺 Gitea 密码：请用 GITEA_PASS=<密码> bash scripts/push_gitea.sh 或作为第一个参数传入。" >&2
  echo "（密码只走内存/命令行，脚本故意不内置，免得落盘。）" >&2
  exit 2
fi

cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain)" ]; then
  echo "工作区不干净，先提交（上线前也应先 commit）：" >&2
  git status --short >&2
  exit 3
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "== 推送 $BRANCH → Gitea（$REPO_URL）=="
echo "   本地 HEAD = $(git rev-parse --short HEAD)"

# 一次性内嵌密码；输出里把密码打码，避免它进日志/终端回滚缓冲
git push "https://${USER}:${PASS}@git.wangjinding.com/Jidekin/api-gateway.git" "${BRANCH}:${BRANCH}" 2>&1 \
  | sed "s/${PASS}/[PASS]/g"

# 推完核对：远端 HEAD 必须 = 本地 HEAD；并复查 .git/config 没被塞进明文密码
REMOTE_HEAD="$(git ls-remote "https://${USER}:${PASS}@git.wangjinding.com/Jidekin/api-gateway.git" "refs/heads/${BRANCH}" | awk '{print $1}')"
LOCAL_HEAD="$(git rev-parse HEAD)"
if [ "$REMOTE_HEAD" = "$LOCAL_HEAD" ]; then
  echo "✓ 远端 = 本地（${LOCAL_HEAD:0:7}）"
else
  echo "✗ 远端(${REMOTE_HEAD:0:7}) ≠ 本地(${LOCAL_HEAD:0:7}) —— 推送可能没成功" >&2
  exit 4
fi

if grep -q "$PASS" .git/config 2>/dev/null; then
  echo "✗ .git/config 里出现了明文密码，请立刻清理" >&2
  exit 5
fi
echo "✓ .git/config 无明文密码"
