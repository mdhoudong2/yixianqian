"""点赞的纯逻辑：日期/周桶、有效性、每日额度、转化率、性别占比、汇总时机。

这些都是「算错了不会报错、只会悄悄给错数」的东西——比如把已取消的赞算进额度，
用户就会莫名其妙点不动；周桶算错，周汇总就会发两次或者一次都不发。
"""
from datetime import datetime, timedelta

from lib import praise, quota

TZ = quota.TZ


def _dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


# ---------------------------------------------------------------- 日期 / 周桶

def test_day_key_is_a_plain_date():
    assert praise.day_key(_dt("2026-09-29 23:59")) == "2026-09-29"


def test_week_key_uses_iso_weeks():
    """2026-09-28 是周一，到 10-04 是同一个 ISO 周；10-05 才换周。"""
    assert praise.week_key(_dt("2026-09-28 00:01")) == praise.week_key(_dt("2026-10-04 23:59"))
    assert praise.week_key(_dt("2026-10-05 00:01")) != praise.week_key(_dt("2026-10-04 23:59"))


def test_week_key_does_not_break_across_new_year():
    """ISO 周跨年是连续的（%G-W%V），不会冒出 2026-W00 这种桶。"""
    for d in ("2026-12-28 12:00", "2027-01-01 12:00", "2027-01-03 12:00"):
        wk = praise.week_key(_dt(d))
        assert wk[:4].isdigit() and "-W" in wk
    assert praise.week_key(_dt("2026-12-31 12:00")) == praise.week_key(_dt("2027-01-01 12:00"))


# ---------------------------------------------------------------- 有效性

def test_only_the_whitelisted_status_counts():
    """正向白名单。以后加状态时不能静默变成「有效」。"""
    assert praise.is_praise_active(praise.PRAISE_STATUS_ACTIVE)
    assert not praise.is_praise_active(praise.PRAISE_STATUS_CANCELLED)
    assert not praise.is_praise_active("")
    assert not praise.is_praise_active("已失效")


# ---------------------------------------------------------------- 每日额度

def test_daily_left_counts_todays_active_praises():
    recs = [(praise.PRAISE_STATUS_ACTIVE, "2026-09-29")] * 3
    assert praise.daily_left(recs, 10, day="2026-09-29") == 7


def test_cancelling_gives_the_slot_back():
    """取消掉的赞不占次数：口径是「每天最多 10 个有效赞」，不是「最多点 10 下」。"""
    recs = [(praise.PRAISE_STATUS_ACTIVE, "2026-09-29")] * 9
    recs.append((praise.PRAISE_STATUS_CANCELLED, "2026-09-29"))
    assert praise.daily_left(recs, 10, day="2026-09-29") == 1


def test_yesterdays_praises_do_not_count():
    """额度每天重置，按「归属日期」数，不按「最近 24 小时」。"""
    recs = [(praise.PRAISE_STATUS_ACTIVE, "2026-09-28")] * 10
    assert praise.daily_left(recs, 10, day="2026-09-29") == 10


def test_daily_left_never_goes_negative():
    """上限被管理员调小之后，昨天多点的那些不该变成负数。"""
    recs = [(praise.PRAISE_STATUS_ACTIVE, "2026-09-29")] * 15
    assert praise.daily_left(recs, 10, day="2026-09-29") == 0


# ---------------------------------------------------------------- 周汇总

def test_weekly_window_opens_sunday_evening():
    assert not praise.weekly_window_open(_dt("2026-10-04 19:59"))   # 周日 19:59
    assert praise.weekly_window_open(_dt("2026-10-04 20:00"))       # 周日 20:00
    assert praise.weekly_window_open(_dt("2026-10-04 23:59"))
    assert not praise.weekly_window_open(_dt("2026-10-05 09:00"))   # 周一早上


def test_summary_week_is_this_week_on_sunday_and_last_week_after():
    """周日晚上汇总本周；其余时间回看上一个周，这样机器人周日没跑也能补发。"""
    sun = _dt("2026-10-04 21:00")
    assert praise.summary_week(sun) == praise.week_key(sun)

    mon = _dt("2026-10-05 09:00")
    assert praise.summary_week(mon) == praise.week_key(sun)
    assert praise.summary_week(mon) != praise.week_key(mon)


