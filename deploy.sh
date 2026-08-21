#!/bin/bash
# 一线牵 monorepo 部署（GitHub 为中心：本地 push → 服务器 pull → 语法检查 → 重启）
# 用法:
#   bash deploy.sh [bot|h5|all] [--pull-only]
#   --pull-only: 代码已有人 push，跳过本地 push，只让服务器 pull + 重启
SERVER="root@172.245.223.118"
TARGET="all"
PULL_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --pull-only|-p) PULL_ONLY=true ;;
    bot|h5|all) TARGET="$arg" ;;
    *) echo "用法: bash deploy.sh [bot|h5|all] [--pull-only]"; exit 1 ;;
  esac
done
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$PULL_ONLY" = true ]; then
  echo "🔄 服务器拉取模式 (pull-only)"
else
  echo "🚀 部署到 $SERVER (monorepo: push → pull --ff-only → 语法检查 → restart)"
  if [ -n "$(git -C "$REPO_DIR" status --porcelain)" ]; then
    echo "❌ 有未提交改动，请先 commit"; exit 1
  fi
  git -C "$REPO_DIR" push origin main || { echo "❌ push 失败"; exit 1; }
fi

deploy_bot() {
  echo "🤖 部署 bot..."
  ssh $SERVER "cd /opt/yixianqian && git pull --ff-only origin main" || { echo "❌ pull 失败"; exit 1; }
  ssh $SERVER "/opt/yixianqian/bot/venv/bin/python -m py_compile /opt/yixianqian/bot/yixianqian_bot_ws.py" || { echo "❌ bot 语法错误"; exit 1; }
  ssh $SERVER "systemctl restart yixianqian" && echo "✅ bot 已重启"
}
deploy_h5() {
  echo "🌐 部署 H5..."
  ssh $SERVER "cd /opt/yixianqian && git pull --ff-only origin main" || { echo "❌ pull 失败"; exit 1; }
  ssh $SERVER "/opt/yixianqian/web/backend/venv/bin/python -m py_compile /opt/yixianqian/web/backend/app.py /opt/yixianqian/web/backend/config.py /opt/yixianqian/web/backend/bitable.py" || { echo "❌ H5 语法错误"; exit 1; }
  ssh $SERVER "systemctl restart yixianqian-h5" && echo "✅ H5 已重启"
}
case "$TARGET" in
  bot) deploy_bot ;;
  h5) deploy_h5 ;;
  all) deploy_bot; deploy_h5 ;;
esac
echo "🎉 完成  日志: ssh $SERVER 'journalctl -u yixianqian -n 50; journalctl -u yixianqian-h5 -n 50'"
