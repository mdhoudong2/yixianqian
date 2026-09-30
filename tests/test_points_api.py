"""H5 麦穗接口契约：形状、错误翻译（不联网）。

前端是 CDN 引的静态页，接口形状一变就是「页面静静少了一块」。这里钉的是
前端真正读的那几个键，顺带覆盖最容易写错的一条接线：

1. **待确认的穗不进余额**——7 天确认期没满就显示成可用，用户会去兑、然后兑失败。
"""
import os
import sys
import time

import pytest

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402

from lib import points, points_invite, points_redeem  # noqa: E402

ME = "ou_me"
OTHER = "ou_other"

# 用户表里属于「我」的那一行。手机号要跟下面登记邀请时用的号码一致，
# 否则「不能邀请自己」那条闸不会触发。
ME_FIELDS = {"用户ID": "U-0001", "昵称": "小北", "手机号": "13800000001",
             "账号状态": "单身"}


@pytest.fixture
def client(monkeypatch):
    """已登录的测试客户端 + 一套「表格里我刚注册好」的桩。

    只桩表格读（用户档案、活动），不桩 lib —— 麦穗的规则全在 lib 里，
    桩掉它这文件就只剩在测 mock 了。
    """
    monkeypatch.setattr(app, "snap_self_user",
                        lambda: {"record_id": "rec_me", "fields": dict(ME_FIELDS)})
    monkeypatch.setattr(app, "snap_find_user_by_openid",
                        lambda oid: {"record_id": "rec_me", "fields": dict(ME_FIELDS)})
    monkeypatch.setattr(app, "_snap", lambda name: [])
    monkeypatch.setattr(app, "refresh_snapshot_table_async", lambda *a, **k: None)
    monkeypatch.setattr(app, "quota_view", lambda oid, likes_snap=None: {
        "anon_left": 10, "anon_total": 10, "real_left": 1, "real_total": 1,
        "real_permanent": 0})
    # 心愿要查「我报没报名」。默认「报了」，各用例按需覆盖成 None。
    monkeypatch.setattr(app.bitable, "get_user_signup",
                        lambda aid, oid: {"record_id": "rec_signup", "fields": {}})
    # 限流计数器是模块级的，跨用例累加。不清的话，跑得越多越容易撞上 429，
    # 而那是测试自己制造的假失败。
    app._rate_store.clear()

    c = app.app.test_client()
    c.set_cookie("yxq_session", app.create_session(ME))
    return c


def _post(c, url, body=None):
    """带 CSRF 头的 POST —— 网关对已登录写接口强制要求，少了这头会 403。"""
    return c.post(url, json=body or {}, headers={"X-Requested-With": "XMLHttpRequest"})


def _fund(n=100, oid=ME):
    points.grant(oid, n, points.KIND_ADMIN, reason="测试预置")


def _delete(c, url):
    """带 CSRF 头的 DELETE —— 网关对已登录写接口（含 DELETE）强制要求。"""
    return c.delete(url, headers={"X-Requested-With": "XMLHttpRequest"})


# ---------------------------------------------------------------- 未登录

@pytest.mark.parametrize("url", [
    "/api/points/me", "/api/points/rules", "/api/points/invite",
    "/api/points/redemptions",
])
def test_reading_without_login_is_401(url):
    c = app.app.test_client()
    assert c.get(url).status_code == 401


def test_writing_without_csrf_header_is_403(client):
    """跨站表单能带上 cookie，所以写接口额外要一个自定义头。"""
    r = client.post("/api/points/redeem", json={"item": "real_like"})
    assert r.status_code == 403


# ---------------------------------------------------------------- 我的麦穗

def test_me_returns_everything_the_header_needs(client, points_db):
    _fund(35)
    d = client.get("/api/points/me").get_json()
    assert d["balance"] == 35
    assert d["earned"] == 35 and d["spent"] == 0
    assert d["pending_points"] == 0 and d["pending_count"] == 0
    # 实名额度两块：兑换攒下的名额，以及叠加后的剩余/总量
    assert d["real_like_quota"] == 0
    assert d["real_left"] == 1 and d["real_total"] == 1
    assert [e["kind"] for e in d["entries"]] == [points.KIND_ADMIN]
    assert d["entries"][0]["kind_label"]        # 前端直接显示这个，不能是空


