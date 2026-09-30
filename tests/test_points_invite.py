"""邀请关系单测：手机号关联、7 天确认期、封禁收回（不联网）。

需求点名的几条都在这儿：奖励不重复发、7 天边界、确认期内封号会收回、
不能邀请自己、一人只能有一个邀请人。

「登记好友手机号」那条路径（先记手机号、好友注册后再关联）已下线，
邀请关系只走「被邀请人注册时带邀请人ID」这一条（`record_from_form`）。
"""
import pytest

from lib import points, points_config, points_invite

INVITER = "ou_inviter"
FRIEND = "ou_friend"
PHONE = "13800001234"


def _invite(points_db, phone=PHONE, inviter=INVITER, invitee=FRIEND, gender="女性"):
    """走注册表单路径建一条 pending 邀请。"""
    return points_invite.record_from_form(invitee, phone, gender, inviter)


def _make_due(clock):
    """把时钟拨过确认期。7 天是默认值，从配置读，不写死。"""
    clock(days=int(points_config.get("invite_confirm_days")))


# ---------------------------------------------------------------- 手机号归一

@pytest.mark.parametrize("raw, want", [
    ("13800001234", "13800001234"),
    ("138 0000 1234", "13800001234"),
    ("138-0000-1234", "13800001234"),
    ("+8613800001234", "13800001234"),
    ("008613800001234", "13800001234"),
    ("  13800001234  ", "13800001234"),
])
def test_phone_normalisation(points_db, raw, want):
    """登记的和注册的必须归一到同一个串上——不然邀请关系永远关联不上，
    而且不报错。"""
    assert points_invite.normalize_phone(raw) == want


@pytest.mark.parametrize("raw", ["", "1380000123", "138000012345", "010-88886666", "abcdefg", None])
def test_invalid_phones_are_rejected(points_db, raw):
    assert points_invite.normalize_phone(raw) == ""


# ---------------------------------------------------------------- 历史名单

def test_a_historical_participant_is_recognised(points_db):
    points_invite.add_historical_participants([("13800001234", "老王")], source="2023")
    assert points_invite.is_historical_participant("138 0000 1234") is True


def test_importing_the_same_list_twice_does_not_double_count(points_db):
    rows = [("13800001234", "老王"), ("13900005678", "小李")]
    assert points_invite.add_historical_participants(rows) == (2, 0)
    assert points_invite.add_historical_participants(rows) == (0, 2)
    assert points_invite.historical_count() == 2


def test_bad_rows_are_skipped_not_fatal(points_db):
    """导几千行时中途炸掉，人不知道该从哪一行接着来。"""
    added, skipped = points_invite.add_historical_participants(
        [("13800001234", "老王"), ("010-88886666", "座机"), ("", "")])
    assert (added, skipped) == (1, 2)


# ---------------------------------------------------------------- 注册表单路径

def test_registering_with_an_inviter_id_creates_the_invite(points_db):
    """被邀请人自己带着「邀请人ID」注册进来。"""
    invite_id, status = _invite(points_db)
    assert status == points_invite.STATUS_PENDING
    assert points_invite.progress(INVITER)["pending"] == 1
    assert invite_id > 0


def test_the_same_phone_can_only_have_one_inviter(points_db):
    """同一个手机号再带另一个邀请人ID 进来，以先记的那位为准。"""
    _invite(points_db)
    invite_id, _ = points_invite.record_from_form(FRIEND, PHONE, "女性", "ou_later")
    row = points_db.connection().execute(
        "SELECT inviter_oid, note FROM invites WHERE id=?", (invite_id,)).fetchone()
    assert row["inviter_oid"] == INVITER
    assert "不一致" in row["note"]


# ---------------------------------------------------------------- 7 天确认期

def test_the_clock_starts_when_the_account_becomes_single(points_db, clock):
    _invite(points_db)
    assert points.balance(INVITER) == 0                  # 审核通过前后都没有穗

    invite_id, status = points_invite.start_confirm_window(FRIEND, PHONE, "女性")
    assert status == points_invite.STATUS_CONFIRMED
    assert points.balance(INVITER) == 0                  # 计时中，仍然没有穗
    row = points_db.connection().execute(
        "SELECT confirm_due_at FROM invites WHERE id=?", (invite_id,)).fetchone()
    assert row["confirm_due_at"] > points_db.now_str()


