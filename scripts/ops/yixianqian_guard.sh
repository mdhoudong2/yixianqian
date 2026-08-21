#!/bin/bash
# 一线牵机器人守护脚本
# 生产（systemd 环境）: start/stop/restart/status 代理到 systemctl，由 systemd 负责自动重启
# ExecStart 使用 run 子命令：前台运行（systemd Type=simple 直接追踪 python 进程，
# 崩溃时 Restart=always 自动拉起，不再依赖 nohup + pidfile 的旧方案）
SERVICE="yixianqian"

case "${1:-status}" in
  run)
    REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
    WORKDIR="$REPO_ROOT/bot"
    cd "$WORKDIR" || exit 1
    PYTHON="$WORKDIR/venv/bin/python"
    [ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
    [ -n "$PYTHON" ] || { echo "未找到 Python"; exit 1; }
    exec "$PYTHON" yixianqian_bot_ws.py
    ;;
  start|stop|restart|status)
    if command -v systemctl >/dev/null 2>&1 && systemctl cat "$SERVICE" >/dev/null 2>&1; then
      exec systemctl "$1" "$SERVICE"
    fi
    echo "systemd 不可用，请直接运行: $0 run"
    exit 1
    ;;
  *)
    echo "用法: $0 {start|stop|restart|status|run}"
    exit 1
    ;;
esac
