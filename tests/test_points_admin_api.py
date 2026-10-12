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


# ---------------------------------------------------------------- 审计必修 3 / 发现 4

def test_invites_status_is_business_status(admin_client, points_db):
    """审计必修 3：邀请行的 status 必须是邀请业务状态（SQLite 实值），账号状态另放。

    旧实现 _name_of 返回的 status 覆盖了业务状态，管理台显示成「单身」。
    """
    with points_db.transaction() as conn:
        conn.execute(
            "INSERT INTO invites (inviter_oid, invitee_phone, invitee_oid, status,"
            " gender, source, created_at) VALUES (?,?,?,?,?,?,?)",
            (ME, "13900000001", "", "pending", "女性", "h5", "2026-10-01 00:00:00"))

    d = admin_client.get("/api/admin/points/invites").get_json()
    assert d["total"] == 1
    row = d["rows"][0]
    assert row["status"] == "pending", "不能被账号状态（单身）覆盖"
    assert row["status_label"] == "待确认"
    assert row["account_status"] == "单身"
    assert row["nickname"] == "小北"
    assert row["phone_masked"] == "139****0001"


def test_redemptions_status_is_business_status(admin_client, points_db):
    with points_db.transaction() as conn:
        conn.execute(
            "INSERT INTO redemptions (user_oid, item, cost, status, params,"
            " idempotency_key, created_at) VALUES (?,?,?,?,?,?,?)",
            (ME, "real_like", 20, "active", "{}", "test-key-1", "2026-10-01 00:00:00"))

    d = admin_client.get("/api/admin/points/redemptions").get_json()
    row = d["rows"][0]
    assert row["status"] == "active", "不能被账号状态（单身）覆盖"
    assert row["status_label"] == "生效中"
    assert row["account_status"] == "单身"
    assert row["item_label"] == "额外实名喜欢"


@pytest.mark.parametrize("endpoint", ["ledger", "redemptions", "invites"])
def test_limit_offset_non_numeric_is_400(admin_client, endpoint):
    """管理台审计发现 4：limit/offset 裸 int() 曾打成 500。"""
    assert admin_client.get(
        f"/api/admin/points/{endpoint}?limit=abc").status_code == 400
    assert admin_client.get(
        f"/api/admin/points/{endpoint}?offset=abc").status_code == 400
    assert admin_client.get(
        f"/api/admin/points/{endpoint}?limit=3&offset=0").status_code == 200
