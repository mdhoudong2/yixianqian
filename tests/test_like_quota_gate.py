"""H5 喜欢额度门禁的计数口径单测（不联网）。

回归的是 2026-09 测试服 E2E 抓到的那次超发：用户在限流窗口滑动的那一瞬间
连点两次，第 11 颗匿名额度被放行了。根因是三个计数源**全都有损**——
在途意图 60 秒后就不算了、likes 快照 20 秒才刷一轮、quota.json 更慢——
而它们之间的缝正好落在双击上。

修法是不再各数各的，改成「快照 ∪ 在途意图」按 (目标, 月份) 去重后，
整份喂给 lib.quota 的函数。这里锁的就是这个并集口径。
"""
import os
import sys
import time

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend"
)
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402
from app import _intent_likes, _like_triples_for, quota_view  # noqa: E402
from config import (  # noqa: E402
    F_LIKE_INITIATOR_OPENID,
    F_LIKE_MONTH,
    F_LIKE_STATUS,
    F_LIKE_TARGET_OPENID,
    F_LIKE_TYPE,
)

from lib import quota  # noqa: E402

ME = "ou_me"
THIS_MONTH = quota.month_key()


def _months_before(n):
    """当前月往前推 n 个月的 %Y-%m。不写死日期，否则这个测试下个月就过期。"""
    y, m = int(THIS_MONTH[:4]), int(THIS_MONTH[5:])
    m -= n
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}"


def _text(v):
    """多维表格文本字段的值形态。"""
    return [{"text": v, "type": "text"}]


def _record(target, month=THIS_MONTH, status="单向喜欢", like_type="匿名", initiator=ME):
    return {"fields": {
        F_LIKE_INITIATOR_OPENID: _text(initiator),
        F_LIKE_TARGET_OPENID: _text(target),
        F_LIKE_STATUS: status,
        F_LIKE_TYPE: like_type,
        F_LIKE_MONTH: _text(month),
    }}


def _intent(target, month=THIS_MONTH, like_type="匿名", oid=ME):
    return {"oid": oid, "target": target, "type": like_type, "month": month,
            "ts": time.time()}


def setup_function():
    """每个用例都从干净的在途意图开始——它是个模块级字典，会跨用例漏。"""
    _intent_likes.clear()


def test_counts_a_like_that_landed_but_the_snapshot_has_not_seen_yet():
    """核心回归：已受理、已落库、但快照还没刷到的那一条必须算数。

    这条就是第 11 颗额度被放行的原因——意图过了 60 秒不算了，快照又还没刷到，
    两边都说「没这条」。
    """
    snap = [_record(f"ou_t{i}") for i in range(9)]  # 快照里只有 9 条
    _intent_likes["k1"] = _intent("ou_t9")          # 第 10 条刚受理，快照没刷到

    triples = _like_triples_for(ME, snap)
    assert len(triples) == 10
    # 满 10 颗：门禁必须在这里拦住，而不是放行第 11 次
    assert quota.anon_left(triples, THIS_MONTH) == 0


def test_snapshot_catching_up_does_not_double_count():
    """快照追上来之后，同一条喜欢不能既算快照那份又算意图那份。

    去重做错的方向有两个，这里都锁住：算两次会让用户提前被挡在门外，
    算零次就是上面那条超发。
    """
    snap = [_record(f"ou_t{i}") for i in range(10)]  # 10 条全在快照里了
    _intent_likes["k1"] = _intent("ou_t9")           # 其中一条的意图还留着

    triples = _like_triples_for(ME, snap)
    assert len(triples) == 10
    assert quota.anon_left(triples, THIS_MONTH) == 0


def test_same_target_in_two_months_is_two_separate_likes():
    """同一个人上个月喜欢过、这个月又喜欢，是两条独立记录。

    只按目标去重会把上个月那条吞掉，那条到期该退的额度就退不出来了
    （lib.quota.anon_left 正是靠「满 3 个月」的那条来退额）。
    """
    snap = [_record("ou_same", month=_months_before(quota.ANON_EXPIRE_MONTHS)),
            _record("ou_same", month=THIS_MONTH)]
    triples = _like_triples_for(ME, snap)
    assert len(triples) == 2
    # 本月的算 used，满 3 个月那条退一颗 → 10 - 1 + 1 = 10
    assert quota.anon_left(triples, THIS_MONTH) == 10


def test_ignores_other_people_and_rejected_likes():
    """别人的喜欢、被驳回的喜欢都不占自己的额度。

    注意断言的是 lib.quota 算出来的额度，不是三元组的条数：被驳回的记录
    **本来就该原样传进去**（机器人也这么喂），由 lib.quota 决定它不算数。
    在这里多滤一道就等于又写了一份口径。
    """
    snap = [
        _record("ou_t1", initiator="ou_someone_else"),
        _record("ou_t2", status="被驳回"),
        _record("ou_t3"),
    ]
    triples = _like_triples_for(ME, snap)
    assert quota.anon_left(triples, THIS_MONTH) == 9


