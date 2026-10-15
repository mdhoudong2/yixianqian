"""一对一 H5 API 契约（不联网，桩表驱动；Flask test client + cookie session）。

2026-10-15：用户端一对一选择三端点（candidates/select/status）已随 onetoone_select
死页删除——统一志愿语义迁至分组端点，等价用例迁入 tests/test_group_select_validation.py
与 tests/test_signin_gate.py。本文件保留 result 侧防回归网：

1. chatted 作者过滤：纯函数单测 + /result 端点（别人的留言不得标「已聊」）；
2. 决定性读取 fresh：快照 activities=未开始、live=已完成 → result 以 live 为准；
3. fresh 结果读取：bot 刚写入的结果必须立刻可见（不吃 300s 快照）。

另覆盖 my_groups 过滤已取消 + 按活动去重。

桩法比照 tests/test_group_admin_api.py：只桩「存取」，差集/去重/锁/刷新调用全跑真实现。
"""
import os
import sys
import time

import pytest

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402

from lib import grouping as lib_grouping  # noqa: E402

# 一对一那两张表在 CI 无 local_config 时默认值为空——统一换成测试表 ID，
# app 与 bitable 两个模块的全局都要换（端点与 bitable.get_user_onetoone_selection 各用各的）。
SELECT_TBL = "tbl_test_oto_select"
RESULT_TBL = "tbl_test_oto_result"
ACT = "A-0002"
ME = "ou_me"


# ---------------------------------------------------------------- 假表

class FakeBitable:
    """够用的内存假表：`operator: is` / `isNot`，可注入搜索失败与读延迟。"""

    def __init__(self, tables=None, search_fails=None, search_sleep=None):
        self.tables = {k: [{"record_id": r["record_id"], "fields": dict(r["fields"])}
                           for r in v] for k, v in (tables or {}).items()}
        self.search_fails = search_fails or set()
        self.search_sleep = search_sleep or {}
        self.calls = []
        self._seq = 0

    def search_records(self, table_id, filter_conditions=None, *a, **k):
        self.calls.append(("search", table_id))
        if table_id in self.search_fails:
            return None
        conds = filter_conditions or {}
        if isinstance(conds, dict):
            conds = conds.get("conditions", [])
        rows = [r for r in self.tables.get(table_id, []) if self._match(r, conds)]
        # 先取快照再延迟返回：模拟「读在途」窗口。锁外查重的双击会各自拿到
        # 同一份空快照后各插一行；锁内查重时后到者醒来看到的是前者已写入的行。
        if table_id in self.search_sleep:
            time.sleep(self.search_sleep[table_id])
        return rows

    @staticmethod
    def _match(row, conds):
        for c in conds:
            want = (c.get("value") or [""])[0]
            got = row["fields"].get(c.get("field_name"), "")
            if isinstance(got, list):
                got = "".join(x.get("text", "") for x in got)
            if c.get("operator") == "isNot":
                if got == want:
                    return False
            elif got != want:
                return False
        return True

    def update_record(self, table_id, record_id, fields):
        self.calls.append(("update", table_id, record_id, dict(fields)))
        for r in self.tables.get(table_id, []):
            if r["record_id"] == record_id:
                r["fields"].update(fields)
                return r
        return None

    def create_record(self, table_id, fields):
        self._seq += 1
        row = {"record_id": f"rec_new{self._seq}", "fields": dict(fields)}
        self.tables.setdefault(table_id, []).append(row)
        self.calls.append(("create", table_id, dict(fields)))
        return row

    def writes(self):
        return [c for c in self.calls if c[0] != "search"]


# ---------------------------------------------------------------- 桩数据

def _user_fields(oid, uid, gender, nick, status="单身"):
    return {"飞书用户ID": oid, "用户ID": uid, "性别": gender, "昵称": nick, "账号状态": status}


USERS = {
    ME: _user_fields(ME, "U-0001", "男性", "小北"),
    "ou_m2": _user_fields("ou_m2", "U-0002", "男性", "阿北"),
    "ou_f1": _user_fields("ou_f1", "U-0003", "女性", "阿南"),
    "ou_f2": _user_fields("ou_f2", "U-0004", "女性", "小西"),
    "ou_f3": _user_fields("ou_f3", "U-0005", "女性", "小东"),
    "ou_f4": _user_fields("ou_f4", "U-0006", "女性", "小南"),
    "ou_f5": _user_fields("ou_f5", "U-0007", "女性", "小中"),
    "ou_f6": _user_fields("ou_f6", "U-0008", "女性", "小六"),
    "ou_f7": _user_fields("ou_f7", "U-0009", "女性", "小七"),
    "ou_f9": _user_fields("ou_f9", "U-0011", "女性", "未报名"),
}
FEMME_SEVEN = ["ou_f1", "ou_f2", "ou_f3", "ou_f4", "ou_f5", "ou_f6", "ou_f7"]


