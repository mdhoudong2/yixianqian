"""个人资料写接口的校验（审计 L1 / L2 回归）。

- 待审核/审核不通过/已退出 不能上传照片（与 PUT /api/profile 同门禁）；
- 资料编辑加范围/长度上限：身高 100-250、昵称 ≤30 字、短文本 ≤200、长文本 ≤2000、
  多选单项 ≤50 且 ≤30 项（旧实现 99999 / 300 字可直接落库）。
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
H = {"X-Requested-With": "XMLHttpRequest"}


def _me(status="单身"):
    return {"record_id": "rec_me", "fields": {"飞书用户ID": ME, "账号状态": status,
                                              "昵称": "小北", "性别": "男性"}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, "snap_self_user", lambda: _me())
    app._rate_store.clear()
    c = app.app.test_client()
    c.set_cookie("yxq_session", app.create_session(ME))
    return c


# ---------------------------------------------------------------- L2 纯函数

def test_height_must_be_in_range():
    with pytest.raises(ValueError):
        app._normalize_editable_update({app.F_HEIGHT: 99999})
    with pytest.raises(ValueError):
        app._normalize_editable_update({app.F_HEIGHT: 99})
    assert app._normalize_editable_update({app.F_HEIGHT: 176}) == {app.F_HEIGHT: 176}


def test_nickname_length_capped():
    with pytest.raises(ValueError):
        app._normalize_editable_update({app.F_NICKNAME: "名" * 300})
    assert app._normalize_editable_update({app.F_NICKNAME: "小北"}) == {app.F_NICKNAME: "小北"}


def test_short_text_length_capped():
    with pytest.raises(ValueError):
        app._normalize_editable_update({app.F_CITY: "城" * 201})
    assert app._normalize_editable_update({app.F_CITY: "深圳"}) == {app.F_CITY: "深圳"}


def test_long_text_field_allows_longer_but_capped():
    assert app._normalize_editable_update({app.F_PERSONALITY: "我" * 1500}) == {
        app.F_PERSONALITY: "我" * 1500}
    with pytest.raises(ValueError):
        app._normalize_editable_update({app.F_PERSONALITY: "我" * 2001})


def test_multi_item_too_long_rejected():
    with pytest.raises(ValueError):
        app._normalize_editable_update({app.F_SELF_HOBBIES: ["爱" * 51]})


# ---------------------------------------------------------------- L2 端点

def test_put_profile_rejects_out_of_range_height(client):
    r = client.put("/api/profile", json={app.F_HEIGHT: 99999}, headers=H)
    assert r.status_code == 400
    assert "身高" in r.get_json()["error"]


def test_put_profile_rejects_overlong_nickname(client):
    r = client.put("/api/profile", json={app.F_NICKNAME: "名" * 300}, headers=H)
    assert r.status_code == 400


# ---------------------------------------------------------------- L1 照片门禁

def test_pending_user_cannot_upload_photo(monkeypatch):
    monkeypatch.setattr(app, "snap_self_user", lambda: _me("待审核"))
    app._rate_store.clear()
    c = app.app.test_client()
    c.set_cookie("yxq_session", app.create_session(ME))
    r = c.post("/api/profile/photo", data={}, headers=H)
    assert r.status_code == 403
    assert r.get_json()["gate"] == "待审核"


def test_exited_user_cannot_upload_photo(monkeypatch):
    monkeypatch.setattr(app, "snap_self_user", lambda: _me("已退出"))
    app._rate_store.clear()
    c = app.app.test_client()
    c.set_cookie("yxq_session", app.create_session(ME))
    r = c.post("/api/profile/photo", data={}, headers=H)
    assert r.status_code == 403
