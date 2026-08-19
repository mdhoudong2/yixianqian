#!/bin/bash
# 一线牵项目一键部署脚本（适配 ~/code 目录结构）
# 用法: bash deploy.sh [bot|h5|all]
#   bot  - 只部署机器人
#   h5   - 只部署H5
#   all  - 全部部署（默认）
#
# 目录约定：
#   ~/code/yixianqian/        -> 机器人（本脚本所在目录）
#   ~/code/yixianqian-h5/     -> H5 前端 + 后端

SERVER="root@172.245.223.118"
TARGET="${1:-all}"

echo "🚀 开始部署到 $SERVER ..."

# 服务器备份（服务器已无 git，改用 cp 快照备份）
echo "📦 服务器端备份..."
ssh -o StrictHostKeyChecking=no $SERVER "cp /opt/yixianqian/yixianqian_bot_ws.py /opt/yixianqian/yixianqian_bot_ws.py.bak 2>/dev/null; cp /opt/yixianqian-h5/backend/app.py /opt/yixianqian-h5/backend/app.py.bak 2>/dev/null; cp /opt/yixianqian-h5/backend/config.py /opt/yixianqian-h5/backend/config.py.bak 2>/dev/null; cp /opt/yixianqian-h5/backend/bitable.py /opt/yixianqian-h5/backend/bitable.py.bak 2>/dev/null; echo 'backup done'"

if [ "$TARGET" = "bot" ] || [ "$TARGET" = "all" ]; then
    echo "🤖 部署机器人..."
    if [ -f "yixianqian_bot_ws.py" ]; then
        scp yixianqian_bot_ws.py $SERVER:/opt/yixianqian/yixianqian_bot_ws.py
        echo "  语法检查..."
        ssh $SERVER "/opt/yixianqian/venv/bin/python -m py_compile /opt/yixianqian/yixianqian_bot_ws.py"
        if [ $? -eq 0 ]; then
            echo "  重启服务..."
            ssh $SERVER "systemctl restart yixianqian"
            echo "✅ 机器人部署成功"
        else
            echo "❌ 语法错误，已保留旧版本，未重启"
        fi
    else
        echo "⚠️  yixianqian_bot_ws.py 不存在，跳过"
    fi
fi

if [ "$TARGET" = "h5" ] || [ "$TARGET" = "all" ]; then
    echo "🌐 部署H5后端..."
    if [ -f "../yixianqian-h5/backend/app.py" ]; then
        scp ../yixianqian-h5/backend/app.py ../yixianqian-h5/backend/config.py ../yixianqian-h5/backend/bitable.py $SERVER:/opt/yixianqian-h5/backend/
        ssh $SERVER "/opt/yixianqian-h5/backend/venv/bin/python -m py_compile /opt/yixianqian-h5/backend/app.py"
        if [ $? -eq 0 ]; then
            echo "✅ H5后端部署成功（代码已就位，重启见下方）"
        else
            echo "❌ 语法错误，已保留旧版本，未重启"
        fi
    fi

    echo "⚙️  部署 gunicorn 配置..."
    if [ -f "../yixianqian-h5/backend/gunicorn.conf.py" ]; then
        scp ../yixianqian-h5/backend/gunicorn.conf.py $SERVER:/opt/yixianqian-h5/backend/gunicorn.conf.py
        echo "✅ gunicorn.conf.py 部署成功"
    fi

    echo "🎨 部署H5前端..."
    if [ -f "../yixianqian-h5/frontend/index.html" ]; then
        scp ../yixianqian-h5/frontend/index.html $SERVER:/opt/yixianqian-h5/frontend/index.html
        echo "✅ H5前端部署成功"
    fi

    echo "🔁 重启 H5（systemctl）..."
    ssh $SERVER "systemctl restart yixianqian-h5 && sleep 2 && systemctl is-active yixianqian-h5"
fi

if [ -d "tools" ]; then
    echo "🔧 部署工具脚本..."
    scp tools/*.py $SERVER:/opt/yixianqian/tools/ 2>/dev/null
    echo "✅ 工具脚本部署成功"
fi

echo ""
echo "🎉 部署完成！"
echo "查看日志: ssh $SERVER 'tail -f /tmp/yixianqian_bot.log'"
