#!/usr/bin/env bash
# 一条命令建仓 + 推送 + 触发云端构建。
#
# 前提：本机装了 GitHub CLI 并已登录（gh auth login）。
# 用法：
#   bash scripts/push-local.sh                 # 仓库名默认 bachata-s4-savefix
#   bash scripts/push-local.sh my-repo-name
set -euo pipefail

repo_name=${1:-bachata-s4-savefix}
repo_root=$(builtin cd -- "${BASH_SOURCE[0]%/*}/.." && builtin pwd -P)

cd "$repo_root"

command -v gh >/dev/null || {
  echo "需要 GitHub CLI：https://cli.github.com/  （装完执行 gh auth login）" >&2
  exit 1
}
gh auth status >/dev/null 2>&1 || {
  echo "还没登录，先执行：gh auth login" >&2
  exit 1
}

if [ ! -d .git ]; then
  git init -q -b main
  git config user.email "port@local"
  git config user.name "port"
  git add -A
  git commit -q -m "save_data backup completion callbacks port + cloud build"
fi

if git remote get-url origin >/dev/null 2>&1; then
  echo "== 已存在 origin，直接推送"
  git push -u origin main
else
  echo "== 创建仓库 ${repo_name} 并推送"
  gh repo create "$repo_name" --public --source=. --remote=origin --push
fi

echo
echo "== 已推送。构建工作流：build-savefix-core"
gh run list --limit 3 2>/dev/null || true
echo
echo "查看进度：gh run watch"