def _act(status="收集中", activity_id=ACT, group_status="未开始"):
    return {"record_id": f"rec_act_{activity_id}",
            "fields": {"活动ID": activity_id, "活动名称": "关系自洽",
                       "一对一状态": status, "一对一功能开启": "是",
                       "分组状态": group_status, "分组功能开启": "否"}}


def _signup(oid, status="已报名", activity_id=ACT):
    return {"record_id": f"rec_s_{activity_id}_{oid}",
            "fields": {"活动ID": activity_id, "报名人open_id": oid,
                       "报名人昵称": USERS.get(oid, {}).get("昵称", "某人"), "状态": status}}


def _res(oid, target, rank=1, activity_id=ACT):
    return {"record_id": f"rec_res_{activity_id}_{oid}_{target}",
            "fields": {"活动ID": activity_id, "排名": rank, "用户open_id": oid,
                       "必聊对象open_id": target, "必聊对象昵称": USERS.get(target, {}).get("昵称", "")}}


def _msg(author, target, status="正常", content="历史留言"):
    return {"record_id": f"rec_msg_{author}_{target}_{status}",
            "fields": {"留言人open_id": author, "目标用户open_id": target,
                       "状态": status, "内容": content}}


def _signups_all():
    return [_signup(o) for o in [ME, "ou_m2"] + FEMME_SEVEN]


# ---------------------------------------------------------------- 测试脚手架

class Api:
    """假表 + 快照桩（stale/live 分离）+ 会话就绪的 test client。"""

    def __init__(self, monkeypatch, tmp_path):
        self.monkeypatch = monkeypatch
        monkeypatch.setattr(app, "ONETOONE_SELECT_TABLE", SELECT_TBL)
        monkeypatch.setattr(app, "ONETOONE_RESULT_TABLE", RESULT_TBL)
        monkeypatch.setattr(app.bitable, "ONETOONE_SELECT_TABLE", SELECT_TBL)
        monkeypatch.setattr(app.bitable, "ONETOONE_RESULT_TABLE", RESULT_TBL)
        monkeypatch.setattr(lib_grouping, "log", lambda *a, **k: None)
        app.configure_grouping_lib(str(tmp_path))
        app._rate_store.clear()

        self.fresh_calls = []
        self.refresh_calls = []
        self.stale = {}
        self.live_activities = None
        self.users = dict(USERS)
        self.me = {"record_id": "rec_u_me", "fields": dict(USERS[ME])}

        monkeypatch.setattr(app, "_snap_live", self._snap_live)
        monkeypatch.setattr(app, "_snap", self._snap_live)
        monkeypatch.setattr(app, "_snap_fresh", self._fresh)
        monkeypatch.setattr(app, "refresh_snapshot_table_async", self.refresh_calls.append)
        monkeypatch.setattr(app, "snap_self_user", lambda: self.me)
        monkeypatch.setattr(app, "snap_find_user_by_openid", self._find_user)

        self.client = app.app.test_client()
        self.client.set_cookie("yxq_session", app.create_session(ME))

    # -- 快照桩 --
    def _snap_live(self, key):
        return list(self.stale.get(key, []))

    def _fresh(self, key):
        self.fresh_calls.append(key)
        if key == "activities" and self.live_activities is not None:
            return list(self.live_activities)
        return list(self.stale.get(key, []))

    def _find_user(self, oid):
        fields = self.users.get(oid)
        return {"record_id": f"rec_u_{oid}", "fields": dict(fields)} if fields else None

    # -- 数据安装 --
    def install(self, *, activities=None, stale_activities=None, live_activities=None,
                signups=None, stale_signups=None, selections=(), stale_selections=None,
                results=(), stale_results=None, messages=(), users=None, me=True,
                search_sleep=None, search_fails=None):
        if users is not None:
            self.users = dict(users)
        self.me = ({"record_id": "rec_u_me", "fields": dict(self.users[ME])} if me else None)

        acts = [_act()] if activities is None else list(activities)
        signups = _signups_all() if signups is None else list(signups)
        self.fake = FakeBitable({
            app.bitable.ACTIVITY_TABLE_ID: acts,
            app.bitable.SIGNUP_TABLE_ID: signups,
            SELECT_TBL: list(selections),
            RESULT_TBL: list(results),
        }, search_sleep=search_sleep, search_fails=search_fails)
        self.monkeypatch.setattr(app.bitable, "search_records", self.fake.search_records)
        self.monkeypatch.setattr(app.bitable, "create_record", self.fake.create_record)
        self.monkeypatch.setattr(app.bitable, "update_record", self.fake.update_record)

        self.stale = {
            "activities": list(stale_activities if stale_activities is not None else acts),
            "signups": list(stale_signups if stale_signups is not None else signups),
            "onetoone_selections": list(stale_selections if stale_selections is not None
                                        else selections),
            "onetoone_results": list(stale_results if stale_results is not None else results),
            "messages": list(messages),
            "users": [{"record_id": f"rec_u_{o}", "fields": dict(f)}
                      for o, f in self.users.items()],
            "group_selections": [],
            "group_results": [],
            "observers": [],
        }
        self.live_activities = list(live_activities) if live_activities is not None else None
        return self.fake

    def get(self, path):
        return self.client.get(path)

    def post(self, path, payload):
        return self.client.post(path, json=payload,
                                headers={"X-Requested-With": "XMLHttpRequest"})


