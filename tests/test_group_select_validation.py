"""分组志愿提交校验（审计 G1 回归）：非法输入 400 且零写入。

旧实现只查「至少一个 / 最多 7 个」，自己 / 未报名 / 重复 / 非字符串全放行
（``{"choices":[123]}`` 还会 500）；这里把每个非法输入钉成 400 + 零写入，
合法输入 → 200 + 恰一行。

2026-10-15：原一对一选择三端点（candidates/select/status）已随 onetoone_select
死页删除，其等价用例迁入本文件（统一志愿语义由分组端点承载）：
me=None 不 500、并发双击锁内查重、快照未命中回退实时 update、
活动状态决定性读取走 fresh；签到闸门相关迁入 tests/test_signin_gate.py。
2026-10-16：提交口径由「1..7」收紧为「必须恰好 7」，未满/超出 → 400 SELECT_NOT_FULL。
"""
import os
import sys
import threading

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
    "ou_f3": _user("ou_f3", "U-0005", "女性", "小东"),
    "ou_f4": _user("ou_f4", "U-0006", "女性", "小南"),
    "ou_f5": _user("ou_f5", "U-0007", "女性", "小中"),
    "ou_f6": _user("ou_f6", "U-0008", "女性", "小六"),
    "ou_f7": _user("ou_f7", "U-0009", "女性", "小七"),
    "ou_f9": _user("ou_f9", "U-0011", "女性", "未报名"),
}
FEMME_SEVEN = ["ou_f1", "ou_f2", "ou_f3", "ou_f4", "ou_f5", "ou_f6", "ou_f7"]


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
    """最小脚手架：假表 + 快照桩（stale/live 分离）+ 会话；只桩存取，校验逻辑跑真实现。"""

    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        monkeypatch.setattr(app, "GROUP_SELECT_TABLE", GS_TBL)
        monkeypatch.setattr(app.bitable, "GROUP_SELECT_TABLE", GS_TBL)
        app._rate_store.clear()
        self.fresh_calls = []
        self.refresh_calls = []
        self.snapshot = {"activities": [], "signups": [], "group_selections": [],
                         "users": [], "observers": []}
        self.live = {}   # fresh 读覆盖（未命中回退 snapshot）
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
        if key in self.live:
            return list(self.live[key])
        return list(self.snapshot.get(key, []))

    def install(self):
        signups = [_signup(oid) for oid in [ME, "ou_m2"] + FEMME_SEVEN]
        self.fake = FakeBitable({
            app.bitable.ACTIVITY_TABLE_ID: [_act()],
            app.bitable.SIGNUP_TABLE_ID: signups,
            GS_TBL: [],
        })
        self.monkeypatch.setattr(app.bitable, "search_records", self.fake.search_records)
        # 候选端点走 _grouping_client 直读报名表（与 signin_gate/realname 脚手架同款桩法）
        self.monkeypatch.setattr(app, "_grouping_client", self.fake)
        self.monkeypatch.setattr(app.bitable, "create_record", self.fake.create_record)
        self.monkeypatch.setattr(app.bitable, "update_record", self.fake.update_record)
        self.snapshot["activities"] = [_act()]
        self.snapshot["signups"] = list(signups)
        self.snapshot["group_selections"] = []
        return self.fake

    def get(self, path):
        return self.client.get(path)

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


@pytest.mark.parametrize("choices,err,code", [
    ([], "请选满7个志愿（当前已选 0 个）", "SELECT_NOT_FULL"),
    (None, "请选满7个志愿（当前已选 0 个）", "SELECT_NOT_FULL"),
    ("ou_f1", "请选满7个志愿（当前已选 0 个）", "SELECT_NOT_FULL"),
    (["ou_f1"] * 8, "请选满7个志愿（当前已选 8 个）", "SELECT_NOT_FULL"),
    ([123], "志愿格式不合法", ""),
    ([""], "志愿格式不合法", ""),
    ([ME] + FEMME_SEVEN[:6], "第1个志愿不在可选列表中（须为本活动已报名的异性）", ""),
    (["ou_m2"] + FEMME_SEVEN[:6], "第1个志愿不在可选列表中（须为本活动已报名的异性）", ""),
    (["ou_f9"] + FEMME_SEVEN[:6], "第1个志愿不在可选列表中（须为本活动已报名的异性）", ""),
    (["ou_f1"] + FEMME_SEVEN[:6], "不能重复选择同一人", ""),
], ids=["空列表", "缺字段", "非列表", "8项", "整数项", "空串项", "含自己", "含同性",
        "含未报名者", "重复"])
def test_group_select_invalid_is_400_with_zero_writes(api, choices, err, code):
    r = api.post(choices)
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["error"] == err
    if code:
        assert r.get_json()["code"] == code
    assert api.fake.writes() == [], "非法志愿必须零写入"


