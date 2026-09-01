"""gunicorn 配置 —— 一线牵 H5 后端（/opt/yixianqian/web/backend/）

关键点：本地快照缓存（refresh_snapshot / start_snapshot_loop）依赖线程，
而 gunicorn 的 worker 是 master fork 出来的子进程，线程不会跨 fork 存活。
如果不在 worker 内重新预热并启动刷新，读接口会全部回退到实时飞书 API，
每个请求卡 1~2 秒（表现为前端"点一下转圈圈"）。

post_fork 在每次 worker 进程 fork 之后、开始接收请求之前执行：
先同步拉一次快照保证首个请求即命中缓存，再启动后台 15s 定时刷新线程。
"""
import logging
import os

# 单 worker 多线程：快照缓存在进程内共享，多 worker 会各自持有快照副本，
# 造成「切换状态后另一 worker 门禁漏拦」「取消喜欢后卡片池短暂不更新」等竞态。
# 当前用户量级下单 worker + 8 线程吞吐足够，且后台快照轮询配额减半。
workers = 1
threads = 16
# 绑定地址可通过环境变量 BIND 覆盖（测试服 127.0.0.1:8092）
bind = os.environ.get("BIND", "127.0.0.1:8091")
timeout = 300
graceful_timeout = 60
max_requests = 1000
max_requests_jitter = 50


def post_fork(server, worker):
    try:
        import threading

        import app as appmod
        # 1000人压测时同步拉全量会阻塞 worker 启动 30s+，导致 /api/version 亦超时；改为后台线程异步拉取
        threading.Thread(target=appmod.refresh_snapshot, daemon=True, name="snapshot-warm").start()
        appmod.start_snapshot_loop()
    except Exception as e:
        logging.getLogger(__name__).warning("worker 快照初始化失败: %s", e)
