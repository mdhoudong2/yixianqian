#!/bin/bash
# 一线牵 monorepo 部署（GitHub 为中心：本地 push → 服务器 pull → 语法检查 → 重启）
# 用法:
#   bash deploy.sh [bot|h5|all] [prod|test] [--pull-only]
#   --pull-only: 代码已有人 push，跳过本地 push，只让服务器 pull + 重启
# 环境:
#   prod = 生产  (main    分支, /opt/yixianqian,      systemd: yixianqian / yixianqian-h5,      端口 8091)
#   test = 测试服(develop 分支, /opt/yixianqian-test, systemd: yixianqian-test / yixianqian-h5-test, 端口 8092)
SERVER="root@172.245.223.118"
TARGET="all"
ENV="prod"
PULL_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --pull-only|-p) PULL_ONLY=true ;;
    bot|h5|all) TARGET="$arg" ;;
    prod|test) ENV="$arg" ;;
    *) echo "用法: bash deploy.sh [bot|h5|all] [prod|test] [--pull-only]"; exit 1 ;;
  esac
done
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

case "$ENV" in
  prod) BRANCH="main";      REMOTE_DIR="/opt/yixianqian";      BOT_UNIT="yixianqian";      H5_UNIT="yixianqian-h5" ;;
  test) BRANCH="develop";   REMOTE_DIR="/opt/yixianqian-test"; BOT_UNIT="yixianqian-test"; H5_UNIT="yixianqian-h5-test" ;;
esac

if [ "$PULL_ONLY" = true ]; then
  echo "🔄 服务器拉取模式 (pull-only, env=$ENV)"
else
  echo "🚀 部署到 $SERVER (env=$ENV, branch=$BRANCH → pull --ff-only → 语法检查 → restart)"
  if [ -n "$(git -C "$REPO_DIR" status --porcelain)" ]; then
    echo "❌ 有未提交改动，请先 commit"; exit 1
  fi
  git -C "$REPO_DIR" push origin "$BRANCH" || { echo "❌ push 失败"; exit 1; }
fi

deploy_bot() {
  echo "🤖 部署 bot ($ENV)..."
  ssh $SERVER "cd $REMOTE_DIR && git pull --ff-only origin $BRANCH" || { echo "❌ pull 失败"; exit 1; }
  ssh $SERVER "$REMOTE_DIR/bot/venv/bin/python -m py_compile $REMOTE_DIR/bot/yixianqian_bot_ws.py $REMOTE_DIR/bot/constants.py $REMOTE_DIR/bot/cards.py" || { echo "❌ bot 语法错误"; exit 1; }
  ssh $SERVER "systemctl restart $BOT_UNIT" && echo "✅ $BOT_UNIT 已重启"
}
deploy_h5() {
  echo "🌐 部署 H5 ($ENV)..."
  ssh $SERVER "cd $REMOTE_DIR && git pull --ff-only origin $BRANCH" || { echo "❌ pull 失败"; exit 1; }
  ssh $SERVER "$REMOTE_DIR/web/backend/venv/bin/python -m py_compile $REMOTE_DIR/web/backend/app.py $REMOTE_DIR/web/backend/config.py $REMOTE_DIR/web/backend/bitable.py $REMOTE_DIR/web/backend/gunicorn.conf.py" || { echo "❌ H5 语法错误"; exit 1; }
  ssh $SERVER "systemctl restart $H5_UNIT" && echo "✅ $H5_UNIT 已重启"
}
case "$TARGET" in
  bot) deploy_bot ;;
  h5) deploy_h5 ;;
  all) deploy_bot; deploy_h5 ;;
esac
echo "🎉 完成  日志: ssh $SERVER 'journalctl -u $BOT_UNIT -n 50 --no-pager; journalctl -u $H5_UNIT -n 50 --no-pager'"