def test_rejected_then_re_liked_same_target_counts_once_as_active():
    """被驳回之后重新喜欢成功：同 (目标, 月份) 的两条记录只能按有效的那条算一次。

    留错了两个方向都错：留被驳回那条 → 少算一颗额度；两条都算 → 多算一颗。
    """
    snap = [_record("ou_t1", status="被驳回"), _record("ou_t1")]
    # 先喂被驳回的、再喂有效的，两个顺序都要收敛到同一个结果
    assert quota.anon_left(_like_triples_for(ME, snap), THIS_MONTH) == 9
    assert quota.anon_left(_like_triples_for(ME, list(reversed(snap))), THIS_MONTH) == 9


def test_missing_like_type_counts_as_anon():
    """没写「喜欢类型」的存量行按匿名算，与 lib.quota 的缺省口径一致。"""
    snap = [_record("ou_t1", like_type="")]
    triples = _like_triples_for(ME, snap)
    assert len(triples) == 1
    assert quota.anon_left(triples, THIS_MONTH) == 9


def test_real_name_likes_do_not_eat_anonymous_quota():
    """实名不占匿名那 10 颗：两条实名进池子，匿名仍应是满的。"""
    snap = [_record("ou_t1", like_type="实名"), _record("ou_t2", like_type="实名")]
    triples = _like_triples_for(ME, snap)
    assert quota.anon_left(triples, THIS_MONTH) == 10


def _authority(anon_left=10, real_left=1, permanent=0):
    """把机器人发布的额度快照钉成指定值。"""
    app._quota_file = lambda: {"quota": {ME: {
        "anon_left": anon_left, "anon_total": 10,
        "real_left": real_left, "real_total": 1 + permanent,
        "real_permanent": permanent}}}


def test_quota_view_never_double_counts_once_the_bot_has_caught_up(monkeypatch):
    """机器人已经扣掉这几颗、在途意图却还没过期时，不能再扣第二次。

    这是 E2E 里第 10 次点击被误拒的直接原因：quota.json 已经算到只剩 1 颗，
    9 条意图还在窗口内，相加得 1 − 9 → 0，于是把还有额度的用户拦在门外。
    """
    monkeypatch.setattr(app, "_quota_file", lambda: {"quota": {ME: {
        "anon_left": 1, "anon_total": 10, "real_left": 1, "real_total": 1,
        "real_permanent": 0}}})
    snap = [_record(f"ou_t{i}") for i in range(9)]
    for i in range(9):
        _intent_likes[f"k{i}"] = _intent(f"ou_t{i}")

    assert quota_view(ME, snap)["anon_left"] == 1


def test_quota_view_still_gives_zero_second_feedback_before_the_bot_catches_up(monkeypatch):
    """反过来那一半也要成立：机器人还没追上时，刚点下去的那一颗要立刻体现。

    只信权威值的话，用户点完看到的额度会先跳回去、等机器人对账再跳回来。
    """
    monkeypatch.setattr(app, "_quota_file", lambda: {"quota": {ME: {
        "anon_left": 10, "anon_total": 10, "real_left": 1, "real_total": 1,
        "real_permanent": 0}}})
    _intent_likes["k0"] = _intent("ou_t0")  # 刚受理，表里还没有

    assert quota_view(ME, [])["anon_left"] == 9


def test_quota_view_real_left_keeps_permanent_invite_slots(monkeypatch):
    """实名那道也不能被「本月已用过」一刀切：永久名额要算进去。"""
    monkeypatch.setattr(app, "_quota_file", lambda: {"quota": {ME: {
        "anon_left": 10, "anon_total": 10, "real_left": 3, "real_total": 3,
        "real_permanent": 2}}})
    snap = [_record("ou_t1", like_type="实名")]

    assert quota_view(ME, snap)["real_left"] == 2


def test_real_left_counts_permanent_invite_slots():
    """有永久名额的人用完本月那次实名后，不该被「本月用过实名」一刀切拦掉。

    这是第二道防线最容易写错的地方——旧写法一看本月有实名记录就 400，
    把邀请/加赠换来的永久名额给忽略了。
    """
    snap = [_record("ou_t1", like_type="实名")]
    triples = _like_triples_for(ME, snap)
    assert quota.real_left(triples, permanent=0, ym=THIS_MONTH) == 0   # 没名额：用完
    assert quota.real_left(triples, permanent=2, ym=THIS_MONTH) == 2   # 有 2 个：还能用
