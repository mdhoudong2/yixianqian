"""refresh_snapshot_table 的 single-flight + coalesce 语义（不联网，桩 fetcher 驱动）。

背景：管理台首页三端点并发 fresh 读，同一张表曾被重复全表抓取 2–3 次，全部挤在
10 req/s 令牌桶后排队，页面偶发 Failed to fetch。本文件锁住修复后的三条契约：
- coalesce 窗口内成功抓过 → 后续调用不再打飞书；
- 并发调用由 per-key 锁串行化，后到者复用前一次结果（single-flight）；
- coalesce=0（写后刷新/背景 loop 语义）即使刚抓过也一定重抓。
"""
import os
import sys
import threading
import time

import pytest

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402

KEY = "activities"


@pytest.fixture
def snap_env(monkeypatch):
    """隔离该表的快照/时间戳/冷却，返回 fetcher 调用记录列表。"""
    calls = []

    def fetcher():
        calls.append(time.time())
        return [{"record_id": "rec_snap_test"}]

    monkeypatch.setitem(app._SNAPSHOT_FETCHERS, KEY, fetcher)
    monkeypatch.setitem(app._snapshot, KEY, [])
    monkeypatch.setitem(app._snapshot_last_ok, KEY, 0.0)
    monkeypatch.delitem(app._SNAPSHOT_FAIL_UNTIL, KEY, raising=False)
    return calls


def test_coalesce_skips_within_window(snap_env):
    app.refresh_snapshot_table(KEY, coalesce=2.0)
    app.refresh_snapshot_table(KEY, coalesce=2.0)
    assert len(snap_env) == 1


def test_coalesce_expired_refetches(snap_env, monkeypatch):
    monkeypatch.setitem(app._snapshot_last_ok, KEY, time.time() - 5.0)
    app.refresh_snapshot_table(KEY, coalesce=2.0)
    assert len(snap_env) == 1


def test_write_path_ignores_coalesce(snap_env, monkeypatch):
    monkeypatch.setitem(app._snapshot_last_ok, KEY, time.time())
    app.refresh_snapshot_table(KEY, coalesce=0.0)
    assert len(snap_env) == 1  # 写后刷新必须真抓


def test_single_flight_waiter_reuses_inflight(monkeypatch):
    calls = []
    inflight = threading.Event()
    release = threading.Event()

    def slow_fetcher():
        calls.append(1)
        inflight.set()
        release.wait(5)
        return [{"record_id": "rec_snap_test"}]

    monkeypatch.setitem(app._SNAPSHOT_FETCHERS, KEY, slow_fetcher)
    monkeypatch.setitem(app._snapshot, KEY, [])
    monkeypatch.setitem(app._snapshot_last_ok, KEY, 0.0)
    monkeypatch.delitem(app._SNAPSHOT_FAIL_UNTIL, KEY, raising=False)

    first = threading.Thread(target=app.refresh_snapshot_table,
                             args=(KEY,), kwargs={"coalesce": 2.0})
    first.start()
    assert inflight.wait(5), "第一次抓取未开始"
    second = threading.Thread(target=app.refresh_snapshot_table,
                              args=(KEY,), kwargs={"coalesce": 2.0})
    second.start()
    time.sleep(0.2)  # 让 second 进入锁等待；若没有锁，它会各自发起第二次 fetch
    release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert len(calls) == 1  # 后到者复用前一次结果，没有另起抓取
