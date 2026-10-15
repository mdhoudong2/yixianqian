"""管理员一对一管理台 API 契约（不联网，桩表驱动）。

覆盖 C3（R2 互斥管理台）：
- 9+ 端点鉴权（未登录 401 / 非管理员 403）；
- 互斥写入器：set_mode / activity/mode / onetoone/flag 的「双栏位单笔 PUT」；
  set_group_flag 的条件合并（在 test_group_admin_api.py 另有对照）；
- 动作层 guard：EXCLUSIVE_MODE 的拦截与*假表无对侧栏位时不触发*的 no-op；
- preview 零写入；start 破坏性清结果 + 失败如实上报；run job 生命周期；
- H5 与 bot 的 configure 注入逐栏相等（与分组台同款防线）。

桩法比照 tests/test_group_admin_api.py：Flask test client + cookie session +
monkeypatch 快照；假表复用 tests/test_onetoone_parity.py 的 FakeBitable——
桩「存取」，幂等/回滚/算法/锁全跑 lib 真实现。
"""
import contextlib
import os
import sys
import threading
import time

import pytest

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402

import onetoone as bot_onetoone  # noqa: E402
from lib import grouping as lib_grouping  # noqa: E402
from lib import onetoone as lib_onetoone  # noqa: E402
from lib import storage  # noqa: E402
from tests.test_grouping_parity import (  # noqa: E402
    ACTIVITY_TABLE_ID,
    SIGNUP_TABLE_ID,
    _signup,
)
from tests.test_onetoone_parity import FakeBitable  # noqa: E402

ADMIN = "ou_test_admin"
ME = "ou_me"
ACT = "A-0002"

# 测试表 ID：fixture 里同步注入 app 与 lib.onetoone，避免打到真实测试库的测试表
SELECT_TBL = "tbl_test_oto_admin_select"
RESULT_TBL = "tbl_test_oto_admin_result"


# ---------------------------------------------------------------- 桩数据

def _user(oid, uid, gender, nickname):
    return {"record_id": f"rec_u_{oid}",
            "fields": {"飞书用户ID": oid, "用户ID": uid, "性别": gender, "昵称": nickname}}


USER_DIR = {
    "ou_m0": _user("ou_m0", "U-0001", "男性", "小北"),
    "ou_m1": _user("ou_m1", "U-0002", "男性", "阿北"),
    "ou_f0": _user("ou_f0", "U-0003", "女性", "阿南"),
    "ou_f1": _user("ou_f1", "U-0004", "女性", "小西"),
    "ou_f9": _user("ou_f9", "U-0009", "女性", "阿东"),
}


def _find_user(open_id):
    """H5 版契约：返回单条记录或 None（app._grouping_find_user_by_openid 会包成列表）。"""
    return USER_DIR.get(open_id)


def _oto_act(oto_status="收集中", oto_flag="是", group_status="未开始", group_flag="否",
             **over):
    fields = {"活动ID": ACT, "活动名称": "关系自洽",
              "一对一状态": oto_status, "一对一功能开启": oto_flag,
              "分组状态": group_status, "分组功能开启": group_flag}
    fields.update(over)
    return {"record_id": "rec_act", "fields": fields}


def _oto_sel(oid, gender, choices=()):
    fields = {"活动ID": ACT, "选择人open_id": oid, "选择人性别": gender}
    for i in range(7):
        fields[f"第{i + 1}志愿"] = choices[i] if i < len(choices) else ""
    return {"record_id": f"rec_oto_{oid}", "fields": fields}


def _oto_res_row(oid, target, rank=1):
    """结果表行（字段名与 web/backend/config.py F_OTO_* 一致）。"""
    return {"record_id": f"rec_res_{oid}_{target}",
            "fields": {"活动ID": ACT, "排名": rank, "用户open_id": oid,
                       "用户昵称": USER_DIR.get(oid, {}).get("fields", {}).get("昵称", ""),
                       "用户性别": USER_DIR.get(oid, {}).get("fields", {}).get("性别", ""),
                       "必聊对象open_id": target,
                       "必聊对象昵称": USER_DIR.get(target, {}).get("fields", {}).get("昵称", "")}}


def _balanced(n_males, n_females, oid_names=("ou_m", "ou_f")):
    """n 男 n 女、每人一条志愿的报名与志愿记录（与分组台同名夹具同构）。"""
    signups, selections = [], []
    for i in range(n_males):
        oid = f"{oid_names[0]}{i}"
        signups.append(_signup(oid, USER_DIR[oid]["fields"]["昵称"]))
        selections.append(_oto_sel(oid, "男性", ()))
    for i in range(n_females):
        oid = f"{oid_names[1]}{i}"
        signups.append(_signup(oid, USER_DIR[oid]["fields"]["昵称"]))
        selections.append(_oto_sel(oid, "女性", ()))
    return signups, selections


def _find_act(fake, activity_id):
    for r in fake.tables.get(ACTIVITY_TABLE_ID, []):
        if r["fields"].get("活动ID") == activity_id:
            return r
    return None


# ---------------------------------------------------------------- 测试脚手架

