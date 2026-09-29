"""点赞的机器人侧：每周汇总私聊 + 管理员「点赞统计」。

两件事各有一个「不报错但很糟」的失败方式：
- 周汇总发重了，用户一周收到几十条一模一样的私聊（循环半小时一轮）；
- 统计的男女占比分母用错，数字看着正常但一直是偏的，没人能发现。

飞书接口全部打桩，`store` 的占位函数也桩掉——那是 JSON 文件读写，不是这里要考的。
"""
from datetime import datetime, timedelta

import pytest

import praise_admin
from praise_admin import praise_stats_text, send_weekly_summaries

from lib import praise, quota

TZ = quota.TZ

# 用 lib.praise 里的真选项名，不写字面量「男」「女」：写成「男」的用例全都会过，
# 却正好绕过「选项名对不对」这个唯一会静默出错的点（见 praise.GENDER_MALE 的注释）。
M = praise.GENDER_MALE
F = praise.GENDER_FEMALE

WEEK = "2026-W40"          # 2026-09-28(一) ~ 2026-10-04(日)
SUN_AFTER = datetime(2026, 10, 4, 21, 0, tzinfo=TZ)      # 窗口内
MON = datetime(2026, 10, 5, 9, 0, tzinfo=TZ)             # 窗口外


def _row(status, week, target=None, day=None, initiator=None):
    f = {"状态": status, "归属周": week}
    if target is not None:
        f["被点赞人open_id"] = target
    if day is not None:
        f["归属日期"] = day
    if initiator is not None:
        f["点赞人open_id"] = initiator
    return {"record_id": "r", "fields": f}


def _user(oid, gender, status="单身"):
    return {"record_id": "u_" + oid,
            "fields": {"飞书用户ID": oid, "性别": gender, "账号状态": status}}


def _like(a, b):
    return {"record_id": "l", "fields": {"发起用户open_id": a, "目标用户open_id": b}}


@pytest.fixture
def env(monkeypatch):
    """桩掉表 ID、表内容、私聊发送、以及「这周发过了」的占位文件。"""
    sent = []
    reserved = set()
    state = {"praise": [], "users": [], "likes": [], "send_ok": True}

    def _search(table_id, *a, **k):
        if table_id == praise_admin.PRAISE_TABLE_ID:
            return state["praise"]
        if table_id == praise_admin.USER_TABLE_ID:
            return state["users"]
        if table_id == praise_admin.LIKE_TABLE_ID:
            return state["likes"]
        return []

    monkeypatch.setattr(praise_admin, "PRAISE_TABLE_ID", "tbl_praise")
    monkeypatch.setattr(praise_admin, "search_records", _search)
    monkeypatch.setattr(praise_admin, "send_text_message",
                        lambda oid, text: sent.append((oid, text)) or state["send_ok"])
    # 占位就是「一个 (周, 人) 只能占一次」——真实现是 JSON 文件，这里只要语义。
    monkeypatch.setattr(praise_admin, "reserve_praise_week",
                        lambda wk, oid: (wk, oid) not in reserved
                        and not reserved.add((wk, oid)))
    monkeypatch.setattr(praise_admin, "unreserve_praise_week",
                        lambda wk, oid: reserved.discard((wk, oid)))
    state["sent"] = sent
    state["reserved"] = reserved
    return state


# ---------------------------------------------------------------- 每周汇总

def test_only_people_who_got_praise_this_week_are_messaged(env):
    """需求第 6 条：本周没收到赞的人**不发**。发一条「本周 0 个赞」是纯骚扰。"""
    env["praise"] = [
        _row(praise.PRAISE_STATUS_ACTIVE, WEEK, target="ou_a"),
        _row(praise.PRAISE_STATUS_ACTIVE, WEEK, target="ou_a"),
        _row(praise.PRAISE_STATUS_ACTIVE, WEEK, target="ou_b"),
        _row(praise.PRAISE_STATUS_ACTIVE, "2026-W39", target="ou_c"),   # 上周
    ]
    sent, week = send_weekly_summaries(SUN_AFTER)
    assert week == WEEK and sent == 2
    assert sorted(oid for oid, _ in env["sent"]) == ["ou_a", "ou_b"]
    body = dict(env["sent"])["ou_a"]
    assert "2" in body and "ou_b" not in body      # 只说个数，不说谁


def test_nothing_is_sent_when_nobody_got_praise(env):
    env["praise"] = [_row(praise.PRAISE_STATUS_ACTIVE, "2026-W39", target="ou_c")]
    assert send_weekly_summaries(SUN_AFTER) == (0, WEEK)
    assert env["sent"] == []


def test_cancelled_praise_does_not_earn_a_message(env):
    """取消掉的赞不算数——不然「点完就撤」也能给人发提醒。"""
    env["praise"] = [
        _row(praise.PRAISE_STATUS_ACTIVE, WEEK, target="ou_a"),
        _row(praise.PRAISE_STATUS_CANCELLED, WEEK, target="ou_b"),
    ]
    send_weekly_summaries(SUN_AFTER)
    assert [oid for oid, _ in env["sent"]] == ["ou_a"]


