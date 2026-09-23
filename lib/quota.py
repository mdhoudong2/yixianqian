"""月度额度：月份桶、喜欢有效性判定、额度计算（bot 与 H5 共用）。

## 为什么用「月份桶」而不是天数差

`当前月 − 记录月 >= 3` 这样的字符串桶比较，而不是 `timedelta(days=90)`。
月份天数不等，用 90/91/92 天会让 2 月 1 日的喜欢到 5 月 2 日才到期，用户觉得随机。
这与 `lib/photo_quota.py` 的既有做法一致。

**歧义已定**：1 月 31 日的喜欢，按桶算在 4 月 1 日到期（不是 5 月 1 日）。

## 为什么显式钉时区

服务器当前恰好是 Asia/Shanghai，但没有任何代码或 systemd 配置保证这一点
（`yixianqian.service` 里只有 `PYTHONUNBUFFERED=1`）。换机器或改配置就会
静默偏移 8 小时——月界跑到北京时间早上 8 点，而且本地开发与线上不一致会让
测试全过、生产全错。所以这里显式指定，不依赖系统时区。

## 额度模型

    匿名剩余 = max(0, 10 + 本月到期返还数 − 本月发起的有效匿名喜欢数)
    实名剩余 = max(0, 1 + 永久名额 − 本月发起的有效实名喜欢数)

「不累积」体现在：月初额度恒为 10，上个月剩多少不带过来。
「到期返还」体现在：3 个月前发起的匿名单向喜欢在本月失效，退回 1 颗。
"""
import time
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 无 tzdata 的极端环境退化为系统时区
    TZ = None

# 每月匿名喜欢额度。月初补满，不累积。
MONTHLY_ANON_HEARTS = 10
# 每月实名喜欢基础次数（另有邀请/加赠得来的永久名额）。
MONTHLY_REAL_HEARTS = 1
# 匿名单向喜欢几个月后静默作废。实名喜欢不作废（记录永久保留）。
ANON_EXPIRE_MONTHS = 3

LIKE_STATUS_SINGLE = "单向喜欢"
LIKE_STATUS_MUTUAL = "相互喜欢"
# 被驳回：机器人拦下的无效喜欢（重复 / 自喜欢 / 昵称撞车无法定人）。
# 这条对应表里的选项名，改表里的选项就要改这里——两处必须逐字一致。
LIKE_STATUS_REJECTED = "被驳回"

# 正向白名单：只有明确处于这两个状态才算「有效喜欢」。
# 不要写成 `!= "被驳回"` 这种否定式——以后再加状态（比如到期作废）时，
# 否定式会静默把新状态当成有效，数值悄悄算错而不报错。
LIKE_STATUS_ACTIVE = (LIKE_STATUS_SINGLE, LIKE_STATUS_MUTUAL)

LIKE_TYPE_ANON = "匿名"
LIKE_TYPE_REAL = "实名"


def now():
    """当前时间（显式时区）。"""
    return datetime.now(TZ) if TZ else datetime.now()


def month_key(dt=None):
    """时间 → 月份桶 '2026-09'。不传则取当前月。"""
    return (dt or now()).strftime("%Y-%m")


def month_add(ym, n):
    """月份桶加减：('2026-01', 1) → '2026-02'，n 可为负。"""
    try:
        y, m = (int(x) for x in str(ym).split("-")[:2])
    except (ValueError, AttributeError):
        return ""
    m += n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def months_between(a, b):
    """b 比 a 晚几个月（b − a）。同月为 0，b 早于 a 为负。无法解析返回 None。"""
    try:
        ya, ma = (int(x) for x in str(a).split("-")[:2])
        yb, mb = (int(x) for x in str(b).split("-")[:2])
    except (ValueError, AttributeError):
        return None
    return (yb - ya) * 12 + (mb - ma)


def is_like_active(status, like_type, month, ym=None):
    """这条喜欢现在还算数吗？

    - 状态不在白名单（被驳回 / 空 / 没见过的值）→ 不算数
    - 相互喜欢 → 永远算数（配对成功，不作废）
    - 实名喜欢 → 永远算数（产品规则：记录不变，只按月的名额会翻新）
    - 匿名单向喜欢 → 满 ANON_EXPIRE_MONTHS 个月后静默作废

    month 读不到时**保守保留**：宁可留着一条过期记录，也不要把有效记录
    静默作废——后者会让用户的额度和配对结果凭空变化。调用方应另行告警。
    """
    if status not in LIKE_STATUS_ACTIVE:
        return False
    if status == LIKE_STATUS_MUTUAL:
        return True
    if like_type != LIKE_TYPE_ANON:
        return True
    if not month:
        return True
    gap = months_between(month, ym or month_key())
    if gap is None:
        return True
    return gap < ANON_EXPIRE_MONTHS


def anon_left(records, ym=None):
    """本月匿名剩余额度。records 为 (状态, 喜欢类型, 归属月份) 三元组的可迭代。"""
    ym = ym or month_key()
    used = 0
    refunded = 0
    for status, like_type, month in records:
        if like_type != LIKE_TYPE_ANON:
            continue
        if status not in LIKE_STATUS_ACTIVE:
            continue
        gap = months_between(month, ym) if month else None
        if gap == 0:
            used += 1
        elif gap == ANON_EXPIRE_MONTHS:
            # 恰好在「本月」满期：退回一颗。只在满期当月退一次，
            # 用 == 而不是 >=，否则过期记录会在之后每个月反复退。
            refunded += 1
    return max(0, MONTHLY_ANON_HEARTS + refunded - used)


def anon_used(records, ym=None):
    """本月已用掉的匿名额度（供展示「已用 N / 10」）。"""
    ym = ym or month_key()
    return sum(1 for status, like_type, month in records
               if like_type == LIKE_TYPE_ANON
               and status in LIKE_STATUS_ACTIVE
               and month == ym)


def real_left(records, permanent=0, ym=None):
    """本月实名剩余次数。permanent = 邀请 + 管理员加赠得来的永久名额。

    注意：实名喜欢不过期，「一个月后返还名额」就是月初自然翻新——
    计数只看本月，不需要额外的返还逻辑。
    """
    ym = ym or month_key()
    used = sum(1 for status, like_type, month in records
               if like_type == LIKE_TYPE_REAL
               and status in LIKE_STATUS_ACTIVE
               and month == ym)
    return max(0, MONTHLY_REAL_HEARTS + int(permanent or 0) - used)


def real_total(permanent=0):
    """实名总额度 = 每月基础 1 次 + 永久名额。"""
    return MONTHLY_REAL_HEARTS + int(permanent or 0)


def stamp():
    """额度文件的更新时间戳。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")