def test_confirmed_invites_are_pending_not_spendable(client, points_db, clock):
    """确认期内的邀请显示成「在路上的穗」，但不进余额、兑不动。"""
    points_invite.record_from_form("ou_friend", "13800000002", "女性", ME)
    points_invite.start_confirm_window("ou_friend", "13800000002", "女性")

    d = client.get("/api/points/me").get_json()
    assert d["pending_points"] == 20       # 女生 20 穗，看得见
    assert d["pending_count"] == 1
    assert d["balance"] == 0               # 但花不了

    r = _post(client, "/api/points/redeem", {"item": "real_like"})
    assert r.status_code == 400
    assert "余额不足" in r.get_json()["error"]


def test_pending_turns_into_balance_once_the_window_expires(client, points_db, clock):
    points_invite.record_from_form("ou_friend", "13800000002", "女性", ME)
    points_invite.start_confirm_window("ou_friend", "13800000002", "女性")
    clock(days=points_invite.points_config.get("invite_confirm_days"))

    points_invite.settle_all(is_banned=lambda oid: False)
    d = client.get("/api/points/me").get_json()
    assert d["balance"] == 20
    assert d["pending_points"] == 0


def test_me_reports_the_extra_real_like_quota(client, points_db):
    _fund(100)
    _post(client, "/api/points/redeem", {"item": "real_like", "request_key": "k1"})
    d = client.get("/api/points/me").get_json()
    assert d["real_like_quota"] == 1
    assert d["balance"] == 80


# ---------------------------------------------------------------- 规则说明

def test_rules_list_every_item_with_a_live_price(client, points_db):
    d = client.get("/api/points/rules").get_json()
    got = {i["item"]: i for i in d["items"]}
    assert set(got) == set(points_redeem.ITEM_LABELS)
    assert got["real_like"]["cost"] == points_redeem.cost_of("real_like")
    assert all(i["label"] and i["desc"] for i in d["items"])
    assert d["config"]["invite_confirm_days"] >= 0
    assert d["notes"]


def test_rules_reflect_an_admin_price_change(client, points_db):
    """管理员在机器人那边改了价，规则页刷新就变——数值只有一处真理。"""
    from lib import points_config
    points_config.set_value("redeem_wish", 88)
    d = client.get("/api/points/rules").get_json()
    cost = {i["item"]: i["cost"] for i in d["items"]}
    assert cost["wish"] == 88


# ---------------------------------------------------------------- 邀请

def test_invite_view_shows_id_share_text_and_my_link(client, points_db):
    d = client.get("/api/points/invite").get_json()
    assert d["inviter_id"] == "U-0001"        # 注册表单里填的就是这个
    assert d["share_text"]
    assert d["register_url"]
    assert d["progress"]["total"] == 0 and d["list"] == []


# ---------------------------------------------------------------- 兑换

def test_redeem_returns_balance_and_order_for_the_client(client, points_db):
    _fund(50)
    d = _post(client, "/api/points/redeem", {"item": "real_like"}).get_json()
    assert d["ok"] and d["balance"] == 30
    assert d["order"]["item_label"] == "额外实名喜欢"
    assert d["order"]["status_label"]


def test_redeem_without_enough_points_says_so(client, points_db):
    d = _post(client, "/api/points/redeem", {"item": "matchmaker",
                                             "condition": "找一个爱笑的"}).get_json()
    assert "余额不足" in d["error"]


def test_the_same_request_key_is_not_charged_twice(client, points_db):
    """双击/断网重试都会带同一个 request_key，第二次必须是「已兑过」而不是再扣一笔。"""
    _fund(100)
    first = _post(client, "/api/points/redeem",
                  {"item": "real_like", "request_key": "abc"}).get_json()
    second = _post(client, "/api/points/redeem",
                   {"item": "real_like", "request_key": "abc"}).get_json()
    assert first["ok"] and first["balance"] == 80
    assert "已经开过单" in second["error"]
    assert client.get("/api/points/me").get_json()["balance"] == 80


def test_an_unknown_item_is_rejected_before_touching_the_ledger(client, points_db):
    _fund(100)
    r = _post(client, "/api/points/redeem", {"item": "换个女朋友"})
    assert r.status_code == 400
    assert client.get("/api/points/me").get_json()["balance"] == 100


