#!/bin/bash
# 一线牵机器人守护脚本
WORKDIR="/opt/yixianqian/bot"
if [ ! -f "$WORKDIR/yixianqian_bot_ws.py" ] && [ -f "/opt/yixianqian/yixianqian_bot_ws.py" ]; then WORKDIR="/opt/yixianqian"; fi
SCRIPT="yixianqian_bot_ws.py"
PIDFILE="$WORKDIR/yixianqian_bot.pid"
LOGFILE="/tmp/yixianqian_bot.log"
cd "$WORKDIR" || exit 1
is_running(){ [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }
start_service(){
  if is_running; then echo "已运行 PID=$(cat "$PIDFILE")"; return 0; fi
  PYTHON="$WORKDIR/venv/bin/python"
  [ -x "$PYTHON" ] || PYTHON="/opt/yixianqian/venv/bin/python"
  PIP="${PYTHON%/*}/pip"
  $PYTHON -c "import lark_oapi" 2>/dev/null || { echo "安装 lark-oapi..."; $PIP install lark-oapi -q; }
  echo "启动 $SCRIPT ..."; nohup $PYTHON "$SCRIPT" >>"$LOGFILE" 2>&1 & echo $! >"$PIDFILE"; echo "PID $!"; 
}
stop_service(){ if is_running; then kill "$(cat "$PIDFILE")"; rm -f "$PIDFILE"; echo "已停止"; else echo "未运行"; fi; }
case "${1:-start}" in start) start_service;; stop) stop_service;; restart) stop_service; sleep 1; start_service;; status) if is_running; then echo "运行中 PID=$(cat "$PIDFILE")"; else echo "未运行"; fi;; *) echo "用法: $0 [start|stop|restart|status]";; esac
