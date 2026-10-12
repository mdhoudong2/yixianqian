"""留言写接口的校验与限流（审计 L4 / L6 / G3 回归）。

- 目标必须存在（旧实现不校验，留言挂在“幽灵卡片”上）；
- 缺 target / 缺 content 的文案分开（旧实现一律说「内容不能为空」）；
- 举报（G3）限流 5 次/分钟/用户——现行设计没有可判定的「卡片浏览者」记录，
  按审计口径放宽为「登录可报 + 限流」，挡住刷爆管理端待办。
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
TARGET = "ou_t1"

USERS = {
    ME: {"飞书用户ID": ME, "用户ID": "U-0001", "性别": "男性", "昵称": "小北",
         "账号状态": "单身"},
    TARGET: {"飞书用户ID": TARGET, "用户ID": "U-0002", "性别": "女性", "昵称": "阿南",
             "账号状态": "单身"},
}

H = {"X-Requested-With": "XMLHttpRequest"}


class FakeBitable:
    def __init__(self):
        self.records = {}
        self.created = []
        self.updates = []

    def search_records(self, table_id, filter_conditions=None, *a, **k):
        return []

    def get_record(self, table_id, record_id):
        return self.records.get(record_id)

    def create_record(self, table_id, fields):
        self.created.append((table_id, dict(fields)))
        return {"record_id": f"rec_new{len(self.created)}", "fields": fields}

    def update_record(self, table_id, record_id, fields):
        self.updates.append((table_id, record_id, dict(fields)))
        return {"record_id": record_id, "fields": fields}


@pytest.fixture
def api(monkeypatch, tmp_path):
    fake = FakeBitable()
    monkeypatch.setattr(app.bitable, "search_records", fake.search_records)
    monkeypatch.setattr(app.bitable, "get_record", fake.get_record)
    monkeypatch.setattr(app.bitable, "create_record", fake.create_record)
    monkeypatch.setattr(app.bitable, "update_record", fake.update_record)
    monkeypatch.setattr(app, "snap_self_user",
                        lambda: {"record_id": "rec_u_me", "fields": dict(USERS[ME])})
    monkeypatch.setattr(
        app, "snap_find_user_by_openid",
        lambda oid: ({"record_id": "rec_u_" + oid, "fields": dict(USERS[oid])}
                     if oid in USERS else None))
    monkeypatch.setattr(app, "_snap", lambda key: [])
    monkeypatch.setattr(app, "_snap_live", lambda key: [])
    # 举报历史文件与通知都别碰真实环境
    monkeypatch.setattr(app, "_REPORTED_HISTORY_FILE", str(tmp_path / "reported.json"))
    monkeypatch.setattr(app, "send_text_message", lambda *a, **k: True)
    app._rate_store.clear()
    c = app.app.test_client()
    c.set_cookie("yxq_session", app.create_session(ME))
    return c, fake


def _post(c, path, payload):
    return c.post(path, json=payload, headers=H)


# ---------------------------------------------------------------- L4 / L6 发留言

def test_message_missing_target_says_target(api):
    c, fake = api
    r = _post(c, "/api/messages", {"content": "你好"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "缺少目标用户"
    assert fake.created == []


def test_message_missing_content_says_content(api):
    c, fake = api
    r = _post(c, "/api/messages", {"target_openid": TARGET})
    assert r.status_code == 400
    assert r.get_json()["error"] == "内容不能为空"
    assert fake.created == []


def test_message_unknown_target_is_400_with_zero_writes(api):
    c, fake = api
    r = _post(c, "/api/messages", {"target_openid": "ou_ghost", "content": "你好"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "目标用户不存在"
    assert fake.created == [], "幽灵目标不得产生写入"


def test_message_valid_target_writes(api):
    c, fake = api
    r = _post(c, "/api/messages", {"target_openid": TARGET, "content": "你好"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert len(fake.created) == 1


# ---------------------------------------------------------------- G3 举报限流

def _message_record(rid="rec_msg1"):
    return {"record_id": rid, "fields": {
        "留言人open_id": "ou_author", "目标用户open_id": TARGET,
        "状态": "正常", "内容": "内容" + rid,
        "留言人昵称": "某人", "留言人用户ID": "U-0009",
        "创建时间": 1791800000000}}


def test_report_rate_limited_per_user(api):
    c, fake = api
    fake.records["rec_msg1"] = _message_record("rec_msg1")
    codes = [_post(c, "/api/messages/rec_msg1/report", {"reason": "广告"}).status_code
             for _ in range(6)]
    assert codes[:5] == [200] * 5, codes
    assert codes[5] == 429, "第 6 次必须被限流（5 次/分钟/用户）"