class Api:
    """假表 + fresh 快照桩 + 调用记录；管理员/普通用户会话已就绪。"""

    def __init__(self, monkeypatch, tmp_path):
        self._monkeypatch = monkeypatch
        self.fake = FakeBitable({})
        monkeypatch.setattr(app, "_grouping_client", self.fake)
        self.snapshot = {"activities": [], "group_selections": [], "group_results": [],
                         "onetoone_selections": [], "onetoone_results": [],
                         "signups": [], "users": [], "observers": [], "messages": []}
        self.fresh_calls = []
        self.refresh_calls = []
        monkeypatch.setattr(app, "_snap_fresh", self._fresh)
        monkeypatch.setattr(app, "_snap", lambda key: list(self.snapshot.get(key, [])))
        monkeypatch.setattr(app, "_snap_live", lambda key: list(self.snapshot.get(key, [])))
        monkeypatch.setattr(app, "refresh_snapshot_table_async", self.refresh_calls.append)
        # 模式/开关端点改同步刷新后，同口径记录同步入口（coalesce 默认 0.0=写后必抓）
        monkeypatch.setattr(app, "refresh_snapshot_table",
                            lambda key, coalesce=0.0: self.refresh_calls.append(key))
        self.client = app.app.test_client()
        self.client.set_cookie("yxq_session", app.create_session(ADMIN))
        self.client_me = app.app.test_client()
        self.client_me.set_cookie("yxq_session", app.create_session(ME))

    def _fresh(self, key):
        self.fresh_calls.append(key)
        return list(self.snapshot.get(key, []))

    def install(self, activity=None, signups=(), selections=(), results=(),
                extra_activities=()):
        """装假表与快照（活动默认：一对一收集中、功能开启）。"""
        self.tables = {
            ACTIVITY_TABLE_ID: [activity if activity is not None else _oto_act()],
            SIGNUP_TABLE_ID: list(signups),
            SELECT_TBL: list(selections),
            RESULT_TBL: list(results),
        }
        self.fake = FakeBitable(self.tables)
        self._monkeypatch.setattr(app, "_grouping_client", self.fake)
        self.snapshot["activities"] = list(extra_activities) + list(self.tables[ACTIVITY_TABLE_ID])
        self.snapshot["signups"] = list(signups)
        self.snapshot["onetoone_selections"] = list(selections)
        self.snapshot["onetoone_results"] = list(results)
        return self.fake

    def get(self, path, client=None):
        return (client or self.client).get(path)

    def post(self, path, payload=None, client=None):
        return (client or self.client).post(
            path, json=payload or {}, headers={"X-Requested-With": "XMLHttpRequest"})


@pytest.fixture
def api(monkeypatch, tmp_path):
    # 表 ID 换成测试表，并把注入同步给 lib.onetoone（app 端 configure 每调一次都重读全局）
    monkeypatch.setattr(app, "ONETOONE_SELECT_TABLE", SELECT_TBL)
    monkeypatch.setattr(app, "ONETOONE_RESULT_TABLE", RESULT_TBL)
    monkeypatch.setattr(lib_grouping, "log", lambda *a, **k: None)
    monkeypatch.setattr(lib_onetoone, "log", lambda *a, **k: None)
    monkeypatch.setattr(app, "snap_find_user_by_openid", _find_user)
    monkeypatch.setattr(app.bitable, "search_records", lambda *a, **k: [])
    monkeypatch.setattr(app, "GROUPING_JOBS_FILE", str(tmp_path / "grouping_jobs.json"))
    monkeypatch.setattr(app, "ONETOONE_JOBS_FILE", str(tmp_path / "onetoone_jobs.json"))
    app.configure_grouping_lib(str(tmp_path))
    app.configure_onetoone_lib(str(tmp_path))
    app._rate_store.clear()
    return Api(monkeypatch, tmp_path)


