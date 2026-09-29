"""点赞：日期桶、有效性、每日额度、转化率与汇总口径（bot 与 H5 共用）。

## 点赞和喜欢是两条线

喜欢会配对、会解锁聊天、有月度额度、能换麦穗；点赞**都不**。它是个轻动作：
匿名、不通知、不配对、不挂钩麦穗，数量只有本人可见。所以这里不 import quota，
也不写喜欢表的任何字段——两条线只在 H5 的按钮上碰一次面（点过赞 → 提示要不要
喜欢；已经喜欢过 → 不再显示点赞按钮）。

## 为什么日期/周写成文本字段

`创建时间` 是飞书自动字段，既不稳定也不好过滤。喜欢表为此专门有「归属月份」，
在受理那一刻把桶钉死（见 `lib/quota.month_key` 的注释）。点赞照抄这个口径：
受理时写 `归属日期`（YYYY-MM-DD）和 `归属周`（ISO 周 YYYY-Www），之后所有统计
都按这两个字段数，不回头读创建时间。

## 时区

复用 `lib/quota.now()`——它显式钉了 Asia/Shanghai。**不要用 `datetime.now()`**：
服务器时区没有任何配置保证，「今天」跑到北京时间早上 8 点会让每日额度凭空重置。
"""
from datetime import timedelta

from lib import quota

# 状态。表里的选项名，改表就要改这里——两处必须逐字一致。
PRAISE_STATUS_ACTIVE = "有效"
PRAISE_STATUS_CANCELLED = "已取消"

# 正向白名单：只有明确处于这个状态才算「一个赞」。
# 不要写成 `!= "已取消"` 的否定式——以后加状态（比如「已失效」）时，
# 否定式会静默把新状态当成有效，统计和额度悄悄算错而不报错。
PRAISE_STATUSES_ACTIVE = (PRAISE_STATUS_ACTIVE,)

# 点赞对象类型。现在只有「用户资料」；以后要赞问答内容时加一个选项即可，
# 内容 ID 写进「点赞对象ID」字段（见 create_praise_table.py）。
OBJECT_USER_PROFILE = "用户资料"
PRAISE_OBJECT_TYPES = (OBJECT_USER_PROFILE,)

# 用户表的性别选项名，**不是**「男」「女」。与 web/backend/app.py 的资料表单
# （'性别' 的 options）以及 bot/grouping.py 必须逐字一致。
# 写错成「男」「女」不会报错，只会让下面的分组统计把所有人都归进兜底那一类，
# 而正式的男女两行变成「没有账号正常的X性用户」——看着像没人用，其实全算错了。
GENDER_MALE = "男性"
GENDER_FEMALE = "女性"
GENDERS = (GENDER_MALE, GENDER_FEMALE)

# 每周汇总在周日的这个点之后发。见 `summary_week()`。
WEEKLY_SEND_WEEKDAY = 6          # 周一=0，周日=6
WEEKLY_SEND_HOUR = 20


def day_key(dt=None):
    """时间 → 日期桶 '2026-09-29'。不传则取今天。"""
    return (dt or quota.now()).strftime("%Y-%m-%d")


def week_key(dt=None):
    """时间 → 周桶 '2026-W40'（ISO 周，周一是第一天）。

    用 ISO 周而不是「距某天的天数 / 7」：跨年那周的编号是连续的，不会出现
    「2026-W00」或年末两周共用一个桶。与 `lib/recommend.week_key` 同口径。
    """
    return (dt or quota.now()).strftime("%G-W%V")


def is_praise_active(status):
    """这条点赞现在还算数吗？取消掉的不算，状态读空/没见过的也不算。"""
    return status in PRAISE_STATUSES_ACTIVE


def daily_left(records, limit, day=None):
    """今天还能点几个赞。

    records 是 (状态, 归属日期) 的可迭代，limit 来自配置
    （`points_config.get("daily_praise_limit")`）。

    **取消掉的赞不占次数**：口径是「每天最多有 10 个有效赞」，不是「最多点 10 下」。
    点赞匿名、不通知、不配对，刷次数没有任何收益，而误点一下就罚掉一次会让人
    不敢点。要改成「点过就算」，把 status 那个条件去掉即可。
    """
    d = day or day_key()
    used = 0
    for status, day_of in records:
        if day_of == d and is_praise_active(status):
            used += 1
    return max(0, int(limit) - used)


def received_by_week(records, week=None):
    """这一周每个人各收到几个赞 → {被点赞人open_id: 个数}。

    records 是 (状态, 归属周, 被点赞人open_id) 的可迭代。只数有效的。
    """
    wk = week or week_key()
    out = {}
    for status, week_of, target_oid in records:
        if week_of != wk or not is_praise_active(status) or not target_oid:
            continue
        out[target_oid] = out.get(target_oid, 0) + 1
    return out


def converted_ratio(praises, like_pairs):
    """点赞之后有没有转成喜欢 → (转成的条数, 点赞总条数, 比例)。

    `praises` 是 (点赞人open_id, 被点赞人open_id) 的可迭代（只传有效的）；
    `like_pairs` 是 {(点赞人, 被点赞人)} —— 这两个人之间**有过**喜欢即可，
    不看喜欢现在是不是还有效：用户点完赞去表白了，后来又取消/被驳回，
    那次「转化」确实发生过。

    比例为 0 条点赞时返回 0.0，不是除零。
    """
    pairs = list(praises)
    if not pairs:
        return 0, 0, 0.0
    hit = sum(1 for p in pairs if p in like_pairs)
    return hit, len(pairs), hit / len(pairs)


def received_share(users, received_oids):
    """收到过赞的用户占比，按性别分组 → {性别: {"total": n, "received": m, "ratio": r}}

    `users` 是 (open_id, 性别) 的可迭代（分母：账号正常且填了性别的用户）；
    `received_oids` 是收到过至少一个赞的 open_id 集合。

    分母由调用方决定——这里不认识「账号正常」是什么意思。口径要与「用户统计」
    指令一致，否则两个数字对不上，管理员会以为坏了一个。
    """
    out = {}
    for oid, gender in users:
        if not gender:
            continue
        slot = out.setdefault(gender, {"total": 0, "received": 0, "ratio": 0.0})
        slot["total"] += 1
        if oid in received_oids:
            slot["received"] += 1
    for slot in out.values():
        slot["ratio"] = slot["received"] / slot["total"] if slot["total"] else 0.0
    return out


def weekly_window_open(dt=None):
    """现在到发周汇总的点了吗（周日 20:00 之后）。"""
    d = dt or quota.now()
    return (d.weekday(), d.hour) >= (WEEKLY_SEND_WEEKDAY, WEEKLY_SEND_HOUR)


def summary_week(dt=None):
    """这次要汇总的是哪个周。

    周日 20:00 之后 → 本周（一周的赞刚收齐）。
    其余时间 → 上一个周：机器人周日晚上没跑起来的话，周一还能补发，
    发不出去的下周同一时刻之前也一直会重试（靠调用方的「这周发过了」记账去重）。
    """
    d = dt or quota.now()
    if weekly_window_open(d):
        return week_key(d)
    return week_key(d - timedelta(days=7))


def weekly_message(count):
    """每周私聊的正文。只说个数——点赞是匿名的，说不出是谁点的。"""
    return (f"👍 本周你收到了 {int(count)} 个赞\n\n"
            "有人注意到了你。点赞是匿名的，不会告诉你是谁点的。")
