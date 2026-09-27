#!/usr/bin/env bash
# 一键把 release/ 里的 zip 和说明发到 GitHub Release。
# 用法：   ./release/publish.sh          ← 发布 / 更新
#         ./release/publish.sh --check  ← 只看当前有没有这个 release、挂了哪些文件
set -euo pipefail

REPO="cpufreestyle/live-autoclicker"
TAG="v1.0.0"
TITLE="v1.0.0 · 直播按钮自动点击器（Windows x64）"
DIR="/d/aishare/repo/live-autoclicker/release"
ZIP="$DIR/LiveAutoClicker-v1.0.0-win64.zip"
NOTES="$DIR/RELEASE_NOTES-v1.0.0.md"

if ! command -v gh >/dev/null 2>&1; then
  echo "错误：没找到 gh（GitHub CLI）。" >&2
  exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  cat >&2 <<'MSG'
错误：gh 还没登录。先跑一次（会打印一个 8 位验证码，浏览器打开
https://github.com/login/device 输进去即可，scope 选 repo）：

    gh auth login --hostname github.com --git-protocol ssh --web

然后重跑本脚本。
MSG
  exit 1
fi
[ -f "$ZIP" ]   || { echo "错误：缺 $ZIP" >&2;   exit 1; }
[ -f "$NOTES" ] || { echo "错误：缺 $NOTES" >&2; exit 1; }

if [ "${1:-}" = "--check" ]; then
  echo "== $REPO 现有 release =="
  gh release list -R "$REPO" || true
  if gh release view "$TAG" -R "$REPO" >/dev/null 2>&1; then
    echo "== $TAG 已存在，当前挂载 =="
    gh release view "$TAG" -R "$REPO" --json assets \
      --template '{{range .assets}}{{.name}}  {{.size}} bytes{{"\n"}}{{end}}'
  else
    echo "$TAG 还不存在。"
  fi
  exit 0
fi

if gh release view "$TAG" -R "$REPO" >/dev/null 2>&1; then
  echo "== $TAG 已存在：更新标题与说明 =="
  gh release edit "$TAG" -R "$REPO" --title "$TITLE" --notes-file "$NOTES" --latest
else
  echo "== $TAG 不存在：新建 =="
  gh release create "$TAG" -R "$REPO" --title "$TITLE" --notes-file "$NOTES" --latest
fi

echo "== 上传资源（重名会覆盖）=="
gh release upload "$TAG" -R "$REPO" --clobber "$ZIP"

echo "== 完成 =="
gh release view "$TAG" -R "$REPO" --json tagName,name,isDraft,isPrerelease,url \
  --template '{{.tagName}} | {{.name}} | draft={{.isDraft}} | {{.url}}{{"\n"}}'
gh release view "$TAG" -R "$REPO" --json assets \
  --template '{{range .assets}}  - {{.name}}  {{.size}} bytes{{"\n"}}{{end}}'