def _poll_job(api, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = api.get("/api/admin/onetoone/job?job_id=" + job_id)
        assert r.status_code == 200
        job = r.get_json()
        if job["state"] in ("done", "failed", "unknown"):
            return job
        time.sleep(0.05)
    raise AssertionError("job 未在期限内结束")


def _wait_refresh_calls(api, expected, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if set(api.refresh_calls) >= set(expected):
            return
        time.sleep(0.01)
    raise AssertionError(
        f"刷新调用未在 {timeout}s 内收敛：expected {sorted(expected)}, "
        f"actual {sorted(api.refresh_calls)}")


ENDPOINTS = [
    ("GET", "/api/admin/onetoone/status?activity_id=" + ACT, None),
    ("GET", "/api/admin/onetoone/unsubmitted?activity_id=" + ACT, None),
    ("GET", "/api/admin/onetoone/result?activity_id=" + ACT, None),
    ("POST", "/api/admin/onetoone/preview", {"activity_id": ACT}),
    ("POST", "/api/admin/onetoone/start", {"activity_id": ACT}),
    ("POST", "/api/admin/onetoone/run", {"activity_id": ACT}),
    ("GET", "/api/admin/onetoone/job?job_id=x", None),
    ("POST", "/api/admin/onetoone/flag", {"activity_id": ACT, "enabled": True}),
    ("POST", "/api/admin/activity/mode", {"activity_id": ACT, "mode": "group"}),
]
ENDPOINT_IDS = [p.split("?")[0].split("/api/admin/")[1].replace("/", "_").replace("?", "_")
                for m, p, _ in ENDPOINTS]


def _call(c, method, path, payload):
    if method == "POST":
        return c.post(path, json=payload or {},
                      headers={"X-Requested-With": "XMLHttpRequest"})
    return c.get(path)


# ---------------------------------------------------------------- 鉴权

@pytest.mark.parametrize("method,path,payload", ENDPOINTS, ids=ENDPOINT_IDS)
def test_unauthenticated_is_401(method, path, payload):
    c = app.app.test_client()
    assert _call(c, method, path, payload).status_code == 401


@pytest.mark.parametrize("method,path,payload", ENDPOINTS, ids=ENDPOINT_IDS)
def test_non_admin_is_403(api, method, path, payload):
    assert _call(api.client_me, method, path, payload).status_code == 403


# ---------------------------------------------------------------- 互斥写入器

def test_set_mode_writes_both_flags_in_one_put(api):
    """L1 不变量：一次 PUT 同时写两个栏位（同一笔 update payload；飞书单条 update 本身不是整笔事务，互斥由活动锁串行化保证）。"""
    fake = api.install(activity=_oto_act(oto_status="未开始", oto_flag="否"))
    r = api.post("/api/admin/activity/mode", {"activity_id": ACT, "mode": "onetoone"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["mode"] == "onetoone" and body["changed"] is True
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert len(updates) == 1, "必须恰一笔 update"
    assert updates[0][3] == {"分组功能开启": "否", "一对一功能开启": "是"}
    assert api.refresh_calls == ["activities"]

    # 已经是目标态：幂等跳过（不再写、不再刷快照）
    api.snapshot["activities"][0]["fields"]["一对一功能开启"] = "是"
    r2 = api.post("/api/admin/activity/mode", {"activity_id": ACT, "mode": "onetoone"})
    assert r2.status_code == 200 and r2.get_json()["changed"] is False
    assert len([c for c in fake.writes() if c[0] == "update"]) == 1


@pytest.mark.parametrize("mode,expect", [
    ("group", {"分组功能开启": "是", "一对一功能开启": "否"}),
    ("onetoone", {"分组功能开启": "否", "一对一功能开启": "是"}),
], ids=["group", "onetoone"])
def test_set_mode_dual_write_all_modes(api, mode, expect):
    # 两种状态都已收尾（flag=否、已完成）：切换允许，且恒为一笔双栏位 PUT
    fake = api.install(activity=_oto_act(oto_status="已完成", oto_flag="否",
                                         group_status="已完成", group_flag="否"))
    r = api.post("/api/admin/activity/mode", {"activity_id": ACT, "mode": mode})
    assert r.status_code == 200
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert len(updates) == 1 and updates[0][3] == expect


def test_set_mode_none_corrects_conflict_and_writes_both_off(api):
    """冲突态纠偏：双开脏数据下切 none 一笔把两个 flag 都关掉（一键纠偏的另一分支）。"""
    fake = api.install(activity=_oto_act(oto_status="已完成", oto_flag="是",
                                         group_status="已完成", group_flag="是"))
    r = api.post("/api/admin/activity/mode", {"activity_id": ACT, "mode": "none"})
    assert r.status_code == 200
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert len(updates) == 1 and updates[0][3] == {"分组功能开启": "否", "一对一功能开启": "否"}


def test_set_mode_none_idempotent_when_nothing_open(api):
    fake = api.install(activity=_oto_act(oto_status="未开始", oto_flag="否", group_flag="否"))
    r = api.post("/api/admin/activity/mode", {"activity_id": ACT, "mode": "none"})
    assert r.status_code == 200 and r.get_json()["changed"] is False
    assert [c for c in fake.writes() if c[0] == "update"] == []


def test_mode_conflict_correction_blocked_when_closing_side_collecting(api):
    """冲突纠偏的例外仅限「已收尾」侧：正在收集中/已截止的一侧仍必须拦。"""
    fake = api.install(activity=_oto_act(oto_status="收集中", oto_flag="是",
                                         group_status="已完成", group_flag="是"))
    r = api.post("/api/admin/activity/mode", {"activity_id": ACT, "mode": "group"})
    assert r.status_code == 409
    assert "一对一" in r.get_json()["blocked_sides"]
    assert fake.writes() == []


def test_set_mode_conflict_correction_single_put(api):
    """历史脏数据「双是」：纠偏也走一笔双栏位 PUT，不可能残留双是。"""
    fake = api.install(activity=_oto_act(oto_status="已完成", oto_flag="是",
                                         group_status="已完成", group_flag="是"))
    r = api.post("/api/admin/activity/mode", {"activity_id": ACT, "mode": "group"})
    assert r.status_code == 200
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert len(updates) == 1
    assert updates[0][3] == {"分组功能开启": "是", "一对一功能开启": "否"}


def test_sequential_mode_switches_never_both_yes(api):
    """连续切换的每一步都是一笔双栏位 PUT → 任何中间态都不会「双是」。"""
    api.install(activity=_oto_act(oto_flag="否", group_flag="否"))
    lib_grouping.set_mode(ACT, "group", client=api.fake,
                          activity=_oto_act(oto_flag="否", group_flag="否"))
    cur = _find_act(api.fake, ACT)
    lib_grouping.set_mode(ACT, "onetoone", client=api.fake, activity=cur)
    f = _find_act(api.fake, ACT)["fields"]
    assert not (f["分组功能开启"] == "是" and f["一对一功能开启"] == "是")
    assert f["一对一功能开启"] == "是" and f["分组功能开启"] == "否"
    updates = [c for c in api.fake.writes() if c[0] == "update"]
    assert updates and all(set(u[3]) == {"分组功能开启", "一对一功能开启"} for u in updates), \
        "每一次切换都必须整笔写两个栏位"


def test_activity_mode_blocked_when_closing_side_collecting(api):
    fake = api.install(activity=_oto_act(oto_flag="否", group_flag="是", group_status="收集中"))
    r = api.post("/api/admin/activity/mode", {"activity_id": ACT, "mode": "onetoone"})
    assert r.status_code == 409
    assert r.get_json()["code"] == "MODE_SWITCH_BLOCKED"
    assert fake.writes() == [], "拒绝切换必须零写入"


def test_activity_mode_blocked_when_flag_off_but_collecting(api):
    """审计必修 1（漏洞复现）：flag=否 + 分组状态=收集中 时切模式必须 409。

    用户端 select 只看状态不看 flag，此时分组通道实际还开着；旧实现（AND 判定）
    放行切换 → 两条通道并存、互斥不变量被绕过。
    """
    fake = api.install(activity=_oto_act(oto_flag="否", group_flag="否", group_status="收集中"))
    r = api.post("/api/admin/activity/mode", {"activity_id": ACT, "mode": "onetoone"})
    assert r.status_code == 409
    assert r.get_json()["code"] == "MODE_SWITCH_BLOCKED"
    assert "分组" in r.get_json()["blocked_sides"]
    assert fake.writes() == [], "拒绝切换必须零写入"


def test_activity_mode_allows_switch_when_closing_flag_on_but_done(api):
    """已收尾（状态=已完成）的 flag=是 不再拦截（2026-10-13 修死锁）。

    切换本身是一次原子双栏位写：同一 PUT 关闭旧入口、开启新入口，与 bot
    「开启一对一功能」同一套 set_mode 语义（该路径本就不拦 flag）。拦 flag 会造成
    「完成分组后无法切一对一、控制台又无手动关 flag 入口」的死锁（A-0011 实测）。
    只有 收集中/已截止 才必须先收尾（另两条用例锁定）。
    """
    fake = api.install(activity=_oto_act(oto_status="已完成", oto_flag="是",
                                         group_flag="否", group_status="已完成"))
    r = api.post("/api/admin/activity/mode", {"activity_id": ACT, "mode": "group"})
    assert r.status_code == 200, r.get_json()
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert len(updates) == 1 and updates[0][3] == {"分组功能开启": "是", "一对一功能开启": "否"}


def test_activity_mode_blocked_when_active_job(api):
    fake = api.install(activity=_oto_act(oto_flag="否", group_flag="是", group_status="已完成"))
    active = {"activity_id": ACT, "operator": ADMIN, "state": "running", "pid": os.getpid(),
              "progress": "", "created_at": int(time.time()), "started_at": int(time.time()),
              "finished_at": None, "result": None, "error": ""}
    storage.update_json(app.GROUPING_JOBS_FILE, {}, lambda jobs: {**jobs, "activejob0001": active})
    r = api.post("/api/admin/activity/mode", {"activity_id": ACT, "mode": "none"})
    assert r.status_code == 409 and r.get_json()["code"] == "MODE_SWITCH_BLOCKED"
    assert fake.writes() == []


def test_activity_mode_validation_and_missing(api):
    api.install()
    assert api.post("/api/admin/activity/mode",
                    {"activity_id": ACT, "mode": "both"}).status_code == 400
    assert api.post("/api/admin/activity/mode", {"mode": "group"}).status_code == 400
    assert api.post("/api/admin/activity/mode",
                    {"activity_id": "A-9999", "mode": "group"}).status_code == 404


def test_onetoone_flag_enable_dual_write_disable_single(api):
    fake = api.install(activity=_oto_act(oto_status="已完成", oto_flag="否", group_flag="否"))
    r = api.post("/api/admin/onetoone/flag", {"activity_id": ACT, "enabled": True})
    assert r.status_code == 200 and r.get_json()["onetoone_flag"] == "是"
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert len(updates) == 1
    assert updates[0][3] == {"分组功能开启": "否", "一对一功能开启": "是"}, "开启=双栏位一 PUT"

    # 关闭：只写自身=否（不碰分组栏位）
    api.snapshot["activities"][0]["fields"]["一对一功能开启"] = "是"
    r2 = api.post("/api/admin/onetoone/flag", {"activity_id": ACT, "enabled": False})
    assert r2.status_code == 200 and r2.get_json()["onetoone_flag"] == "否"
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert len(updates) == 2 and updates[1][3] == {"一对一功能开启": "否"}

    # 已是「否」：幂等不写（fresh 读会看到刚写的值，测试里手动同步快照模拟这次实时读）
    api.snapshot["activities"][0]["fields"]["一对一功能开启"] = "否"
    r3 = api.post("/api/admin/onetoone/flag", {"activity_id": ACT, "enabled": False})
    assert r3.status_code == 200
    assert len([c for c in fake.writes() if c[0] == "update"]) == 2


def test_onetoone_flag_corrects_dirty_both_on(api):
    """双是脏数据下再点开启：走 set_mode 双写把分组=否一并纠正。"""
    fake = api.install(activity=_oto_act(oto_status="已完成", oto_flag="是", group_flag="是"))
    r = api.post("/api/admin/onetoone/flag", {"activity_id": ACT, "enabled": True})
    assert r.status_code == 200
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert len(updates) == 1
    assert updates[0][3] == {"分组功能开启": "否", "一对一功能开启": "是"}


@contextlib.contextmanager
def _busy_lock(activity_id, timeout):
    raise lib_grouping.GroupingBusy(activity_id)
    yield  # pragma: no cover


@pytest.mark.parametrize("enabled,lock_module", [
    (True, lib_grouping),
    (False, lib_onetoone),
], ids=["enable_via_set_mode", "disable_own_lock"])
def test_onetoone_flag_busy_returns_409(api, monkeypatch, enabled, lock_module):
    """开启（走 set_mode）与关闭（自持锁）两条路抢锁失败都回 409：无 ok、零写入。

    关闭路的锁符号绑定在 lib.onetoone 命名空间，与 set_mode 用的不是同一个——两条路
    各自 mock；将来任一条路漏了锁，对应用例都会变红。
    """
    fake = api.install(activity=_oto_act(oto_status="已完成",
                                         oto_flag="否" if enabled else "是",
                                         group_flag="否"))
    monkeypatch.setattr(lock_module, "grouping_lock", _busy_lock)
    r = api.post("/api/admin/onetoone/flag", {"activity_id": ACT, "enabled": enabled})
    assert r.status_code == 409
    body = r.get_json()
    assert "ok" not in body and body["busy_activity_id"] == ACT
    assert fake.writes() == [], "抢锁失败不得写任何东西"


def test_activity_mode_busy_returns_409(api, monkeypatch):
    # flag=否（两种状态都没开着）：过 closing 检查后才会走到抢锁，这样测的是锁路径
    fake = api.install(activity=_oto_act(oto_status="已完成", oto_flag="否", group_flag="否"))
    monkeypatch.setattr(lib_grouping, "grouping_lock", _busy_lock)
    r = api.post("/api/admin/activity/mode", {"activity_id": ACT, "mode": "group"})
    assert r.status_code == 409
    body = r.get_json()
    assert "ok" not in body and body["busy_activity_id"] == ACT
    assert fake.writes() == [], "抢锁失败不得写任何东西"


@pytest.mark.parametrize("enabled", [True, False], ids=["enable", "disable"])
def test_onetoone_flag_write_failed_returns_500(api, enabled):
    """update_record 返回假 → 500 WRITE_FAILED；开启/关闭两条路都绝不回 ok:true。"""
    fake = api.install(activity=_oto_act(oto_status="已完成",
                                         oto_flag="否" if enabled else "是",
                                         group_flag="否"))
    fake.update_record = lambda *a, **k: None
    r = api.post("/api/admin/onetoone/flag", {"activity_id": ACT, "enabled": enabled})
    assert r.status_code == 500
    body = r.get_json()
    assert body["code"] == "WRITE_FAILED" and "ok" not in body
    assert api.refresh_calls == [], "写失败不刷新快照"


def test_activity_mode_write_failed_returns_500(api):
    fake = api.install(activity=_oto_act(oto_status="已完成", oto_flag="否", group_flag="否"))
    fake.update_record = lambda *a, **k: None
    r = api.post("/api/admin/activity/mode", {"activity_id": ACT, "mode": "group"})
    assert r.status_code == 500
    body = r.get_json()
    assert body["code"] == "WRITE_FAILED" and "ok" not in body
    assert api.refresh_calls == [], "写失败不刷新快照"


# ---------------------------------------------------------------- 动作层 guard

def test_guard_noop_when_counterpart_fields_absent(api):
    """假表/旧表没有分组栏位 → guard 不触发，与旧行为逐字一致（parity 保护面）。"""
    # 活动只有一对一栏位（无分组功能开启/分组状态）
    act = {"record_id": "rec_act", "fields": {"活动ID": ACT, "活动名称": "关系自洽",
                                              "一对一状态": "收集中"}}
    fake = api.install(activity=act)
    res = lib_onetoone.start_collection(ACT, client=fake, activity=act)
    assert res["code"] == "OK"
    res2 = lib_onetoone.execute(ACT, 1, client=fake, activity=act,
                                find_user_by_openid=app._grouping_find_user_by_openid,
                                female_chats=2)
    # 无参与者 → NO_PARTICIPANTS（与迁移前一致），绝无 EXCLUSIVE_MODE
    assert res2["code"] == "NO_PARTICIPANTS"


@pytest.mark.parametrize("flag,status", [("是", "未开始"), ("否", "收集中"), ("否", "已截止")],
                         ids=["flag_on", "collecting", "closed"])
def test_guard_blocks_onetoone_start(api, flag, status):
    """「开始一对一」：对面分组 flag=是 或状态∈{收集中,已截止} → EXCLUSIVE_MODE 零写入。"""
    act = _oto_act(oto_status="未开始", oto_flag="否",
                   group_flag=flag, group_status=status)
    fake = api.install(activity=act)
    res = lib_onetoone.start_collection(ACT, client=fake, activity=act)
    assert res["code"] == "EXCLUSIVE_MODE" and res["blocked_by"] == "group"
    assert fake.writes() == []


def test_guard_blocks_onetoone_execute_when_group_flag_on(api):
    """「执行一对一」：对面分组 flag=是 → EXCLUSIVE_MODE 零写入（状态层不拦执行）。"""
    act = _oto_act(oto_status="收集中", group_flag="是", group_status="已完成")
    fake = api.install(activity=act)
    res = lib_onetoone.execute(ACT, 1, client=fake, activity=act,
                               find_user_by_openid=app._grouping_find_user_by_openid,
                               female_chats=2)
    assert res["code"] == "EXCLUSIVE_MODE"
    assert fake.writes() == []


def test_guard_does_not_block_execute_on_group_status_only(api):
    """执行不拦「对面只是收集中」：flag 层是唯一权威，状态层只拦「开始」。"""
    signups, selections = _balanced(2, 2)
    act = _oto_act(oto_status="收集中", group_flag="否", group_status="已完成")
    fake = api.install(activity=act, signups=signups, selections=selections)
    res = lib_onetoone.execute(ACT, 1, client=fake, activity=act,
                               find_user_by_openid=app._grouping_find_user_by_openid,
                               female_chats=2)
    assert res["code"] == "OK"


def test_start_endpoint_exclusive_mode_409(api):
    fake = api.install(activity=_oto_act(oto_status="未开始", oto_flag="否",
                                         group_flag="是", group_status="已完成"))
    r = api.post("/api/admin/onetoone/start", {"activity_id": ACT})
    assert r.status_code == 409 and r.get_json()["code"] == "EXCLUSIVE_MODE"
    assert fake.writes() == []


# ---------------------------------------------------------------- preview 零写入

def test_preview_writes_nothing(api):
    signups, selections = _balanced(2, 2)
    fake = api.install(signups=signups, selections=selections,
                       results=[_oto_res_row("ou_m0", "ou_f0")])
    r = api.post("/api/admin/onetoone/preview", {"activity_id": ACT})
    assert r.status_code == 200
    body = r.get_json()
    assert body["can_execute"] is False
    assert body["participant_count"] == 4
    assert body["existing_results"] == 1
    assert fake.writes() == [], "preview 一次写都不能有"
    assert _find_act(fake, ACT)["fields"]["一对一状态"] == "收集中"


def test_preview_participant_count_and_blockers(api):
    signups, selections = _balanced(2, 2)
    api.install(signups=signups, selections=selections)
    body = api.post("/api/admin/onetoone/preview",
                    {"activity_id": ACT, "female_chats": 2}).get_json()
    assert body["participant_count"] == 4
    assert body["male_count"] == 2 and body["female_count"] == 2
    assert body["existing_results"] == 0 and body["can_execute"] is True
    assert body["blockers"] == []

    # 女生场次缺省 → 拦下（can_execute False）
    body = api.post("/api/admin/onetoone/preview", {"activity_id": ACT}).get_json()
    assert body["can_execute"] is False
    assert any("请指定女生场次" in b for b in body["blockers"])

    api.install()
    body = api.post("/api/admin/onetoone/preview",
                    {"activity_id": ACT, "female_chats": 2}).get_json()
    assert body["can_execute"] is False
    assert any("没有已报名" in b for b in body["blockers"])


# ---------------------------------------------------------------- start：破坏性且如实上报

def test_start_clears_results_and_reports(api):
    signups, selections = _balanced(2, 2)
    fake = api.install(activity=_oto_act(oto_status="已完成"), signups=signups,
                       selections=selections,
                       results=[_oto_res_row("ou_m0", "ou_f0"), _oto_res_row("ou_m1", "ou_f1")])
    r = api.post("/api/admin/onetoone/start", {"activity_id": ACT})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["cleared_results"] == 2
    assert body["onetoone_status"] == "收集中"
    assert fake.tables[RESULT_TBL] == []
    assert _find_act(fake, ACT)["fields"]["一对一状态"] == "收集中"
    assert sorted(api.refresh_calls) == ["activities", "onetoone_results"]


def test_start_search_failure_reports_500(api):
    fake = api.install(activity=_oto_act(oto_status="已完成"))
    fake.search_fails = {RESULT_TBL}
    r = api.post("/api/admin/onetoone/start", {"activity_id": ACT})
    assert r.status_code == 500
    assert r.get_json()["code"] == "RESULTS_SEARCH_FAILED"
    assert _find_act(fake, ACT)["fields"]["一对一状态"] == "收集中", "状态已写入，如实可见"
    assert api.refresh_calls == ["activities"]


def test_start_busy_lock_returns_409(api, monkeypatch):
    fake = api.install(activity=_oto_act(oto_status="已完成"))
    monkeypatch.setattr(app, "GROUPING_API_LOCK_TIMEOUT", 0.1)
    with lib_grouping.grouping_lock(ACT, timeout=1):
        r = api.post("/api/admin/onetoone/start", {"activity_id": ACT})
    assert r.status_code == 409
    assert fake.writes() == [], "抢锁失败不得写任何东西"


# ---------------------------------------------------------------- run：job 生命周期

def test_run_returns_202_and_job_reaches_done(api):
    signups, selections = _balanced(2, 2)
    api.install(signups=signups, selections=selections)
    r = api.post("/api/admin/onetoone/run", {"activity_id": ACT, "female_chats": 2})
    assert r.status_code == 202
    body = r.get_json()
    assert body["ok"] is True and body["state"] == "pending" and body["job_id"]

    job = _poll_job(api, body["job_id"])
    assert job["state"] == "done"
    assert job["result"]["code"] == "OK"
    assert job["result"]["participant_count"] == 4
    assert job["result"]["total_pairs"] > 0
    assert api.fake.tables[RESULT_TBL], "结果必须真的写进表"
    assert _find_act(api.fake, ACT)["fields"]["一对一状态"] == "已完成"
    _wait_refresh_calls(api, {"activities", "onetoone_results"})


def test_run_duplicate_submission_returns_existing_job(api):
    signups, selections = _balanced(2, 2)
    api.install(signups=signups, selections=selections)
    entered = threading.Event()
    release = threading.Event()

    def slow_execute(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return {"code": "ALREADY_DONE", "activity_id": ACT, "activity_name": "关系自洽"}

    orig = lib_onetoone.execute
    lib_onetoone.execute = slow_execute
    try:
        r1 = api.post("/api/admin/onetoone/run", {"activity_id": ACT, "female_chats": 2})
        assert r1.status_code == 202
        job_id = r1.get_json()["job_id"]
        assert entered.wait(timeout=5), "job 线程应已开始执行"

        r2 = api.post("/api/admin/onetoone/run", {"activity_id": ACT, "female_chats": 2})
        assert r2.status_code == 409
        assert r2.get_json()["job_id"] == job_id

        release.set()
        assert _poll_job(api, job_id)["state"] == "done"
    finally:
        lib_onetoone.execute = orig


def test_run_search_failure_aborts_without_writes(api):
    signups, selections = _balanced(2, 2)
    fake = api.install(signups=signups, selections=selections)
    fake.search_fails = {RESULT_TBL}
    r = api.post("/api/admin/onetoone/run", {"activity_id": ACT, "female_chats": 2})
    assert r.status_code == 202
    job = _poll_job(api, r.get_json()["job_id"])
    assert job["state"] == "done"
    assert job["result"]["code"] == "SEARCH_FAILED"
    assert fake.writes() == [], "读不到既有结果就不能动任何栏位"


def test_job_exception_recorded_and_lock_released(api):
    signups, selections = _balanced(2, 2)
    api.install(signups=signups, selections=selections)
    orig = lib_onetoone.execute

    def boom(*args, **kwargs):
        raise RuntimeError("写表炸了")

    lib_onetoone.execute = boom
    try:
        r = api.post("/api/admin/onetoone/run", {"activity_id": ACT, "female_chats": 2})
        assert r.status_code == 202
        job = _poll_job(api, r.get_json()["job_id"])
        assert job["state"] == "failed" and "写表炸了" in job["error"]
        _wait_refresh_calls(api, {"activities", "onetoone_results"})
    finally:
        lib_onetoone.execute = orig

    # 异常路径也必须放锁：重跑能正常跑完
    r2 = api.post("/api/admin/onetoone/run", {"activity_id": ACT, "female_chats": 2})
    assert r2.status_code == 202
    assert _poll_job(api, r2.get_json()["job_id"])["state"] == "done"


def test_dead_process_job_reports_unknown_and_does_not_block(api):
    dead = {"activity_id": ACT, "operator": "ou_old", "state": "running",
            "pid": os.getpid() + 1000, "progress": "", "created_at": int(time.time()),
            "started_at": int(time.time()), "finished_at": None, "result": None, "error": ""}
    storage.update_json(app.ONETOONE_JOBS_FILE, {}, lambda jobs: {**jobs, "deadbeef0000": dead})
    r = api.get("/api/admin/onetoone/job?job_id=deadbeef0000")
    assert r.status_code == 200 and r.get_json()["state"] == "unknown"

    signups, selections = _balanced(2, 2)
    api.install(signups=signups, selections=selections)
    r2 = api.post("/api/admin/onetoone/run", {"activity_id": ACT, "female_chats": 2})
    assert r2.status_code == 202, "上个进程的残留 job 不能永远挡住重跑"
    assert _poll_job(api, r2.get_json()["job_id"])["state"] == "done"


def test_onetoone_job_conflict_prunes_expired(api):
    now = int(time.time())
    active = {"activity_id": ACT, "round": 1, "operator": ADMIN, "state": "running",
              "pid": os.getpid(), "progress": "", "created_at": now, "started_at": now,
              "finished_at": None, "result": None, "error": ""}
    expired = {"activity_id": "A-0009", "round": 1, "operator": ADMIN, "state": "done",
               "pid": os.getpid(), "progress": "",
               "created_at": now - app.GROUPING_JOB_TTL - 10,
               "started_at": now - app.GROUPING_JOB_TTL - 10,
               "finished_at": None, "result": None, "error": ""}
    storage.update_json(app.ONETOONE_JOBS_FILE, {},
                        lambda jobs: {**jobs, "activejob001": active, "expiredjob01": expired})
    conflict = app._create_onetoone_job("newjob000001", ACT, 1, ADMIN)
    assert conflict == "activejob001"
    jobs = storage.load_json(app.ONETOONE_JOBS_FILE, {})
    assert "expiredjob01" not in jobs
    assert "newjob000001" not in jobs


def test_job_endpoint_validation(api):
    api.install()
    assert api.get("/api/admin/onetoone/job").status_code == 400
    assert api.get("/api/admin/onetoone/job?job_id=nope").status_code == 404


def test_run_large_round_accepted_and_written(api):
    """不限轮次：round=999 照常入列并完成，结果行带「轮次=999」（旧上限 1-50 已废）。"""
    signups, selections = _balanced(2, 2)
    api.install(signups=signups, selections=selections)
    r = api.post("/api/admin/onetoone/run",
                 {"activity_id": ACT, "round": 999, "female_chats": 2})
    assert r.status_code == 202, "大轮次必须能入列执行"
    job = _poll_job(api, r.get_json()["job_id"])
    assert job["state"] == "done" and job["result"]["code"] == "OK"
    rows = [row for row in api.fake.tables[RESULT_TBL]
            if row["fields"].get("轮次") == "999"]
    assert len(rows) == 8, "2男2女各出 2 条必聊，全部带轮次 999"
    _wait_refresh_calls(api, {"activities", "onetoone_results"})


def test_run_payload_validation_and_missing(api):
    api.install()
    assert api.post("/api/admin/onetoone/run", {"activity_id": ""}).status_code == 400
    # 轮次不限（字段已转文本）：非 ≥1 整数在 H5 run 收口 → 400（大轮次合法，另有用例）
    assert api.post("/api/admin/onetoone/run",
                    {"activity_id": ACT, "round": 0, "female_chats": 2}).status_code == 400
    assert api.post("/api/admin/onetoone/run",
                    {"activity_id": ACT, "round": "2", "female_chats": 2}).status_code == 400
    # 女生场次缺省在 H5 run 收口（非法值另测，避免撞 run 端点的 5 次/60s 限流）
    assert api.post("/api/admin/onetoone/run", {"activity_id": ACT}).status_code == 400
    assert api.post("/api/admin/onetoone/run",
                    {"activity_id": "A-9999", "female_chats": 2}).status_code == 404
    assert api.post("/api/admin/onetoone/start", {"activity_id": "A-9999"}).status_code == 404
    assert api.post("/api/admin/onetoone/preview", {}).status_code == 400
    assert api.post("/api/admin/onetoone/flag",
                    {"activity_id": ACT, "enabled": "yes"}).status_code == 400


def test_run_female_chats_invalid_values_rejected(api):
    """女生场次非法值（0/非整数/布尔）在 H5 run 收口 → 400。"""
    api.install()
    for bad in (0, "2", True):
        assert api.post("/api/admin/onetoone/run",
                        {"activity_id": ACT, "female_chats": bad}).status_code == 400, bad


# ---------------------------------------------------------------- 读端点

def _submitted_setup():
    signups = [_signup("ou_m0", "小北"), _signup("ou_m1", "阿北"),
               _signup("ou_f0", "阿南"), _signup("ou_f9", "阿东")]
    selections = [_oto_sel("ou_m0", "男性", ("ou_f0",)), _oto_sel("ou_m1", "男性", ()),
                  _oto_sel("ou_f0", "女性", ("ou_m0",))]
    results = [_oto_res_row("ou_m0", "ou_f0", 1), _oto_res_row("ou_f0", "ou_m0", 1)]
    return signups, selections, results


def test_status_counts_mode_and_fresh(api):
    signups, selections, results = _submitted_setup()
    api.install(activity=_oto_act(oto_status="已完成", oto_flag="是", group_flag="否"),
                signups=signups, selections=selections, results=results)
    api.fresh_calls.clear()
    body = api.get("/api/admin/onetoone/status?activity_id=" + ACT).get_json()
    assert body["onetoone_status"] == "已完成"
    assert body["signup_count"] == 4 and body["submitted_count"] == 3
    assert body["unsubmitted_count"] == 1
    assert body["result_count"] == 2 and body["matched_count"] == 2
    assert body["mode"] == "onetoone" and body["conflict"] is False
    assert set(api.fresh_calls) == {"activities", "onetoone_selections", "signups",
                                    "onetoone_results"}


def test_unsubmitted_endpoint_lists_and_marks(api):
    signups, selections, results = _submitted_setup()
    api.install(signups=signups, selections=selections, results=results)
    body = api.get("/api/admin/onetoone/unsubmitted?activity_id=" + ACT).get_json()
    assert body["total"] == 1
    assert body["unsubmitted"][0]["open_id"] == "ou_f9"
    assert body["unsubmitted"][0]["user_id"] == "U-0009"
    assert [x["open_id"] for x in body["submitted"]] == ["ou_m0", "ou_m1", "ou_f0"]
    assert body["all_submitted"] is False


def test_result_endpoint_groups_by_person(api):
    signups, selections, results = _submitted_setup()
    api.install(signups=signups, selections=selections, results=results)
    body = api.get("/api/admin/onetoone/result?activity_id=" + ACT).get_json()
    assert body["result_count"] == 2 and body["matched_count"] == 2
    people = {p["open_id"]: p for p in body["people"]}
    assert set(people) == {"ou_m0", "ou_f0"}
    assert [t["open_id"] for t in people["ou_m0"]["partners"]] == ["ou_f0"]
    assert people["ou_m0"]["partners"][0]["rank"] == 1
    assert people["ou_m0"]["partners"][0]["nickname"] == "阿南"


def test_activities_endpoint_extended_fields(api):
    signups, selections, results = _submitted_setup()
    api.install(activity=_oto_act(oto_status="已完成", oto_flag="是", group_flag="是"),
                signups=signups, selections=selections, results=results)
    body = api.get("/api/admin/group/activities").get_json()
    item = body["activities"][0]
    assert item["onetoone_flag"] == "是" and item["group_flag"] == "是"
    assert item["onetoone_submitted_count"] == 3
    assert item["mode"] == "conflict" and item["conflict"] is True


# ---------------------------------------------------------------- 注入对照

def test_configure_injection_matches_bot(monkeypatch, tmp_path):
    """H5 与 bot 注入 lib.onetoone 的表 ID/栏位名逐栏相等（对照分组同款防线）。"""
    captured = []

    def capture(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(lib_onetoone, "configure", capture)
    bot_onetoone.configure_onetoone_lib(str(tmp_path))
    app.configure_onetoone_lib(str(tmp_path))

    assert len(captured) == 2
    bot_kwargs, h5_kwargs = captured
    assert h5_kwargs["fields"] == bot_kwargs["fields"]
    for key in ("onetoone_select_table", "onetoone_result_table", "activity_table_id",
                "signup_table_id", "lock_dir"):
        assert h5_kwargs[key] == bot_kwargs[key], key
    assert set(h5_kwargs["fields"]) == set(lib_onetoone._REQUIRED_FIELDS)


def test_single_preference_table_wiring():
    """志愿表统一（2026-10-13）：H5 与 bot 的一对一志愿表都必须 = 分组志愿表。

    用户只维护一份志愿（随时可改），执行分组/一对一时各自实时读取；若哪天有人把
    local_config 的 ONETOONE_SELECT_TABLE 填回旧表，这条会红。
    """
    assert app.ONETOONE_SELECT_TABLE == app.GROUP_SELECT_TABLE
    import constants as bot_constants  # noqa: E402
    assert bot_constants.ONETOONE_SELECT_TABLE == bot_constants.GROUP_SELECT_TABLE


def test_group_configure_injection_matches_bot_for_new_fields(monkeypatch, tmp_path):
    """lib.grouping 新增的一对一栏位两端注入一致（set_mode/guard 的配置面）。"""
    import grouping as bot_grouping

    captured = []

    def capture(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(lib_grouping, "configure", capture)
    bot_grouping.configure_grouping_lib(str(tmp_path))
    app.configure_grouping_lib(str(tmp_path))

    assert len(captured) == 2
    bot_kwargs, h5_kwargs = captured
    assert h5_kwargs["fields"] == bot_kwargs["fields"]
    assert h5_kwargs["fields"]["onetoone_flag"] == "一对一功能开启"
    assert h5_kwargs["fields"]["onetoone_status"] == "一对一状态"
    assert set(h5_kwargs["fields"]) == set(lib_grouping._REQUIRED_FIELDS)


# ---------------------------------------------------------------- 多轮（Phase 2 追加）

def _oto_res_round(oid, target, rnd, rank=1):
    row = _oto_res_row(oid, target, rank)
    row["fields"]["轮次"] = str(rnd)
    return row


def test_status_includes_rounds(api):
    """status 与 group/status 都补轮次信息（空轮次行 = 第 1 轮）。"""
    api.install(activity=_oto_act(oto_status="已完成"),
                results=[_oto_res_round("ou_m0", "ou_f0", 1),
                         _oto_res_round("ou_m0", "ou_f1", 2)])
    body = api.get("/api/admin/onetoone/status?activity_id=" + ACT).get_json()
    assert body["rounds"] == [1, 2] and body["latest_round"] == 2

    g = api.get("/api/admin/group/status?activity_id=" + ACT).get_json()
    assert g["onetoone_rounds"] == [1, 2] and g["onetoone_latest_round"] == 2


def test_result_endpoint_round_param_and_latest(api):
    """result 端点：round 省略取最新、指定轮次取指定轮；round 非法 400。"""
    api.install(activity=_oto_act(oto_status="已完成"),
                results=[_oto_res_round("ou_m0", "ou_f0", 1),
                         _oto_res_round("ou_m0", "ou_f1", 2)])
    latest = api.get("/api/admin/onetoone/result?activity_id=" + ACT).get_json()
    assert latest["round"] == 2 and latest["rounds"] == [1, 2]
    assert [t["open_id"] for t in latest["people"][0]["partners"]] == ["ou_f1"]

    first = api.get("/api/admin/onetoone/result?activity_id=" + ACT + "&round=1").get_json()
    assert first["round"] == 1
    assert [t["open_id"] for t in first["people"][0]["partners"]] == ["ou_f0"]

    assert api.get("/api/admin/onetoone/result?activity_id=" + ACT
                   + "&round=x").status_code == 400


def test_preview_accepts_round_and_reports_round_fields(api):
    api.install(results=[_oto_res_round("ou_m0", "ou_f0", 1)])
    body = api.post("/api/admin/onetoone/preview",
                    {"activity_id": ACT, "round": 1}).get_json()
    assert body["round"] == 1 and body["existing_rounds"] == [1]
    assert body["existing_round_results"] == 1 and body["can_execute"] is False

    body2 = api.post("/api/admin/onetoone/preview",
                     {"activity_id": ACT, "round": 2}).get_json()
    assert body2["existing_round_results"] == 0
    assert body2["results_preserved"] == 1, "开启新一轮保留历史"

    assert api.post("/api/admin/onetoone/preview",
                    {"activity_id": ACT, "round": "x"}).status_code == 400


def test_start_with_clear_results_false_keeps_history(api):
    """start 收 clear_results：False = 开启新一轮（保留历史），默认 True 不变。"""
    signups, selections = _balanced(2, 2)
    fake = api.install(activity=_oto_act(oto_status="已完成"), signups=signups,
                       selections=selections,
                       results=[_oto_res_round("ou_m0", "ou_f0", 1),
                                _oto_res_round("ou_m0", "ou_f0", 2)])
    r = api.post("/api/admin/onetoone/start", {"activity_id": ACT, "clear_results": False})
    assert r.status_code == 200
    assert r.get_json()["cleared_results"] == 0
    assert len(fake.tables[RESULT_TBL]) == 2, "历史轮次结果保留"

    assert api.post("/api/admin/onetoone/start",
                    {"activity_id": ACT, "clear_results": "yes"}).status_code == 400


def test_run_round_param_and_round_rows(api):
    """run 收 round：job/结果都锚定该轮；round 非法 400。"""
    signups, selections = _balanced(2, 2)
    api.install(signups=signups, selections=selections)
    r = api.post("/api/admin/onetoone/run", {"activity_id": ACT, "round": 2, "female_chats": 2})
    assert r.status_code == 202
    body = r.get_json()
    assert body["round"] == 2
    job = _poll_job(api, body["job_id"])
    assert job["state"] == "done" and job["round"] == 2
    assert job["result"]["round"] == 2
    rows = [x for x in api.fake.tables[RESULT_TBL] if x["fields"].get("轮次") == "2"]
    assert rows and len(rows) == job["result"]["total_pairs"]

    assert api.post("/api/admin/onetoone/run",
                    {"activity_id": ACT, "round": 0}).status_code == 400


def test_onetoone_job_conflict_key_is_activity_and_round(api):
    """排队层幂等键 = (活动, 轮次)：同轮冲突、异轮并行入列。"""
    now = int(time.time())
    active = {"activity_id": ACT, "round": 1, "operator": ADMIN, "state": "running",
              "pid": os.getpid(), "progress": "", "created_at": now, "started_at": now,
              "finished_at": None, "result": None, "error": ""}
    storage.update_json(app.ONETOONE_JOBS_FILE, {}, lambda jobs: {**jobs, "activejob001": active})
    assert app._create_onetoone_job("newjob000001", ACT, 1, ADMIN) == "activejob001"
    assert app._create_onetoone_job("newjob000002", ACT, 2, ADMIN) is None
