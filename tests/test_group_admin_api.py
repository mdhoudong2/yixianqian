"""管理员分组管理台 API 契约（不联网，桩表驱动）。

覆盖 RFC §7.5 的 A1–A10 与迁移红线：
- 鉴权全部走 _require_admin（未登录 401 / 非管理员 403）；
- preview 零写入（R8：绝不能碰「分组状态」把活动误截止）；
- H5 端 execute_round（分组）与 lib 的 fail-closed reset 路径的契约；
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

# 未迁移表的形态：基类 FakeBitable（无 field_exists）即精确模拟。fixture 会把模块
# 全局的 FakeBitable 换成 _BatchFake，这里先留好原类的别名供「无栏位」用例使用。
_UnmigratedFake = FakeBitable


# ---------------------------------------------------------------- 桩数据

class _BatchFake(FakeBitable):
    """FakeBitable + batch_update_records + schema 级 field_exists（模拟已迁移表）。"""

    def batch_update_records(self, table_id, records, batch_size=500):
        updated = 0
        for r in records or []:
            if self.update_record(table_id, r["record_id"], r["fields"]):
                updated += 1
        return updated

    def field_exists(self, table_id, field_name):
        # 迁移后的活动表含一对一栏位（schema 级，与记录里有没有值无关）
        return table_id == ACTIVITY_TABLE_ID and field_name == "一对一功能开启"


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
    monkeypatch.setattr(sys.modules[__name__], "FakeBitable", _BatchFake)
    monkeypatch.setattr(lib_grouping, "log", lambda *a, **k: None)
    monkeypatch.setattr(app, "snap_find_user_by_openid", _find_user)
    monkeypatch.setattr(app.bitable, "search_records", lambda *a, **k: [])
    monkeypatch.setattr(app, "GROUPING_JOBS_FILE", str(tmp_path / "grouping_jobs.json"))
    # 状态端点的活跃 job 探测会读两个 job 文件：一对一那个也要重定向到 tmp，
    # 否则单测会去读真实数据目录（污染 + 非确定性）。
    monkeypatch.setattr(app, "ONETOONE_JOBS_FILE", str(tmp_path / "onetoone_jobs.json"))
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


def _wait_refresh_calls(api, expected, timeout=5.0):
    """等 job 线程补齐刷新调用后再断言（有上限，不是睡死等）。

    产品路径是「终态先落盘、再调 refresh_snapshot_table_async」（见 _run_grouping_job），
    轮询线程可能在两次调用之间就观察到终态；审计 400 次复现约 5% 首察未记录、
    50ms 内 100% 补齐。这里等调用集合收敛，避免把窗口竞态误报成产品漏刷。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if set(api.refresh_calls) >= set(expected):
            return
        time.sleep(0.01)
    raise AssertionError(
        f"刷新调用未在 {timeout}s 内收敛：expected {sorted(expected)}, "
        f"actual {sorted(api.refresh_calls)}")


