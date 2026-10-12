#!/usr/bin/env bash
# 推送到自建 Gitea（2026-10-12 老板改口径：以后改动只推 Gitea，不再推 CNB）
#
# 用法（**默认免密**，什么都不用带）：
#   bash scripts/push_gitea.sh
# 需要临时换凭据时才显式给：
#   GITEA_TOKEN=<token> bash scripts/push_gitea.sh
#   bash scripts/push_gitea.sh <token或密码>
#
# 凭据查找顺序（找到即用，找不到就报错退出）：
#   1) 环境变量 GITEA_TOKEN（API token）
#   2) 命令行第一个参数
#   3) 环境变量 GITEA_PASS（账号密码）
#   4) 本机文件 ~/.gitea_token（600，只有本机、不在任何仓库里）← **日常走这条，即免密**
#
# 安全口径（与已停用的 push_cnb.sh 同一套）：
#   · 凭据只**一次性内嵌进本次推送 URL**，用完即走 —— 本脚本**不写 `.git/config`**，
#     也不把凭据打进任何输出（输出里自动打码）。
#   · 推完自动核对「远端 HEAD = 本地 HEAD」，并复查 `.git/config` 没有明文凭据。
#   · 退出码：2=没凭据 / 3=工作区不干净 / 4=推送后远端≠本地 / 5=.git/config 出现明文。
#
# 为什么改推 Gitea：CNB 那把 token 2026-10-12 实测已失效（Repository Not Found /
#   not logged in），老板同期起了自建 Gitea（https://git.wangjinding.com）。
# 凭据存放（**别丢**）：token 记在 `MEMORY.md`「自建 Gitea」段 + 本机 `~/.gitea_token`；
#   **绝不写进本仓库任何文件**（仓库是要推到远端的）。
set -euo pipefail

REPO_URL="https://git.wangjinding.com/Jidekin/api-gateway.git"
USER="Jidekin"
TOKEN_FILE="${HOME}/.gitea_token"

CRED="${GITEA_TOKEN:-${1:-${GITEA_PASS:-}}}"
if [ -z "$CRED" ] && [ -r "$TOKEN_FILE" ]; then
  CRED="$(cat "$TOKEN_FILE")"
fi
if [ -z "$CRED" ]; then
  echo "缺 Gitea 凭据：本机 ~/.gitea_token 读不到，也没给 GITEA_TOKEN / GITEA_PASS / 参数。" >&2
  echo "（凭据记在 MEMORY.md「自建 Gitea」段；脚本故意不内置，免得落盘进仓库。）" >&2
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

# 一次性内嵌凭据；输出里把凭据打码，避免它进日志/终端回滚缓冲
git push "https://${USER}:${CRED}@git.wangjinding.com/Jidekin/api-gateway.git" "${BRANCH}:${BRANCH}" 2>&1 \
  | sed "s/${CRED}/[CRED]/g"

# 推完核对：远端 HEAD 必须 = 本地 HEAD；并复查 .git/config 没被塞进明文凭据
REMOTE_HEAD="$(git ls-remote "https://${USER}:${CRED}@git.wangjinding.com/Jidekin/api-gateway.git" "refs/heads/${BRANCH}" | awk '{print $1}')"
LOCAL_HEAD="$(git rev-parse HEAD)"
if [ "$REMOTE_HEAD" = "$LOCAL_HEAD" ]; then
  echo "✓ 远端 = 本地（${LOCAL_HEAD:0:7}）"
else
  echo "✗ 远端(${REMOTE_HEAD:0:7}) ≠ 本地(${LOCAL_HEAD:0:7}) —— 推送可能没成功" >&2
  exit 4
fi

if grep -q "$CRED" .git/config 2>/dev/null; then
  echo "✗ .git/config 里出现了明文凭据，请立刻清理" >&2
  exit 5
fi
echo "✓ .git/config 无明文凭据"
