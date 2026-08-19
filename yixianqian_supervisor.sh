#!/bin/bash
# 一线牵自动守护：每30秒检查服务，挂了自动拉起
WORKDIR="/home/user/.super_doubao/super-doubao-runtime/workspace"
PIDFILE="$WORKDIR/yixianqian_supervisor.pid"
LOGFILE="/tmp/yixianqian_supervisor.log"

echo $$ > "$PIDFILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 守护进程启动 PID=$$" >> "$LOGFILE"

while true; do
    if ! bash "$WORKDIR/yixianqian_guard.sh" status 2>/dev/null | grep -q "运行中"; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 服务未运行，自动重启..." >> "$LOGFILE"
        bash "$WORKDIR/yixianqian_guard.sh" start >> "$LOGFILE" 2>&1
    fi
    sleep 30
done
