"""优先名额 / 费用减免的单子怎么收尾（bot/auto_tasks 的周期结算）。

这一层考的是**兑现**：兑换那一刻只是开单扣穗，真正「报上名」「退穗」都发生在
机器人循环里。少了它，用户花 30 穗买到的就是一张没人管的单子。

多维表格全部打桩——这些用例要钉住的是「什么状态做什么动作」，不是飞书接口。
"""
import auto_tasks
from constants import (
    ACTIVITY_STATUS_CANCELLED,
    STATUS_ACTIVITY_FINISHED,
    STATUS_SIGNUP_FULL,
    STATUS_SIGNUP_NOT_STARTED,
    STATUS_SIGNUP_OPEN,
)
from lib import points, points_redeem

ACT_ID = "A-0001"


def _activity(status):
    return {"record_id": "rec_act",
            "fields": {"活动ID": ACT_ID, "活动名称": "周末桌游", "活动状态": status,
                       "报名人数上限": 10}}


def _order(points_db, item=points_redeem.ITEM_PRIORITY, status=STATUS_SIGNUP_OPEN,
           fee=0):
    """给账本里造一张「已生效」的单子，返回它。"""
    points.grant("ou_me", 100, points.KIND_ADMIN, reason="测试预置")
    act = {"id": ACT_ID, "title": "周末桌游", "quota": 10, "fee": fee,
           "start_at": "2026-10-01 19:00:00", "open": True}
    if item == points_redeem.ITEM_PRIORITY:
        return points_redeem.redeem_priority("ou_me", act)
    return points_redeem.redeem_fee_discount("ou_me", act)


def _stub(monkeypatch, status, *, signups=None, search_ok=True):
    """打桩活动查询与报名表。`signups` 是报名表里已有的记录。"""
    monkeypatch.setattr(auto_tasks, "find_activity_by_id",
                        lambda aid: _activity(status) if aid == ACT_ID else None)
    written = []
    monkeypatch.setattr(auto_tasks, "create_record",
                        lambda tid, fields: written.append(fields) or {"record_id": "r1"})
    monkeypatch.setattr(auto_tasks.bitable, "search_records",
                        lambda *a, **k: (signups or []) if search_ok else None)
    monkeypatch.setattr(auto_tasks, "_nickname_of", lambda oid: "猴哥")
    monkeypatch.setattr(auto_tasks, "send_text_message", lambda oid, t: True)
    return written


def test_registration_open_signs_the_holder_up(points_db, monkeypatch):
    """活动开始报名 → 替他报上名。这是这 30 穗买到的东西。"""
    _order(points_db)
    written = _stub(monkeypatch, STATUS_SIGNUP_OPEN)

    assert auto_tasks.auto_settle_priority_orders() == (1, 0)
    assert len(written) == 1
    assert written[0]["活动ID"] == ACT_ID
    assert written[0]["报名人open_id"] == "ou_me"
    assert written[0]["状态"] == "已报名"
    assert points.balance("ou_me") == 70          # 穗照扣，买的就是这个名额


def test_a_full_activity_still_takes_the_holder(points_db, monkeypatch):
    """满员也报——「每场优先名额 ≤ 总名额 30%」已经把超出的人头框住了。"""
    _order(points_db)
    written = _stub(monkeypatch, STATUS_SIGNUP_FULL)

    assert auto_tasks.auto_settle_priority_orders() == (1, 0)
    assert len(written) == 1


def test_nothing_happens_before_registration_opens(points_db, monkeypatch):
    """「未开始报名」只是等——这时候报上名是抢跑。"""
    _order(points_db)
    written = _stub(monkeypatch, STATUS_SIGNUP_NOT_STARTED)

    assert auto_tasks.auto_settle_priority_orders() == (0, 0)
    assert written == []
    assert points.balance("ou_me") == 70


def test_the_holder_is_not_signed_up_twice(points_db, monkeypatch):
    """他自己已经报过名了（或上一轮报过）→ 不重复写。"""
    _order(points_db)
    written = _stub(monkeypatch, STATUS_SIGNUP_OPEN, signups=[
        {"fields": {"活动ID": ACT_ID, "报名人open_id": "ou_me", "状态": "已报名"}}])

    assert auto_tasks.auto_settle_priority_orders() == (0, 0)
    assert written == []