def test_the_same_week_is_never_sent_twice(env):
    """循环半小时一轮，不占位就会把人刷屏——这是这个任务最要紧的一条。"""
    env["praise"] = [_row(praise.PRAISE_STATUS_ACTIVE, WEEK, target="ou_a")]
    assert send_weekly_summaries(SUN_AFTER)[0] == 1
    assert send_weekly_summaries(SUN_AFTER)[0] == 0
    assert send_weekly_summaries(SUN_AFTER)[0] == 0
    assert len(env["sent"]) == 1


def test_a_failed_send_is_retried_next_round(env):
    """发失败要退回占位。用户不会知道自己本该收到一条，漏了就是永远漏了。"""
    env["praise"] = [_row(praise.PRAISE_STATUS_ACTIVE, WEEK, target="ou_a")]
    env["send_ok"] = False
    assert send_weekly_summaries(SUN_AFTER)[0] == 0

    env["send_ok"] = True
    assert send_weekly_summaries(SUN_AFTER)[0] == 1


def test_the_next_week_gets_its_own_message(env):
    """占位是按周算的：下周收到了赞，还是要发。"""
    env["praise"] = [_row(praise.PRAISE_STATUS_ACTIVE, WEEK, target="ou_a")]
    send_weekly_summaries(SUN_AFTER)

    next_week = "2026-W41"
    env["praise"] = [_row(praise.PRAISE_STATUS_ACTIVE, next_week, target="ou_a")]
    assert send_weekly_summaries(datetime(2026, 10, 11, 21, 0, tzinfo=TZ))[0] == 1


def test_monday_after_the_window_does_not_send_this_week(env):
    """周一早上窗口已关：这时该汇总的是「上一周」——机器人周日没跑起来也能补发，
    但绝不能把周一当天刚点的赞提前数进「本周」。"""
    env["praise"] = [_row(praise.PRAISE_STATUS_ACTIVE, WEEK, target="ou_a")]
    sent, week = send_weekly_summaries(MON)
    assert week == WEEK and sent == 1

    # 补发过了，周一再跑不会重复
    assert send_weekly_summaries(MON)[0] == 0


def test_an_unconfigured_table_sends_nothing(env, monkeypatch):
    """生产还没建表时，循环不能炸，也不能往空表 ID 里查。"""
    monkeypatch.setattr(praise_admin, "PRAISE_TABLE_ID", "")
    env["praise"] = [_row(praise.PRAISE_STATUS_ACTIVE, WEEK, target="ou_a")]
    assert send_weekly_summaries(SUN_AFTER) == (0, "")
    assert env["sent"] == []


def test_the_loop_does_not_touch_the_table_outside_the_window(env, monkeypatch):
    """窗口外一轮下来连表都不该查——半小时一轮，白查一天就是几十次全表扫描。"""
    looked_up = []
    monkeypatch.setattr(praise_admin, "search_records",
                        lambda tid, *a, **k: looked_up.append(tid) or [])
    monkeypatch.setattr(praise_admin.praise, "weekly_window_open", lambda dt=None: False)
    _run_one_loop_round(monkeypatch)
    assert looked_up == []


def test_the_loop_does_send_inside_the_window(env, monkeypatch):
    """窗口内该真的发出去——上一条测的是「不发」，这条防的是「干脆从不发」。"""
    env["praise"] = [_row(praise.PRAISE_STATUS_ACTIVE, WEEK, target="ou_a")]
    monkeypatch.setattr(praise_admin.praise, "weekly_window_open", lambda dt=None: True)
    monkeypatch.setattr(praise_admin.praise, "summary_week", lambda dt=None: WEEK)
    _run_one_loop_round(monkeypatch)
    assert [oid for oid, _ in env["sent"]] == ["ou_a"]


def _run_one_loop_round(monkeypatch):
    """跑循环体一次就出来：让 time.sleep 抛一个哨兵，把这个死循环截断。

    换掉的是**praise_admin 自己的** time 引用，不是 `time` 模块的 sleep 属性——
    `monkeypatch.setattr(praise_admin.time, "sleep", ...)` 改的是全局共享的那个
    模块对象，别的线程（H5 的 spool worker 就在同一个进程里 sleep）会跟着炸。
    """
    monkeypatch.setattr(praise_admin, "time", _FakeClock)
    try:
        praise_admin.praise_weekly_loop(interval=1)
    except _Stop:
        pass


class _Stop(Exception):
    pass


class _FakeClock:
    """顶上循环用到的那个 time：sleep 抛哨兵，把死循环在第一轮末尾截断。"""

    @staticmethod
    def sleep(_seconds):
        raise _Stop


# ---------------------------------------------------------------- 点赞统计