def test_group_select_accepts_exactly_seven_and_writes_one_row(api):
    """恰好 7 个 → 200 且恰写一行（2026-10-16 起由「1..7」收紧为必须恰好 7）。

    无已执行轮次时提交轮次 R=1，行内写「轮次」=1。
    """
    r = api.post(FEMME_SEVEN)
    assert r.status_code == 200, r.get_json()
    creates = [c for c in api.fake.writes() if c[0] == "create"]
    assert len(creates) == 1
    assert len(api.fake.tables[GS_TBL]) == 1
    assert api.fake.tables[GS_TBL][0]["fields"]["选择人open_id"] == ME
    assert api.fake.tables[GS_TBL][0]["fields"]["第1志愿"] == "ou_f1"
    assert api.fake.tables[GS_TBL][0]["fields"]["轮次"] == 1, "R=1 时行轮次应记 1"


def test_group_select_six_choices_is_rejected(api):
    """6 个（差 1 个）→ 400 SELECT_NOT_FULL，错误里带当前已选数。"""
    r = api.post(FEMME_SEVEN[:6])
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["code"] == "SELECT_NOT_FULL"
    assert r.get_json()["error"] == "请选满7个志愿（当前已选 6 个）"
    assert api.fake.writes() == [], "未选满必须零写入"


def test_group_select_me_none_is_not_500(api, monkeypatch):
    """迁移自原一对一 select：me=None 必须被门禁拦住，不能 500。"""
    monkeypatch.setattr(app, "snap_self_user", lambda: None)
    r = api.post(FEMME_SEVEN)
    assert r.status_code == 404, "me=None 必须被门禁/防护拦住，不能 500"
    assert r.get_json()["error"] == "用户不存在"


def test_group_select_concurrent_double_click_creates_once(api):
    """迁移自原一对一 select：两个线程同时提交 → 锁内查重，create 恰 1 次、表内 1 行。

    （把查重挪出锁外时，两次查空会各 create 一次，本用例出现 2 行即红。）
    """
    api.fake.search_sleep = {GS_TBL: 0.3}
    codes = []

    def worker():
        c = app.app.test_client()
        c.set_cookie("yxq_session", app.create_session(ME))
        r = c.post(f"/api/activities/{ACT}/group/select",
                   json={"choices": FEMME_SEVEN},
                   headers={"X-Requested-With": "XMLHttpRequest"})
        codes.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert codes == [200, 200]
    creates = [c for c in api.fake.writes() if c[0] == "create"]
    updates = [c for c in api.fake.writes() if c[0] == "update"]
    assert len(creates) == 1, "并发双击只应 create 一次"
    assert len(updates) == 1, "后到的一次应命中已有记录走 update"
    assert len(api.fake.tables[GS_TBL]) == 1, "表内只该有 1 行"


def test_group_select_snapshot_miss_falls_back_to_live_update(api):
    """迁移自原一对一 select：快照无记录、live 有 → 走 update 而不是 create（实时兜底）。"""
    existing = {"record_id": "rec_gs_me", "fields": {"活动ID": ACT, "选择人open_id": ME,
                                                     "选择人性别": "男性"}}
    api.fake.tables[GS_TBL].append(existing)
    r = api.post(FEMME_SEVEN)
    assert r.status_code == 200, r.get_json()
    assert not [c for c in api.fake.writes() if c[0] == "create"]
    updates = [c for c in api.fake.writes() if c[0] == "update"]
    assert len(updates) == 1 and updates[0][2] == existing["record_id"]


# ---------------------------------------------------------------- fresh 读取（迁移自原一对一端点）

def _live_stale_acts(status_live, status_stale="未开始"):
    live = _act()
    live["fields"]["分组状态"] = status_live
    stale = _act()
    stale["fields"]["分组状态"] = status_stale
    return live, stale


def test_group_candidates_uses_live_status(api):
    live, stale = _live_stale_acts("收集中")
    api.snapshot["activities"] = [stale]
    api.live["activities"] = [live]
    api.fresh_calls.clear()
    r = api.get(f"/api/activities/{ACT}/group/candidates")
    assert r.status_code == 200, "快照「未开始」、live「收集中」时应以 live 为准"
    assert "activities" in api.fresh_calls


def test_group_select_uses_live_status(api):
    live, stale = _live_stale_acts("收集中")
    api.snapshot["activities"] = [stale]
    api.live["activities"] = [live]
    api.fresh_calls.clear()
    r = api.post(FEMME_SEVEN)
    assert r.status_code == 200, "快照「未开始」、live「收集中」时应以 live 为准"
    assert "activities" in api.fresh_calls


def test_group_status_uses_live_status(api):
    live, stale = _live_stale_acts("收集中")
    api.snapshot["activities"] = [stale]
    api.live["activities"] = [live]
    api.fresh_calls.clear()
    r = api.get(f"/api/activities/{ACT}/group/status")
    assert r.status_code == 200
    assert r.get_json()["group_status"] == "收集中"
    assert "activities" in api.fresh_calls
