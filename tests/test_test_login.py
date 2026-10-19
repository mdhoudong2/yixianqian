"""測試服快速登入：預設關閉、密鑰校驗、白名單與 session 建立。

此入口只供測試服查看網頁；生產 local_config 不設 TEST_LOGIN_ENABLED/KEY 即 404。
"""
import os
import sys

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend"
)
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402

_KEY = "test-key-123"


def _record(uid="U-0006", oid="ou_test_0006"):
    return {
        "record_id": "rec_test",
        "fields": {
            app.F_USER_ID: uid,
            app.F_FEISHU_ID: oid,
            app.F_NICKNAME: "测试用户",
            app.F_ACCOUNT_STATUS: "单身",
        },
    }


def _client():
    app.app.config["TESTING"] = True
    return app.app.test_client()


def _enable(monkeypatch, ids=None):
    monkeypatch.setattr(app, "TEST_LOGIN_ENABLED", True)
    monkeypatch.setattr(app, "TEST_LOGIN_KEY", _KEY)
    monkeypatch.setattr(app, "TEST_LOGIN_ALLOW_IDS", ids or ["U-0006"])
    monkeypatch.setattr(app, "roles_of", lambda oid: {"user"})


def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr(app, "TEST_LOGIN_ENABLED", False)
    monkeypatch.setattr(app, "TEST_LOGIN_KEY", _KEY)
    r = _client().get("/test-login?key=" + _KEY)
    assert r.status_code == 404


def test_wrong_key_is_404(monkeypatch):
    _enable(monkeypatch)
    r = _client().get("/test-login?key=wrong")
    assert r.status_code == 404
    r = _client().post("/test-login", data={"key": "wrong", "uid": "U-0006"})
    assert r.status_code == 404


def test_form_renders_with_valid_key(monkeypatch):
    _enable(monkeypatch)
    r = _client().get("/test-login?key=" + _KEY)
    assert r.status_code == 200
    assert "U-0006" in r.get_data(as_text=True)
    assert "TEST ONLY" in r.get_data(as_text=True)


def test_login_by_user_id_sets_session(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(app.bitable, "raw_search_records", lambda *a, **k: [_record()])
    r = _client().post("/test-login", data={"key": _KEY, "uid": "u-0006"})
    assert r.status_code == 302
    assert r.headers.get("Location") == "/"
    assert "yxq_session=" in (r.headers.get("Set-Cookie") or "")


def test_login_by_openid_sets_session(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(app.bitable, "raw_search_records", lambda *a, **k: [_record()])
    r = _client().post("/test-login", data={"key": _KEY, "uid": "OU_TEST_0006"})
    assert r.status_code == 302
    assert "yxq_session=" in (r.headers.get("Set-Cookie") or "")


def test_disallowed_user_is_403(monkeypatch):
    _enable(monkeypatch, ids=["U-9999"])
    monkeypatch.setattr(app.bitable, "raw_search_records", lambda *a, **k: [_record()])
    r = _client().post("/test-login", data={"key": _KEY, "uid": "U-0006"})
    assert r.status_code == 403


def test_unknown_user_is_404(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(app.bitable, "raw_search_records", lambda *a, **k: [])
    r = _client().post("/test-login", data={"key": _KEY, "uid": "U-0006"})
    assert r.status_code == 404


def test_duplicate_user_is_409(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(app.bitable, "raw_search_records",
                        lambda *a, **k: [_record(), _record(oid="ou_other")])
    r = _client().post("/test-login", data={"key": _KEY, "uid": "U-0006"})
    assert r.status_code == 409
