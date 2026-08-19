#!/bin/bash
# 一线牵机器人 - 开发版守护脚本
# 用法: bash yixianqian_guard_dev.sh [start|stop|restart|status]
WORKDIR="/home/user/.super_doubao/super-doubao-runtime/workspace"
SCRIPT="yixianqian_bot_ws.py"
PIDFILE="$WORKDIR/yixianqian_bot_dev.pid"
LOGFILE="/tmp/yixianqian_bot_dev.log"
export YIXIANQIAN_ENV=dev
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
        echo "开发版已在运行，PID=$(cat "$PIDFILE")"
        return 0
    fi
    python3 -c "import lark_oapi" 2>/dev/null || {
        echo "正在安装 lark-oapi..."
        pip3 install lark-oapi -i https://pypi.org/simple/ -q
    }
    echo "启动一线牵机器人【开发版】..."
    nohup python3 "$SCRIPT" >> "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 2
    if is_running; then
        echo "开发版启动成功，PID=$(cat "$PIDFILE")"
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
        echo "停止开发版，PID=$pid..."
        kill "$pid" 2>/dev/null
        sleep 2
        if kill -0 "$pid" 2>/dev/null; then
            echo "强制停止..."
            kill -9 "$pid" 2>/dev/null
        fi
        rm -f "$PIDFILE"
        echo "开发版已停止"
    else
        echo "开发版未运行"
        rm -f "$PIDFILE"
    fi
}
status_service() {
    if is_running; then
        echo "开发版运行中，PID=$(cat "$PIDFILE")"
        echo "--- 最近10行日志 ---"
        tail -10 "$LOGFILE"
    else
        echo "开发版未运行"
    fi
}
case "${1:-status}" in
    start)   start_service ;;
    stop)    stop_service ;;
    restart) stop_service; start_service ;;
    status)  status_service ;;
    *)       echo "用法: $0 {start|stop|restart|status}" ;;
esac
