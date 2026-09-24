#!/bin/bash
# 一线牵 monorepo 部署（GitHub 为中心：本地 push → 服务器 pull → 语法检查 → 重启）
# 用法:
#   bash deploy.sh [bot|h5|all] [prod|test] [--pull-only] [--yes-prod]
#   --pull-only: 代码已有人 push，跳过本地 push，只让服务器 pull + 重启
#   --yes-prod : 生产部署必须显式带此确认（不带则拒绝，防止手误默认打生产）
# 环境:
#   prod = 生产  (main    分支, /opt/yixianqian,      systemd: yixianqian / yixianqian-h5,      端口 8091)
#   test = 测试服(develop 分支, /opt/yixianqian-test, systemd: yixianqian-test / yixianqian-h5-test, 端口 8092)
set -euo pipefail
SERVER="root@172.245.223.118"
TARGET="all"
ENV="prod"
PULL_ONLY=false
YES_PROD=false
for arg in "$@"; do
  case "$arg" in
    --pull-only|-p) PULL_ONLY=true ;;
    --yes-prod) YES_PROD=true ;;
    bot|h5|all) TARGET="$arg" ;;
    prod|test) ENV="$arg" ;;
    *) echo "用法: bash deploy.sh [bot|h5|all] [prod|test] [--pull-only] [--yes-prod]"; exit 1 ;;
  esac
done
if [ "$ENV" = "prod" ] && [ "$YES_PROD" != true ]; then
  echo "⛔ 生产部署需要显式确认：请加 --yes-prod；只想发测试服请传 test"
  exit 1
fi
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

case "$ENV" in
  prod) BRANCH="main";      REMOTE_DIR="/opt/yixianqian";      BOT_UNIT="yixianqian";      H5_UNIT="yixianqian-h5";      PORT=8091 ;;
  test) BRANCH="develop";   REMOTE_DIR="/opt/yixianqian-test"; BOT_UNIT="yixianqian-test"; H5_UNIT="yixianqian-h5-test"; PORT=8092 ;;
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

wait_active_unit() {
  local unit="$1" i
  for i in $(seq 1 15); do
    if ssh $SERVER "systemctl is-active --quiet $unit"; then
      return 0
    fi
    sleep 1
  done
  echo "❌ $unit 重启后未进入 active"
  return 1
}

wait_http() {
  local unit="$1" port="$2" i
  for i in $(seq 1 20); do
    if ssh $SERVER "systemctl is-active --quiet $unit" && \
       ssh $SERVER "curl -sf -o /dev/null http://127.0.0.1:$port/api/version"; then
      return 0
    fi
    sleep 1
  done
  echo "❌ $unit 健康检查失败（127.0.0.1:$port/api/version 不通）"
  return 1
}

deploy_bot() {
  echo "🤖 部署 bot ($ENV)..."
  ssh $SERVER "cd $REMOTE_DIR && git pull --ff-only origin $BRANCH" || { echo "❌ pull 失败"; exit 1; }
  # 全量语法检查（不是只挑几个文件）：以前只 compile 三个文件，commands/auto_tasks 打错字照样上线
  ssh $SERVER "$REMOTE_DIR/bot/venv/bin/python -m compileall -q $REMOTE_DIR/bot $REMOTE_DIR/lib $REMOTE_DIR/web/backend" || { echo "❌ bot 语法错误"; exit 1; }
  ssh $SERVER "systemctl restart $BOT_UNIT" && wait_active_unit "$BOT_UNIT" && echo "✅ $BOT_UNIT 已重启且 active"
}
deploy_h5() {
  echo "🌐 部署 H5 ($ENV)..."
  ssh $SERVER "cd $REMOTE_DIR && git pull --ff-only origin $BRANCH" || { echo "❌ pull 失败"; exit 1; }
  ssh $SERVER "$REMOTE_DIR/web/backend/venv/bin/python -m compileall -q $REMOTE_DIR/web/backend $REMOTE_DIR/lib $REMOTE_DIR/bot" || { echo "❌ H5 语法错误"; exit 1; }
  ssh $SERVER "systemctl restart $H5_UNIT" && wait_http "$H5_UNIT" "$PORT" && echo "✅ $H5_UNIT 已重启且 /api/version 可达"
}
case "$TARGET" in
  bot) deploy_bot ;;
  h5) deploy_h5 ;;
  all) deploy_bot; deploy_h5 ;;
esac
echo "🎉 完成  日志: ssh $SERVER 'journalctl -u $BOT_UNIT -n 50 --no-pager; journalctl -u $H5_UNIT -n 50 --no-pager'"