def test_matchmaker_needs_a_condition(client, points_db):
    _fund(100)
    d = _post(client, "/api/points/redeem", {"item": "matchmaker"}).get_json()
    assert "条件" in d["error"]
    assert client.get("/api/points/me").get_json()["balance"] == 100


def test_only_one_open_matchmaker_order_at_a_time(client, points_db):
    _fund(200)
    _post(client, "/api/points/redeem", {"item": "matchmaker", "condition": "温柔"})
    second = _post(client, "/api/points/redeem",
                   {"item": "matchmaker", "condition": "再找一次"}).get_json()
    assert "没完成" in second["error"]
    assert client.get("/api/points/me").get_json()["balance"] == 150


def test_redemptions_lists_my_orders_newest_first(client, points_db):
    _fund(100)
    _post(client, "/api/points/redeem", {"item": "real_like"})
    _post(client, "/api/points/redeem", {"item": "matchmaker", "condition": "爱笑"})
    lst = client.get("/api/points/redemptions").get_json()["list"]
    assert [o["item"] for o in lst] == ["matchmaker", "real_like"]
    assert lst[0]["params"]["condition"] == "爱笑"    # 前端要回显条件


# ---------------------------------------------------------------- 活动类兑换

def _activity(monkeypatch, *, fee=100, quota=10, status="报名中", name="周末桌游",
              start_ts=None):
    rec = {"record_id": "rec_act", "fields": {
        "活动名称": name, "报名人数上限": quota, "费用": fee, "活动状态": status,
        "开始时间": start_ts}}
    monkeypatch.setattr(app, "snap_resolve_activity", lambda aid: (rec, "ACT-1"))
    return rec


def test_priority_redeem_checks_the_activity(client, points_db, monkeypatch):
    _activity(monkeypatch)
    _fund(100)
    d = _post(client, "/api/points/redeem",
              {"item": "priority_signup", "activity_record_id": "rec_act"}).get_json()
    assert d["ok"] and d["balance"] == 70
    assert d["order"]["params"]["activity_id"] == "ACT-1"


def test_priority_redeem_on_a_closed_activity_is_refused(client, points_db, monkeypatch):
    _activity(monkeypatch, status="已结束")
    _fund(100)
    d = _post(client, "/api/points/redeem",
              {"item": "priority_signup", "activity_record_id": "rec_act"}).get_json()
    assert "不能使用" in d["error"]
    assert client.get("/api/points/me").get_json()["balance"] == 100


def test_priority_redeem_works_before_registration_opens(client, points_db, monkeypatch):
    """「未开始报名」也算可兑——这恰恰是抢优先位最有用的时候。

    需求原文是「已发布、且未开始报名或还有名额」，所以这两个状态都得放行；
    只有「已满员」「已结束」这类才拦。
    """
    _activity(monkeypatch, status="未开始报名")
    _fund(100)
    d = _post(client, "/api/points/redeem",
              {"item": "priority_signup", "activity_record_id": "rec_act"}).get_json()
    assert d["ok"] and d["balance"] == 70


def test_priority_redeem_is_refused_when_the_activity_is_full(client, points_db,
                                                              monkeypatch):
    """已满员 = 没有名额可给，优先位也用不了。"""
    _activity(monkeypatch, status="已满员")
    _fund(100)
    d = _post(client, "/api/points/redeem",
              {"item": "priority_signup", "activity_record_id": "rec_act"}).get_json()
    assert "不能使用" in d["error"]
    assert client.get("/api/points/me").get_json()["balance"] == 100


def test_redeeming_for_a_missing_activity_is_404(client, points_db, monkeypatch):
    monkeypatch.setattr(app, "snap_resolve_activity", lambda aid: (None, None))
    _fund(100)
    r = _post(client, "/api/points/redeem",
              {"item": "wish", "activity_record_id": "nope", "target_open_id": OTHER})
    assert r.status_code == 404


def test_wish_must_name_a_target_and_not_yourself(client, points_db, monkeypatch):
    _activity(monkeypatch)
    _fund(200)
    d = _post(client, "/api/points/redeem",
              {"item": "wish", "activity_record_id": "rec_act"}).get_json()
    assert "指定一位" in d["error"]

    d = _post(client, "/api/points/redeem",
              {"item": "wish", "activity_record_id": "rec_act",
               "target_open_id": ME}).get_json()
    assert "自己" in d["error"]
    assert client.get("/api/points/me").get_json()["balance"] == 200


