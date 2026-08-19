#!/bin/bash
# 一线牵机器人守护脚本
# 用法: bash yixianqian_guard.sh [start|stop|restart|status]

WORKDIR="/opt/yixianqian"
SCRIPT="yixianqian_bot_ws.py"
PIDFILE="$WORKDIR/yixianqian_bot.pid"
LOGFILE="/tmp/yixianqian_bot.log"

cd "$WORKDIR" || exit 1

is_running() {
    if [ -f "$PIDFILE" ]; then
        local pid
        pid=$(cat "$PIDFILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

start_service() {
    if is_running; then
        echo "服务已在运行，PID=$(cat "$PIDFILE")"
        return 0
    fi

    # 确保依赖已安装
    PYTHON="/opt/yixianqian/venv/bin/python"
    PIP="/opt/yixianqian/venv/bin/pip"

    $PYTHON -c "import lark_oapi" 2>/dev/null || {
        echo "正在安装 lark-oapi..."
        $PIP install lark-oapi -i https://pypi.org/simple/ -q
    }

    echo "启动一线牵机器人..."
    nohup $PYTHON "$SCRIPT" >> "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 2
    if is_running; then
        echo "启动成功，PID=$(cat "$PIDFILE")"
        echo "日志: tail -f $LOGFILE"
    else
        echo "启动失败，请查看日志: $LOGFILE"
        return 1
    fi
}

stop_service() {
    if is_running; then
        local pid
        pid=$(cat "$PIDFILE")
        echo "停止服务，PID=$pid..."
        kill "$pid" 2>/dev/null
        sleep 2
        if kill -0 "$pid" 2>/dev/null; then
            echo "强制停止..."
            kill -9 "$pid" 2>/dev/null
        fi
        rm -f "$PIDFILE"
        echo "服务已停止"
    else
        echo "服务未运行"
        rm -f "$PIDFILE"
    fi
}

status_service() {
    if is_running; then
        echo "服务运行中，PID=$(cat "$PIDFILE")"
        echo "--- 最近10行日志 ---"
        tail -10 "$LOGFILE"
    else
        echo "服务未运行"
    fi
}

case "${1:-status}" in
    start)   start_service ;;
    stop)    stop_service ;;
    restart) stop_service; start_service ;;
    status)  status_service ;;
    *)       echo "用法: $0 {start|stop|restart|status}" ;;
esac