@pytest.fixture
def api(monkeypatch, tmp_path):
    a = Api(monkeypatch, tmp_path)
    a.install()
    return a


# ================================================================
# 网 1：chatted 作者过滤
# ================================================================

def test_chatted_targets_pure_function():
    """别人的留言、已删除的留言都不算「已聊」——只认我本人发出的。"""
    msgs = [
        _msg("ou_b", "ou_x"),
        _msg(ME, "ou_x"),
        _msg(ME, "ou_y", status="已删除"),
        _msg("ou_c", "ou_z"),
    ]
    assert app._chatted_targets(msgs, ME) == {"ou_x"}


def test_result_chatted_requires_author_endpoint(api):
    """/result 端到端：对 X 有「别人→X、我→X」时，X 已聊；Y 只有我的已删除留言 → 未聊。"""
    api.install(
        activities=[_act("已完成")],
        live_activities=[_act("已完成")],
        results=[_res(ME, "ou_f1", 1), _res(ME, "ou_f2", 2), _res(ME, "ou_f3", 3)],
        messages=[
            _msg("ou_m2", "ou_f1"),               # 别人的留言
            _msg(ME, "ou_f1"),                    # 我自己的留言
            _msg(ME, "ou_f2", status="已删除"),   # 我的已删除留言
            _msg("ou_m2", "ou_f3"),               # 别人给 Z 的留言
        ],
    )
    r = api.get(f"/api/activities/{ACT}/onetoone/result")
    assert r.status_code == 200
    chatted = {p["openid"]: p["chatted"] for p in r.get_json()["partners"]}
    assert chatted == {"ou_f1": True, "ou_f2": False, "ou_f3": False}


# ================================================================
# 网 2：決定性讀取 fresh（result 端；candidates/select/status 已删、
#       等价 fresh 用例迁至 tests/test_group_select_validation.py）
# ================================================================

def _live_vs_stale(status_live, status_stale="未开始"):
    return {"activities": [_act(status_live)],
            "stale_activities": [_act(status_stale)],
            "live_activities": [_act(status_live)],
            "signups": _signups_all(),
            "stale_signups": _signups_all()}


def test_result_uses_live_status(api):
    api.install(
        activities=[_act("已完成")],
        stale_activities=[_act("未开始")],
        live_activities=[_act("已完成")],
        results=[_res(ME, "ou_f1", 1)],
    )
    api.fresh_calls.clear()
    r = api.get(f"/api/activities/{ACT}/onetoone/result")
    assert r.status_code == 200, "快照「未开始」、live「已完成」时应以 live 为准"
    assert r.get_json()["partners"][0]["openid"] == "ou_f1"
    assert "activities" in api.fresh_calls


def test_result_reads_fresh_results(api, monkeypatch):
    """审计 G2：bot 刚写入/换轮的一对一结果必须立刻可见（fresh），不吃 300s 快照。

    桩：活动 fresh 读=已完成、fresh 结果=1 条；fixture 的 stale 结果为空。
    旧实现走 _snap_live → 空 → count=0（本用例红）。
    """
    live_act = _act("已完成")
    live_res = [_res(ME, "ou_f1", 1)]
    monkeypatch.setattr(app, "_snap_fresh", lambda key: {
        "activities": [live_act],
        "onetoone_results": live_res,
    }.get(key, []))
    r = api.get(f"/api/activities/{ACT}/onetoone/result")
    assert r.status_code == 200
    assert r.get_json()["count"] == 1, "必须读到 fresh 结果，不能吃陈旧快照"


# ================================================================
# my_groups：过滤已取消 + 按活动去重
# ================================================================

def _cancelled_and_active_signups():
    rows = [_signup(ME, status="已取消") for _ in range(4)]
    rows.append(_signup(ME))  # 唯一有效的一条（同活动）
    return rows


def test_my_groups_filters_cancelled_and_dedupes(api):
    api.install(
        activities=[_act("已完成", group_status="收集中")],
        live_activities=[_act("已完成", group_status="收集中")],
        signups=_cancelled_and_active_signups(),
        stale_signups=_cancelled_and_active_signups(),
    )
    r = api.get("/api/activities/mine/groups")
    acts = r.get_json()["activities"]
    assert len(acts) == 1
    assert acts[0]["activity_id"] == ACT