def test_pending_points_cannot_be_spent(points_db, clock):
    """「待确认的麦穗不能用于兑换」——不需要额外拦截，它在余额里根本不存在。"""
    _invite(points_db)
    points_invite.start_confirm_window(FRIEND, PHONE, "女性")
    assert points_invite.progress(INVITER)["pending_points"] == 20

    with pytest.raises(points.InsufficientBalance):
        points.add_entry(INVITER, -20, points.KIND_REDEEM)


def test_nothing_is_paid_before_the_seventh_day(points_db, clock):
    _invite(points_db)
    points_invite.start_confirm_window(FRIEND, PHONE, "女性")

    clock(days=6, hours=23, seconds=59)
    assert points_invite.settle_all() == {"finalized": 0, "clawed_back": 0}
    assert points.balance(INVITER) == 0


def test_paid_out_once_the_window_closes(points_db, clock):
    _invite(points_db)
    points_invite.start_confirm_window(FRIEND, PHONE, "女性")

    _make_due(clock)
    assert points_invite.settle_all() == {"finalized": 1, "clawed_back": 0}
    assert points.balance(INVITER) == 20
    assert points_invite.progress(INVITER)["finalized"] == 1


def test_settling_early_is_refused(points_db, clock):
    """直接调 settle 也不能提前发——早发出去的穗可能已经被兑换掉了。"""
    _invite(points_db)
    invite_id, _ = points_invite.start_confirm_window(FRIEND, PHONE, "女性")
    with pytest.raises(points_invite.InviteError, match="确认期"):
        points_invite.settle(invite_id)
    assert points.balance(INVITER) == 0


def test_the_reward_is_never_paid_twice(points_db, clock):
    """奖励不重复发：反复扫、反复结算都只发一次。"""
    _invite(points_db)
    invite_id, _ = points_invite.start_confirm_window(FRIEND, PHONE, "女性")
    _make_due(clock)

    points_invite.settle_all()
    points_invite.settle_all()
    points_invite.settle(invite_id)               # 已经是 finalized 了
    points_db.connection().execute(           # 就算有人手贱把状态改回去
        "UPDATE invites SET status='confirmed' WHERE id=?", (invite_id,))
    points_invite.settle(invite_id)

    assert points.balance(INVITER) == 20
    assert len(points.entries(INVITER)) == 1


def test_a_later_config_change_does_not_move_an_open_window(points_db, clock):
    """确认期一开始就把到期时刻写死。否则运营把 7 天改成 3 天，
    会有一批人凭空提前到期。"""
    _invite(points_db)
    invite_id, _ = points_invite.start_confirm_window(FRIEND, PHONE, "女性")
    before = points_db.connection().execute(
        "SELECT confirm_due_at FROM invites WHERE id=?", (invite_id,)).fetchone()

    points_config.set_value("invite_confirm_days", 3)
    after = points_db.connection().execute(
        "SELECT confirm_due_at FROM invites WHERE id=?", (invite_id,)).fetchone()
    assert before["confirm_due_at"] == after["confirm_due_at"]


# ---------------------------------------------------------------- 封禁收回

def test_a_ban_inside_the_window_cancels_the_reward(points_db, clock):
    _invite(points_db)
    points_invite.start_confirm_window(FRIEND, PHONE, "女性")

    clock(days=3)
    assert points_invite.claw_back_for_invitee(FRIEND, reason="确认期内被封禁") == 1

    _make_due(clock)
    assert points_invite.settle_all() == {"finalized": 0, "clawed_back": 0}
    assert points.balance(INVITER) == 0
    assert points_invite.progress(INVITER)["clawed_back"] == 1


def test_a_ban_on_day_three_counts_even_if_lifted_on_day_five(points_db, clock):
    """需求说的「7 天内被封禁」算的是**发生过**，不是「到期那一刻还在封」。
    所以封禁要立刻处理，不能等结算那天再看一眼。"""
    _invite(points_db)
    points_invite.start_confirm_window(FRIEND, PHONE, "女性")

    clock(days=3)
    points_invite.claw_back_for_invitee(FRIEND)
    clock(days=2)                                  # 解封了
    _make_due(clock)
    points_invite.settle_all()
    assert points.balance(INVITER) == 0


