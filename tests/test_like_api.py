"""喜欢接口：目标必须在册单身 + like_type 白名单（审计 G4 / L3 回归）。

- 给已脱单/已退出/待审核的用户点喜欢会消耗额度、只留对方看不到的悬挂记录
  → 卡片池同口径：目标账号状态必须「单身」；
- 未知 like_type 旧实现静默转匿名 → 调用端以为按实名记了，直接 400 不消耗额度。
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
TARGET_SINGLE = "ou_f1"
TARGET_MARRIED = "ou_f2"

USERS = {
    ME: {"飞书用户ID": ME, "用户ID": "U-0001", "性别": "男性", "昵称": "小北",
         "账号状态": "单身"},
    TARGET_SINGLE: {"飞书用户ID": TARGET_SINGLE, "用户ID": "U-0002", "性别": "女性",
                    "昵称": "阿南", "账号状态": "单身"},
    TARGET_MARRIED: {"飞书用户ID": TARGET_MARRIED, "用户ID": "U-0003", "性别": "女性",
                     "昵称": "小西", "账号状态": "已脱单"},
}

H = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(app, "snap_self_user",
                        lambda: {"record_id": "rec_u_me", "fields": dict(USERS[ME])})
    monkeypatch.setattr(
        app, "snap_find_user_by_openid",
        lambda oid: ({"record_id": "rec_u_" + oid, "fields": dict(USERS[oid])}
                     if oid in USERS else None))
    monkeypatch.setattr(app, "_snap", lambda key: [])
    app._rate_store.clear()
    c = app.app.test_client()
    c.set_cookie("yxq_session", app.create_session(ME))
    return c


def _post(c, payload):
    return c.post("/api/like", json=payload, headers=H)


def test_unknown_like_type_is_400(api):
    r = _post(api, {"target_openid": TARGET_SINGLE, "like_type": "神秘类型"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "like_type 不合法"


def test_like_target_must_be_single(api):
    r = _post(api, {"target_openid": TARGET_MARRIED})
    assert r.status_code == 400
    assert r.get_json()["error"] == "对方当前不在牵线名单中"


def test_like_target_not_found_still_404(api):
    r = _post(api, {"target_openid": "ou_ghost"})
    assert r.status_code == 404
