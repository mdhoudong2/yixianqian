"""自助现场签到：管理端设码 + 用户端 4 位码签到 + 用户接口不泄露签到码。

桩法沿用 tests/test_signin_gate.py 的 api fixture（FakeBitable + 快照桩 + 会话）。
"""
import json
import os
import sys

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402

from tests.test_signin_gate import (  # noqa: E402
    ACT,
    ATT,
    FLAG,
    _act_group,
    _me_client,
    _signup_att,
    api as signin_api,
)

CODE = "0123"


def _act(code=CODE, **over):
    fields = {"签到码": code, FLAG: "是"}
    fields.update(over)
    return _act_group(**fields)


def _self_signin(client, code=CODE):
    return client.post(
        "/api/activities/" + ACT + "/self-signin",
        json={"code": code},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )


def _admin_set_code(sess, code):
    return sess.post("/api/admin/activity/signin-code",
                    {"activity_id": ACT, "code": code})


# ---------------------------------------------------------------- 用户端：失败路径

def test_self_signin_requires_login():
    c = app.app.test_client()
    assert _self_signin(c).status_code == 401


def test_self_signin_requires_signup(signin_api):
    signin_api.install(activity=_act(), signups=[_signup_att("ou_f0", "")])
    r = _self_signin(_me_client("ou_m0"))
    assert r.status_code == 403
    assert r.get_json()["error"] == "你未报名此活动"


def test_self_signin_requires_activity_flag(signin_api):
    signin_api.install(activity=_act(**{FLAG: "否"}), signups=[_signup_att("ou_m0", "")])
    r = _self_signin(_me_client("ou_m0"))
    assert r.status_code == 400
    assert r.get_json()["error"] == "本活动未开启现场签到"


def test_self_signin_requires_code_configured(signin_api):
    signin_api.install(activity=_act_group(), signups=[_signup_att("ou_m0", "")])
    r = _self_signin(_me_client("ou_m0"))
    assert r.status_code == 400
    assert r.get_json()["error"] == "本活动未设置签到码，请联系管理员"


def test_self_signin_wrong_code_does_not_write(signin_api):
    signin_api.install(activity=_act(), signups=[_signup_att("ou_m0", "")])
    r = _self_signin(_me_client("ou_m0"), "9999")
    assert r.status_code == 400
    assert r.get_json()["error"] == "签到码不正确，请向现场工作人员确认"
    assert signin_api.fake.writes() == []
    assert signin_api.fake.tables[app.SIGNUP_TABLE_ID][0]["fields"][ATT] == ""


def test_self_signin_absent_is_not_overwritten(signin_api):
    signin_api.install(activity=_act(), signups=[_signup_att("ou_m0", "未到")])
    r = _self_signin(_me_client("ou_m0"))
    assert r.status_code == 400
    assert r.get_json()["error"] == "你已被标记为未到，请联系管理员"
    assert signin_api.fake.writes() == []
    assert signin_api.fake.tables[app.SIGNUP_TABLE_ID][0]["fields"][ATT] == "未到"


# ---------------------------------------------------------------- 用户端：成功 / 幂等

def test_self_signin_success_then_idempotent(signin_api):
    signin_api.install(activity=_act(), signups=[_signup_att("ou_m0", "")])
    c = _me_client("ou_m0")
    r = _self_signin(c)
    assert r.status_code == 200, r.get_json()
    assert r.get_json() == {"ok": True, "attendance": "按时"}
    assert signin_api.fake.tables[app.SIGNUP_TABLE_ID][0]["fields"][ATT] == "按时"
    assert "signups" in signin_api.refresh_calls
    writes = len(signin_api.fake.writes())

    # 已到 → 幂等成功（错误码也不校验、不重复写）
    r2 = _self_signin(c, "0000")
    assert r2.status_code == 200, r2.get_json()
    assert r2.get_json() == {"ok": True, "already": True, "attendance": "按时"}
    assert len(signin_api.fake.writes()) == writes


def test_self_signin_late_counts_as_already_present(signin_api):
    signin_api.install(activity=_act(), signups=[_signup_att("ou_m0", "迟到")])
    r = _self_signin(_me_client("ou_m0"), "0000")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "already": True, "attendance": "迟到"}
    assert signin_api.fake.writes() == []


