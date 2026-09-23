"""月度额度模型：月份桶、喜欢有效性、额度计算。

这些是纯函数，不需要多维表格就能跑。额度算错会直接导致用户少点几次喜欢
或者白赚额度，而且不报错，所以边界要钉死。
"""
from lib.quota import (
    ANON_EXPIRE_MONTHS,
    LIKE_STATUS_MUTUAL,
    LIKE_STATUS_REJECTED,
    LIKE_STATUS_SINGLE,
    MONTHLY_ANON_HEARTS,
    anon_left,
    anon_used,
    is_like_active,
    month_add,
    month_key,
    months_between,
    real_left,
    real_total,
)

# (状态, 喜欢类型, 归属月份)
ACTIVE_ANON_NOW = (LIKE_STATUS_SINGLE, "匿名", "2026-09")
ACTIVE_REAL_NOW = (LIKE_STATUS_SINGLE, "实名", "2026-09")


# ==================== 月份桶 ====================

def test_month_add_crosses_year():
    assert month_add("2026-01", 1) == "2026-02"
    assert month_add("2026-12", 1) == "2027-01"
    assert month_add("2026-01", -1) == "2025-12"
    assert month_add("2026-09", 3) == "2026-12"
    assert month_add("2026-11", 3) == "2027-02"
    assert month_add("", 1) == ""


def test_months_between():
    assert months_between("2026-09", "2026-09") == 0
    assert months_between("2026-09", "2026-12") == 3
    assert months_between("2026-12", "2027-03") == 3
    assert months_between("2026-12", "2026-09") == -3
    assert months_between("", "2026-09") is None


# ==================== 喜欢有效性 ====================

def test_cancelled_and_unknown_status_is_inactive():
    """被驳回、空状态、没见过的值都不算数。

    「被驳回」行会永久留在表里（bot 的同名/自喜欢/重复拦截仍会产出），
    新公式必须继续排除它们，否则那些记录会复活成有效喜欢。
    """
    assert is_like_active(LIKE_STATUS_REJECTED, "匿名", "2026-09", "2026-09") is False
    assert is_like_active("", "匿名", "2026-09", "2026-09") is False
    assert is_like_active("已作废", "匿名", "2026-09", "2026-09") is False


def test_mutual_like_never_expires():
    """配对成功就永久有效，不受到期规则影响。"""
    assert is_like_active(LIKE_STATUS_MUTUAL, "匿名", "2020-01", "2026-09") is True


def test_real_like_never_expires():
    """实名喜欢不作废——产品规则是「记录不变」，只有按月的名额会翻新。"""
    assert is_like_active(LIKE_STATUS_SINGLE, "实名", "2020-01", "2026-09") is True


def test_anon_like_expires_after_three_months():
    assert is_like_active(LIKE_STATUS_SINGLE, "匿名", "2026-09", "2026-09") is True
    assert is_like_active(LIKE_STATUS_SINGLE, "匿名", "2026-08", "2026-09") is True
    assert is_like_active(LIKE_STATUS_SINGLE, "匿名", "2026-07", "2026-09") is True
    assert is_like_active(LIKE_STATUS_SINGLE, "匿名", "2026-06", "2026-09") is False
    assert is_like_active(LIKE_STATUS_SINGLE, "匿名", "2025-09", "2026-09") is False


def test_jan_31_like_expires_apr_1_by_bucket():
    """按桶比较的歧义已定：跨年也算，1 月的喜欢 4 月到期。"""
    assert is_like_active(LIKE_STATUS_SINGLE, "匿名", "2026-01", "2026-03") is True
    assert is_like_active(LIKE_STATUS_SINGLE, "匿名", "2026-01", "2026-04") is False


def test_missing_month_keeps_like_active():
    """读不到月份时保守保留：宁可留一条过期记录，也不要静默作废有效记录。"""
    assert is_like_active(LIKE_STATUS_SINGLE, "匿名", "", "2026-09") is True
    assert is_like_active(LIKE_STATUS_SINGLE, "匿名", None, "2026-09") is True


# ==================== 匿名额度 ====================

def test_fresh_month_starts_full():
    assert anon_left([], "2026-09") == MONTHLY_ANON_HEARTS
    # 上个月用剩的不带过来：上月用了 7 次，本月照样满额
    assert anon_left([(LIKE_STATUS_SINGLE, "匿名", "2026-08")], "2026-09") == 10


def test_used_this_month_deducts():
    recs = [(LIKE_STATUS_SINGLE, "匿名", "2026-09")] * 4
    assert anon_left(recs, "2026-09") == 6
    assert anon_used(recs, "2026-09") == 4


def test_expiry_refunds_quota_in_that_month_only():
    """3 个月前发起的匿名喜欢在本月失效，退回一颗——但只在满期当月退一次。"""
    old = (LIKE_STATUS_SINGLE, "匿名", "2026-06")
    assert anon_left([old], "2026-09") == 11        # 满 3 个月，退 1 颗
    assert anon_left([old], "2026-10") == 10        # 不能每个月都退
    assert anon_left([old], "2026-12") == 10


def test_refund_can_exceed_monthly_quota():
    """退回的是额度的「退还」，不是「新发」，所以能超过 10。

    场景：本月已点满 10 次，此时 3 个月前那批喜欢满期退回 3 颗。
    """
    used = [(LIKE_STATUS_SINGLE, "匿名", "2026-09")] * 10
    expired = [(LIKE_STATUS_SINGLE, "匿名", "2026-06")] * 3
    assert anon_left(used + expired, "2026-09") == 3


def test_cancelled_likes_do_not_consume_quota():
    """「被驳回」行不占额度，也不产生退回。"""
    recs = [(LIKE_STATUS_REJECTED, "匿名", "2026-09"),
            (LIKE_STATUS_REJECTED, "匿名", "2026-06")]
    assert anon_left(recs, "2026-09") == 10


def test_real_likes_do_not_touch_anon_quota():
    """10 颗是匿名专用，实名不占。"""
    assert anon_left([ACTIVE_REAL_NOW], "2026-09") == 10


def test_mutual_likes_still_consume_the_month_they_were_sent():
    """配对成功不退还当月额度——额度是「点出去」时消耗的。"""
    assert anon_left([(LIKE_STATUS_MUTUAL, "匿名", "2026-09")], "2026-09") == 9


# ==================== 实名额度 ====================

def test_real_quota_base_and_permanent():
    assert real_left([], 0, "2026-09") == 1
    assert real_left([], 3, "2026-09") == 4
    assert real_total(3) == 4


def test_real_quota_refreshes_monthly():
    """「一个月后返还名额」就是月初自然翻新，不需要额外逻辑。"""
    assert real_left([ACTIVE_REAL_NOW], 0, "2026-09") == 0
    assert real_left([ACTIVE_REAL_NOW], 0, "2026-10") == 1


def test_real_quota_ignores_anon_usage():
    recs = [(LIKE_STATUS_SINGLE, "匿名", "2026-09")] * 5
    assert real_left(recs, 2, "2026-09") == 3


def test_expired_anon_like_does_not_consume_anon_quota():
    """满 3 个月的匿名喜欢既不计入已用、也不计入退回（不是本月发的）。"""
    expired = (LIKE_STATUS_SINGLE, "匿名", "2026-06")
    assert anon_used([expired], "2026-09") == 0
    assert anon_left([expired], "2026-09") == 10 + 1


def test_constants_are_sane():
    assert MONTHLY_ANON_HEARTS == 10
    assert ANON_EXPIRE_MONTHS == 3
    assert month_key()  # 时区可解析，不会抛
