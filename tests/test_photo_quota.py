"""照片上传每日限额纯逻辑单测：限额、跨日重置、独立用户与清理。"""
from lib.photo_quota import (
    PHOTO_DAILY_LIMIT,
    daily_allowed,
    daily_remaining,
    daily_used,
    prune_days,
    record_upload,
)


def test_first_n_allowed_then_block():
    rec = None
    for _ in range(PHOTO_DAILY_LIMIT):
        ok, rec = record_upload(rec, "2026-09-12")
        assert ok
    assert daily_used(rec, "2026-09-12") == PHOTO_DAILY_LIMIT
    assert daily_remaining(rec, "2026-09-12") == 0
    assert not daily_allowed(rec, "2026-09-12")
    ok, rec2 = record_upload(rec, "2026-09-12")
    assert not ok
    assert rec2["n"] == PHOTO_DAILY_LIMIT


def test_next_day_resets():
    rec = None
    for _ in range(PHOTO_DAILY_LIMIT):
        _, rec = record_upload(rec, "2026-09-12")
    assert not daily_allowed(rec, "2026-09-12")
    assert daily_allowed(rec, "2026-09-13")
    assert daily_used(rec, "2026-09-13") == 0


def test_custom_limit():
    ok, rec = record_upload(None, "2026-09-12", limit=1)
    assert ok
    assert not daily_allowed(rec, "2026-09-12", limit=1)


def test_prune_days_keeps_today_and_yesterday():
    days = {
        "ou_today": {"date": "2026-09-12", "n": 3},
        "ou_yesterday": {"date": "2026-09-11", "n": 9},
        "ou_old": {"date": "2026-09-01", "n": 1},
    }
    out = prune_days(days, "2026-09-12")
    assert "ou_today" in out
    assert "ou_yesterday" in out
    assert "ou_old" not in out


def test_normalize_bad_record():
    assert daily_used({"date": "2026-09-12", "n": "x"}, "2026-09-12") == 0
    assert daily_used(None, "2026-09-12") == 0
