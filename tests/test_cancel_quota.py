"""lib/cancel_quota.py 单测：自然月限额、跨月重置、超限拦截、首建写满。"""
from lib import cancel_quota as q


def test_allow_up_to_limit_then_block():
    ym = "2026-09"
    rec = None
    for _ in range(q.CANCEL_QUOTA_LIMIT):
        ok, rec = q.record_month_cancel(rec, ym)
        assert ok
    assert q.month_remaining(rec, ym) == 0
    assert not q.month_allowed(rec, ym)
    ok, rec = q.record_month_cancel(rec, ym)
    assert not ok
    assert rec["n"] == q.CANCEL_QUOTA_LIMIT


def test_new_month_resets():
    old = {"ym": "2026-08", "n": 3}
    # 上月已用满，本月归零
    assert q.month_used(old, "2026-09") == 0
    assert q.month_remaining(old, "2026-09") == q.CANCEL_QUOTA_LIMIT
    ok, rec = q.record_month_cancel(old, "2026-09")
    assert ok and rec == {"ym": "2026-09", "n": 1}


def test_malformed_record_treated_as_zero():
    assert q.month_used(None, "2026-09") == 0
    assert q.month_used({"ym": "2026-09", "n": "x"}, "2026-09") == 0


def test_seed_exhausted_only_named():
    months = q.seed_exhausted({"ou_a", "ou_b"}, "2026-09")
    assert months["ou_a"] == {"ym": "2026-09", "n": q.CANCEL_QUOTA_LIMIT}
    assert months["ou_b"] == {"ym": "2026-09", "n": q.CANCEL_QUOTA_LIMIT}
    assert not q.month_allowed(months["ou_a"], "2026-09")
    # 未在名单内的用户不出现（本月从 0 开始）
    assert "ou_c" not in months