def test_stats_report_the_three_numbers_the_admin_asked_for(env):
    env["praise"] = [
        _row(praise.PRAISE_STATUS_ACTIVE, WEEK, target="ou_g1", day="2026-10-01",
             initiator="ou_b1"),
        _row(praise.PRAISE_STATUS_ACTIVE, WEEK, target="ou_g1", day="2026-10-01",
             initiator="ou_b2"),
        _row(praise.PRAISE_STATUS_ACTIVE, WEEK, target="ou_b1", day="2026-09-30",
             initiator="ou_g1"),
        _row(praise.PRAISE_STATUS_CANCELLED, WEEK, target="ou_g2", day="2026-09-30",
             initiator="ou_b1"),
    ]
    env["users"] = [_user("ou_b1", M), _user("ou_b2", M),
                    _user("ou_g1", F), _user("ou_g2", F)]
    env["likes"] = [_like("ou_b1", "ou_g1")]        # b1 赞完去喜欢了 g1

    text = praise_stats_text()
    assert "有效点赞 3 条" in text and "已取消 1 条" in text
    assert "2026-10-01  2 个" in text and "2026-09-30  1 个" in text
    assert "1/3（33.3%）" in text                   # 转喜欢比例
    assert f"{M}：1/2（50.0%）" in text              # 收到过赞的男性
    # 女性 2 人里只有 g1 收到过**有效**赞；g2 那条被取消了，不算收到。
    assert f"{F}：1/2（50.0%）" in text
    # 分组是照着真选项名分的：写错成「男」「女」的话，上面两行会变成
    # 「没有账号正常的男性用户」，而所有人都落进兜底那一类。
    assert "性别写成" not in text


def test_the_gender_denominator_excludes_inactive_accounts(env):
    """分母只算账号正常的用户，和「用户统计」保持一个口径——
    两个数字对不上时管理员会以为坏了一个。"""
    env["praise"] = [_row(praise.PRAISE_STATUS_ACTIVE, WEEK, target="ou_b1",
                          day="2026-10-01", initiator="ou_g1")]
    env["users"] = [_user("ou_b1", M), _user("ou_b2", M),
                    _user("ou_b3", M, status="已退出"),
                    _user("ou_b4", M, status="审核不通过")]
    text = praise_stats_text()
    assert f"{M}：1/2" in text


def test_a_user_without_a_gender_is_not_in_the_denominator(env):
    """没填性别的进分母，男女两个比例都会被拉低，而管理员看不出为什么。"""
    env["praise"] = [_row(praise.PRAISE_STATUS_ACTIVE, WEEK, target="ou_b1",
                          day="2026-10-01", initiator="ou_g1")]
    env["users"] = [_user("ou_b1", M), _user("ou_b2", "")]
    assert f"{M}：1/1" in praise_stats_text()


def test_a_like_that_was_later_removed_still_counts_as_a_conversion(env):
    """点完赞去表白了，后来取消——那次转化确实发生过（_like_pairs 不按状态过滤）。"""
    env["praise"] = [_row(praise.PRAISE_STATUS_ACTIVE, WEEK, target="ou_g1",
                          day="2026-10-01", initiator="ou_b1")]
    env["users"] = [_user("ou_b1", M), _user("ou_g1", F)]
    env["likes"] = [_like("ou_b1", "ou_g1")]
    assert "1/1（100.0%）" in praise_stats_text()


def test_stats_with_no_praise_at_all_do_not_divide_by_zero(env):
    env["users"] = [_user("ou_b1", M)]
    text = praise_stats_text()
    assert "有效点赞 0 条" in text
    assert "0/0（0.0%）" in text
    assert "还没有点赞" in text


def test_stats_say_so_when_the_table_is_not_configured(env, monkeypatch):
    monkeypatch.setattr(praise_admin, "PRAISE_TABLE_ID", "")
    text = praise_stats_text()
    assert "还没配置" in text and "create_praise_table" in text


def test_stats_only_look_at_the_last_few_days(env, monkeypatch):
    """「每天的点赞总数」看最近几天就够了；全量列出来管理员也读不完。"""
    monkeypatch.setattr(praise_admin, "STATS_DAYS", 2)
    env["praise"] = [
        _row(praise.PRAISE_STATUS_ACTIVE, WEEK, target="ou_g1", day=f"2026-10-0{i}",
             initiator="ou_b1")
        for i in (1, 2, 3, 4)
    ]
    text = praise_stats_text()
    assert "2026-10-04" in text and "2026-10-03" in text
    assert "2026-10-01" not in text


def test_the_report_never_lists_who_praised_whom(env):
    """统计是汇总数，不是名单。管理员也不该能按人查「谁赞了谁」——
    点赞是匿名的，一旦有一处泄露，整条线就白设计了。"""
    env["praise"] = [_row(praise.PRAISE_STATUS_ACTIVE, WEEK, target="ou_abcdefgh",
                          day="2026-10-01", initiator="ou_12345678")]
    env["users"] = [_user("ou_12345678", M), _user("ou_abcdefgh", F)]
    text = praise_stats_text()
    assert "ou_12345678" not in text and "ou_abcdefgh" not in text


def test_a_week_always_has_seven_days():
    """钉住 W40 这个桶：上面所有用例的日期都建在它上面，桶的定义变了要一起改。"""
    start = datetime(2026, 9, 28, 12, 0, tzinfo=TZ)
    assert {praise.week_key(start + timedelta(days=i)) for i in range(7)} == {WEEK}
