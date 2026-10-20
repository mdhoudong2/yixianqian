"""管理端活动状态 API 契约（不联网，桩表驱动）。

覆盖 POST /api/admin/activity/status 的鉴权、五值校验、写后读回、同值幂等与
404，以及两个管理台状态端点新增的 activity_status / activity_status_options。
桩法比照 tests/test_group_admin_api.py：Flask test client + cookie session +
monkeypatch 快照 / 活动写入口。
"""
import os
import sys

import pytest

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402
from constants import (  # noqa: E402
    ACTIVITY_TABLE_ID,
    FIELD_ACTIVITY_ID,
    FIELD_ACTIVITY_NAME,
    FIELD_ACTIVITY_STATUS,
)

ADMIN = "ou_test_admin"
ME = "ou_me"
ACT = "A-0017"
OPTIONS = ["未开始报名", "报名中", "已满员", "已结束", "已取消"]


class FakeActivityTable:
    """只桩活动表存取：update_record 就地改字段。"""

    def __init__(self, activity):
        self.tables = {ACTIVITY_TABLE_ID: [activity]}
        self.writes = []

    def update_record(self, table_id, record_id, fields):
        self.writes.append((table_id, record_id, dict(fields)))
        for rec in self.tables.get(table_id, []):
            if rec["record_id"] == record_id:
                rec["fields"].update(fields)
                return rec
        return None


def _activity(status="报名中"):
    return {"record_id": "rec_act_status", "fields": {
        FIELD_ACTIVITY_ID: ACT,
        FIELD_ACTIVITY_NAME: "活动状态测试",
        FIELD_ACTIVITY_STATUS: status,
        "分组功能开启": "否",
        "一对一功能开启": "否",
    }}


class Api:
    """假活动表 + fresh 快照桩 + 管理员/普通用户会话。"""

    def __init__(self, monkeypatch, tmp_path):
        self.activity = _activity()
        self.fake = FakeActivityTable(self.activity)
        monkeypatch.setattr(app, "_grouping_client", self.fake)
        self.snapshot = {"activities": [self.activity]}
        self.fresh_calls = []
        self.refreshes = []
        monkeypatch.setattr(app, "_snap_fresh", self._fresh)
        monkeypatch.setattr(app, "refresh_snapshot_table_async", self.refreshes.append)
        # 状态端点会探测活跃 job：两个 job 文件都重定向到 tmp，避免读真实数据目录
        monkeypatch.setattr(app, "GROUPING_JOBS_FILE", str(tmp_path / "grouping_jobs.json"))
        monkeypatch.setattr(app, "ONETOONE_JOBS_FILE", str(tmp_path / "onetoone_jobs.json"))
        app._rate_store.clear()
        self.client = app.app.test_client()
        self.client.set_cookie("yxq_session", app.create_session(ADMIN))
        self.client_me = app.app.test_client()
        self.client_me.set_cookie("yxq_session", app.create_session(ME))

    def _fresh(self, key):
        self.fresh_calls.append(key)
        return list(self.snapshot.get(key, []))


@pytest.fixture
def api(monkeypatch, tmp_path):
    return Api(monkeypatch, tmp_path)


def _post(api, status, activity_id=ACT, client=None):
    return (client or api.client).post(
        "/api/admin/activity/status",
        json={"activity_id": activity_id, "status": status},
        headers={"X-Requested-With": "XMLHttpRequest"})


def test_status_requires_login(api):
    anon = app.app.test_client()
    assert _post(api, "已结束", client=anon).status_code == 401


def test_status_requires_admin(api):
    assert _post(api, "已结束", client=api.client_me).status_code == 403


@pytest.mark.parametrize("bad", ["", "已发布", "未开始", "已结束 ", None, 123])
def test_invalid_status_is_400(api, bad):
    r = _post(api, bad)
    assert r.status_code == 400
    assert r.get_json()["error"] == "状态不合法"
    assert api.fake.writes == []


@pytest.mark.parametrize("status", OPTIONS)
def test_each_status_updates_and_reads_back(api, status):
    api.activity["fields"][FIELD_ACTIVITY_STATUS] = ""   # 五个值逐一都是真实变更
    r = _post(api, status)
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "status": status}
    assert api.activity["fields"][FIELD_ACTIVITY_STATUS] == status
    assert api.refreshes == ["activities"]

    r = api.client.get("/api/admin/group/status?activity_id=" + ACT)
    assert r.status_code == 200
    assert r.get_json()["activity_status"] == status


def test_same_status_is_idempotent(api):
    assert _post(api, "已结束").status_code == 200
    api.fake.writes.clear()
    api.refreshes.clear()

    r = _post(api, "已结束")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "status": "已结束"}
    assert api.fake.writes == []
    assert api.refreshes == []


def test_missing_activity_is_404(api):
    r = _post(api, "已结束", activity_id="A-9999")
    assert r.status_code == 404
    assert r.get_json()["error"] == "活动不存在"
    assert api.fake.writes == []


@pytest.mark.parametrize("path", [
    "/api/admin/group/status?activity_id=" + ACT,
    "/api/admin/onetoone/status?activity_id=" + ACT,
])
def test_status_endpoints_include_activity_status_and_options(api, path):
    r = api.client.get(path)
    assert r.status_code == 200
    data = r.get_json()
    assert data["activity_status"] == "报名中"
    assert data["activity_status_options"] == OPTIONS
