"""lib/cancel_quota.py 单测：滚动窗口裁剪、限额拦截、首建回填。"""
import time

from lib import cancel_quota as q


def test_windowed_prunes_expired():
    now = time.time()
    records = [now - 8 * 86400, now - 6 * 86400, now - 10, now]
    assert q.windowed(records, now) == [now - 6 * 86400, now - 10, now]


def test_allow_up_to_limit_then_block():
    now = time.time()
    records = []
    for _ in range(q.CANCEL_QUOTA_LIMIT):
        ok, records = q.record_cancel(records, now)
        assert ok
    assert q.quota_remaining(records, now) == 0
    ok, records = q.record_cancel(records, now)
    assert not ok
    assert len(records) == q.CANCEL_QUOTA_LIMIT


def test_window_slides_and_quota_recovers():
    now = time.time()
    records = [now] * q.CANCEL_QUOTA_LIMIT
    later = now + q.CANCEL_QUOTA_WINDOW_S + 1
    assert q.quota_allowed(records, later)
    assert q.quota_remaining(records, later) == q.CANCEL_QUOTA_LIMIT


def test_seed_caps_history_and_forces_exhausted():
    now = time.time()
    cancels = q.seed_cancels({"ou_a": 10, "ou_b": 1}, {"ou_c"}, now)
    assert cancels["ou_a"] == [now] * q.CANCEL_QUOTA_LIMIT
    assert cancels["ou_b"] == [now]
    assert cancels["ou_c"] == [now] * q.CANCEL_QUOTA_LIMIT
    assert not q.quota_allowed(cancels["ou_a"], now)
    assert not q.quota_allowed(cancels["ou_c"], now)
    assert q.quota_allowed(cancels["ou_b"], now)