def test_received_by_week_counts_only_this_week_and_only_active():
    recs = [
        (praise.PRAISE_STATUS_ACTIVE, "2026-W40", "ou_a"),
        (praise.PRAISE_STATUS_ACTIVE, "2026-W40", "ou_a"),
        (praise.PRAISE_STATUS_ACTIVE, "2026-W40", "ou_b"),
        (praise.PRAISE_STATUS_CANCELLED, "2026-W40", "ou_b"),   # 取消的不算
        (praise.PRAISE_STATUS_ACTIVE, "2026-W39", "ou_c"),      # 上周的不算
    ]
    assert praise.received_by_week(recs, week="2026-W40") == {"ou_a": 2, "ou_b": 1}


def test_weekly_message_says_the_count():
    msg = praise.weekly_message(3)
    assert "3" in msg and "匿名" in msg


# ---------------------------------------------------------------- 统计口径

def test_conversion_ratio_counts_pairs_that_also_liked():
    praises = [("ou_a", "ou_b"), ("ou_c", "ou_d"), ("ou_e", "ou_f")]
    likes = {("ou_a", "ou_b")}          # 只有 a 点完赞去喜欢了
    hit, total, ratio = praise.converted_ratio(praises, likes)
    assert (hit, total) == (1, 3)
    assert abs(ratio - 1 / 3) < 1e-9


def test_conversion_ratio_of_nothing_is_zero_not_a_crash():
    assert praise.converted_ratio([], set()) == (0, 0, 0.0)


def test_conversion_counts_a_like_that_was_later_cancelled():
    """点完赞去表白了，后来取消——那次转化确实发生过，不能回头不算。"""
    assert praise.converted_ratio([("ou_a", "ou_b")], {("ou_a", "ou_b")})[0] == 1


def test_received_share_splits_by_gender():
    users = [("ou_a", "男"), ("ou_b", "男"), ("ou_c", "女"), ("ou_d", "女")]
    share = praise.received_share(users, {"ou_a", "ou_c"})
    assert share["男"] == {"total": 2, "received": 1, "ratio": 0.5}
    assert share["女"] == {"total": 2, "received": 1, "ratio": 0.5}


def test_received_share_ignores_users_without_a_gender():
    """没填性别的人进不了分母——否则男女两个比例都算不准。"""
    share = praise.received_share([("ou_a", "男"), ("ou_b", "")], {"ou_b"})
    assert share["男"]["total"] == 1 and share["男"]["received"] == 0
    assert "" not in share


def test_received_share_of_an_empty_gender_is_zero_not_a_crash():
    share = praise.received_share([("ou_a", "男")], set())
    assert share["男"]["ratio"] == 0.0


def test_the_gender_option_names_are_the_ones_the_user_table_uses():
    """「男」「女」是这组用例里随手写的假数据，用户表里根本没有这两个选项。

    `received_share` 按传进来的字符串分组，写错它不会报错——只会让「点赞统计」
    的男女两行都空着、所有人落进兜底那一类。所以这里把真选项名钉住。
    """
    assert praise.GENDERS == ("男性", "女性")
    assert (praise.GENDER_MALE, praise.GENDER_FEMALE) == praise.GENDERS


# ---------------------------------------------------------------- 和 quota 的时区一致

def test_day_key_uses_the_same_clock_as_the_heart_quota():
    """两处「今天」必须同源。分开取 now() 的话，跨零点前后会各自算出不同的日子。

    传同一个时刻进去（而不是各取一次 now()），否则测试自己会在零点前后闪。
    """
    now = quota.now()
    assert praise.day_key(now) == now.strftime("%Y-%m-%d")
    assert praise.week_key(now) == now.strftime("%G-W%V")
    # 和喜欢表的「归属月份」是同一套 now()
    assert praise.day_key(now)[:7] == quota.month_key(now)


def test_a_week_always_contains_seven_day_keys():
    start = _dt("2026-09-28 12:00")
    days = {praise.day_key(start + timedelta(days=i)) for i in range(7)}
    weeks = {praise.week_key(start + timedelta(days=i)) for i in range(7)}
    assert len(days) == 7 and len(weeks) == 1
