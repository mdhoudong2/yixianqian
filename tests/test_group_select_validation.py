"""分组志愿提交校验（审计 G1 回归）：与 onetoone/select 同口径的 400 且零写入。

旧实现只查「至少一个 / 最多 7 个」，自己 / 未报名 / 重复 / 非字符串全放行
（``{"choices":[123]}`` 还会 500）；这里把每个非法输入钉成 400 + 零写入，
合法输入 → 200 + 恰一行。
"""
import os
import sys

import pytest

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402

from tests.test_onetoone_api import (  # noqa: E402
    ACT,
    ME,
    FakeBitable,
)

GS_TBL = "tbl_test_group_select_validation"


def _user(oid, uid, gender, nick, status="单身"):
    return {"飞书用户ID": oid, "用户ID": uid, "性别": gender, "昵称": nick,
            "账号状态": status}


USERS = {
    ME: _user(ME, "U-0001", "男性", "小北"),
    "ou_m2": _user("ou_m2", "U-0002", "男性", "阿北"),
    "ou_f1": _user("ou_f1", "U-0003", "女性", "阿南"),
    "ou_f2": _user("ou_f2", "U-0004", "女性", "小西"),
    "ou_f9": _user("ou_f9", "U-0011", "女性", "未报名"),
}


def _act():
    return {"record_id": "rec_act", "fields": {
        "活动ID": ACT, "活动名称": "关系自洽",
        "分组状态": "收集中", "分组功能开启": "否",
        "一对一状态": "未开始", "一对一功能开启": "否"}}


def _signup(oid):
    return {"record_id": f"rec_s_{oid}", "fields": {
        "活动ID": ACT, "报名人open_id": oid,
        "报名人昵称": USERS.get(oid, {}).get("昵称", "某人"), "状态": "已报名"}}


class Api:
    """最小脚手架：假表 + 快照桩 + 会话；只桩存取，校验逻辑跑真实现。"""

    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        monkeypatch.setattr(app, "GROUP_SELECT_TABLE", GS_TBL)
        monkeypatch.setattr(app.bitable, "GROUP_SELECT_TABLE", GS_TBL)
        app._rate_store.clear()
        self.fresh_calls = []
        self.refresh_calls = []
        self.snapshot = {"activities": [], "signups": [], "group_selections": [],
                         "users": [], "observers": []}
        monkeypatch.setattr(app, "_snap_live", lambda key: list(self.snapshot.get(key, [])))
        monkeypatch.setattr(app, "_snap", lambda key: list(self.snapshot.get(key, [])))
        monkeypatch.setattr(app, "_snap_fresh", self._fresh)
        monkeypatch.setattr(app, "refresh_snapshot_table_async", self.refresh_calls.append)
        monkeypatch.setattr(app, "snap_self_user",
                            lambda: {"record_id": "rec_u_me", "fields": dict(USERS[ME])})
        monkeypatch.setattr(
            app, "snap_find_user_by_openid",
            lambda oid: ({"record_id": "rec_u_" + oid, "fields": dict(USERS[oid])}
                         if oid in USERS else None))
        self.fake = None
        self.client = app.app.test_client()
        self.client.set_cookie("yxq_session", app.create_session(ME))

    def _fresh(self, key):
        self.fresh_calls.append(key)
        return list(self.snapshot.get(key, []))

    def install(self):
        signups = [_signup(ME), _signup("ou_m2"), _signup("ou_f1"), _signup("ou_f2")]
        self.fake = FakeBitable({
            app.bitable.ACTIVITY_TABLE_ID: [_act()],
            app.bitable.SIGNUP_TABLE_ID: signups,
            GS_TBL: [],
        })
        self.monkeypatch.setattr(app.bitable, "search_records", self.fake.search_records)
        self.monkeypatch.setattr(app.bitable, "create_record", self.fake.create_record)
        self.monkeypatch.setattr(app.bitable, "update_record", self.fake.update_record)
        self.snapshot["activities"] = [_act()]
        self.snapshot["signups"] = list(signups)
        self.snapshot["group_selections"] = []
        return self.fake

    def post(self, choices):
        return self.client.post(
            f"/api/activities/{ACT}/group/select",
            json={} if choices is None else {"choices": choices},
            headers={"X-Requested-With": "XMLHttpRequest"})


@pytest.fixture
def api(monkeypatch):
    a = Api(monkeypatch)
    a.install()
    return a


@pytest.mark.parametrize("choices,err", [
    ([], "请至少选择一个志愿"),
    (None, "请至少选择一个志愿"),
    ("ou_f1", "请至少选择一个志愿"),
    (["ou_f1"] * 8, "最多选择7个志愿"),
    ([123], "志愿格式不合法"),
    ([""], "志愿格式不合法"),
    ([ME], "第1个志愿不在可选列表中（须为本活动已报名的异性）"),
    (["ou_m2"], "第1个志愿不在可选列表中（须为本活动已报名的异性）"),
    (["ou_f9"], "第1个志愿不在可选列表中（须为本活动已报名的异性）"),
    (["ou_f1", "ou_f1"], "不能重复选择同一人"),
], ids=["空列表", "缺字段", "非列表", "8项", "整数项", "空串项", "含自己", "含同性",
        "含未报名者", "重复"])
def test_group_select_invalid_is_400_with_zero_writes(api, choices, err):
    r = api.post(choices)
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["error"] == err
    assert api.fake.writes() == [], "非法志愿必须零写入"


def test_group_select_valid_writes_one_row(api):
    r = api.post(["ou_f1"])
    assert r.status_code == 200, r.get_json()
    creates = [c for c in api.fake.writes() if c[0] == "create"]
    assert len(creates) == 1
    assert len(api.fake.tables[GS_TBL]) == 1
    assert api.fake.tables[GS_TBL][0]["fields"]["选择人open_id"] == ME
    assert api.fake.tables[GS_TBL][0]["fields"]["第1志愿"] == "ou_f1"
