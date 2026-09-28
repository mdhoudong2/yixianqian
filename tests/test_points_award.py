"""协助组织 / 管理员加减的单测（不联网）。

这两条是「人说了算」的路径，测试的重点不在算得对不对，而在**约束守没守住**：
单次上下限、每人每场上限、必填的活动和原因、扣穗留操作人。
"""
import pytest

from lib import points, points_award, points_config

ADMIN = "ou_admin"
USER = "ou_user"
ACT = "ACT-001"


def _seed(points_db, amount=100):
    """先给一笔底，免得测扣穗时撞上余额不足。"""
    points.add_entry(USER, amount, points.KIND_ADMIN, reason="测试底分")


# ---------------------------------------------------------------- 协助组织

def test_assist_writes_an_entry_tagged_with_the_activity(points_db):
    entry_id, after = points_award.grant_assist(
        USER, ACT, 30, "帮忙签到", operator_oid=ADMIN)
    assert after == 30
    row = points.entries(USER)[0]
    assert row["kind"] == points.KIND_ASSIST
    assert row["kind_label"] == "协助组织"
    assert row["ref_type"] == points_award.REF_TYPE_ACTIVITY
    assert row["ref_id"] == ACT
    assert row["operator_oid"] == ADMIN
    assert row["created_at"]                       # 时间由账本记，不由调用方传
    assert "帮忙签到" in row["reason"]


@pytest.mark.parametrize("amount", [9, 51])
def test_assist_outside_the_range_is_refused(points_db, amount):
    low = points_config.get("assist_min")
    high = points_config.get("assist_max")
    with pytest.raises(points_award.AwardError, match=f"{low}~{high}"):
        points_award.grant_assist(USER, ACT, amount, "帮忙")
    assert points.balance(USER) == 0


def test_assist_accumulates_up_to_the_per_activity_cap(points_db):
    """「同一人同一活动最多 50 穗」不能靠单次上限兜底——两次 30 单次都没超。"""
    cap = points_config.get("assist_activity_cap")
    points_award.grant_assist(USER, ACT, 30, "第一次")
    with pytest.raises(points_award.AwardError, match="超过上限"):
        points_award.grant_assist(USER, ACT, 30, "第二次")
    assert points_award.activity_assist_total(USER, ACT) == 30
    assert points.balance(USER) == 30
    assert points.balance(USER) <= cap


def test_the_cap_is_per_activity_not_per_person(points_db):
    points_award.grant_assist(USER, ACT, 50, "第一场")
    points_award.grant_assist(USER, "ACT-002", 50, "第二场")
    assert points.balance(USER) == 100


def test_the_cap_is_per_person_not_per_activity(points_db):
    points_award.grant_assist(USER, ACT, 50, "我")
    points_award.grant_assist("ou_other", ACT, 50, "别人")
    assert points.balance(USER) == 50


def test_a_refund_releases_the_activity_quota(points_db):
    """退还过的协助不该继续占着本场额度——否则「本场已满」会一直挡着，
    而账上其实一分钱没给。"""
    entry_id, _ = points_award.grant_assist(USER, ACT, 50, "帮忙")
    points.reverse_entry(entry_id, points.KIND_REFUND, reason="活动取消了")

    assert points_award.activity_assist_total(USER, ACT) == 0
    points_award.grant_assist(USER, ACT, 50, "重新帮忙")
    assert points.balance(USER) == 50


def test_a_partial_refund_releases_exactly_what_was_refunded(points_db):
    first, _ = points_award.grant_assist(USER, ACT, 30, "第一次")
    points_award.grant_assist(USER, ACT, 20, "第二次")
    points.reverse_entry(first, points.KIND_REFUND, reason="退掉第一笔")
    assert points_award.activity_assist_total(USER, ACT) == 20
    points_award.grant_assist(USER, ACT, 30, "补回来")   # 20 + 30 = 50，刚好到顶
    assert points_award.activity_assist_total(USER, ACT) == 50
    with pytest.raises(points_award.AwardError, match="超过上限"):
        points_award.grant_assist(USER, ACT, 10, "再来一点")


def test_a_redeem_elsewhere_is_not_mistaken_for_a_refund(points_db):
    """额度释放认的是「这条协助有没有被反向」，不是「这个数字有没有出现过」。"""
    _seed(points_db, 100)
    entry_id, _ = points_award.grant_assist(USER, ACT, 50, "帮忙")
    # 造一条 ref_id 恰好等于那个流水号、但来自别处的流水
    points.add_entry(USER, -20, points.KIND_REDEEM, reason="兑了个别的",
                     ref_type="redeem", ref_id=str(entry_id))
    assert points_award.activity_assist_total(USER, ACT) == 50


