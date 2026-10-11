"""分组互斥锁单测（不联网）：分活动隔离、互斥、逾时、配置前置、路径安全。

这些用例跑真线程和真进程——锁的全部价值就在于「另一个进程 / 另一条线程也进不来」，
用假锁测等于什么都没测（多进程用法比照 tests/test_storage.py）。
"""
import multiprocessing
import os
import threading
import time

import pytest

from lib import grouping as lib_grouping


@pytest.fixture
def lock_dir(tmp_path):
    """给一份干净的锁目录，并把模块级配置还原，免得用例之间串味。"""
    saved = lib_grouping._lock_dir
    lib_grouping.configure(lock_dir=str(tmp_path))
    yield tmp_path
    lib_grouping._lock_dir = saved


def _proc_hold(lock_dir, activity_id, out_path, hold_s):
    """子进程：抢锁成功就把持有区间写进 out_path，抢不到写 busy。"""
    from lib import grouping as g
    g.configure(lock_dir=lock_dir)
    try:
        with g.grouping_lock(activity_id, timeout=30):
            enter = time.time()
            time.sleep(hold_s)
            out_path.write_text(f"{enter:.6f} {time.time():.6f}", encoding="utf-8")
    except g.GroupingBusy:
        out_path.write_text("busy", encoding="utf-8")


def _proc_try(lock_dir, activity_id, out_path, timeout):
    """子进程：只试一把就出结论。"""
    from lib import grouping as g
    g.configure(lock_dir=lock_dir)
    try:
        with g.grouping_lock(activity_id, timeout=timeout):
            out_path.write_text("ok", encoding="utf-8")
    except g.GroupingBusy:
        out_path.write_text("busy", encoding="utf-8")


def _proc_hold_until_released(lock_dir, activity_id, ready, release):
    """独立进程（spawn）：拿到锁后一直持有，直到看到 release。

    必须是模块级函数：spawn 要 pickle 这个 target，本地闭包 pickle 不了。
    """
    from lib import grouping as g
    g.configure(lock_dir=lock_dir)
    with g.grouping_lock(activity_id, timeout=30):
        ready.set()
        release.wait(30)


def _fork(target, *args):
    """从当前（pytest 主）进程 fork 出 contender。

    这里刻意用 fork：L3 要的就是「contender 自己从未持过锁」的语义——主进程不
    持有该路径的锁，fork 出去的 contender 拿到一把全新、未持有的线程锁，于是它
    被挡只可能是 flock 的功劳。若换成 spawn，语义一样成立（子进程本来就是干净的），
    但 fork 让「 contender 的锁确实是继承来的、且是未持有态」这件事更贴近真实 bot
    场景（bot 自己的管理指令线程也是同进程 fork 出来的）。

    代价：多线程进程里 fork 会触发 CPython 的
    `DeprecationWarning: This process is multi-threaded, use of fork() may lead to
    deadlocks in the child`。已评估过，结论是**保留 fork**：警告非致命（本仓库未把
    DeprecationWarning 设为 error），本文件的 fork 次数改前改后都是 3（L2 两个 +
    L3 一个），没有增加；而 L3 的 contender 若换 spawn 反而失去「继承来的锁确为
    未持有态」这层真实性。子进程做的事也很小（configure + 抢一次锁 + 写一个文件），
    实际死锁风险低。
    """
    p = multiprocessing.get_context("fork").Process(target=target, args=args)
    p.start()
    return p


def _spawn(target, *args):
    """独立 spawn 出一个进程（不复用主进程内存）。

    spawn 而非 fork，是为了让 holder 拿到的 threading.Lock 与主进程**毫无关系**：
    fork 会把主进程里「可能正持有」的锁状态一起复制过去，holder 就有可能被一把
    它从没碰过的、继承来的线程锁绊住。spawn 从零起一个解释器，主进程当时是单线程
    还是多线程都不影响（顺带避开 `fork() may lead to deadlocks` 警告）。
    """
    p = multiprocessing.get_context("spawn").Process(target=target, args=args)
    p.start()
    return p


def _join(p):
    p.join(30)
    assert p.exitcode == 0, f"子进程异常退出 exitcode={p.exitcode}"


def test_lock_is_per_activity(lock_dir):
    """L1：A 的锁不挡 B——两场分组本就该能并行，全局一把锁是粒度错。"""
    order = []
    b_entered = threading.Event()

    def hold_b():
        with lib_grouping.grouping_lock("A-0003", timeout=5):
            order.append("B")
            b_entered.set()

    with lib_grouping.grouping_lock("A-0002", timeout=1):
        order.append("A")
        t = threading.Thread(target=hold_b)
        t.start()
        t.join(timeout=5)
        assert b_entered.is_set(), "B 场被 A 场的锁挡住了"
    t.join(timeout=5)
    assert order == ["A", "B"]


def test_lock_serializes_same_activity(lock_dir):
    """L2：两个进程同时抢同一场，只有一个能进去，两段持有区间不重叠。"""
    outs = [lock_dir / f"p{i}.txt" for i in range(2)]
    procs = [_fork(_proc_hold, str(lock_dir), "A-0002", out, 0.4) for out in outs]
    for p in procs:
        _join(p)
    spans = []
    for out in outs:
        line = out.read_text(encoding="utf-8")
        assert line != "busy", "持锁 0.4 秒不该抢不到"
        enter, leave = (float(x) for x in line.split())
        spans.append((enter, leave))
    spans.sort()
    assert spans[0][1] <= spans[1][0], f"两个进程同时进了临界区：{spans}"


