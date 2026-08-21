# -*- coding: utf-8 -*-
"""gunicorn 配置 —— 一线牵 H5 后端（/opt/yixianqian-h5/backend/）

关键点：本地快照缓存（refresh_snapshot / start_snapshot_loop）依赖线程，
而 gunicorn 的 worker 是 master fork 出来的子进程，线程不会跨 fork 存活。
如果不在 worker 内重新预热并启动刷新，读接口会全部回退到实时飞书 API，
每个请求卡 1~2 秒（表现为前端"点一下转圈圈"）。

post_fork 在每次 worker 进程 fork 之后、开始接收请求之前执行：
先同步拉一次快照保证首个请求即命中缓存，再启动后台 15s 定时刷新线程。
"""
import logging

workers = 2
threads = 4
bind = "127.0.0.1:8091"
timeout = 120
graceful_timeout = 30


def post_fork(server, worker):
    try:
        import app as appmod
        appmod.refresh_snapshot()
        appmod.start_snapshot_loop()
    except Exception as e:
        logging.getLogger(__name__).warning("worker 快照初始化失败: %s", e)
