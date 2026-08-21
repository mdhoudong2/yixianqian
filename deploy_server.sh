#!/bin/bash
# 纯服务器拉取（已有人 push，直接在服务器 pull+重启）
SERVER="root@172.245.223.118"
TARGET="${1:-all}"
echo "🔄 服务器拉取 (pull-only)"
deploy_bot(){ ssh $SERVER "cd /opt/yixianqian && git pull --ff-only origin main" || exit 1; ssh $SERVER "/opt/yixianqian/bot/venv/bin/python -m py_compile /opt/yixianqian/bot/yixianqian_bot_ws.py 2>/dev/null || /opt/yixianqian/venv/bin/python -m py_compile /opt/yixianqian/bot/yixianqian_bot_ws.py" || exit 1; ssh $SERVER "systemctl restart yixianqian"; echo "✅ bot"; }
deploy_h5(){ ssh $SERVER "cd /opt/yixianqian && git pull --ff-only origin main 2>/dev/null || cd /opt/yixianqian-h5 && git pull --ff-only origin main" || exit 1; ssh $SERVER "/opt/yixianqian/web/backend/venv/bin/python -m py_compile /opt/yixianqian/web/backend/app.py 2>/dev/null || /opt/yixianqian-h5/backend/venv/bin/python -m py_compile /opt/yixianqian/web/backend/app.py" || exit 1; ssh $SERVER "systemctl restart yixianqian-h5"; echo "✅ H5"; }
case "$TARGET" in bot) deploy_bot;; h5) deploy_h5;; all) deploy_bot; deploy_h5;; *) echo "用法: bash deploy_server.sh [bot|h5|all]"; exit 1;; esac
echo "🎉 完成"
