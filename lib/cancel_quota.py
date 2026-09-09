"""取消喜欢配额：每自然月最多 3 次（跨月自动重置）。

背景：匿名喜欢 + 相互揭晓机制下，“喜欢→看是否相互→取消”可零成本试探谁喜欢自己，
因此对“取消喜欢”按自然月限次。本模块只放纯函数（无外部依赖，可单测）；
持久化与接口接线见 web/backend/app.py。

配额文件格式：{"v": 2, "months": {open_id: {"ym": "2026-09", "n": 2}}}
"""

CANCEL_QUOTA_LIMIT = 3
QUOTA_FILE_VERSION = 2

# 目标用户ID：上线时本月配额直接写满（历史高频喜欢/取消，疑似扫池试探）
QUOTA_EXHAUST_USER_IDS = ("U-0009",)


def normalize(rec, ym):
    """把记录对齐到指定月份：非本月一律视为本月 0 次。"""
    if isinstance(rec, dict) and rec.get("ym") == ym:
        try:
            n = int(rec.get("n", 0))
        except Exception:
            n = 0
        return {"ym": ym, "n": max(0, n)}
    return {"ym": ym, "n": 0}


def month_used(rec, ym):
    return normalize(rec, ym)["n"]


def month_remaining(rec, ym, limit=CANCEL_QUOTA_LIMIT):
    return max(0, limit - month_used(rec, ym))


def month_allowed(rec, ym, limit=CANCEL_QUOTA_LIMIT):
    return month_used(rec, ym) < limit


def record_month_cancel(rec, ym, limit=CANCEL_QUOTA_LIMIT):
    """记一次本月取消。返回 (ok, 新记录)：超限时 ok=False 且次数不再增加。"""
    cur = normalize(rec, ym)
    if cur["n"] >= limit:
        return False, cur
    cur["n"] += 1
    return True, cur


def seed_exhausted(force_oids, ym, limit=CANCEL_QUOTA_LIMIT):
    """首建配额文件：把指定用户本月配额直接写满（其余用户本月从 0 开始）。"""
    months = {}
    for oid in (force_oids or ()):
        if oid:
            months[oid] = {"ym": ym, "n": limit}
    return months