def test_wish_needs_my_own_signup(client, points_db, monkeypatch):
    """规则是「报名成功后指定」：人不在场，心愿没人能安排，30 穗会白花。"""
    _activity(monkeypatch)
    _fund(100)
    monkeypatch.setattr(app.bitable, "get_user_signup", lambda aid, oid: None)

    d = _post(client, "/api/points/redeem",
              {"item": "wish", "activity_record_id": "rec_act",
               "target_open_id": OTHER}).get_json()
    assert "先报名" in d["error"]
    assert client.get("/api/points/me").get_json()["balance"] == 100


def test_wish_form_errors_win_over_the_signup_gate(client, points_db, monkeypatch):
    """没填对方是谁、又没报名时，先说的是「填完」，不是「你还没报名」。

    两句都对，但顺序反了会让人先跑去报名，回来再填一次才发现还差一步。
    """
    _activity(monkeypatch)
    _fund(100)
    monkeypatch.setattr(app.bitable, "get_user_signup", lambda aid, oid: None)

    d = _post(client, "/api/points/redeem",
              {"item": "wish", "activity_record_id": "rec_act"}).get_json()
    assert "指定一位" in d["error"]


def test_wish_works_once_i_am_signed_up(client, points_db, monkeypatch):
    """这个用例同时钉住「查的是实表不是快照」：快照可以空着，报名记录必须读得到。"""
    _activity(monkeypatch)
    _fund(100)
    monkeypatch.setattr(app, "_snap", lambda name: [])
    monkeypatch.setattr(app, "snap_signup", lambda aid, oid: None)   # 快照说没有
    d = _post(client, "/api/points/redeem",
              {"item": "wish", "activity_record_id": "rec_act",
               "target_open_id": OTHER}).get_json()
    assert d["ok"] and d["balance"] == 70


# ---------------------------------------------------------------- 取消报名时的优先名额

def _cancel_env(monkeypatch, *, hours_to_start):
    """取消报名的环境：活动「报名中」、我有一条「已报名」记录。"""
    _activity(monkeypatch)
    monkeypatch.setattr(app, "snap_signup",
                        lambda aid, oid: {"record_id": "rec_signup"})
    monkeypatch.setattr(app.bitable, "update_record", lambda t, r, v: True)
    monkeypatch.setattr(app.bitable, "get_timestamp",
                        lambda f, k, d=None: time.time() + hours_to_start * 3600)


def _buy_priority():
    act = {"id": "ACT-1", "title": "周末桌游", "quota": 10, "fee": 0,
           "start_at": "", "open": True}
    return points_redeem.redeem_priority(ME, act)


def test_cancelling_early_gives_the_priority_points_back(client, points_db, monkeypatch):
    """开始前 48 小时以上主动取消 → 退穗。"""
    _cancel_env(monkeypatch, hours_to_start=72)
    _fund(100)
    order = _buy_priority()
    assert client.get("/api/points/me").get_json()["balance"] == 70

    d = _delete(client, "/api/activities/rec_act/signup").get_json()
    assert d["ok"] and d["balance"] == 100
    assert "30 穗" in d["message"]
    assert points_redeem.get(order["id"])["status"] == points_redeem.ST_REFUNDED


def test_cancelling_late_keeps_the_points(client, points_db, monkeypatch):
    """48 小时以内取消 → 不退穗，但单子要收掉——留着 active 会一直占着
    「每场 30%」的名额上限。"""
    _cancel_env(monkeypatch, hours_to_start=10)
    _fund(100)
    order = _buy_priority()

    d = _delete(client, "/api/activities/rec_act/signup").get_json()
    assert d["ok"] and "balance" not in d
    assert client.get("/api/points/me").get_json()["balance"] == 70
    assert points_redeem.get(order["id"])["status"] == points_redeem.ST_CANCELLED


def test_cancelling_without_a_priority_order_is_unaffected(client, points_db,
                                                           monkeypatch):
    """没兑过优先名额的人取消报名，回复跟以前一模一样（不多一句退穗）。"""
    _cancel_env(monkeypatch, hours_to_start=72)
    d = _delete(client, "/api/activities/rec_act/signup").get_json()
    assert d == {"ok": True, "message": "已取消报名"}