def test_timeout_raises_grouping_busy(lock_dir):
    """L3：跨进程 flock 被别人占着时，逾时要抛 GroupingBusy，不能无限等。

    必须钉死的语义：**contender 进不来，只能是 flock 挡的**。
    早先这里写成「父进程持锁 → fork 子进程」，看着对，其实是假绿：
    fork 把父进程那把**正持有**的 threading.Lock 原样复制给了子进程
    （copy-on-write），子进程于是被一把继承来的进程内线程锁挡住，根本没走到
    flock。把 fcntl 停掉这条用例照样绿——所以它压根没测到 _flock_wait 的逾时
    分支，删掉 deadline 检查也拦不住。

    现在的构造让两个角色彻底解耦：
    - holder：独立 **spawn** 进程，拿到锁后保持持有；
    - contender：从主进程 **fork**，而主进程此前**从未持有**这把锁，主进程里
      _thread_locks[该路径] 要么不存在、要么是未持有态，于是 contender fork 出
      的进程内锁是**全新、未持有**的——acquire 立刻成功，被挡只能来自 flock。
    """
    ctx = multiprocessing.get_context("spawn")
    holder_ready, holder_release = ctx.Event(), ctx.Event()
    holder = _spawn(_proc_hold_until_released, str(lock_dir), "A-0002",
                    holder_ready, holder_release)
    try:
        assert holder_ready.wait(30), "holder 进程未能在 30s 内拿到锁"
        out = lock_dir / "child.txt"
        # 主进程从未持有该路径的锁，contender fork 出来的线程锁必然是未持有态。
        _join(_fork(_proc_try, str(lock_dir), "A-0002", out, 0.5))
    finally:
        holder_release.set()
        holder.join(30)
    assert out.read_text(encoding="utf-8") == "busy", \
        "contender 未被 flock 挡住：它拿到了锁，说明进程内锁被继承成持有态了"


def test_thread_lock_times_out_instead_of_deadlock(lock_dir):
    """L4：同一进程第二条线程要能超时退出，否则锁把管理指令线程池堵死。"""
    holding = threading.Event()
    release = threading.Event()
    outcome = []

    def hold():
        with lib_grouping.grouping_lock("A-0002", timeout=5):
            holding.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert holding.wait(5)
        try:
            with lib_grouping.grouping_lock("A-0002", timeout=0.3):
                outcome.append("acquired")
        except lib_grouping.GroupingBusy:
            outcome.append("busy")
    finally:
        release.set()
        t.join(5)
    assert outcome == ["busy"]


def test_lock_requires_configure(monkeypatch):
    """L5：没 configure 就用锁要报 RuntimeError，而不是猜个目录 / 报 FileNotFoundError。

    猜目录的后果是锁写在别处——看起来加了锁，其实没加。
    """
    monkeypatch.setattr(lib_grouping, "_lock_dir", None)
    with pytest.raises(RuntimeError):
        with lib_grouping.grouping_lock("A-0002", timeout=1):
            pass


def test_lock_file_stays_inside_lock_dir(lock_dir):
    """L6：活动 ID 带路径穿越，锁文件也必须落在配置目录里。"""
    with lib_grouping.grouping_lock("../../etc/passwd", timeout=1):
        pass
    files = list(lock_dir.iterdir())
    assert len(files) == 1, f"锁目录内应只留一个锁文件，实际 {[f.name for f in files]}"
    path = files[0].resolve()
    assert path.parent == lock_dir.resolve()
    assert path.name.startswith("yixianqian_grouping_")


def test_lock_path_is_flat_per_activity(lock_dir):
    """锁文件按活动平铺在共享数据目录下，与既有的 yixianqian_*.json 同一形态。"""
    with lib_grouping.grouping_lock("A-0002", timeout=1):
        pass
    assert [f.name for f in lock_dir.iterdir()] == ["yixianqian_grouping_A-0002.lock"]


def test_thread_lock_registry_is_bounded(lock_dir):
    """L7：常驻进程不能每见一个活动 ID 就永久多一把锁——超过上限要清掉没人用的。

    bot 是 websocket 常驻进程，这条是无界增长的护栏。清理只动「在用计数为 0」的
    条目，见 _thread_lock。
    """
    saved = dict(lib_grouping._thread_locks)
    try:
        cap = lib_grouping._MAX_THREAD_LOCKS
        for i in range(cap + 20):
            with lib_grouping.grouping_lock(f"A-{i:04d}", timeout=1):
                pass
        assert len(lib_grouping._thread_locks) <= cap
    finally:
        lib_grouping._thread_locks.clear()
        lib_grouping._thread_locks.update(saved)


def test_held_lock_is_never_evicted(lock_dir):
    """L8：正在持有的锁绝不能被上限清理掉——被删掉等于凭空多一把锁，互斥静默失效。"""
    saved = dict(lib_grouping._thread_locks)
    try:
        with lib_grouping.grouping_lock("A-HELD", timeout=5):
            for i in range(lib_grouping._MAX_THREAD_LOCKS + 20):
                with lib_grouping.grouping_lock(f"A-{i:04d}", timeout=1):
                    pass
            path = os.path.join(
                str(lock_dir),
                f"yixianqian_grouping_{lib_grouping._sanitize('A-HELD')}.lock")
            assert path in lib_grouping._thread_locks, "正在持有的锁被淘汰了"
    finally:
        lib_grouping._thread_locks.clear()
        lib_grouping._thread_locks.update(saved)


def test_sanitize_keeps_name_short_and_escapeless():
    # 「.」也不在白名单里：两个点连成「..」照样不能留在文件名中
    assert lib_grouping._sanitize("../../etc/passwd") == "______etc_passwd"
    assert "/" not in lib_grouping._sanitize("a/b\\c d")
    assert len(lib_grouping._sanitize("x" * 200)) == 64