ENDPOINTS = [
    ("GET", "/api/admin/group/activities", None),
    ("GET", "/api/admin/group/status?activity_id=" + ACT, None),
    ("GET", "/api/admin/group/unsubmitted?activity_id=" + ACT, None),
    ("POST", "/api/admin/group/preview", {"activity_id": ACT, "round": 1}),
    ("POST", "/api/admin/group/reset", {"activity_id": ACT}),
    ("POST", "/api/admin/group/run",
     {"activity_id": ACT, "males_per_group": 2, "females_per_group": 2}),
    ("GET", "/api/admin/group/job?job_id=x", None),
    ("GET", "/api/admin/group/result?activity_id=" + ACT, None),
    ("POST", "/api/admin/group/flag", {"activity_id": ACT, "enabled": True}),
    # C4 现场签到三端点（同走 _require_admin + rate limit）
    ("GET", "/api/admin/activity/signin?activity_id=" + ACT, None),
    ("POST", "/api/admin/activity/signin", {"activity_id": ACT}),
    ("POST", "/api/admin/activity/signin/flag", {"activity_id": ACT, "enabled": True}),
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


# ---------------------------------------------------------------- A4 reset 快照刷新

def test_a4_reset_refreshes_exactly_activities_and_group_results(api):
    signups, selections = _balanced(2, 2)
    fake = api.install(signups=signups, selections=selections,
                       results=[_res("ou_m0", 1, "1"), _res("ou_f0", 1, "1")])

    r = api.post("/api/admin/group/reset", {"activity_id": ACT})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["cleared_results"] == 2
    # 恰为两张表：多刷一张是浪费，少刷一张用户端读到旧结果
    assert sorted(api.refresh_calls) == ["activities", "group_results"]
    assert fake.tables[GROUP_RESULT_TABLE] == []
    assert _find_act(fake, ACT)["fields"]["分组状态"] == "", "状态清空=空串（未开始）"


def test_reset_without_results_still_clears_status(api):
    """空结果也照常清状态（幂等）；不产生 batch_delete。"""
    fake = api.install(activity=_act(分组状态="已完成"))
    r = api.post("/api/admin/group/reset", {"activity_id": ACT})
    assert r.status_code == 200 and r.get_json()["cleared_results"] == 0
    assert _find_act(fake, ACT)["fields"]["分组状态"] == ""
    assert sorted(api.refresh_calls) == ["activities", "group_results"]


def test_reset_search_failure_returns_500_without_deleting(api):
    """fail-closed：清空结果读失败 → 500 如实报错，零删除、状态原样、不刷快照。"""
    signups, selections = _balanced(2, 2)
    fake = api.install(signups=signups, selections=selections,
                       results=[_res("ou_m0", 1, "1")])
    fake.search_fails = {GROUP_RESULT_TABLE}

    r = api.post("/api/admin/group/reset", {"activity_id": ACT})
    assert r.status_code == 500
    assert r.get_json()["code"] == "RESULTS_SEARCH_FAILED"
    assert fake.tables[GROUP_RESULT_TABLE], "历史结果不能被误删"
    assert _find_act(fake, ACT)["fields"]["分组状态"] == "收集中"
    assert api.refresh_calls == [], "零变化不刷快照"


def test_reset_busy_lock_returns_409(api, monkeypatch):
    fake = api.install(results=[_res("ou_m0", 1, "1")])
    monkeypatch.setattr(app, "GROUPING_API_LOCK_TIMEOUT", 0.1)
    with lib_grouping.grouping_lock(ACT, timeout=1):
        r = api.post("/api/admin/group/reset", {"activity_id": ACT})
    assert r.status_code == 409
    assert fake.writes() == [], "抢锁失败不得写任何东西"


def test_preview_round_new_fields_for_new_round(api):
    """preview 加法字段：开启新一轮要能显示「保留 N 轮 / 共 X 条」。"""
    signups, selections = _balanced(2, 2)
    api.install(signups=signups, selections=selections,
                results=[_res("ou_m0", 1, "1"), _res("ou_f0", 1, "1"),
                         _res("ou_m0", 2, "2")])

    body = api.post("/api/admin/group/preview",
                    {"activity_id": ACT, "round": 3}).get_json()
    assert body["rounds_preserved"] == [1, 2]
    assert body["results_preserved"] == 3
    assert body["historical_results_to_clear"] == 3, "旧字段语义不变"


def test_group_preview_default_round_is_latest_plus_one(api):
    """preview 的 round 缺省=最新已有轮次+1（与 run 端点同口径）。"""
    # 已有第 1、2 轮结果 → 推第 3 轮，且第 3 轮尚无结果（写死 1 的突变在这里必红）
    api.install(activity=_act(分组状态="已完成"),
                results=[_res("ou_m0", 1, "1"), _res("ou_f0", 1, "2")])
    body = api.post("/api/admin/group/preview", {"activity_id": ACT}).get_json()
    assert body["round"] == 3
    assert body["existing_round_results"] == 0
    assert body["existing_rounds"] == [1, 2]

    # 无任何结果 → 第 1 轮（空表是合法状态，不等于读失败）
    api.install()
    body = api.post("/api/admin/group/preview", {"activity_id": ACT}).get_json()
    assert body["round"] == 1
    assert body["existing_round_results"] == 0
    assert body["existing_rounds"] == []

    # 结果表读失败 → 500 明报（fail-closed，不得拿 0 当「没跑过」）
    fake = api.install(results=[_res("ou_m0", 1, "1")])
    fake.search_fails = {GROUP_RESULT_TABLE}
    r = api.post("/api/admin/group/preview", {"activity_id": ACT})
    assert r.status_code == 500
    assert r.get_json()["code"] == "SEARCH_FAILED"
    assert fake.writes() == [], "preview 读失败也必须零写入"


# ---------------------------------------------------------------- A5 run → job

def test_a5_run_returns_202_and_job_reaches_done(api):
    signups, selections = _balanced(2, 2)
    api.install(signups=signups, selections=selections)

    r = api.post("/api/admin/group/run",
                 {"activity_id": ACT, "males_per_group": 2, "females_per_group": 2,
                  "round": 1})
    assert r.status_code == 202
    body = r.get_json()
    assert body["ok"] is True and body["state"] == "pending" and body["job_id"]

    job = _poll_job(api, body["job_id"])
    assert job["state"] == "done"
    assert job["result"]["code"] == "OK"
    assert job["result"]["group_count"] == 1
    assert job["result"]["participant_count"] == 4
    assert api.fake.tables[GROUP_RESULT_TABLE], "结果必须真的写进表"
    _wait_refresh_calls(api, {"activities", "group_results"})


def test_a5b_run_default_round_is_latest_plus_one_and_keeps_history(api):
    """run 的 round 缺省=最新已有轮次+1：已有两轮 → 打第3轮，历史原样保留。"""
    signups, selections = _balanced(2, 2)
    api.install(signups=signups, selections=selections,
                results=[_res("ou_m0", 1, "1"), _res("ou_m0", 1, "2")])

    r = api.post("/api/admin/group/run",
                 {"activity_id": ACT, "males_per_group": 2, "females_per_group": 2})
    assert r.status_code == 202
    assert r.get_json()["round"] == 3, "缺省轮次=最新+1"
    job = _poll_job(api, r.get_json()["job_id"])
    assert job["state"] == "done" and job["result"]["code"] == "OK"
    rounds = [row["fields"].get("轮次") for row in api.fake.tables[GROUP_RESULT_TABLE]]
    assert rounds.count("1") == 1 and rounds.count("2") == 1
    assert rounds.count("3") == 4


def test_run_passes_group_sizes_to_lib(api, monkeypatch):
    """每组人数是执行入参：run 把它随 job 传给 lib.execute_round（不再读活动栏位）。"""
    signups, selections = _balanced(2, 2)
    api.install(signups=signups, selections=selections)
    captured = {}

    def fake_execute(activity_id, round_no, *, client, activity, find_user_by_openid,
                     males_per_group, females_per_group, treat_search_failure_as_empty):
        captured.update(round=round_no, m=males_per_group, f=females_per_group,
                        treat=treat_search_failure_as_empty)
        return {"code": "OK", "activity_id": activity_id, "round": round_no,
                "activity_name": "关系自洽", "males_per_group": males_per_group,
                "females_per_group": females_per_group, "group_count": 1,
                "participant_count": 6, "groups": []}

    monkeypatch.setattr(lib_grouping, "execute_round", fake_execute)
    r = api.post("/api/admin/group/run",
                 {"activity_id": ACT, "males_per_group": 3, "females_per_group": 2,
                  "round": 5})
    assert r.status_code == 202
    job = _poll_job(api, r.get_json()["job_id"])
    assert job["state"] == "done" and job["result"]["code"] == "OK"
    assert captured == {"round": 5, "m": 3, "f": 2, "treat": False}, \
        "H5 必须传 treat_search_failure_as_empty=False（R7）"


def test_a5c_run_proceeds_with_unsubmitted_and_missing_selections(api):
    """执行路径零改动的回归（2026-10-16 志愿新规则只管「提交」）：

    报名者里有人从未填志愿（无志愿行）、有人志愿为空 → 分组照常执行到 done。
    算法参与者取自志愿行（现行为）——没填的人不进算法、但执行绝不因此被拦。
    用户需求原文：不管有人没按要求填、甚至有人没填，仍像现在一样可以执行。
    """
    signups, selections = _balanced(2, 2)          # selections 的志愿全为空
    signups.append(_signup("ou_f9", "阿东"))       # ou_f9 报名但从未填志愿
    api.install(signups=signups, selections=selections)

    r = api.post("/api/admin/group/run",
                 {"activity_id": ACT, "round": 1, "males_per_group": 2,
                  "females_per_group": 2})
    assert r.status_code == 202
    job = _poll_job(api, r.get_json()["job_id"])
    assert job["state"] == "done", job
    assert job["result"]["code"] == "OK", "有人未填志愿不得拦住执行"
    assert job["result"]["participant_count"] == 4, "参与者=有志愿行的 4 人（现口径）"
    assert api.fake.tables[GROUP_RESULT_TABLE], "结果必须真的写进表"


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

    payload = {"activity_id": ACT, "round": 1, "males_per_group": 2, "females_per_group": 2}
    r1 = api.post("/api/admin/group/run", payload)
    assert r1.status_code == 202
    job_id = r1.get_json()["job_id"]
    assert entered.wait(timeout=5), "job 线程应已开始执行"

    r2 = api.post("/api/admin/group/run", payload)
    assert r2.status_code == 409
    assert r2.get_json()["job_id"] == job_id

    release.set()
    assert _poll_job(api, job_id)["state"] == "done"


# ---------------------------------------------------------------- A7 活动锁

@pytest.mark.parametrize("path,payload", [
    ("/api/admin/group/run", {"activity_id": ACT, "round": 1,
                              "males_per_group": 2, "females_per_group": 2}),
    ("/api/admin/group/reset", {"activity_id": ACT}),
], ids=["run", "reset"])
def test_a7_activity_lock_busy_returns_409_with_busy_activity(api, monkeypatch, path, payload):
    signups, selections = _balanced(2, 2)
    api.install(signups=signups, selections=selections)
    monkeypatch.setattr(app, "GROUPING_API_LOCK_TIMEOUT", 0.1)

    with lib_grouping.grouping_lock(ACT, timeout=1):
        r = api.post(path, payload)
    assert r.status_code == 409
    assert r.get_json()["busy_activity_id"] == ACT


# ---------------------------------------------------------------- A8 preview blockers

def test_a8_preview_blockers_and_unbounded_round(api):
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

    # 4) 轮次不限（字段已转文本，旧上限 1-50 已废）：51 不再是拦截项，必须可执行
    api.install(signups=signups, selections=selections)
    body = api.post("/api/admin/group/preview", {"activity_id": ACT, "round": 51}).get_json()
    assert body["can_execute"] is True
    assert body["blockers"] == []
    assert api.fake.writes() == [], "每条路径都必须零写入（preview 是纯读红线）"


# ---------------------------------------------------------------- A9 fresh 读取

def test_a9_read_endpoints_use_fresh_snapshots(api):
    """判定依赖 bot 写入的分组状态/结果 → 必须 fresh=True（docs/读取一致性.md 规则一）；
    并按活动实际模式/状态只读该模式 UI 用得到的表（mode=none 不读任何子表）。

    省读的表格判定依据：index.html 详情页模板按 gaDetailMode 分支渲染（none 进度
    tab 只有引导卡；group 用报名/志愿/轮次；onetoone 用报名/一对一志愿/结果），
    列表行 gaRowSub 按 mode 与 group_status 分支。未读字段回应仍在（schema 不变）。
    """
    signups, selections = _balanced(2, 2)
    signups.append(_signup("ou_f9", "阿东"))          # ou_f9 报名未提交
    results = [_res("ou_m0", 1, "1"), _res("ou_f0", 1, "2")]

    # ---- status：group 模式 → activities / group_selections / signups / group_results ----
    api.install(activity=_act(分组功能开启="是"), signups=signups,
                selections=selections, results=results)
    api.fresh_calls.clear()
    r = api.get("/api/admin/group/status?activity_id=" + ACT)
    assert r.status_code == 200
    assert set(api.fresh_calls) == {"activities", "group_selections", "signups",
                                    "group_results"}
    st = r.get_json()
    assert st["signup_count"] == 5 and st["submitted_count"] == 4
    assert st["unsubmitted_count"] == 1 and st["male_count"] == 2 and st["female_count"] == 2
    assert st["rounds"] == [1, 2] and st["latest_round"] == 2
    assert st["mode"] == "group" and st["active_job"] is None
    # 未读的一对一栏位仍在（schema 不变，值为空）
    assert st["onetoone_submitted_count"] == 0 and st["onetoone_rounds"] == []

    # ---- status：mode=none（进度 tab 只有引导卡）→ 只读 activities ----
    api.install(signups=signups, selections=selections, results=results)
    api.fresh_calls.clear()
    st = api.get("/api/admin/group/status?activity_id=" + ACT).get_json()
    assert set(api.fresh_calls) == {"activities"}
    assert st["mode"] == "none"
    assert st["signup_count"] == 0 and st["submitted_count"] == 0 and st["rounds"] == []
    assert st["onetoone_submitted_count"] == 0 and st["onetoone_result_count"] == 0

    # ---- status：onetoone 模式 → activities / signups / 一对一两表 ----
    api.install(activity=_act(一对一功能开启="是", 一对一状态="已完成"))
    api.fresh_calls.clear()
    st = api.get("/api/admin/group/status?activity_id=" + ACT).get_json()
    assert set(api.fresh_calls) == {"activities", "signups",
                                    "onetoone_selections", "onetoone_results"}
    assert st["mode"] == "onetoone" and st["submitted_count"] == 0 and st["rounds"] == []

    # ---- activities 列表：「活动集合联集」——分组行要计数与轮次、一对一行要计数 ----
    act_done = _act(活动ID="A-0003", 分组功能开启="是", 分组状态="已完成")
    act_oto = _act(活动ID="A-0004", 一对一功能开启="是", 一对一状态="已完成")
    res_a3 = _res("ou_m0", 1, "1")
    res_a3["fields"]["活动ID"] = "A-0003"
    api.install(activity=_act(分组功能开启="是"), signups=signups, selections=selections,
                results=results + [res_a3], extra_activities=[act_done, act_oto])
    api.fresh_calls.clear()
    r = api.get("/api/admin/group/activities")
    assert r.status_code == 200
    assert set(api.fresh_calls) == {"activities", "group_selections", "group_results",
                                    "onetoone_selections"}
    by_id = {x["activity_id"]: x for x in r.get_json()["activities"]}
    assert by_id[ACT]["group_status"] == "收集中"
    assert by_id[ACT]["available_rounds"] == [1, 2] and by_id[ACT]["latest_round"] == 2
    assert by_id[ACT]["submitted_count"] == 4 and by_id[ACT]["has_results"] is True
    assert by_id["A-0003"]["submitted_count"] == 0
    assert by_id["A-0003"]["available_rounds"] == [1] and by_id["A-0003"]["latest_round"] == 1
    assert by_id["A-0004"]["mode"] == "onetoone" and by_id["A-0004"]["onetoone_submitted_count"] == 0

    # ---- activities 列表：只有「未开始」的活动 → 一张子表都不读 ----
    api.install(activity=_act(分组功能开启="是", 分组状态="未开始"),
                extra_activities=[_act(活动ID="A-0005", 一对一功能开启="是",
                                       一对一状态="未开始")])
    api.fresh_calls.clear()
    r = api.get("/api/admin/group/activities")
    assert set(api.fresh_calls) == {"activities"}
    by_id = {x["activity_id"]: x for x in r.get_json()["activities"]}
    assert by_id[ACT]["submitted_count"] == 0 and by_id[ACT]["available_rounds"] == []
    assert by_id["A-0005"]["onetoone_submitted_count"] == 0

    # ---- unsubmitted：名册端仍按原契约读 activities/group_selections/signups ----
    signups, selections = _balanced(2, 2)
    signups.append(_signup("ou_f9", "阿东"))
    api.install(signups=signups, selections=selections)
    api.fresh_calls.clear()
    r = api.get("/api/admin/group/unsubmitted?activity_id=" + ACT)
    assert r.status_code == 200
    assert set(api.fresh_calls) == {"activities", "group_selections", "signups"}
    un = r.get_json()
    assert un["total"] == 1 and un["all_submitted"] is False
    assert un["unsubmitted"][0]["open_id"] == "ou_f9"
    assert un["unsubmitted"][0]["user_id"] == "U-0009"


def test_activities_list_reads_rounds_for_collecting_activity(api):
    """审计必修 2（A-0011 实测）：收集中但已有历史轮次 → 列表必须读结果表。

    旧实现只对「已完成」设 need_rounds，整场活动集合里没有已完成活动时
    group_results 根本不读，收集中活动的 has_results/latest_round/
    available_rounds 全是空，卡片看起来「没有结果」。
    """
    signups, selections = _balanced(2, 2)
    results = [_res("ou_m0", 1, "1"), _res("ou_m0", 1, "2"), _res("ou_f0", 1, "2")]
    api.install(activity=_act(), signups=signups, selections=selections, results=results)
    api.fresh_calls.clear()
    r = api.get("/api/admin/group/activities")
    assert r.status_code == 200
    assert "group_results" in api.fresh_calls, "收集中也要读结果表"
    item = r.get_json()["activities"][0]
    assert item["group_status"] == "收集中"
    assert item["has_results"] is True
    assert item["latest_round"] == 2
    assert item["available_rounds"] == [1, 2]


def test_group_run_round_validation_and_unbounded_high_round(api):
    """不限轮次：0/-1/非整数/布尔回 400 零写入；51/999 这类大轮次合法可执行。"""
    fake = api.install()
    base = {"activity_id": ACT, "males_per_group": 2, "females_per_group": 2}
    for bad in (0, -1, "2", True):
        assert api.post("/api/admin/group/run", {**base, "round": bad}).status_code == 400, bad
    app._rate_store.clear()          # run 端点 5 次/60s 限流：分段清理，测的是校验不是限流
    # 每组人数缺省/非法同样 400
    assert api.post("/api/admin/group/run", {"activity_id": ACT, "round": 1}).status_code == 400
    assert api.post("/api/admin/group/run",
                    {"activity_id": ACT, "males_per_group": 0, "females_per_group": 2,
                     "round": 1}).status_code == 400
    assert fake.writes() == [], "拒绝的入参不得产生任何写入"

    signups, selections = _balanced(2, 2)
    api.install(signups=signups, selections=selections)
    app._rate_store.clear()
    r = api.post("/api/admin/group/run", {**base, "round": 999})
    assert r.status_code == 202, "大轮次必须能入列执行"
    job = _poll_job(api, r.get_json()["job_id"])
    assert job["state"] == "done" and job["result"]["code"] == "OK"
    rows = api.fake.tables[GROUP_RESULT_TABLE]
    assert rows and all(row["fields"].get("轮次") == "999" for row in rows), \
        "第999轮结果必须写入且带轮次"
    _wait_refresh_calls(api, {"activities", "group_results"})


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
    # 迁移后的表含一对一栏位 → 开启时无条件合写对侧=否（schema 判别，见上方用例）；
    # 「签到默认打开」自开始动作迁移到本路径：空 → 同笔补「是」
    assert len(updates) == 1 \
        and updates[0][3] == {"分组功能开启": "是", "一对一功能开启": "否",
                              "签到功能开启": "是"}
    assert api.refresh_calls == ["activities"]

    # 真实环境 fresh=True 会从飞书读到刚写入的「是」；同步桩快照模拟这次实时读
    api.snapshot["activities"][0]["fields"]["分组功能开启"] = "是"
    r = api.post("/api/admin/group/flag", {"activity_id": ACT, "enabled": True})
    assert r.status_code == 200 and r.get_json()["group_flag"] == "是"
    assert len([c for c in fake.writes() if c[0] == "update"]) == 1, "幂等：不重复写"


def test_group_flag_busy_returns_409_with_busy_activity(api, monkeypatch):
    """flag 写入与执行作业共用活动锁：撞锁快速失败回 409，零写入、无 ok 字段。"""
    fake = api.install()

    @contextlib.contextmanager
    def busy_lock(activity_id, timeout):
        raise lib_grouping.GroupingBusy(activity_id)
        yield  # pragma: no cover

    monkeypatch.setattr(lib_grouping, "grouping_lock", busy_lock)
    r = api.post("/api/admin/group/flag", {"activity_id": ACT, "enabled": True})
    assert r.status_code == 409
    body = r.get_json()
    assert "ok" not in body and body["busy_activity_id"] == ACT
    assert fake.writes() == [], "抢锁失败不得写任何东西"


def test_group_flag_write_failed_returns_500(api):
    """update_record 返回假 → 500 WRITE_FAILED，绝不回 ok:true（静默写失败防护）。"""
    fake = api.install()
    fake.update_record = lambda *a, **k: None

    r = api.post("/api/admin/group/flag", {"activity_id": ACT, "enabled": True})
    assert r.status_code == 500
    body = r.get_json()
    assert body["code"] == "WRITE_FAILED" and "ok" not in body
    assert api.refresh_calls == [], "写失败不刷新快照"


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

    r = api.post("/api/admin/group/run",
                 {"activity_id": ACT, "round": 1, "males_per_group": 2,
                  "females_per_group": 2})
    assert r.status_code == 202
    job = _poll_job(api, r.get_json()["job_id"])
    assert job["state"] == "done"
    assert job["result"]["code"] == "SEARCH_FAILED"
    assert fake.writes() == [], "读不到既有结果就不能动任何字段"
    assert _find_act(fake, ACT)["fields"]["分组状态"] == "收集中", "失败零状态变更"
    # job 线程先落终态、后刷快照：等刷新调用补齐，避免漏到下一个用例的
    # mock 列表里（全量跑时偶发：test_reset_search_failure 的 == [] 被串台）。
    _wait_refresh_calls(api, {"activities", "group_results"})


def test_run_default_round_search_failure_returns_500(api):
    """round 缺省时推演读失败 → 500 明报（不得拿 0 当「没跑过」）。"""
    fake = api.install()
    fake.search_fails = {GROUP_RESULT_TABLE}
    r = api.post("/api/admin/group/run",
                 {"activity_id": ACT, "males_per_group": 2, "females_per_group": 2})
    assert r.status_code == 500
    assert r.get_json()["code"] == "SEARCH_FAILED"
    assert fake.writes() == []


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
    r = api.post("/api/admin/group/run",
                 {"activity_id": ACT, "round": 1, "males_per_group": 2,
                  "females_per_group": 2})
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
    r = api.post("/api/admin/group/run",
                 {"activity_id": ACT, "round": 1, "males_per_group": 2,
                  "females_per_group": 2})
    assert r.status_code == 202
    job = _poll_job(api, r.get_json()["job_id"])
    assert job["state"] == "failed"
    assert "写表炸了" in job["error"] and job["result"] is None
    # 终态落盘与两次刷新调用之间存在窗口（审计 400 次复现约 5% 先看到 failed）。
    # 先等刷新收敛，再做严格 `==` 断言——修的是竞态，不是放宽检查。
    _wait_refresh_calls(api, {"activities", "group_results"})
    assert set(api.refresh_calls) == {"activities", "group_results"}

    # 异常路径也必须把锁放掉，否则整场活动再也跑不了分组
    monkeypatch.setattr(lib_grouping, "execute_round", real_execute)
    r2 = api.post("/api/admin/group/run",
                  {"activity_id": ACT, "round": 2, "males_per_group": 2,
                   "females_per_group": 2})
    assert r2.status_code == 202
    assert _poll_job(api, r2.get_json()["job_id"])["state"] == "done"


def test_payload_validation_and_missing_resources(api):
    api.install()
    assert api.post("/api/admin/group/preview",
                    {"activity_id": ACT, "round": 0}).status_code == 400
    assert api.post("/api/admin/group/preview",
                    {"activity_id": "", "round": 1}).status_code == 400
    # reset：缺 activity_id → 400
    assert api.post("/api/admin/group/reset", {}).status_code == 400
    # run：轮次非法 / 每组人数缺省或非法 → 400
    assert api.post("/api/admin/group/run",
                    {"activity_id": ACT, "round": "1", "males_per_group": 2,
                     "females_per_group": 2}).status_code == 400
    assert api.post("/api/admin/group/run",
                    {"activity_id": ACT, "round": 1}).status_code == 400
    assert api.post("/api/admin/group/run",
                    {"activity_id": ACT, "males_per_group": 2, "females_per_group": 0,
                     "round": 1}).status_code == 400
    assert api.post("/api/admin/group/flag",
                    {"activity_id": ACT, "enabled": "yes"}).status_code == 400
    # 已删除的 start 端点必须保持不可用（移除志愿收集阶段的回归护栏）：
    # POST 会被 Flask 静态兜底路由判 405，关键是没有可用的 start 端点
    assert api.post("/api/admin/group/start",
                    {"activity_id": ACT, "males_per_group": 2,
                     "females_per_group": 2}).status_code in (404, 405)
    assert api.get("/api/admin/group/job?job_id=nope").status_code == 404
    assert api.get("/api/admin/group/status?activity_id=A-9999").status_code == 404
    assert api.get("/api/admin/group/unsubmitted?activity_id=A-9999").status_code == 404
    assert api.post("/api/admin/group/preview",
                    {"activity_id": "A-9999", "round": 1}).status_code == 404
    assert api.post("/api/admin/group/reset",
                    {"activity_id": "A-9999"}).status_code == 404
    assert api.post("/api/admin/group/run",
                    {"activity_id": "A-9999", "males_per_group": 2,
                     "females_per_group": 2}).status_code == 404


# ---------------------------------------------------------------- C4：现场签到端点冒烟

def test_signin_admin_roster_marks_and_flag(api):
    """签到端点冒烟：fresh 名册 + 两态单笔 + flag 幂等。

    完整契约（失败注入/多条报名/迟到拒绝等）在 tests/test_signin_gate.py。
    """
    act = _act(签到功能开启="是")
    signups = [_signup("ou_m0", "小北"), _signup("ou_f0", "阿南")]
    signups[0]["fields"]["签到状态"] = "迟到"   # 历史遗留值：也算已到（D13）
    fake = api.install(activity=act, signups=signups)

    body = api.get("/api/admin/activity/signin?activity_id=" + ACT).get_json()
    assert body["signin_enabled"] is True
    assert body["stats"] == {"checked_in": 1, "signed_up": 2, "not_checked_in": 1}
    by_oid = {x["open_id"]: x for x in body["roster"]}
    assert by_oid["ou_m0"]["checked_in"] is True
    assert by_oid["ou_m0"]["user_id"] == "U-0001"

    r = api.post("/api/admin/activity/signin",
                 {"activity_id": ACT, "open_id": "ou_f0", "state": "未到"})
    assert r.status_code == 200
    row = [x for x in fake.tables[SIGNUP_TABLE_ID]
           if x["fields"]["报名人open_id"] == "ou_f0"][0]
    assert row["fields"]["签到状态"] == "未到"

    # flag：已是「是」→ 幂等零写；关 → 写「否」
    updates_before = [c for c in fake.writes() if c[0] == "update"]
    assert api.post("/api/admin/activity/signin/flag",
                    {"activity_id": ACT, "enabled": True}).get_json()["signin_flag"] == "是"
    assert [c for c in fake.writes() if c[0] == "update"] == updates_before, "幂等：不重复写"
    assert api.post("/api/admin/activity/signin/flag",
                    {"activity_id": ACT, "enabled": False}).get_json()["signin_flag"] == "否"
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert len(updates) == len(updates_before) + 1
    assert updates[-1][3] == {"签到功能开启": "否"}


# ---------------------------------------------------------------- 审查必修 1/2/5：去重口径

def _dirty_signups_and_selections():
    """首行性别缺失 + 次行有效 + 重复行：旧 H5 口径会吞人/多算行数。"""
    signups = [_signup("ou_m0", "小北"), _signup("ou_m1", "阿北"), _signup("ou_f0", "阿南")]
    selections = [
        _sel("ou_m0", "", ("ou_f0",)),        # 首行性别缺失
        _sel("ou_m0", "男性", ("ou_f0",)),    # 次行有效（旧 H5 把人整个吞掉）
        _sel("ou_m0", "男性", ("ou_f0",)),    # 重复志愿行
        _sel("ou_f0", "女性", ("ou_m0",)),
    ]
    return signups, selections


def test_status_shares_lib_dedup_and_adds_up_with_unsubmitted(api):
    """必修 1/2/5：H5 状态、名单与 lib 共用去重口径，已提交 + 未提交 == 报名总数。

    计数只在分组模式的详情 UI 显示，故用分组模式活动（读取契约见 test_a9）。
    """
    signups, selections = _dirty_signups_and_selections()
    api.install(activity=_act(分组功能开启="是"), signups=signups, selections=selections)

    st = api.get("/api/admin/group/status?activity_id=" + ACT).get_json()
    assert st["submitted_count"] == 2, "重复行只算一次"
    assert st["male_count"] == 1 and st["female_count"] == 1, "首行缺失不吞人"
    assert st["unsubmitted_count"] == 1

    # 与 preview（lib 路径）同一口径——旧实现这里 H5 是 0、preview 是 1
    pv = api.post("/api/admin/group/preview", {"activity_id": ACT, "round": 1}).get_json()
    assert (pv["male_count"], pv["female_count"]) == (st["male_count"], st["female_count"])
    assert pv["skipped_no_gender"] == 1

    un = api.get("/api/admin/group/unsubmitted?activity_id=" + ACT).get_json()
    assert un["total"] == 1 and un["all_submitted"] is False
    assert len(un["submitted"]) == 2, "已填写名单与去重集合同口径"
    by_oid = {x["open_id"]: x for x in un["submitted"]}
    assert set(by_oid) == {"ou_m0", "ou_f0"}
    assert by_oid["ou_m0"]["user_id"] == "U-0001"
    assert by_oid["ou_m0"]["gender"] == "男性"
    assert by_oid["ou_m0"]["nickname"] == "小北"
    assert by_oid["ou_f0"]["user_id"] == "U-0003"

    assert st["submitted_count"] + un["total"] == st["signup_count"] == 3


def test_status_group_flag_is_yes_or_no(api):
    """必修 6：/status 带 group_flag（是/否），前端不必再补拉 /activities。"""
    api.install()
    st = api.get("/api/admin/group/status?activity_id=" + ACT).get_json()
    assert st["group_flag"] == "否"

    api.install(activity=_act(分组功能开启="是"))
    st = api.get("/api/admin/group/status?activity_id=" + ACT).get_json()
    assert st["group_flag"] == "是"


# ---------------------------------------------------------------- 审查必修 4：完整结果端点

def _result(oid, group_no, round_no, gender="男性"):
    rec = _res(oid, group_no, round_no)
    rec["record_id"] = f"rec_res_{round_no}_{group_no}_{oid}"
    rec["fields"]["用户性别"] = gender
    return rec


def test_result_endpoint_serves_history_rounds_isomorphic_with_job(api):
    """必修 4：任何历史轮次都能查，结构与 job result.groups 同构。"""
    signups, selections = _balanced(2, 2)
    results = [
        _result("ou_m0", 1, "1"), _result("ou_f0", 1, "1", "女性"),
        _result("ou_f0", 1, "2", "女性"), _result("ou_m0", 1, "2"),
    ]
    api.install(signups=signups, selections=selections, results=results)

    body = api.get("/api/admin/group/result?activity_id=" + ACT).get_json()
    assert body["activity_id"] == ACT and body["name"] == "关系自洽"
    assert body["round"] == 2 and body["rounds"] == [1, 2]
    assert len(body["groups"]) == 1
    g = body["groups"][0]
    assert set(g) == {"group_id", "members"}
    assert g["group_id"] == 1
    assert [m["open_id"] for m in g["members"]] == ["ou_m0", "ou_f0"], "男先女后"
    # 2026-10-15 真名改造（BRIEF 3）：member 增补 real_name/user_id 供「真名＋编号」；
    # 无 users 快照来源时 real_name/user_id 为空串（本用例不装 users 快照）
    assert all(set(m) == {"open_id", "gender", "display_name", "real_name", "user_id"}
               for m in g["members"])
    assert g["members"][0]["display_name"] == "ou_m0", "结果表昵称为空 → 退回 open_id"

    b1 = api.get("/api/admin/group/result?activity_id=" + ACT + "&round=1").get_json()
    assert b1["round"] == 1
    assert [m["open_id"] for m in b1["groups"][0]["members"]] == ["ou_m0", "ou_f0"]

    assert api.get("/api/admin/group/result?activity_id=" + ACT + "&round=abc").status_code == 400
    assert api.get("/api/admin/group/result?activity_id=" + ACT + "&round=0").status_code == 400
    assert api.get("/api/admin/group/result?activity_id=A-9999").status_code == 404


def test_result_display_name_fallback_matches_job_when_nickname_empty(api):
    """必修 2：报名昵称为空时，job result.groups 与 /result 的 display_name
    都退回 open_id，同一轮分组的展示口径完全一致。"""
    signups = [_signup("ou_m0", ""), _signup("ou_m1", ""),
               _signup("ou_f0", ""), _signup("ou_f1", "")]
    selections = [_sel("ou_m0", "男性"), _sel("ou_m1", "男性"),
                  _sel("ou_f0", "女性"), _sel("ou_f1", "女性")]
    api.install(signups=signups, selections=selections)

    r = api.post("/api/admin/group/run",
                 {"activity_id": ACT, "round": 1, "males_per_group": 2,
                  "females_per_group": 2})
    assert r.status_code == 202
    job = _poll_job(api, r.get_json()["job_id"])
    assert job["state"] == "done" and job["result"]["code"] == "OK"

    body = api.get("/api/admin/group/result?activity_id=" + ACT).get_json()
    job_groups = job["result"]["groups"]
    assert len(job_groups) == len(body["groups"]) == 1

    job_names = {m["open_id"]: m["display_name"] for m in job_groups[0]["members"]}
    res_names = {m["open_id"]: m["display_name"] for m in body["groups"][0]["members"]}
    assert job_names == res_names, "job 与 /result 的 display_name 必须一致"
    assert all(name == oid for oid, name in job_names.items()), "空昵称必须退回 open_id"


# ---------------------------------------------------------------- 审查必修 3：job TTL 兜底

def _job_record(state="running", created_at=None, started_at=None, activity_id=ACT,
                round_no=1, pid=None):
    now = int(time.time())
    return {"activity_id": activity_id, "round": round_no, "operator": "ou_test_admin",
            "state": state, "pid": os.getpid() if pid is None else pid, "progress": "",
            "created_at": now if created_at is None else created_at,
            "started_at": now if started_at is None else started_at,
            "finished_at": None, "result": None, "error": ""}


def test_same_pid_stale_job_reports_unknown_and_allows_rerun(api):
    """必修 3 负向验证：pid 相同但超过执行上限的 job 不再永久 409。"""
    old = int(time.time()) - app.GROUPING_JOB_MAX_RUNTIME - 60
    stale = _job_record(created_at=old, started_at=old)
    storage.update_json(app.GROUPING_JOBS_FILE, {}, lambda jobs: {**jobs, "stalejob0001": stale})

    r = api.get("/api/admin/group/job?job_id=stalejob0001")
    assert r.status_code == 200 and r.get_json()["state"] == "unknown"

    signups, selections = _balanced(2, 2)
    api.install(signups=signups, selections=selections)
    r = api.post("/api/admin/group/run",
                 {"activity_id": ACT, "round": 1, "males_per_group": 2,
                  "females_per_group": 2})
    assert r.status_code == 202, "pid 相同的过期 job 不能永久挡住重跑"
    assert _poll_job(api, r.get_json()["job_id"])["state"] == "done"


def test_job_conflict_branch_prunes_expired_records(api):
    """必修 3：冲突分支也要跑 TTL 清理（旧实现冲突早退，过期记录永远清不掉）。"""
    now = int(time.time())
    active = _job_record(created_at=now, started_at=now)
    expired = _job_record(state="done", created_at=now - app.GROUPING_JOB_TTL - 10,
                          started_at=now - app.GROUPING_JOB_TTL - 10)
    storage.update_json(app.GROUPING_JOBS_FILE, {},
                        lambda jobs: {**jobs, "activejob001": active,
                                      "expiredjob01": expired})

    conflict = app._create_grouping_job("newjob000001", ACT, 1, "ou_test_admin")

    assert conflict == "activejob001"
    jobs = storage.load_json(app.GROUPING_JOBS_FILE, {})
    assert "expiredjob01" not in jobs, "冲突时 TTL 清理必须照样跑"
    assert "newjob000001" not in jobs, "冲突时不入列"


# ---------------------------------------------------------------- 审查必修 7：提交志愿的竞态

def test_group_select_decides_create_update_inside_file_lock(api, monkeypatch):
    """必修 7：决定 create/update 的查询必须在 file_lock 内。

    锁外双查双空 → 双击产生两行重复志愿。这里用调用顺序钉死修法：查到没有后
    才 create，且整段在锁内。
    """
    events = []

    class _SpyLock:
        def __enter__(self):
            events.append("lock_enter")
            return self

        def __exit__(self, *exc):
            events.append("lock_exit")
            return False

    fake = api.install()
    monkeypatch.setattr(app, "file_lock", _SpyLock())
    monkeypatch.setattr(app, "active_gate", lambda oid: None)
    act = api.snapshot["activities"][0]
    monkeypatch.setattr(app, "snap_resolve_activity", lambda aid, fresh=False: (act, ACT))
    monkeypatch.setattr(app, "snap_signup",
                        lambda aid, oid: {"record_id": "rec_s", "fields": {}})
    monkeypatch.setattr(app, "snap_self_user",
                        lambda: {"record_id": "rec_u",
                                 "fields": {"昵称": "小北", "性别": "男性"}})
    monkeypatch.setattr(app, "snap_group_selection", lambda aid, oid: None)
    # 新增的候选集校验要读实表报名（与 group/candidates 端点同口径）：桩 7 行有效候选
    # （2026-10-16 起提交必须恰好 7 人；候选女性由本地 _find_user 桩保证）
    monkeypatch.setattr(app, "snap_find_user_by_openid",
                        lambda oid: {"record_id": "rec_u_" + oid,
                                     "fields": {"飞书用户ID": oid, "昵称": "阿南", "性别": "女性"}})
    monkeypatch.setattr(app.bitable, "get_signups",
                        lambda aid: [_signup(f"ou_f{i}", "阿南") for i in range(1, 8)])

    def _lookup(aid, oid):
        events.append("lookup")
        return None

    monkeypatch.setattr(app.bitable, "get_user_group_selection", _lookup)

    def _create(table_id, fields):
        events.append("create")
        return fake.create_record(table_id, fields)

    monkeypatch.setattr(app.bitable, "create_record", _create)

    r = api.post("/api/activities/" + ACT + "/group/select",
                 {"choices": [f"ou_f{i}" for i in range(1, 8)]})
    assert r.status_code == 200
    assert events == ["lock_enter", "lookup", "create", "lock_exit"], \
        "查 existing 与 create/update 必须同处锁内，且查询在前"


# ---------------------------------------------------------------- C3：互斥 guard 与条件合并

def test_group_run_blocked_when_onetoone_flag_on(api):
    """「执行分组」：对面一对一 flag=是 → job 结果 EXCLUSIVE_MODE、零写入。"""
    signups, selections = _balanced(2, 2)
    fake = api.install(activity=_act(一对一功能开启="是"),
                       signups=signups, selections=selections)
    r = api.post("/api/admin/group/run",
                 {"activity_id": ACT, "round": 1, "males_per_group": 2,
                  "females_per_group": 2})
    assert r.status_code == 202
    job = _poll_job(api, r.get_json()["job_id"])
    assert job["state"] == "done"
    assert job["result"]["code"] == "EXCLUSIVE_MODE"
    assert fake.writes() == []


def test_group_guard_noop_without_onetoone_fields(api):
    """no-op 对照：活动表没有一对一栏位时（既有生产/parity 桩表形态），
    execute_round 照常跑到幂等分支——guard 没有改变旧行为。"""
    signups, selections = _balanced(2, 2)
    fake = api.install(activity=_act(), signups=signups, selections=selections,
                       results=[_res("ou_m0", 1, "1")])
    r = api.post("/api/admin/group/run",
                 {"activity_id": ACT, "round": 1, "males_per_group": 2,
                  "females_per_group": 2})
    assert r.status_code == 202
    job = _poll_job(api, r.get_json()["job_id"])
    assert job["result"]["code"] == "ALREADY_DONE", "无对侧栏位 → 不触发 guard"
    assert not [c for c in fake.writes() if c[0] == "batch_delete"]


def test_set_group_flag_merges_counterpart_and_signin_default(api):
    """schema 判别：表里有一对一栏位 → 开启分组时**无条件**合写「一对一=否」（同一笔）。

    修复语义：不再看快照里的现值（并发窗口里读到的必是旧值；空值场景
    search_records 还会省略 key）。逐一覆盖 一对一=是 / =否 / 空（key 缺失）。
    同时「签到默认打开」自开始动作迁移到本路径：空 → 同笔补「是」。
    """
    for value in ("是", "否", None):
        fields = {"分组功能开启": "否"}
        if value is not None:
            fields["一对一功能开启"] = value
        fake = api.install(activity=_act(**fields))
        r = api.post("/api/admin/group/flag", {"activity_id": ACT, "enabled": True})
        assert r.status_code == 200 and r.get_json()["group_flag"] == "是"
        updates = [c for c in fake.writes() if c[0] == "update"]
        assert len(updates) == 1, f"value={value!r} 必须恰一笔 update（同一笔 payload 合写多栏）"
        assert updates[0][3] == {"分组功能开启": "是", "一对一功能开启": "否",
                                 "签到功能开启": "是"}, f"value={value!r}"


def test_set_group_flag_no_merge_without_field_exists(api, monkeypatch):
    """表里没有该栏位（未迁移形态/parity 桩表）→ 不合写对侧；签到默认照写。"""
    monkeypatch.setattr(sys.modules[__name__], "FakeBitable", _UnmigratedFake)
    fake = api.install(activity=_act(分组功能开启="否", 一对一功能开启="是"))
    r = api.post("/api/admin/group/flag", {"activity_id": ACT, "enabled": True})
    assert r.status_code == 200
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert len(updates) == 1 and updates[0][3] == {"分组功能开启": "是", "签到功能开启": "是"}

    fake2 = api.install(activity=_act(分组功能开启="否"))
    api.post("/api/admin/group/flag", {"activity_id": ACT, "enabled": True})
    updates2 = [c for c in fake2.writes() if c[0] == "update"]
    assert len(updates2) == 1 and updates2[0][3] == {"分组功能开启": "是", "签到功能开启": "是"}


def test_set_group_flag_disable_never_touches_onetoone(api):
    """关闭分组只写自身=否；即使一对一=是（脏数据）也不合并不动它，且不补签到默认。"""
    fake = api.install(activity=_act(分组功能开启="是", 一对一功能开启="是"))
    api.post("/api/admin/group/flag", {"activity_id": ACT, "enabled": False})
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert len(updates) == 1 and updates[0][3] == {"分组功能开启": "否"}
