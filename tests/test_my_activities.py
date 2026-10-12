"""GET /api/activities/mine：我的活动列表必须过滤「已取消」+ 按活动去重（N1 回归）。

同族端点 my_groups / my_onetoone 用 snap_active_signups_by_openid（过滤已取消 +
按活动去重），my_activities 原先漏用不过滤的 snap_signups_by_openid：
取消报名后活动仍留在「我的活动」，且与活动详情页的 my_signup=false 矛盾。

桩法比照 tests/test_messages_api.py：只桩快照读取，过滤/去重/格式化全跑真实现；
bitable.search_records 直接 fail 掉，确保不触达飞书实时查询。
"""
import os
import sys

import pytest

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402

ME = "ou_me"
ACTIVE = "A-0100"
CANCELLED = "A-0101"
REBOOKED = "A-0102"


def _activity(act_id):
    return {"record_id": f"rec_act_{act_id}",
            "fields": {"活动ID": act_id, "活动名称": "活动" + act_id,
                       "活动状态": "报名中"}}


def _signup(act_id, status="已报名", oid=ME, seq=0):
    return {"record_id": f"rec_s_{act_id}_{oid}_{seq}",
            "fields": {"活动ID": act_id, "报名人open_id": oid, "状态": status}}


@pytest.fixture
def api(monkeypatch):
    tables = {
        "users": [{"record_id": "rec_u_me",
                   "fields": {"飞书用户ID": ME, "用户ID": "U-0001",
                              "昵称": "小北", "账号状态": "单身"}}],
        "observers": [],
    }

    def snap(key):
        return [dict(r) for r in tables.get(key, [])]

    monkeypatch.setattr(app, "_snap_live", snap)
    monkeypatch.setattr(app, "_snap", snap)
    monkeypatch.setattr(
        app.bitable, "search_records",
        lambda *a, **k: pytest.fail("本用例不应触达飞书实时查询"))
    app._rate_store.clear()
    c = app.app.test_client()
    c.set_cookie("yxq_session", app.create_session(ME))
    return c, tables


def _mine_ids(c):
    r = c.get("/api/activities/mine")
    assert r.status_code == 200, r.get_json()
    return [a["activity_id"] for a in r.get_json()["activities"]]


def test_my_activities_filters_cancelled(api):
    c, tables = api
    tables["activities"] = [_activity(ACTIVE), _activity(CANCELLED)]
    tables["signups"] = [_signup(ACTIVE), _signup(CANCELLED, "已取消")]
    r = c.get("/api/activities/mine")
    assert r.status_code == 200, r.get_json()
    acts = r.get_json()["activities"]
    assert [a["activity_id"] for a in acts] == [ACTIVE], \
        "已取消的活动不得出现在「我的活动」"
    assert acts[0]["my_signup"] is True
    assert acts[0]["signup_status"] == "已报名"


def test_my_activities_only_cancelled_is_empty(api):
    c, tables = api
    tables["activities"] = [_activity(CANCELLED)]
    tables["signups"] = [_signup(CANCELLED, "已取消")]
    assert _mine_ids(c) == [], "只有已取消报名时列表应为空"


def test_my_activities_keeps_active_and_dedupes_rebook(api):
    c, tables = api
    tables["activities"] = [_activity(ACTIVE), _activity(REBOOKED)]
    tables["signups"] = [
        _signup(REBOOKED, "已取消"),
        _signup(REBOOKED, "已报名", seq=1),  # 取消后重新报名：后一条（较新）为准
        _signup(ACTIVE),
    ]
    acts = c.get("/api/activities/mine").get_json()["activities"]
    assert sorted(a["activity_id"] for a in acts) == sorted([ACTIVE, REBOOKED])
    statuses = {a["activity_id"]: a["signup_status"] for a in acts}
    assert statuses[REBOOKED] == "已报名", "同一活动多笔时应取最新一条"
    assert statuses[ACTIVE] == "已报名", "正常报名的活动不受影响"
