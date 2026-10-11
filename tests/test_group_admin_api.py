"""管理员分组管理台 API 契约（不联网，桩表驱动）。

覆盖 RFC §7.5 的 A1–A10 与迁移红线：
- 鉴权全部走 _require_admin（未登录 401 / 非管理员 403）；
- preview 零写入（R8：绝不能碰「分组状态」把活动误截止）；
- H5 端 execute_round/start_group 必须传 treat_search_failure_as_empty=False（R7）；
- H5 与 bot 的 configure 注入对照表逐栏相等（静默写错栏位/base 的主要防线）。

桩法比照 tests/test_points_admin_api.py（Flask test client + cookie session +
monkeypatch 快照）；假表复用 tests/test_grouping_parity.py 的 FakeBitable——
桩「存取」，幂等/回滚/算法全跑 lib 真实现。
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

import grouping as bot_grouping  # noqa: E402
from lib import grouping as lib_grouping  # noqa: E402
from lib import storage  # noqa: E402
from tests.test_grouping_parity import (  # noqa: E402
    ACTIVITY_TABLE_ID,
    GROUP_RESULT_TABLE,
    GROUP_SELECT_TABLE,
    SIGNUP_TABLE_ID,
    FakeBitable,
    _act,
    _find_act,
    _res,
    _sel,
    _signup,
)

ADMIN = "ou_test_admin"
ME = "ou_me"
ACT = "A-0002"


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
    return USER_DIR.get(open_id)


def _balanced(n_males, n_females):
    """n 男 n 女、每人交一份志愿的报名与志愿记录。"""
    signups, selections = [], []
    for i in range(n_males):
        oid = f"ou_m{i}"
        signups.append(_signup(oid, USER_DIR[oid]["fields"]["昵称"]))
        selections.append(_sel(oid, "男性", ()))
    for i in range(n_females):
        oid = f"ou_f{i}"
        signups.append(_signup(oid, USER_DIR[oid]["fields"]["昵称"]))
        selections.append(_sel(oid, "女性", ()))
    return signups, selections


def _tables(activity=None, signups=(), selections=(), results=()):
    return {
        ACTIVITY_TABLE_ID: [activity if activity is not None else _act(当前报名人数=4)],
        SIGNUP_TABLE_ID: list(signups),
        GROUP_SELECT_TABLE: list(selections),
        GROUP_RESULT_TABLE: list(results),
    }


class Api:
    """测试脚手架：假表 + fresh 快照桩 + 调用记录；管理员/普通用户会话已就绪。"""

    def __init__(self, monkeypatch, tmp_path):
        self._monkeypatch = monkeypatch
        self.fake = FakeBitable(_tables())
        monkeypatch.setattr(app, "_grouping_client", self.fake)
        self.snapshot = {"activities": [], "group_selections": [], "group_results": [],
                         "signups": [], "users": [], "observers": []}
        self.fresh_calls = []
        self.refresh_calls = []
        monkeypatch.setattr(app, "_snap_fresh", self._fresh)
        monkeypatch.setattr(app, "_snap", lambda key: list(self.snapshot.get(key, [])))
        monkeypatch.setattr(app, "refresh_snapshot_table_async", self.refresh_calls.append)
        self.client = app.app.test_client()
        self.client.set_cookie("yxq_session", app.create_session(ADMIN))
        self.client_me = app.app.test_client()
        self.client_me.set_cookie("yxq_session", app.create_session(ME))

    def _fresh(self, key):
        self.fresh_calls.append(key)
        return list(self.snapshot.get(key, []))

    def install(self, activity=None, signups=(), selections=(), results=(), extra_activities=()):
        """装假表与快照（活动默认：收集中、每组2男2女、当前报名人数=4）。"""
        self.tables = _tables(activity=activity, signups=signups, selections=selections,
                              results=results)
        self.fake = FakeBitable(self.tables)
        self._monkeypatch.setattr(app, "_grouping_client", self.fake)
        self.snapshot["activities"] = list(extra_activities) + list(self.tables[ACTIVITY_TABLE_ID])
        self.snapshot["signups"] = list(signups)
        self.snapshot["group_selections"] = list(selections)
        self.snapshot["group_results"] = list(results)
        return self.fake

    def get(self, path, client=None):
        return (client or self.client).get(path)

    def post(self, path, payload=None, client=None):
        return (client or self.client).post(
            path, json=payload or {}, headers={"X-Requested-With": "XMLHttpRequest"})


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(lib_grouping, "log", lambda *a, **k: None)
    monkeypatch.setattr(app, "snap_find_user_by_openid", _find_user)
    monkeypatch.setattr(app.bitable, "search_records", lambda *a, **k: [])
    monkeypatch.setattr(app, "GROUPING_JOBS_FILE", str(tmp_path / "grouping_jobs.json"))
    lib_grouping.configure(lock_dir=str(tmp_path))
    app._rate_store.clear()
    return Api(monkeypatch, tmp_path)


def _poll_job(api, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = api.get("/api/admin/group/job?job_id=" + job_id)
        assert r.status_code == 200
        job = r.get_json()
        if job["state"] in ("done", "failed", "unknown"):
            return job
        time.sleep(0.05)
    raise AssertionError("job 未在期限内结束")


ENDPOINTS = [
    ("GET", "/api/admin/group/activities", None),
    ("GET", "/api/admin/group/status?activity_id=" + ACT, None),
    ("GET", "/api/admin/group/unsubmitted?activity_id=" + ACT, None),
    ("POST", "/api/admin/group/preview", {"activity_id": ACT, "round": 1}),
    ("POST", "/api/admin/group/start",
     {"activity_id": ACT, "males_per_group": 2, "females_per_group": 2}),
    ("POST", "/api/admin/group/run", {"activity_id": ACT, "round": 1}),
    ("GET", "/api/admin/group/job?job_id=x", None),
    ("POST", "/api/admin/group/flag", {"activity_id": ACT, "enabled": True}),
]
ENDPOINT_IDS = [p.split("?")[0].rsplit("/", 1)[-1] for m, p, _ in ENDPOINTS]


def _call(c, method, path, payload):
    if method == "POST":
        return c.post(path, json=payload or {},
                      headers={"X-Requested-With": "XMLHttpRequest"})
    return c.get(path)


# ---------------------------------------------------------------- A1 / A2 鉴权

@pytest.mark.parametrize("method,path,payload", ENDPOINTS, ids=ENDPOINT_IDS)
def test_a1_unauthenticated_is_401(method, path, payload):
    c = app.app.test_client()
    assert _call(c, method, path, payload).status_code == 401


@pytest.mark.parametrize("method,path,payload", ENDPOINTS, ids=ENDPOINT_IDS)
def test_a2_non_admin_is_403(api, method, path, payload):
    assert _call(api.client_me, method, path, payload).status_code == 403


# ---------------------------------------------------------------- A3 preview 零写入

def test_a3_preview_writes_nothing(api):
    """RFC R8 红线：预览绝不能碰「分组状态」——看一眼就把活动截止是最重的事故。"""
    signups, selections = _balanced(2, 2)
    fake = api.install(signups=signups, selections=selections,
                       results=[_res("ou_m0", 1, "1")])

    r = api.post("/api/admin/group/preview", {"activity_id": ACT, "round": 3})
    assert r.status_code == 200
    body = r.get_json()
    assert body["can_execute"] is True
    assert body["historical_results_to_clear"] == 1
    assert body["historical_rounds_to_clear"] == [1]

    assert fake.writes() == [], "preview 一次写都不能有"
    assert all("分组状态" not in str(call) for call in fake.calls), "preview 不得触碰分组状态"
    assert _find_act(fake, ACT)["fields"]["分组状态"] == "收集中"


# ---------------------------------------------------------------- A4 start 快照刷新

def test_a4_start_refreshes_exactly_activities_and_group_results(api):
    signups, selections = _balanced(2, 2)
    fake = api.install(signups=signups, selections=selections,
                       results=[_res("ou_m0", 1, "1"), _res("ou_f0", 1, "1")])

    r = api.post("/api/admin/group/start",
                 {"activity_id": ACT, "males_per_group": 2, "females_per_group": 2})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["cleared_results"] == 2
    assert body["group_status"] == "收集中" and body["name"] == "关系自洽"
    # 恰为两张表：多刷一张是浪费，少刷一张用户端读到旧状态
    assert sorted(api.refresh_calls) == ["activities", "group_results"]
    assert fake.tables[GROUP_RESULT_TABLE] == []
    assert _find_act(fake, ACT)["fields"]["分组状态"] == "收集中"
    assert _find_act(fake, ACT)["fields"]["每组男生数"] == 2


# ---------------------------------------------------------------- A5 run → job

def test_a5_run_returns_202_and_job_reaches_done(api):
    signups, selections = _balanced(2, 2)
    api.install(signups=signups, selections=selections)

    r = api.post("/api/admin/group/run", {"activity_id": ACT, "round": 1})
    assert r.status_code == 202
    body = r.get_json()
    assert body["ok"] is True and body["state"] == "pending" and body["job_id"]

    job = _poll_job(api, body["job_id"])
    assert job["state"] == "done"
    assert job["result"]["code"] == "OK"
    assert job["result"]["group_count"] == 1
    assert job["result"]["participant_count"] == 4
    assert api.fake.tables[GROUP_RESULT_TABLE], "结果必须真的写进表"
    assert set(api.refresh_calls) >= {"activities", "group_results"}


# ---------------------------------------------------------------- A6 排队层幂等

def test_a6_run_duplicate_submission_returns_existing_job(api, monkeypatch):
    signups, selections = _balanced(2, 2)
    api.install(signups=signups, selections=selections)

    entered = threading.Event()
    release = threading.Event()

    def slow_execute(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return {"code": "ALREADY_DONE", "activity_id": ACT, "activity_name": "关系自洽",
                "round": 1, "males_per_group": 2, "females_per_group": 2}

    monkeypatch.setattr(lib_grouping, "execute_round", slow_execute)

    r1 = api.post("/api/admin/group/run", {"activity_id": ACT, "round": 1})
    assert r1.status_code == 202
    job_id = r1.get_json()["job_id"]
    assert entered.wait(timeout=5), "job 线程应已开始执行"

    r2 = api.post("/api/admin/group/run", {"activity_id": ACT, "round": 1})
    assert r2.status_code == 409
    assert r2.get_json()["job_id"] == job_id

    release.set()
    assert _poll_job(api, job_id)["state"] == "done"


# ---------------------------------------------------------------- A7 活动锁

@pytest.mark.parametrize("path,payload", [
    ("/api/admin/group/run", {"activity_id": ACT, "round": 1}),
    ("/api/admin/group/start", {"activity_id": ACT, "males_per_group": 2,
                                "females_per_group": 2}),
], ids=["run", "start"])
def test_a7_activity_lock_busy_returns_409_with_busy_activity(api, monkeypatch, path, payload):
    signups, selections = _balanced(2, 2)
    api.install(signups=signups, selections=selections)

    @contextlib.contextmanager
    def busy_lock(activity_id, timeout):
        raise lib_grouping.GroupingBusy(activity_id)
        yield  # pragma: no cover

    monkeypatch.setattr(lib_grouping, "grouping_lock", busy_lock)
    r = api.post(path, payload)
    assert r.status_code == 409
    assert r.get_json()["busy_activity_id"] == ACT


# ---------------------------------------------------------------- A8 preview blockers

def test_a8_preview_blockers_cover_all_causes(api):
    # 1) 人数不足（1男1女 < 每组2男2女）
    signups, selections = _balanced(1, 1)
    api.install(signups=signups, selections=selections)
    body = api.post("/api/admin/group/preview", {"activity_id": ACT, "round": 1}).get_json()
    assert body["can_execute"] is False
    assert any("人数不足" in b for b in body["blockers"])
    assert api.fake.writes() == []

    # 2) 每组人数为 0
    signups, selections = _balanced(2, 2)
    api.install(activity=_act(每组男生数=0, 每组女生数=0),
                signups=signups, selections=selections)
    body = api.post("/api/admin/group/preview", {"activity_id": ACT, "round": 1}).get_json()
    assert body["can_execute"] is False
    assert any("必须大于0" in b for b in body["blockers"])

    # 3) 本轮已生成结果
    api.install(signups=signups, selections=selections, results=[_res("ou_m0", 1, "2")])
    body = api.post("/api/admin/group/preview", {"activity_id": ACT, "round": 2}).get_json()
    assert body["can_execute"] is False
    assert any("第2轮已生成分组结果" in b for b in body["blockers"])

    # 4) 轮次超单选字段上限（轮次字段只支持 1-10）
    api.install(signups=signups, selections=selections)
    body = api.post("/api/admin/group/preview", {"activity_id": ACT, "round": 11}).get_json()
    assert body["can_execute"] is False
    assert any("超出单选字段上限" in b for b in body["blockers"])
    assert api.fake.writes() == [], "四种拦截路径都必须零写入"


# ---------------------------------------------------------------- A9 fresh 读取

def test_a9_read_endpoints_use_fresh_snapshots(api):
    """判定依赖 bot 写入的分组状态/结果 → 必须 fresh=True（docs/读取一致性.md 规则一）。"""
    signups, selections = _balanced(2, 2)
    signups.append(_signup("ou_f9", "阿东"))          # ou_f9 报名未提交
    api.install(signups=signups, selections=selections,
                results=[_res("ou_m0", 1, "1"), _res("ou_f0", 1, "2")])

    api.fresh_calls.clear()
    r = api.get("/api/admin/group/activities")
    assert r.status_code == 200
    assert set(api.fresh_calls) == {"activities", "group_selections", "group_results"}
    act = r.get_json()["activities"][0]
    assert act["activity_id"] == ACT and act["group_status"] == "收集中"
    assert act["available_rounds"] == [1, 2] and act["latest_round"] == 2
    assert act["submitted_count"] == 4 and act["has_results"] is True

    api.fresh_calls.clear()
    r = api.get("/api/admin/group/status?activity_id=" + ACT)
    assert r.status_code == 200
    assert set(api.fresh_calls) == {"activities", "group_selections", "signups",
                                    "group_results"}
    st = r.get_json()
    assert st["signup_count"] == 5 and st["submitted_count"] == 4
    assert st["unsubmitted_count"] == 1 and st["male_count"] == 2 and st["female_count"] == 2
    assert st["rounds"] == [1, 2] and st["latest_round"] == 2

    api.fresh_calls.clear()
    r = api.get("/api/admin/group/unsubmitted?activity_id=" + ACT)
    assert r.status_code == 200
    assert set(api.fresh_calls) == {"activities", "group_selections", "signups"}
    un = r.get_json()
    assert un["total"] == 1 and un["all_submitted"] is False
    assert un["unsubmitted"][0]["open_id"] == "ou_f9"
    assert un["unsubmitted"][0]["user_id"] == "U-0009"


# ---------------------------------------------------------------- A10 flag 幂等

def test_a10_flag_is_idempotent(api):
    fake = api.install()

    r = api.post("/api/admin/group/flag", {"activity_id": ACT, "enabled": False})
    assert r.status_code == 200 and r.get_json()["group_flag"] == "否"
    assert fake.writes() == [], "已是目标值（否）不该再写"
    assert api.refresh_calls == []

    r = api.post("/api/admin/group/flag", {"activity_id": ACT, "enabled": True})
    assert r.status_code == 200 and r.get_json()["group_flag"] == "是"
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert len(updates) == 1 and updates[0][3] == {"分组功能开启": "是"}
    assert api.refresh_calls == ["activities"]

    # 真实环境 fresh=True 会从飞书读到刚写入的「是」；同步桩快照模拟这次实时读
    api.snapshot["activities"][0]["fields"]["分组功能开启"] = "是"
    r = api.post("/api/admin/group/flag", {"activity_id": ACT, "enabled": True})
    assert r.status_code == 200 and r.get_json()["group_flag"] == "是"
    assert len([c for c in fake.writes() if c[0] == "update"]) == 1, "幂等：不重复写"


# ---------------------------------------------------------------- 迁移红线 / 附加

def test_configure_injection_matches_bot(monkeypatch, tmp_path):
    """H5 与 bot 注入 lib 的表 ID/栏位名逐栏相等——静默写错 base 的主要防线。"""
    captured = []

    def capture(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(lib_grouping, "configure", capture)
    bot_grouping.configure_grouping_lib(str(tmp_path))
    app.configure_grouping_lib(str(tmp_path))

    assert len(captured) == 2
    bot_kwargs, h5_kwargs = captured
    assert h5_kwargs["fields"] == bot_kwargs["fields"]
    for key in ("activity_table_id", "group_select_table", "group_result_table",
                "signup_table_id", "lock_dir"):
        assert h5_kwargs[key] == bot_kwargs[key], key
    assert set(h5_kwargs["fields"]) == set(lib_grouping._REQUIRED_FIELDS)


def test_run_search_failure_aborts_without_writes(api):
    """R7：H5 必须传 treat_search_failure_as_empty=False——读不到飞书时中止，零写入。"""
    signups, selections = _balanced(2, 2)
    fake = api.install(signups=signups, selections=selections)
    fake.search_fails = {GROUP_RESULT_TABLE}

    r = api.post("/api/admin/group/run", {"activity_id": ACT, "round": 1})
    assert r.status_code == 202
    job = _poll_job(api, r.get_json()["job_id"])
    assert job["state"] == "done"
    assert job["result"]["code"] == "SEARCH_FAILED"
    assert fake.writes() == [], "读不到既有结果就不能动任何字段"
    assert _find_act(fake, ACT)["fields"]["分组状态"] == "收集中"


def test_start_search_failure_returns_500_without_deleting(api):
    """start 严格模式：清历史结果读失败时如实报错。

    lib 先写「收集中」再查历史结果，所以此路径状态已开、结果保留；接口刷新
    activities 让管理台看到真实状态后重试，不吞成成功。
    """
    signups, selections = _balanced(2, 2)
    fake = api.install(signups=signups, selections=selections,
                       results=[_res("ou_m0", 1, "1")])
    fake.search_fails = {GROUP_RESULT_TABLE}

    r = api.post("/api/admin/group/start",
                 {"activity_id": ACT, "males_per_group": 2, "females_per_group": 2})
    assert r.status_code == 500
    assert r.get_json()["code"] == "SEARCH_FAILED"
    assert fake.tables[GROUP_RESULT_TABLE], "历史结果不能被误删"
    assert _find_act(fake, ACT)["fields"]["分组状态"] == "收集中"
    assert api.refresh_calls == ["activities"], "状态已变，必须刷新一次 activities"


def test_dead_process_job_reports_unknown_and_does_not_block(api):
    """H5 重启后 job 记录还在但线程没了 → unknown；旧 job 不阻塞重跑（RFC 4.6 / R10）。"""
    dead = {"activity_id": ACT, "round": 1, "operator": "ou_old", "state": "running",
            "pid": os.getpid() + 1000, "progress": "",
            "created_at": int(time.time()), "started_at": int(time.time()),
            "finished_at": None, "result": None, "error": ""}
    storage.update_json(app.GROUPING_JOBS_FILE, {}, lambda jobs: {**jobs, "deadbeef0000": dead})

    r = api.get("/api/admin/group/job?job_id=deadbeef0000")
    assert r.status_code == 200 and r.get_json()["state"] == "unknown"

    signups, selections = _balanced(2, 2)
    api.install(signups=signups, selections=selections)
    r = api.post("/api/admin/group/run", {"activity_id": ACT, "round": 1})
    assert r.status_code == 202, "上个进程的残留 job 不能永远挡住重跑"
    assert _poll_job(api, r.get_json()["job_id"])["state"] == "done"


def test_job_exception_is_recorded_as_failed_and_releases_lock(api, monkeypatch):
    """job 内意外异常必须落盘为 failed（不能无声消失）、刷新快照，并释放活动锁。"""
    signups, selections = _balanced(2, 2)
    api.install(signups=signups, selections=selections)

    real_execute = lib_grouping.execute_round

    def boom(*args, **kwargs):
        raise RuntimeError("写表炸了")

    monkeypatch.setattr(lib_grouping, "execute_round", boom)
    r = api.post("/api/admin/group/run", {"activity_id": ACT, "round": 1})
    assert r.status_code == 202
    job = _poll_job(api, r.get_json()["job_id"])
    assert job["state"] == "failed"
    assert "写表炸了" in job["error"] and job["result"] is None
    assert set(api.refresh_calls) == {"activities", "group_results"}

    # 异常路径也必须把锁放掉，否则整场活动再也跑不了分组
    monkeypatch.setattr(lib_grouping, "execute_round", real_execute)
    r2 = api.post("/api/admin/group/run", {"activity_id": ACT, "round": 2})
    assert r2.status_code == 202
    assert _poll_job(api, r2.get_json()["job_id"])["state"] == "done"


def test_payload_validation_and_missing_resources(api):
    api.install()
    assert api.post("/api/admin/group/preview",
                    {"activity_id": ACT, "round": 0}).status_code == 400
    assert api.post("/api/admin/group/preview",
                    {"activity_id": "", "round": 1}).status_code == 400
    assert api.post("/api/admin/group/start",
                    {"activity_id": ACT, "males_per_group": 0,
                     "females_per_group": 2}).status_code == 400
    assert api.post("/api/admin/group/run",
                    {"activity_id": ACT, "round": "1"}).status_code == 400
    assert api.post("/api/admin/group/flag",
                    {"activity_id": ACT, "enabled": "yes"}).status_code == 400
    assert api.get("/api/admin/group/job?job_id=nope").status_code == 404
    assert api.get("/api/admin/group/status?activity_id=A-9999").status_code == 404
    assert api.get("/api/admin/group/unsubmitted?activity_id=A-9999").status_code == 404
    assert api.post("/api/admin/group/preview",
                    {"activity_id": "A-9999", "round": 1}).status_code == 404
    assert api.post("/api/admin/group/run",
                    {"activity_id": "A-9999", "round": 1}).status_code == 404
