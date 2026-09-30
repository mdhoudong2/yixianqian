"""兑换单测：扣穗、开单、退还条件、并发不重复扣分（SQLite 临时库，不联网）。

需求点名要验的三条在这里：余额不足不能兑、各兑换项的退还条件、
并发兑换不重复扣分。
"""
import threading

import pytest

from lib import points, points_config, points_redeem

USER = "ou_user"
OTHER = "ou_other"
ACT = "A-0001"


def _fund(points_db, oid=USER, amount=1000):
    points.add_entry(oid, amount, points.KIND_ADMIN, reason="测试底分")


def _act(**kw):
    """调用方传进来的活动事实，形状见 points_redeem._activity。"""
    base = {"id": ACT, "title": "周末桌游", "quota": 10,
            "start_at": "2026-10-01 14:00:00", "open": True}
    base.update(kw)
    return base


def _run_threads(fn, n):
    """n 个线程尽量同时起跑（Barrier 而不是 sleep，否则并发根本没被测到）。

    与 tests/test_points_ledger.py 里同名的是同一个手法，两处各留一份是为了
    让每个测试文件自洽可读。
    """
    barrier = threading.Barrier(n)

    def wrapper():
        barrier.wait()
        fn()

    threads = [threading.Thread(target=wrapper) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


# ---------------------------------------------------------------- 扣穗与开单

def test_a_redemption_writes_the_entry_and_the_order_together(points_db):
    _fund(points_db)
    order = points_redeem.redeem_real_like(USER)

    assert order["item"] == points_redeem.ITEM_REAL_LIKE
    assert order["item_label"] == "额外实名喜欢"
    assert order["cost"] == 20
    assert order["status"] == points_redeem.ST_ACTIVE
    assert order["ledger_id"]

    entry = points.entries(USER)[0]
    assert entry["id"] == order["ledger_id"]
    assert entry["delta"] == -20
    assert entry["kind"] == points.KIND_REDEEM
    assert points.balance(USER) == 980


def test_a_redemption_without_enough_points_is_refused(points_db):
    _fund(points_db, amount=5)
    with pytest.raises(points.InsufficientBalance):
        points_redeem.redeem_real_like(USER)

    assert points.balance(USER) == 5
    assert points_redeem.list_for(USER) == []
    assert len(points.entries(USER)) == 1          # 只有底分那一条


def test_an_empty_account_cannot_redeem_anything(points_db):
    """需求原文：「账户余额不足时不能兑换」。"""
    with pytest.raises(points.InsufficientBalance):
        points_redeem.redeem_real_like(USER)


def test_being_unable_to_afford_it_is_reported_before_being_ineligible(points_db):
    """两个条件都不满足时报「兑不起」：用户能动手解决的只有这一个。"""
    with pytest.raises(points.InsufficientBalance):
        points_redeem.redeem_priority(USER, _act(quota=0))


def test_every_item_has_a_price_from_config(points_db):
    for item in points_redeem.ITEM_LABELS:
        assert points_redeem.cost_of(item) > 0
    assert points_redeem.cost_of(points_redeem.ITEM_MATCHMAKER) == 50


def test_an_unknown_item_is_refused(points_db):
    with pytest.raises(points_redeem.RedeemError):
        points_redeem.cost_of("free_lunch")


def test_the_price_comes_from_config(points_db):
    _fund(points_db)
    points_config.set_value("redeem_real_like", 5)
    order = points_redeem.redeem_real_like(USER)
    assert order["cost"] == 5
    assert points.balance(USER) == 995


# ---------------------------------------------------------------- 幂等

def test_a_retry_with_the_same_request_key_does_not_charge_twice(points_db):
    """H5 每次点击带一个请求号；网络重试带的是同一个号。"""
    _fund(points_db)
    first = points_redeem.redeem_real_like(USER, request_key="abc123")
    with pytest.raises(points_redeem.AlreadyRedeemed):
        points_redeem.redeem_real_like(USER, request_key="abc123")

    assert points.balance(USER) == 980
    assert len(points_redeem.list_for(USER)) == 1
    assert first["id"]


def test_two_different_clicks_are_two_redemptions(points_db):
    """同一个东西兑两次是允许的（额外实名喜欢可以攒），只有重试才拦。"""
    _fund(points_db)
    points_redeem.redeem_real_like(USER, request_key="click-1")
    points_redeem.redeem_real_like(USER, request_key="click-2")
    assert points_redeem.extra_real_like_quota(USER) == 2
    assert points.balance(USER) == 960


def test_without_a_request_key_each_call_is_a_new_redemption(points_db):
    """没有请求号时键退化成序号，仍然唯一——但重试会变成第二笔。
    这是调用方（机器人指令）要接受的语义，不是 bug。"""
    _fund(points_db)
    points_redeem.redeem_real_like(USER)
    points_redeem.redeem_real_like(USER)
    assert len(points_redeem.list_for(USER)) == 2


def test_an_activity_item_is_naturally_one_per_person(points_db):
    """优先报名/心愿的键里带着活动 ID，所以第二次必然撞键。"""
    _fund(points_db)
    points_redeem.redeem_priority(USER, _act())
    with pytest.raises(points_redeem.AlreadyRedeemed):
        points_redeem.redeem_priority(USER, _act())
    assert points.balance(USER) == 970


# ---------------------------------------------------------------- 额外实名喜欢

def test_a_redeemed_real_like_becomes_a_permanent_quota_unit(points_db):
    _fund(points_db)
    assert points_redeem.extra_real_like_quota(USER) == 0
    points_redeem.redeem_real_like(USER)
    assert points_redeem.extra_real_like_quota(USER) == 1


def test_the_redeemed_quota_feeds_straight_into_real_left(points_db):
    """接法是「兑换名额并入 permanent」——lib/quota.real_left 天生就是
    max(0, 1 + permanent − 本月已用)，所以先用免费的、再用兑换的是自动成立的。"""
    from lib import quota
    _fund(points_db)
    ym = quota.month_key()
    strict = quota.real_left([], 0, ym)

    points_redeem.redeem_real_like(USER)
    points_redeem.redeem_real_like(USER)
    assert quota.real_left([], points_redeem.extra_real_like_quota(USER), ym) == strict + 2


def test_a_refunded_redemption_is_not_counted_as_quota(points_db, clock):
    _fund(points_db)
    order = points_redeem.redeem_real_like(USER, request_key="k1")
    assert points_redeem.extra_real_like_quota(USER) == 1

    points_redeem.refund(order["id"], reason="管理员撤销")
    assert points_redeem.extra_real_like_quota(USER) == 0


# ---------------------------------------------------------------- 优先报名

def test_priority_needs_an_activity_that_is_open(points_db):
    _fund(points_db)
    with pytest.raises(points_redeem.ItemUnavailable, match="不能使用优先名额"):
        points_redeem.redeem_priority(USER, _act(open=False))
    assert points.balance(USER) == 1000


def test_priority_needs_the_activity_to_have_quota(points_db):
    _fund(points_db)
    with pytest.raises(points_redeem.ItemUnavailable, match="没有名额"):
        points_redeem.redeem_priority(USER, _act(quota=0))


def test_priority_cap_is_a_share_of_the_activity_quota(points_db):
    """需求原文：每场活动的优先名额不超过总名额的 30%。10 人 → 3 个优先位。"""
    for i in range(4):
        _fund(points_db, f"ou_{i}")
    for i in range(3):
        points_redeem.redeem_priority(f"ou_{i}", _act())
    with pytest.raises(points_redeem.ItemUnavailable, match="优先名额已经满了"):
        points_redeem.redeem_priority("ou_3", _act())

    assert points_redeem.priority_available(_act()) == 0


def test_the_priority_cap_comes_from_config(points_db):
    _fund(points_db)
    points_config.set_value("priority_ratio", 1.0)
    assert points_redeem.priority_available(_act(quota=10)) == 10


def test_a_tiny_activity_with_no_room_for_anyone_is_refused(points_db):
    """10 人 × 30% = 3；2 人 × 30% = 0——不是「至少留一个」。"""
    _fund(points_db)
    with pytest.raises(points_redeem.ItemUnavailable, match="不足 1 个"):
        points_redeem.redeem_priority(USER, _act(quota=2))


def test_a_refunded_priority_frees_its_slot(points_db):
    for i in range(4):
        _fund(points_db, f"ou_{i}")
    for i in range(3):
        points_redeem.redeem_priority(f"ou_{i}", _act())
    first = points_redeem.find("ou_0", points_redeem.ITEM_PRIORITY, ACT)

    points_redeem.refund(first["id"], reason="活动取消")
    points_redeem.redeem_priority("ou_3", _act())        # 空出来的位子给了别人
    assert points.balance("ou_3") == 970


def test_cancelling_early_refunds_but_cancelling_late_does_not(points_db):
    """需求原文：开始前 48 小时前主动取消退穗，48 小时内不退。"""
    assert points_redeem.can_cancel_priority(hours_to_start=72) is True
    assert points_redeem.can_cancel_priority(hours_to_start=48) is True
    assert points_redeem.can_cancel_priority(hours_to_start=47.9) is False
    assert points_redeem.can_cancel_priority(hours_to_start=0) is False
    assert points_redeem.can_cancel_priority(hours_to_start=None) is False


def test_the_cancel_window_comes_from_config(points_db):
    points_config.set_value("priority_cancel_hours", 24)
    assert points_redeem.can_cancel_priority(hours_to_start=25) is True
    assert points_redeem.can_cancel_priority(hours_to_start=20) is False


# ---------------------------------------------------------------- 心愿

def test_a_wish_needs_someone_to_designate(points_db):
    _fund(points_db)
    with pytest.raises(points_redeem.ItemUnavailable, match="指定一位"):
        points_redeem.redeem_wish(USER, _act(), "")


def test_a_wish_cannot_be_designated_to_yourself(points_db):
    _fund(points_db)
    with pytest.raises(points_redeem.ItemUnavailable, match="自己"):
        points_redeem.redeem_wish(USER, _act(), USER)


def test_a_wish_needs_my_own_signup(points_db):
    """自己没报名这场活动 → 不许许愿（人不在场，30 穗会白花）。"""
    _fund(points_db)
    with pytest.raises(points_redeem.ItemUnavailable, match="先报名"):
        points_redeem.redeem_wish(USER, _act(), OTHER, signed_up=False)
    assert points.balance(USER) == 1000         # 一分没扣


def test_the_signup_gate_is_checked_after_the_form_and_the_balance(points_db):
    """顺序有意义：没填完 / 没钱时，说的不该是「你还没报名」。

    两句都对，但顺序反了会让人先跑去报名，回来再填一次才发现还差一步。
    """
    with pytest.raises(points_redeem.ItemUnavailable, match="指定一位"):
        points_redeem.redeem_wish(USER, _act(), "", signed_up=False)   # 也没钱
    _fund(points_db, 1)                        # 有 1 穗，不够 30
    with pytest.raises(points.InsufficientBalance):
        points_redeem.redeem_wish(USER, _act(), OTHER, signed_up=False)


def test_a_wish_remembers_who_was_designated(points_db):
    _fund(points_db)
    order = points_redeem.redeem_wish(USER, _act(), OTHER)
    assert order["params"]["target_oid"] == OTHER
    assert order["params"]["activity_id"] == ACT


def test_wishes_waiting_on_an_activity_can_be_listed(points_db):
    """活动开始时清算用：这些人指定的对象还没报名就退穗。"""
    _fund(points_db)
    _fund(points_db, "ou_b")
    points_redeem.redeem_wish(USER, _act(), OTHER)
    points_redeem.redeem_wish("ou_b", _act(), OTHER)

    waiting = points_redeem.by_activity(
        ACT, item=points_redeem.ITEM_WISH, statuses=[points_redeem.ST_ACTIVE])
    assert len(waiting) == 2
    assert {w["user_oid"] for w in waiting} == {USER, "ou_b"}


def test_marking_a_wish_arranged_keeps_the_points(points_db):
    """「已安排」不退穗——只有「无法安排」才退。"""
    _fund(points_db)
    order = points_redeem.redeem_wish(USER, _act(), OTHER)
    points_redeem.mark(order["id"], points_redeem.ST_ARRANGED, note="已牵线")

    assert points.balance(USER) == 970
    assert points_redeem.get(order["id"])["status_label"] == "已安排"


def test_an_unarrangeable_wish_is_refunded(points_db):
    _fund(points_db)
    order = points_redeem.redeem_wish(USER, _act(), OTHER)
    points_redeem.refund(order["id"], reason="对方没报名，无法安排")
    assert points.balance(USER) == 1000


# ---------------------------------------------------------------- 红娘推荐

def test_a_matchmaker_order_needs_a_condition(points_db):
    _fund(points_db)
    with pytest.raises(points_redeem.ItemUnavailable, match="条件和期望"):
        points_redeem.redeem_matchmaker(USER, "   ")


def test_a_matchmaker_condition_has_a_length_limit(points_db):
    _fund(points_db)
    limit = points_config.get("matchmaker_condition_max_len")
    with pytest.raises(points_redeem.ItemUnavailable, match=f"最多 {limit} 个字"):
        points_redeem.redeem_matchmaker(USER, "想找个" * limit)


def test_only_one_open_matchmaker_order_at_a_time(points_db):
    """需求原文：「每人同时只能有 1 张未完成单」。"""
    _fund(points_db)
    first = points_redeem.redeem_matchmaker(USER, "温柔、爱看书", request_key="k1")
    with pytest.raises(points_redeem.ItemUnavailable, match="还有一张推荐单"):
        points_redeem.redeem_matchmaker(USER, "再写一份", request_key="k2")

    assert points_redeem.get(first["id"])["status"] == points_redeem.ST_PENDING
    assert points.balance(USER) == 950


def test_a_recommended_order_still_blocks_a_new_one(points_db):
    """「已推荐」还没完成，仍然算未完成单。"""
    _fund(points_db)
    order = points_redeem.redeem_matchmaker(USER, "温柔、爱看书")
    points_redeem.mark(order["id"], points_redeem.ST_RECOMMENDED)
    with pytest.raises(points_redeem.ItemUnavailable, match="还有一张推荐单"):
        points_redeem.redeem_matchmaker(USER, "再写一份", request_key="k2")


def test_finishing_an_order_frees_the_slot(points_db):
    _fund(points_db)
    order = points_redeem.redeem_matchmaker(USER, "温柔、爱看书")
    points_redeem.mark(order["id"], points_redeem.ST_DONE)
    points_redeem.redeem_matchmaker(USER, "再写一份", request_key="k2")
    assert points.balance(USER) == 900


def test_a_refunded_order_frees_the_slot_too(points_db):
    """找不到人就退穗，然后他还能再兑——否则一次失败就把人永久挡在外面。"""
    _fund(points_db)
    order = points_redeem.redeem_matchmaker(USER, "温柔、爱看书")
    points_redeem.refund(order["id"], reason="红娘找不到合适的")
    assert points.balance(USER) == 1000
    points_redeem.redeem_matchmaker(USER, "再写一份", request_key="k2")


def test_an_order_nobody_recommended_within_the_deadline_is_overdue(points_db, clock):
    """需求原文：14 天内没推荐就自动退穗。"""
    _fund(points_db)
    order = points_redeem.redeem_matchmaker(USER, "温柔、爱看书")

    clock(days=13, hours=23)
    assert points_redeem.matchmaker_overdue() == []

    clock(hours=2)
    overdue = points_redeem.matchmaker_overdue()
    assert [o["id"] for o in overdue] == [order["id"]]


def test_a_recommended_order_is_not_overdue(points_db, clock):
    """14 天管的是「红娘有没有干活」，不是「用户有没有脱单」。"""
    _fund(points_db)
    order = points_redeem.redeem_matchmaker(USER, "温柔、爱看书")
    points_redeem.mark(order["id"], points_redeem.ST_RECOMMENDED)

    clock(days=30)
    assert points_redeem.matchmaker_overdue() == []


# ---------------------------------------------------------------- 退还

def test_a_refund_returns_exactly_what_was_charged(points_db):
    _fund(points_db)
    order = points_redeem.redeem_priority(USER, _act())
    assert points.balance(USER) == 970

    entry_id, after = points_redeem.refund(order["id"], reason="活动取消")
    assert after == 1000
    assert points.balance(USER) == 1000

    rows = points.entries(USER)                        # 新的在前
    refund_row, original = rows[0], rows[1]
    assert refund_row["id"] == entry_id
    assert refund_row["delta"] == 30
    assert refund_row["kind"] == points.KIND_REFUND
    assert refund_row["ref_type"] == points.REF_TYPE_LEDGER
    assert refund_row["ref_id"] == str(original["id"])
    assert original["id"] == order["ledger_id"]
    assert original["delta"] == -30                    # 原流水一个字没改
    assert original["kind"] == points.KIND_REDEEM


def test_a_refund_survives_a_later_price_change(points_db):
    """退的是当初扣掉的那个数，不是按当前价格重算——否则运营调价会让人多退少退。"""
    _fund(points_db)
    order = points_redeem.redeem_priority(USER, _act())
    points_config.set_value("redeem_priority_signup", 200)

    points_redeem.refund(order["id"], reason="活动取消")
    assert points.balance(USER) == 1000


def test_refunding_twice_returns_the_points_once(points_db):
    """活动取消和红娘标记「无法安排」可能同时发生，两条路都会调退穗。"""
    _fund(points_db)
    order = points_redeem.redeem_wish(USER, _act(), OTHER)
    first = points_redeem.refund(order["id"], reason="活动取消")
    second = points_redeem.refund(order["id"], reason="管理员又点了一次")

    assert first is not None
    assert second is None
    assert points.balance(USER) == 1000
    assert len(points.entries(USER)) == 3              # 底分 + 扣穗 + 一条退穗


def test_a_finished_order_is_not_refunded(points_db):
    """已完成（红娘推荐成了、心愿已安排）说明服务已经交付，不退了。"""
    _fund(points_db)
    order = points_redeem.redeem_wish(USER, _act(), OTHER)
    points_redeem.mark(order["id"], points_redeem.ST_ARRANGED)
    assert points_redeem.refund(order["id"], reason="用户反悔了") is None
    assert points.balance(USER) == 970


def test_refunding_works_even_after_the_points_were_spent(points_db):
    """退穗是正数流水，余额不足拦不到它——用户把穗花光了照样能退。"""
    _fund(points_db, amount=30)
    order = points_redeem.redeem_priority(USER, _act())
    assert points.balance(USER) == 0

    points_redeem.refund(order["id"], reason="活动取消")
    assert points.balance(USER) == 30


def test_a_cancelled_redemption_is_recorded_as_cancelled(points_db):
    _fund(points_db)
    order = points_redeem.redeem_priority(USER, _act())
    points_redeem.refund(order["id"], reason="用户提前 3 天取消",
                         status=points_redeem.ST_CANCELLED)
    assert points_redeem.get(order["id"])["status_label"] == "已取消"


def test_refunding_a_missing_order_is_an_error(points_db):
    with pytest.raises(points_redeem.RedeemError, match="不存在"):
        points_redeem.refund(9999, reason="手滑")


def test_marking_a_missing_order_is_an_error(points_db):
    with pytest.raises(points_redeem.RedeemError, match="不存在"):
        points_redeem.mark(9999, points_redeem.ST_DONE)


def test_marking_an_unknown_status_is_refused(points_db):
    _fund(points_db)
    order = points_redeem.redeem_wish(USER, _act(), OTHER)
    with pytest.raises(points_redeem.RedeemError, match="未知"):
        points_redeem.mark(order["id"], "看起来不错")


# ---------------------------------------------------------------- 并发

def test_concurrent_redemptions_never_overdraw(points_db):
    """账上 100 穗，20 个线程同时兑 20 穗的东西——恰好 5 个成功，余额 0。

    每个线程用不同的请求号，所以撞的不是幂等键而是余额。锁的是
    `BEGIN IMMEDIATE`：事务要是等到第一条写语句才升级成写锁，多个线程会
    各自读到「还有 100」，于是都扣成功，余额跑成负数。
    """
    _fund(points_db, amount=100)
    ok, refused = [], []
    lock = threading.Lock()

    def worker(i):
        def run():
            try:
                points_redeem.redeem_real_like(USER, request_key=f"click-{i}")
                with lock:
                    ok.append(i)
            except points.InsufficientBalance:
                with lock:
                    refused.append(i)
        return run

    barrier = threading.Barrier(20)

    def spawn(i):
        def wrapper():
            barrier.wait()
            worker(i)()
        return wrapper

    threads = [threading.Thread(target=spawn(i)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ok) == 5
    assert len(refused) == 15
    assert points.balance(USER) == 0
    assert len(points_redeem.list_for(USER)) == 5
    assert points_redeem.extra_real_like_quota(USER) == 5


def test_concurrent_retries_of_the_same_click_charge_once(points_db):
    """同一个请求号同时打进来 8 次（用户狂点提交），只该扣一次。"""
    _fund(points_db)
    ok, dup = [], []
    lock = threading.Lock()

    def worker():
        try:
            points_redeem.redeem_real_like(USER, request_key="same-click")
            with lock:
                ok.append(1)
        except points_redeem.AlreadyRedeemed:
            with lock:
                dup.append(1)

    _run_threads(worker, 8)

    assert len(ok) == 1
    assert len(dup) == 7
    assert points.balance(USER) == 980
    assert len(points_redeem.list_for(USER)) == 1


def test_concurrent_priority_redemptions_respect_the_activity_cap(points_db):
    """8 个人同时抢 3 个优先位，只有 3 个能兑上。"""
    for i in range(8):
        _fund(points_db, f"ou_{i}")
    ok, refused = [], []
    lock = threading.Lock()

    def spawn(i):
        def run():
            try:
                points_redeem.redeem_priority(f"ou_{i}", _act())
                with lock:
                    ok.append(i)
            except (points_redeem.ItemUnavailable, points_redeem.AlreadyRedeemed):
                with lock:
                    refused.append(i)
        return run

    barrier = threading.Barrier(8)

    def wrapper_factory(i):
        def wrapper():
            barrier.wait()
            spawn(i)()
        return wrapper

    threads = [threading.Thread(target=wrapper_factory(i)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ok) == 3
    assert len(refused) == 5