def test_assist_needs_an_activity(points_db):
    with pytest.raises(points_award.AwardError, match="活动"):
        points_award.grant_assist(USER, "", 30, "帮忙")
    with pytest.raises(points_award.AwardError, match="活动"):
        points_award.grant_assist(USER, "   ", 30, "帮忙")


def test_assist_needs_a_reason(points_db):
    with pytest.raises(points_award.AwardError, match="原因"):
        points_award.grant_assist(USER, ACT, 30, "")


def test_assist_needs_a_user(points_db):
    with pytest.raises(points_award.AwardError):
        points_award.grant_assist("", ACT, 30, "帮忙")


def test_assist_obeys_a_changed_config(points_db):
    points_config.set_value("assist_min", 20)
    points_config.set_value("assist_max", 20)
    with pytest.raises(points_award.AwardError):
        points_award.grant_assist(USER, ACT, 10, "帮忙")
    points_award.grant_assist(USER, ACT, 20, "帮忙")
    assert points.balance(USER) == 20


def test_the_same_assist_is_not_paid_twice(points_db):
    """管理员手滑发两遍同一条指令（或网络重试）。"""
    key = "assist:ACT-001:ou_user"
    points_award.grant_assist(USER, ACT, 30, "帮忙", idempotency_key=key)
    with pytest.raises(points.DuplicateEntry):
        points_award.grant_assist(USER, ACT, 30, "帮忙", idempotency_key=key)
    assert points.balance(USER) == 30


def test_an_idempotent_replay_does_not_eat_the_activity_quota(points_db):
    """重放被拒的那次没有落库，就不该占额度。"""
    key = "assist:ACT-001:ou_user"
    points_award.grant_assist(USER, ACT, 30, "帮忙", idempotency_key=key)
    with pytest.raises(points.DuplicateEntry):
        points_award.grant_assist(USER, ACT, 30, "帮忙", idempotency_key=key)
    assert points_award.activity_assist_total(USER, ACT) == 30


# ---------------------------------------------------------------- 管理员加减

def test_admin_can_add_and_subtract(points_db):
    points_award.adjust(USER, 50, "活动帮忙", operator_oid=ADMIN)
    points_award.adjust(USER, -20, "误发追回", operator_oid=ADMIN)

    assert points.balance(USER) == 30
    rows = points.entries(USER)
    assert [r["delta"] for r in rows] == [-20, 50]
    assert all(r["kind"] == points.KIND_ADMIN for r in rows)
    assert all(r["operator_oid"] == ADMIN for r in rows)


def test_subtracting_records_a_new_entry_and_leaves_the_original_alone(points_db):
    """扣穗不是撤销原来那条奖励。原流水一个字不改——
    「这笔麦穗怎么来的、又怎么没的」在明细里都查得到。"""
    entry_id, _ = points_award.adjust(USER, 50, "活动帮忙")
    points_award.adjust(USER, -50, "发错了")

    original = points.entries(USER)[-1]
    assert original["id"] == entry_id
    assert original["delta"] == 50                 # 还是 50，没被改成 0
    assert points.balance(USER) == 0
    assert len(points.entries(USER)) == 2


def test_admin_cannot_overdraw(points_db):
    _seed(points_db, 30)
    with pytest.raises(points.InsufficientBalance):
        points_award.adjust(USER, -50, "扣多了")
    assert points.balance(USER) == 30


def test_admin_adjust_needs_a_reason(points_db):
    with pytest.raises(points_award.AwardError, match="原因"):
        points_award.adjust(USER, 50, "")


def test_admin_adjust_refuses_zero(points_db):
    with pytest.raises(points_award.AwardError, match="0"):
        points_award.adjust(USER, 0, "什么都没发生")


def test_admin_adjust_needs_a_user(points_db):
    with pytest.raises(points_award.AwardError):
        points_award.adjust("", 50, "发错了")


def test_the_operator_and_time_are_always_on_the_record(points_db):
    """需求原文：「每次操作都记录操作人和时间」。时间由账本填，操作人靠调用方传——
    传空的话明细里就只剩一条来路不明的加穗，所以这里钉住它。"""
    points_award.adjust(USER, 10, "小奖励", operator_oid=ADMIN)
    row = points.entries(USER)[0]
    assert row["operator_oid"] == ADMIN
    assert len(row["created_at"]) == 19            # YYYY-mm-dd HH:MM:SS