def test_a_ban_at_settlement_time_is_still_caught(points_db, clock):
    """漏网的情况：封禁发生在别处、没人调钩子，结算这一眼是最后一道闸。"""
    _invite(points_db)
    points_invite.start_confirm_window(FRIEND, PHONE, "女性")
    _make_due(clock)

    out = points_invite.settle_all(is_banned=lambda oid: oid == FRIEND)
    assert out == {"finalized": 0, "clawed_back": 1}
    assert points.balance(INVITER) == 0


def test_a_ban_outside_the_window_does_not_touch_a_paid_reward(points_db, clock):
    """出了 7 天就是终局。要事后追回得走管理员扣穗，那条路会留下操作人和原因。"""
    _invite(points_db)
    points_invite.start_confirm_window(FRIEND, PHONE, "女性")
    _make_due(clock)
    points_invite.settle_all()
    assert points.balance(INVITER) == 20

    clock(days=30)
    assert points_invite.claw_back_for_invitee(FRIEND) == 0
    assert points.balance(INVITER) == 20


def test_a_normal_account_is_not_clawed_back(points_db, clock):
    _invite(points_db)
    points_invite.start_confirm_window(FRIEND, PHONE, "女性")
    _make_due(clock)
    out = points_invite.settle_all(is_banned=lambda oid: False)
    assert out == {"finalized": 1, "clawed_back": 0}


# ---------------------------------------------------------------- 性别与金额

def test_a_woman_is_worth_twenty_and_a_man_fifteen(points_db, clock):
    points_invite.record_from_form("ou_f", "13800000001", "女性", INVITER)
    points_invite.record_from_form("ou_m", "13800000002", "男性", INVITER)
    points_invite.start_confirm_window("ou_f", "13800000001", "女性")
    points_invite.start_confirm_window("ou_m", "13800000002", "男性")
    _make_due(clock)
    points_invite.settle_all()
    assert points.balance(INVITER) == 35


def test_an_unknown_gender_pays_the_lower_amount(points_db, clock):
    """性别读不出来时按低的那个发。宁可少发 5 穗：多发的可能已经被兑换掉，
    之后想追回时余额不足，reverse_entry 会失败。"""
    _invite(points_db)
    points_invite.start_confirm_window(FRIEND, PHONE, "")
    _make_due(clock)
    points_invite.settle_all()
    assert points.balance(INVITER) == 15


def test_the_amounts_come_from_config(points_db, clock):
    points_config.set_value("invite_reward_female", 25)
    _invite(points_db)
    points_invite.start_confirm_window(FRIEND, PHONE, "女性")
    _make_due(clock)
    points_invite.settle_all()
    assert points.balance(INVITER) == 25


# ---------------------------------------------------------------- 自己邀请自己

def test_binding_your_own_account_is_rejected(points_db):
    """被邀请人和邀请人是同一个人时，在「变单身」这一步被拦下。"""
    _invite(points_db, invitee=INVITER)
    invite_id, status = points_invite.start_confirm_window(INVITER, PHONE, "女性")
    assert status == points_invite.STATUS_REJECTED
    assert points_invite.progress(INVITER)["rejected"] == 1


# ---------------------------------------------------------------- 展示

def test_list_masks_the_phone_number(points_db):
    _invite(points_db)
    row = points_invite.list_for(INVITER)[0]
    assert "138" not in str(row.get("invitee_phone", ""))
    assert row["phone_masked"] == "138****1234"
    assert row["status_label"] == "待确认"


def test_progress_counts_each_state(points_db, clock):
    points_invite.record_from_form("ou_a", "13800000001", "女性", INVITER)
    points_invite.record_from_form("ou_b", "13800000002", "男性", INVITER)
    points_invite.record_from_form("ou_c", "13800000003", "女性", INVITER)
    points_invite.start_confirm_window("ou_a", "13800000001", "女性")
    points_invite.start_confirm_window("ou_b", "13800000002", "男性")
    _make_due(clock)
    points_invite.settle_all()

    p = points_invite.progress(INVITER)
    assert p["total"] == 3
    assert p["pending"] == 1
    assert p["finalized"] == 2
    assert p["finalized_points"] == 35
    assert p["pending_points"] == 0
