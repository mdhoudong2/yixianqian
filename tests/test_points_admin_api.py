"""管理员麦穗总览接口契约（不联网）。

管理员页是给运营看的只读视图，形状一变页面就静悄悄少一列。这里钉住：
1. 用户列表 = 名录全集（没动过穗的人也在，余额 0），不是只列有穗的；
2. 单用户概括把「穗从哪来、花到哪去」拼齐；
3. 鉴权：未登录 401、非管理员 403。
"""
import os
import sys

import pytest

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402

from lib import points  # noqa: E402

ADMIN = "ou_test_admin"
ME = "ou_me"
OTHER = "ou_other"

# 名录里有两个真实用户：ME 动过穗，OTHER 一笔没动。
USERS = [
    {"record_id": "rec_me", "fields": {"飞书用户ID": ME, "昵称": "小北",
                                       "用户ID": "U-0001", "账号状态": "单身"}},
    {"record_id": "rec_other", "fields": {"飞书用户ID": OTHER, "昵称": "阿南",
                                          "用户ID": "U-0002", "账号状态": "单身"}},
]


@pytest.fixture
def admin_client(monkeypatch, points_db):
    """管理员会话 + 「名录里有两个用户」的桩。只桩表格读，不桩 lib。"""
    monkeypatch.setattr(app, "_snap", lambda name: USERS if name == "users" else [])
    monkeypatch.setattr(app, "refresh_snapshot_table_async", lambda *a, **k: None)
    app._rate_store.clear()

    c = app.app.test_client()
    c.set_cookie("yxq_session", app.create_session(ADMIN))
    return c


def _client_as(open_id):
    c = app.app.test_client()
    c.set_cookie("yxq_session", app.create_session(open_id))
    return c


# ---------------------------------------------------------------- 鉴权

def test_unauthenticated_is_401(points_db):
    assert app.app.test_client().get("/api/admin/points/overview").status_code == 401


def test_non_admin_is_403(monkeypatch, points_db):
    monkeypatch.setattr(app, "_snap", lambda name: [])
    assert _client_as(ME).get("/api/admin/points/overview").status_code == 403


# ---------------------------------------------------------------- 总览

def test_overview_lists_everyone_not_just_people_with_points(admin_client):
    points.grant(ME, 30, points.KIND_ADMIN, reason="预置")
    points.add_entry(ME, -5, points.KIND_REDEEM, reason="兑了一点")

    d = admin_client.get("/api/admin/points/overview").get_json()
    stats = d["stats"]
    assert stats["user_count"] == 1          # 有穗的只有 ME 一个
    assert stats["earned"] == 30 and stats["spent"] == 5
    assert stats["outstanding"] == 25

    by_oid = {u["user_oid"]: u for u in d["users"]}
    assert set(by_oid) == {ME, OTHER}        # 名录全集，含零余额的 OTHER
    assert by_oid[ME]["balance"] == 25 and by_oid[ME]["nickname"] == "小北"
    assert by_oid[OTHER]["balance"] == 0 and by_oid[OTHER]["entry_count"] == 0
    assert d["users"][0]["user_oid"] == ME   # 有穗的排前面


# ---------------------------------------------------------------- 单用户概括

def test_user_summary_breaks_down_earn_by_kind(admin_client):
    points.grant(ME, 50, points.KIND_ADMIN, reason="预置")
    points.grant(ME, 20, points.KIND_INVITE, reason="邀请奖励")

    d = admin_client.get("/api/admin/points/user?user_oid=" + ME).get_json()
    assert d["summary"]["balance"] == 70
    assert d["user"]["nickname"] == "小北"

    kinds = {k["kind"]: k for k in d["earned_by_kind"]}
    assert kinds[points.KIND_ADMIN]["total"] == 50
    assert kinds[points.KIND_INVITE]["total"] == 20
    assert d["invites"]["total"] == 0
    assert d["entries"] and d["entries"][0]["kind_label"]


def test_user_summary_requires_user_oid(admin_client):
    assert admin_client.get("/api/admin/points/user").status_code == 400


def test_user_summary_for_a_zero_balance_user(admin_client):
    """没动过穗的人也查得动，全 0、不报错。"""
    d = admin_client.get("/api/admin/points/user?user_oid=" + OTHER).get_json()
    assert d["summary"]["balance"] == 0
    assert d["user"]["nickname"] == "阿南"
    assert d["earned_by_kind"] == [] and d["redeemed_by_item"] == []
    assert d["entries"] == []
