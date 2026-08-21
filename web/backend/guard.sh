#!/bin/bash
cd /opt/yixianqian-h5/backend
source venv/bin/activate
while true; do
    gunicorn -c gunicorn.conf.py app:app >> /tmp/yixianqian_h5.log 2>&1
    sleep 3
done
