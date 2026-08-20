#!/bin/bash
# 一线牵项目部署脚本（GitHub 为中心：本地 push → 服务器 pull → 重启）
# 用法: bash deploy.sh [bot|h5|all]
#   bot  - 只部署机器人
#   h5   - 只部署H5
#   all  - 全部部署（默认）
#
# 目录约定：
#   ~/code/yixianqian/        -> 机器人仓库（本脚本所在目录）
#   ~/code/yixianqian-h5/     -> H5 仓库
#
# 工作流：本地改完 commit → 本脚本 push 到 GitHub → 服务器 git pull --ff-only → 语法检查 → 重启
# 注意：服务器只 pull 不 reset；venv/ 与 local_config.py 已 gitignore，不会被覆盖。

SERVER="root@172.245.223.118"
TARGET="${1:-all}"

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
H5_DIR="$(cd "$(dirname "$0")/../yixianqian-h5" && pwd)"

echo "🚀 部署到 $SERVER（GitHub 为中心：push → pull → restart）"

deploy_bot() {
    echo "🤖 部署机器人..."
    if [ -n "$(cd "$BOT_DIR" && git status --porcelain)" ]; then
        echo "❌ 机器人仓库有未提交改动，请先 commit 再部署"
        exit 1
    fi
    (cd "$BOT_DIR" && git push origin main) || { echo "❌ push 失败，中止"; exit 1; }
    ssh $SERVER "cd /opt/yixianqian && git pull --ff-only origin main" || { echo "❌ 服务器 pull 失败，中止"; exit 1; }
    ssh $SERVER "/opt/yixianqian/venv/bin/python -m py_compile /opt/yixianqian/yixianqian_bot_ws.py" || { echo "❌ 语法错误，未重启"; exit 1; }
    ssh $SERVER "systemctl restart yixianqian"
    echo "✅ 机器人部署成功"
}

deploy_h5() {
    echo "🌐 部署 H5..."
    if [ -n "$(cd "$H5_DIR" && git status --porcelain)" ]; then
        echo "❌ H5 仓库有未提交改动，请先 commit 再部署"
        exit 1
    fi
    (cd "$H5_DIR" && git push origin main) || { echo "❌ push 失败，中止"; exit 1; }
    ssh $SERVER "cd /opt/yixianqian-h5 && git pull --ff-only origin main" || { echo "❌ 服务器 pull 失败，中止"; exit 1; }
    ssh $SERVER "/opt/yixianqian-h5/backend/venv/bin/python -m py_compile /opt/yixianqian-h5/backend/app.py /opt/yixianqian-h5/backend/config.py /opt/yixianqian-h5/backend/bitable.py" || { echo "❌ 语法错误，未重启"; exit 1; }
    ssh $SERVER "systemctl restart yixianqian-h5"
    echo "✅ H5 部署成功"
}

case "$TARGET" in
    bot) deploy_bot ;;
    h5)  deploy_h5 ;;
    all) deploy_bot && deploy_h5 ;;
    *)   echo "用法: bash deploy.sh [bot|h5|all]"; exit 1 ;;
esac

echo ""
echo "🎉 部署完成！"
echo "查看日志: ssh $SERVER 'tail -f /tmp/yixianqian_bot.log'"
