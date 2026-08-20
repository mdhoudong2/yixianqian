#!/bin/bash
# 服务器拉取部署脚本（pull-only：豆包已 push 到 GitHub，本脚本只负责服务器 git pull + 重启）
# 用法: bash deploy_server.sh [bot|h5|all]
#   bot  - 只部署机器人
#   h5   - 只部署H5
#   all  - 全部部署（默认）
#
# 适用场景：豆包（或他人）已 push 到 GitHub，通知 Claude Code 部署时使用。不做本地 push。
# 与 deploy.sh 的区别：deploy.sh = 本地改完 push + 服务器部署；本脚本 = 纯服务器拉取部署。

SERVER="root@172.245.223.118"
TARGET="${1:-all}"

echo "🔄 服务器拉取部署（pull-only，不 push）"

deploy_bot() {
    echo "🤖 部署机器人..."
    ssh $SERVER "cd /opt/yixianqian && git pull --ff-only origin main" || { echo "❌ 服务器 pull 失败，中止"; exit 1; }
    ssh $SERVER "/opt/yixianqian/venv/bin/python -m py_compile /opt/yixianqian/yixianqian_bot_ws.py" || { echo "❌ 语法错误，未重启"; exit 1; }
    ssh $SERVER "systemctl restart yixianqian"
    echo "✅ 机器人部署成功"
}

deploy_h5() {
    echo "🌐 部署 H5..."
    ssh $SERVER "cd /opt/yixianqian-h5 && git pull --ff-only origin main" || { echo "❌ 服务器 pull 失败，中止"; exit 1; }
    ssh $SERVER "/opt/yixianqian-h5/backend/venv/bin/python -m py_compile /opt/yixianqian-h5/backend/app.py /opt/yixianqian-h5/backend/config.py /opt/yixianqian-h5/backend/bitable.py" || { echo "❌ 语法错误，未重启"; exit 1; }
    ssh $SERVER "systemctl restart yixianqian-h5"
    echo "✅ H5 部署成功"
}

case "$TARGET" in
    bot) deploy_bot ;;
    h5)  deploy_h5 ;;
    all) deploy_bot && deploy_h5 ;;
    *)   echo "用法: bash deploy_server.sh [bot|h5|all]"; exit 1 ;;
esac

echo ""
echo "🎉 部署完成！"
echo "查看日志: ssh $SERVER 'tail -f /tmp/yixianqian_bot.log'"