def test_a_search_failure_does_not_write_a_duplicate(points_db, monkeypatch):
    """查报名表失败时**不能**当成「他没报过」——那会写下第二条报名记录。"""
    _order(points_db)
    written = _stub(monkeypatch, STATUS_SIGNUP_OPEN, search_ok=False)

    assert auto_tasks.auto_settle_priority_orders() == (0, 0)
    assert written == []


def test_a_cancelled_activity_refunds(points_db, monkeypatch):
    """活动取消 → 退穗（新增一笔反向流水，余额自己就对了）。"""
    order = _order(points_db)
    _stub(monkeypatch, ACTIVITY_STATUS_CANCELLED)

    assert auto_tasks.auto_settle_priority_orders() == (0, 1)
    assert points.balance("ou_me") == 100
    assert points_redeem.get(order["id"])["status"] == points_redeem.ST_REFUNDED


def test_a_finished_activity_refunds_only_if_we_never_got_them_in(points_db,
                                                                 monkeypatch):
    """活动结束了还没报上名 → 我们没交付，退穗。

    注意这里查的是「**有没有过**报名记录」，不是「现在是不是已报名」：他报上
    之后又自己取消了，名额在活动方那边已经被占过，不能再退一次穗。
    """
    order = _order(points_db)
    _stub(monkeypatch, STATUS_ACTIVITY_FINISHED)

    assert auto_tasks.auto_settle_priority_orders() == (0, 1)
    assert points.balance("ou_me") == 100
    assert points_redeem.get(order["id"])["status"] == points_redeem.ST_REFUNDED


def test_a_finished_activity_keeps_the_points_when_they_had_a_spot(points_db,
                                                                  monkeypatch):
    """报上过（哪怕后来取消了 / 没到场）→ 名额用掉了，按规则不退，只把单子收尾。"""
    order = _order(points_db)
    _stub(monkeypatch, STATUS_ACTIVITY_FINISHED, signups=[
        {"fields": {"活动ID": ACT_ID, "报名人open_id": "ou_me", "状态": "已取消"}}])

    assert auto_tasks.auto_settle_priority_orders() == (0, 0)
    assert points.balance("ou_me") == 70
    assert points_redeem.get(order["id"])["status"] == points_redeem.ST_DONE


def test_a_missing_activity_is_left_for_a_human(points_db, monkeypatch):
    """活动被删了（或表 ID 配错）→ 留着单子记日志，别自作主张退穗。"""
    order = _order(points_db)
    _stub(monkeypatch, STATUS_SIGNUP_OPEN)
    monkeypatch.setattr(auto_tasks, "find_activity_by_id", lambda aid: None)

    assert auto_tasks.auto_settle_priority_orders() == (0, 0)
    assert points_redeem.get(order["id"])["status"] == points_redeem.ST_ACTIVE


# ---------------------------------------------------------------- 费用减免

def test_a_cancelled_activity_refunds_the_fee_discount(points_db, monkeypatch):
    """活动取消 = 全额退款 → 减免的穗要还回去。"""
    order = _order(points_db, item=points_redeem.ITEM_FEE_DISCOUNT, fee=100)
    _stub(monkeypatch, ACTIVITY_STATUS_CANCELLED)

    assert auto_tasks.auto_settle_fee_discounts() == (1, 0)
    assert points.balance("ou_me") == 100
    assert points_redeem.get(order["id"])["status"] == points_redeem.ST_REFUNDED


def test_a_finished_activity_closes_the_fee_discount_without_refunding(points_db,
                                                                      monkeypatch):
    """活动办完了，减免就是真用掉了，不退；只把单子从「生效中」收掉。"""
    order = _order(points_db, item=points_redeem.ITEM_FEE_DISCOUNT, fee=100)
    _stub(monkeypatch, STATUS_ACTIVITY_FINISHED)

    assert auto_tasks.auto_settle_fee_discounts() == (0, 1)
    assert points.balance("ou_me") == 60
    assert points_redeem.get(order["id"])["status"] == points_redeem.ST_DONE


def test_an_open_activity_leaves_the_fee_discount_alone(points_db, monkeypatch):
    """活动正常进行中 → 不动。"""
    order = _order(points_db, item=points_redeem.ITEM_FEE_DISCOUNT, fee=100)
    _stub(monkeypatch, STATUS_SIGNUP_OPEN)

    assert auto_tasks.auto_settle_fee_discounts() == (0, 0)
    assert points_redeem.get(order["id"])["status"] == points_redeem.ST_ACTIVE
