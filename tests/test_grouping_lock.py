"""分组互斥锁单测（不联网）：分活动隔离、互斥、逾时、配置前置、路径安全。

这些用例跑真线程和真进程——锁的全部价值就在于「另一个进程 / 另一条线程也进不来」，
用假锁测等于什么都没测（多进程用法比照 tests/test_storage.py）。
"""
import multiprocessing
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


def _spawn(target, *args):
    p = multiprocessing.get_context("fork").Process(target=target, args=args)
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
    procs = [_spawn(_proc_hold, str(lock_dir), "A-0002", out, 0.4) for out in outs]
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
    """L3：锁被别的进程占着时，逾时要抛 GroupingBusy，不能无限等。

    子进程的进程内锁是 fork 出来的独立副本，它进不来只可能是 flock 挡住了。
    """
    out = lock_dir / "child.txt"
    with lib_grouping.grouping_lock("A-0002", timeout=5):
        _join(_spawn(_proc_try, str(lock_dir), "A-0002", out, 0.5))
    assert out.read_text(encoding="utf-8") == "busy"


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


def test_sanitize_keeps_name_short_and_escapeless():
    # 「.」也不在白名单里：两个点连成「..」照样不能留在文件名中
    assert lib_grouping._sanitize("../../etc/passwd") == "______etc_passwd"
    assert "/" not in lib_grouping._sanitize("a/b\\c d")
    assert len(lib_grouping._sanitize("x" * 200)) == 64