def test_self_signin_rate_limited(signin_api):
    signin_api.install(activity=_act(), signups=[_signup_att("ou_m0", "")])
    c = _me_client("ou_m0")
    for _ in range(8):
        assert _self_signin(c, "9999").status_code == 400
    r = _self_signin(c, "9999")
    assert r.status_code == 429
    assert "频繁" in r.get_json()["error"]


def test_self_signin_read_failure_is_500(signin_api):
    signin_api.install(activity=_act(), signups=[_signup_att("ou_m0", "")])
    signin_api.fake.search_fails = {app.SIGNUP_TABLE_ID}
    r = _self_signin(_me_client("ou_m0"))
    assert r.status_code == 500
    assert r.get_json()["code"] == "SIGNIN_QUERY_FAILED"


# ---------------------------------------------------------------- 管理端：设码 / 读码

def test_admin_signin_code_requires_admin(signin_api):
    c = app.app.test_client()
    r = c.post("/api/admin/activity/signin-code",
               json={"activity_id": ACT, "code": CODE},
               headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 401
    r2 = signin_api.post("/api/admin/activity/signin-code",
                  {"activity_id": ACT, "code": CODE}, client=signin_api.client_me)
    assert r2.status_code == 403


def test_admin_signin_code_validation(signin_api):
    signin_api.install(activity=_act_group())
    for bad in ("123", "12345", "12a4", "abcd", "", 123, None, True):
        r = signin_api.post("/api/admin/activity/signin-code",
                     {"activity_id": ACT, "code": bad})
        assert r.status_code == 400, (bad, r.get_json())
        assert r.get_json()["error"] == "签到码须为4位数字"
    assert signin_api.fake.writes() == []


def test_admin_signin_code_save_and_status(signin_api):
    signin_api.install(activity=_act_group())
    r = _admin_set_code(signin_api, "0123")
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    assert signin_api.fake.tables[app.ACTIVITY_TABLE_ID][0]["fields"]["签到码"] == "0123"
    assert "activities" in signin_api.refresh_calls

    # 4 位 int 也合法（转字符串后仍是 4 位，不补零）
    assert _admin_set_code(signin_api, 1234).status_code == 200
    assert signin_api.fake.tables[app.ACTIVITY_TABLE_ID][0]["fields"]["签到码"] == "1234"

    # 管理端状态回包含当前码
    signin_api.snapshot["activities"] = signin_api.fake.tables[app.ACTIVITY_TABLE_ID]
    st = signin_api.get("/api/admin/group/status?activity_id=" + ACT)
    assert st.status_code == 200, st.get_json()
    assert st.get_json()["signin_code"] == "1234"


def test_admin_signin_code_write_failure(signin_api, monkeypatch):
    signin_api.install(activity=_act_group())
    monkeypatch.setattr(signin_api.fake, "update_record", lambda *a, **k: None)
    r = _admin_set_code(signin_api, "0123")
    assert r.status_code == 500
    assert r.get_json()["code"] == "WRITE_FAILED"


# ---------------------------------------------------------------- 用户接口不泄露签到码

def _assert_no_code(body):
    text = json.dumps(body, ensure_ascii=False)
    assert "签到码" not in text
    assert CODE not in text


def test_user_activity_endpoints_do_not_leak_signin_code(signin_api, monkeypatch):
    signin_api.install(activity=_act(), signups=[_signup_att("ou_m0", "")])
    monkeypatch.setattr(app, "touch_last_active", lambda *a, **k: None)
    monkeypatch.setattr(app, "attach_quota", lambda brief, oid: brief)
    monkeypatch.setattr(app, "user_has_face", lambda *a, **k: True)
    monkeypatch.setattr(app, "recommend_view", lambda oid: ([], ""))
    monkeypatch.setattr(app, "_cached_brief", lambda oid: {})

    c = _me_client("ou_m0")
    for path in ("/api/activities", "/api/activities/" + ACT,
                 "/api/activities/mine", "/api/home"):
        r = c.get(path)
        assert r.status_code == 200, (path, r.get_json())
        _assert_no_code(r.get_json())
