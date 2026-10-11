"""活动分组：跨进程互斥锁（bot 与 H5 后端共用）。

本模块**不得** import bot/ 或 web/backend/ 下的任何模块（constants / config /
clients / queries）：CI 的 conftest 把仓库根和 bot/ 都插进 sys.path，`import config`
在两端会解析到不同的文件，等于把「读错 base token」变成定时炸弹。锁目录由两端
启动时各自 `configure()` 注入。
"""
import os
import re
import threading
import time
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # 无 flock 的平台退化为仅线程锁，与 lib/storage 同一取舍
    fcntl = None


class GroupingBusy(Exception):
    """该活动的分组互斥锁被别人持着，本次拿不到。"""


_lock_dir = None


def configure(*, lock_dir=None):
    """注入分组锁的落盘目录。两端启动时各调一次，不调就用锁会抛 RuntimeError。"""
    global _lock_dir
    if lock_dir is not None:
        _lock_dir = lock_dir


def _sanitize(activity_id):
    """活动 ID 转安全文件名。

    activity_id 来自聊天输入 / URL，白名单外的字符全部替换掉才拼路径，
    否则「../../etc/passwd」这种输入能把锁文件写到数据目录外面去。
    """
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(activity_id))[:64]


_locks_guard = threading.Lock()
_thread_locks = {}


def _thread_lock(path):
    """同一路径共用一把进程内锁。挡掉最常见的「同一进程的 4 条管理指令线程」竞争。"""
    with _locks_guard:
        if path not in _thread_locks:
            _thread_locks[path] = threading.Lock()
        return _thread_locks[path]


def _flock_wait(f, deadline, activity_id):
    """等跨进程 flock，等到截止时间为止。

    用 LOCK_NB 轮询而不是阻塞版 flock：阻塞版没有逾时，而分组是分钟级作业，
    对端要是卡住不动，一个 flock 就足以把调用方的执行线程池（bot 4 条、gunicorn
    16 条）永久占死——那正是「逾时必填」想避免的失败模式。轮询只发生在真正的
    跨进程争用上，进程内的常规竞争早被线程锁挡掉了。
    """
    if not fcntl:
        return
    while True:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GroupingBusy(activity_id)
            time.sleep(min(0.2, remaining))


@contextmanager
def grouping_lock(activity_id, timeout):
    """按活动分档的互斥锁：进程内 threading.Lock + 跨进程 flock。

    分档而不是全局一把：A 场分组不该把 B 场的管理员一起挡在门外。

    `timeout` 必填，且是**总的**等待预算（线程锁与 flock 共用同一截止时间）。
    分组是分钟级作业（200 人光读志愿就有 40 秒下限，全局 10 req/s 令牌桶），
    无限等待会把调用方的执行线程池占满。抢不到就抛 GroupingBusy，让调用端决定
    怎么告诉管理员——可观察的失败优于不可观察的挂死。

    不可重入：同一线程重复进入会死锁。这是刻意的——重入说明调用方搞错了锁的粒度，
    宁可死锁也不要静默退化成「看起来加了锁其实没加」。
    """
    if not _lock_dir:
        raise RuntimeError("lib.grouping 未 configure(lock_dir=...)，不能使用分组互斥锁")
    path = os.path.join(_lock_dir, f"yixianqian_grouping_{_sanitize(activity_id)}.lock")
    deadline = time.monotonic() + timeout if timeout > 0 else time.monotonic()
    tl = _thread_lock(path)
    # 进程内先挡，剩下的预算拿去等 flock。注意 timeout 不能原样传给 acquire()：
    # threading 把负数当「无限等待」，正好把要防的情况放进来。
    if not tl.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise GroupingBusy(activity_id)
    # 锁文件与数据分离（同 lib/storage._FileLock）：锁不能跟着数据被 rename 走。
    f = open(path, "a+")
    try:
        _flock_wait(f, deadline, activity_id)
        yield
    finally:
        if fcntl:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()
        tl.release()
