"""POST /api/account/status：已结束/已取消活动的历史报名不得再阻塞脱单。

回归背景：toggle_account_status 原先只数「已报名」记录、不看活动状态，
而 cancel_signup 仅允许「报名中」取消；活动一结束，用户就再也取消不了报名，
被永久卡在「单身」无法切「已脱单」。

活动状态由 bot/管理台写入，按 docs/读取一致性.md 规则一必须实时读：
fixture 桩 _snap_fresh("activities") 并记录调用，锁定「判定不吃周期快照」。
其余桩法比照 tests/test_my_activities.py：Flask test client + cookie session +
monkeypatch 快照读取与 spool 写入，全程不触网、不落盘。
"""
import logging
import os
import sys

import pytest

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402

ME = "ou_me"
ENDED = "A-0001"
CANCELLED = "A-0002"
OPEN = "A-0003"
FULL = "A-0004"
MISSING = "A-9999"


def _user(status="单身"):
    return {"record_id": "rec_u_me",
            "fields": {"飞书用户ID": ME, "用户ID": "U-0001",
                       "昵称": "小北", "账号状态": status}}


def _activity(act_id, status):
    return {"record_id": "rec_act_" + act_id,
            "fields": {"活动ID": act_id, "活动名称": "活动" + act_id,
                       "活动状态": status}}


def _signup(act_id, status="已报名", oid=ME, seq=0):
    return {"record_id": "rec_s_%s_%s_%d" % (act_id, oid, seq),
            "fields": {"活动ID": act_id, "报名人open_id": oid, "状态": status}}


@pytest.fixture
def api(monkeypatch):
    state = {"user": _user(), "signups": [], "activities": [], "spooled": [],
             "fresh_calls": []}

    def fresh(key):
        state["fresh_calls"].append(key)
        return list(state["activities"]) if key == "activities" else []

    monkeypatch.setattr(app, "snap_self_user", lambda: state["user"])
    monkeypatch.setattr(app, "snap_signups_by_openid", lambda oid: list(state["signups"]))
    monkeypatch.setattr(app, "_snap_fresh", fresh)
    monkeypatch.setattr(app, "_spool_append", state["spooled"].append)
    # 端点成功后会就地刷新内存快照；换成空表，避免测试污染真实快照。
    monkeypatch.setattr(app, "_snapshot", {"users": []})
    app._rate_store.clear()
    client = app.app.test_client()
    client.set_cookie("yxq_session", app.create_session(ME))
    return client, state


def _post(client):
    return client.post("/api/account/status",
                       headers={"X-Requested-With": "XMLHttpRequest"})


def _assert_to_coupled(client, state, expect_fresh=True):
    r = _post(client)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["account_status"] == "已脱单"
    assert state["spooled"] == [{"type": "status", "record_id": "rec_u_me",
                                 "to_status": "已脱单"}]
    expected_fresh = ["activities"] if expect_fresh else []
    assert state["fresh_calls"] == expected_fresh, "活动状态必须实时读"


def test_ended_activity_signup_does_not_block(api):
    client, state = api
    state["activities"] = [_activity(ENDED, "已结束")]
    state["signups"] = [_signup(ENDED)]
    _assert_to_coupled(client, state)


def test_cancelled_activity_signup_does_not_block(api):
    client, state = api
    state["activities"] = [_activity(CANCELLED, "已取消")]
    state["signups"] = [_signup(CANCELLED)]
    _assert_to_coupled(client, state)


def test_missing_activity_signup_does_not_block(api):
    client, state = api
    # 活动表非空，但报名指向的活动已查不到（如活动记录被删）
    state["activities"] = [_activity(OPEN, "报名中")]
    state["signups"] = [_signup(MISSING)]
    _assert_to_coupled(client, state)


def test_empty_activity_table_fails_open_with_warning(api, caplog):
    client, state = api
    state["signups"] = [_signup(ENDED)]
    with caplog.at_level(logging.WARNING):
        _assert_to_coupled(client, state)
    assert "脱单门禁：活动表不可用" in caplog.text, "fail-open 必须留下可观测告警"


@pytest.mark.parametrize("status", ["报名中", "已满员", "未开始报名"])
def test_active_activity_signup_blocks(api, status):
    client, state = api
    state["activities"] = [_activity(OPEN, status)]
    state["signups"] = [_signup(OPEN)]
    r = _post(client)
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["error"] == "你已报名活动，请先取消报名再脱单"
    assert state["spooled"] == []
    assert state["fresh_calls"] == ["activities"], "拦截路径同样必须实时读活动状态"


def test_cancelled_signup_itself_does_not_block(api):
    client, state = api
    state["activities"] = [_activity(OPEN, "报名中")]
    state["signups"] = [_signup(OPEN, "已取消")]
    # 没有「已报名」记录时无需读活动表（helper 提前返回）
    _assert_to_coupled(client, state, expect_fresh=False)


def test_ended_and_open_signup_still_blocks(api):
    client, state = api
    state["activities"] = [_activity(ENDED, "已结束"), _activity(FULL, "报名中")]
    state["signups"] = [_signup(ENDED), _signup(FULL)]
    r = _post(client)
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["error"] == "你已报名活动，请先取消报名再脱单"
    assert state["spooled"] == []
    assert state["fresh_calls"] == ["activities"]


def test_coupled_to_single_still_allowed(api):
    client, state = api
    state["user"] = _user("已脱单")
    # 回切「单身」不受报名影响（原规则不变，也不该多读活动表）
    state["activities"] = [_activity(OPEN, "报名中")]
    state["signups"] = [_signup(OPEN)]
    r = _post(client)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["account_status"] == "单身"
    assert state["spooled"] == [{"type": "status", "record_id": "rec_u_me",
                                 "to_status": "单身"}]
    assert state["fresh_calls"] == []
