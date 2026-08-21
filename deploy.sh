#!/bin/bash
# 一线牵 monorepo 部署（GitHub 为中心：本地 push → 服务器 pull → 重启）
# 用法: bash deploy.sh [bot|h5|all]  (默认 all)
SERVER="root@172.245.223.118"
TARGET="${1:-all}"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "🚀 部署到 $SERVER (monorepo: push → pull --ff-only → restart)"
if [ -n "$(git -C "$REPO_DIR" status --porcelain)" ]; then
  echo "❌ 有未提交改动，请先 commit"; exit 1
fi
git -C "$REPO_DIR" push origin main || { echo "❌ push 失败"; exit 1; }

deploy_bot() {
  echo "🤖 部署 bot..."
  ssh $SERVER "cd /opt/yixianqian && git pull --ff-only origin main" || { echo "❌ pull 失败"; exit 1; }
  # 兼容期：根目录保留软链/或直接用新路径
  ssh $SERVER "if [ -f /opt/yixianqian/bot/yixianqian_bot_ws.py ]; then /opt/yixianqian/bot/venv/bin/python -m py_compile /opt/yixianqian/bot/yixianqian_bot_ws.py 2>/dev/null || /opt/yixianqian/venv/bin/python -m py_compile /opt/yixianqian/bot/yixianqian_bot_ws.py; else /opt/yixianqian/venv/bin/python -m py_compile /opt/yixianqian/yixianqian_bot_ws.py; fi" || { echo "❌ 语法错误"; exit 1; }
  ssh $SERVER "systemctl restart yixianqian" && echo "✅ bot 已重启"
}
deploy_h5() {
  echo "🌐 部署 H5..."
  ssh $SERVER "cd /opt/yixianqian && git pull --ff-only origin main" 2>/dev/null || ssh $SERVER "cd /opt/yixianqian-h5 && git pull --ff-only origin main" || { echo "❌ pull 失败"; exit 1; }
  ssh $SERVER "if [ -f /opt/yixianqian/web/backend/app.py ]; then /opt/yixianqian/web/backend/venv/bin/python -m py_compile /opt/yixianqian/web/backend/app.py /opt/yixianqian/web/backend/config.py /opt/yixianqian/web/backend/bitable.py 2>/dev/null || /opt/yixianqian-h5/backend/venv/bin/python -m py_compile /opt/yixianqian/web/backend/app.py; else /opt/yixianqian-h5/backend/venv/bin/python -m py_compile /opt/yixianqian-h5/backend/app.py; fi" || { echo "❌ 语法错误"; exit 1; }
  ssh $SERVER "systemctl restart yixianqian-h5" && echo "✅ H5 已重启"
}
case "$TARGET" in bot) deploy_bot;; h5) deploy_h5;; all) deploy_bot; deploy_h5;; *) echo "用法: bash deploy.sh [bot|h5|all]"; exit 1;; esac
echo "🎉 完成  日志: ssh $SERVER 'journalctl -u yixianqian -n 50; tail -f /tmp/yixianqian_bot.log'"
