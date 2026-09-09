"""取消喜欢配额：滚动 7 天最多 3 次。

背景：匿名喜欢 + 相互揭晓机制下，“喜欢→看是否相互→取消”可零成本试探谁喜欢自己。
本模块只放纯函数（无外部依赖，可单测）；持久化与接口接线见 web/backend/app.py。

配额文件格式：{"v": 1, "cancels": {open_id: [unix_ts, ...]}}
"""

CANCEL_QUOTA_LIMIT = 3
CANCEL_QUOTA_WINDOW_S = 7 * 86400
QUOTA_FILE_VERSION = 1

# 目标用户ID：上线时配额直接写满（历史高频喜欢/取消，疑似试探）
QUOTA_EXHAUST_USER_IDS = ("U-0009",)


def windowed(records, now, window_s=CANCEL_QUOTA_WINDOW_S):
    """窗口内的时间戳（过期自动丢弃；容忍 60s 时钟偏移）"""
    return [ts for ts in (records or []) if now - window_s < ts <= now + 60]


def quota_used(records, now, window_s=CANCEL_QUOTA_WINDOW_S):
    return len(windowed(records, now, window_s))


def quota_remaining(records, now, limit=CANCEL_QUOTA_LIMIT,
                    window_s=CANCEL_QUOTA_WINDOW_S):
    return max(0, limit - quota_used(records, now, window_s))


def quota_allowed(records, now, limit=CANCEL_QUOTA_LIMIT,
                  window_s=CANCEL_QUOTA_WINDOW_S):
    return quota_used(records, now, window_s) < limit


def record_cancel(records, now, limit=CANCEL_QUOTA_LIMIT,
                  window_s=CANCEL_QUOTA_WINDOW_S):
    """记一次取消。返回 (ok, 新列表)：超限时 ok=False 且只做裁剪不追加。"""
    pruned = windowed(records, now, window_s)
    if len(pruned) >= limit:
        return False, pruned
    return True, pruned + [now]


def seed_cancels(initiator_to_count, force_exhausted_oids, now,
                 limit=CANCEL_QUOTA_LIMIT):
    """首建配额文件：历史已取消按发起人折算（封顶 limit 次）+ 指定用户强制写满。

    历史表没有取消时间，只能按“发生在窗口内”计入；窗口最长 7 天后自然过期。
    """
    cancels = {}
    for oid, count in (initiator_to_count or {}).items():
        if not oid:
            continue
        n = max(0, min(int(count or 0), limit))
        if n:
            cancels[oid] = [now] * n
    for oid in force_exhausted_oids or ():
        if oid:
            cancels[oid] = [now] * limit
    return cancels
