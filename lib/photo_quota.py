"""照片上传每日限额：每个用户每自然日最多上传 N 次（跨日自动重置）。

背景：图片标签/静默活体接口按月计费，需要防止单账号反复上传刷额度。
本模块只放纯函数（无外部依赖，可单测）；持久化与接口接线见 web/backend/app.py。

配额文件格式：{"v": 1, "days": {open_id: {"date": "2026-09-12", "n": 2}}}
"""

import datetime as _dt

PHOTO_DAILY_LIMIT = 10
QUOTA_FILE_VERSION = 1


def normalize(rec, date):
    """把记录对齐到指定日期：非当日一律视为当日 0 次。"""
    if isinstance(rec, dict) and rec.get("date") == date:
        try:
            n = int(rec.get("n", 0))
        except Exception:
            n = 0
        return {"date": date, "n": max(0, n)}
    return {"date": date, "n": 0}


def daily_used(rec, date):
    return normalize(rec, date)["n"]


def daily_remaining(rec, date, limit=PHOTO_DAILY_LIMIT):
    return max(0, limit - daily_used(rec, date))


def daily_allowed(rec, date, limit=PHOTO_DAILY_LIMIT):
    return daily_used(rec, date) < limit


def record_upload(rec, date, limit=PHOTO_DAILY_LIMIT):
    """记一次上传。返回 (ok, 新记录)：超限时 ok=False 且次数不再增加。"""
    cur = normalize(rec, date)
    if cur["n"] >= limit:
        return False, cur
    cur["n"] += 1
    return True, cur


def prune_days(days, today):
    """只保留今天和昨天的条目，避免配额文件无限增长。"""
    try:
        cutoff = (_dt.date.fromisoformat(today) - _dt.timedelta(days=1)).isoformat()
    except Exception:
        cutoff = today
    out = {}
    for oid, rec in (days or {}).items():
        d = (rec or {}).get("date")
        if isinstance(d, str) and d >= cutoff:
            out[oid] = rec
    return out
