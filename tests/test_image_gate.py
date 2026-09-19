"""图片下载 singleflight 门禁单测（不联网：多并发下同 token 只有一个下载者）。"""
import os
import sys
import threading

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend"
)
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

from app import _image_gate_acquire, _image_gate_release


def test_single_owner_among_concurrent_acquirers():
    """10 个线程同时抢：恰好 1 个 owner，其余拿到同一个未 set 的 gate。"""
    token = "ut_gate_single_owner"
    results = []
    barrier = threading.Barrier(10)

    def grab():
        barrier.wait()
        results.append(_image_gate_acquire(token))

    threads = [threading.Thread(target=grab) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    owners = [r for r in results if r[0]]
    waiters = [r for r in results if not r[0]]
    assert len(owners) == 1
    assert len(waiters) == 9
    gate = owners[0][1]
    assert not gate.is_set()
    assert all(g is gate for _, g in waiters)
    _image_gate_release(token, gate)
    assert gate.is_set()


def test_release_wakes_waiter_then_reacquire_becomes_owner():
    """release 后等待者被唤醒；之后再抢的人成为新一轮 owner（旧 gate 已废弃）。"""
    token = "ut_gate_reacquire"
    is_owner, gate = _image_gate_acquire(token)
    assert is_owner
    seen = []

    def waiter():
        o2, g2 = _image_gate_acquire(token)
        assert not o2 and g2 is gate
        assert g2.wait(10)
        seen.append("served")

    t = threading.Thread(target=waiter)
    t.start()
    _image_gate_release(token, gate)
    t.join(10)
    assert seen == ["served"]
    # 旧 gate 已废弃：重新 acquire 必须成为新 owner 且 gate 是新对象
    o3, g3 = _image_gate_acquire(token)
    assert o3 and g3 is not gate
    _image_gate_release(token, g3)
