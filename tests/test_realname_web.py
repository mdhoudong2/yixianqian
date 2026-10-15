"""活动现场作业面「真名＋编号」读取端契约（不联网，桩表驱动）。

合约（2026-10-15）：显示名 = users 表「姓名」去空白非空 ? 姓名 : 既有来源名
（落库名/昵称）；编号（用户ID）沿用既有布局、缺处补上。写入源头（落库）由
bot/lib 侧负责，本文件只锁 H5 读取端：存量数据里的姓名立刻生效、改名随快照刷新。

断言面：
- 用户端分组/一对一候选 dict 带 real_name（纯空白姓名视为无）；
- 用户端分组结果与必聊名单昵称真名优先，无姓名回退落库名/快照昵称；
- 管理台分组结果 member 带 real_name+user_id、display_name 真名优先；
- 管理台一对一结果 person/partner 昵称真名优先（回退原值）；
- 未提交/已提交名册、报名名单带 real_name。

桩法沿用 tests/test_signin_gate.py 与 tests/test_group_admin_api.py 的
Api/FakeBitable 脚手架（桩「存取」，端点跑真实现）。
"""
import os
import sys

import pytest

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402

from lib import grouping as lib_grouping  # noqa: E402
from lib import onetoone as lib_onetoone  # noqa: E402
from tests.test_group_admin_api import Api, _BatchFake  # noqa: E402
from tests.test_grouping_parity import _act, _sel, _signup  # noqa: E402
from tests.test_onetoone_admin_api import (  # noqa: E402
    RESULT_TBL,
    SELECT_TBL,
    _oto_act,
    _oto_sel,
)
from tests.test_onetoone_admin_api import Api as OtoApi  # noqa: E402

ACT = "A-0002"


# ---------------------------------------------------------------- 桩数据

def _user(oid, uid, gender, nickname, real_name=None):
    fields = {"飞书用户ID": oid, "用户ID": uid, "性别": gender, "昵称": nickname}
    if real_name is not None:
        fields["姓名"] = real_name
    return {"record_id": f"rec_u_{oid}", "fields": fields}


USERS = {
    "ou_m0": _user("ou_m0", "U-0001", "男性", "小北", real_name="陈北"),
    "ou_m1": _user("ou_m1", "U-0002", "男性", "阿北"),              # 无姓名 → 回退
    "ou_m2": _user("ou_m2", "U-0003", "男性", "小中"),              # 无姓名，测昵称兜底
    "ou_f0": _user("ou_f0", "U-0004", "女性", "阿南", real_name=" 李南 "),  # 前后空白 → 去空白
    "ou_f1": _user("ou_f1", "U-0005", "女性", "小西"),              # 无姓名 → 回退
    "ou_f2": _user("ou_f2", "U-0006", "女性", "小东", real_name="   "),     # 纯空白 → 视为无
}


def _gr_row(oid, group_no, nick, gender="男性", round_no="1"):
    """分组结果行（字段名与 web/backend/config.py F_GR_* 一致）。"""
    return {"record_id": f"rec_gr_{group_no}_{oid}",
            "fields": {"活动ID": ACT, "组号": group_no, "用户open_id": oid,
                       "用户昵称": nick, "用户性别": gender, "轮次": round_no}}


def _oto_row(user_oid, target_oid, target_nick, rank=1, round_no=1):
    """一对一结果行（字段名与 web/backend/config.py F_OTO_* 一致）。"""
    return {"record_id": f"rec_oto_{user_oid}_{target_oid}",
            "fields": {"活动ID": ACT, "排名": rank, "轮次": round_no,
                       "用户open_id": user_oid,
                       "用户昵称": USERS[user_oid]["fields"]["昵称"],
                       "用户性别": USERS[user_oid]["fields"]["性别"],
                       "必聊对象open_id": target_oid,
                       "必聊对象昵称": target_nick}}


def _me_client(open_id):
    c = app.app.test_client()
    c.set_cookie("yxq_session", app.create_session(open_id))
    return c


# ---------------------------------------------------------------- 测试脚手架

@pytest.fixture
def group_api(monkeypatch, tmp_path):
    import tests.test_group_admin_api as _tga
    monkeypatch.setattr(_tga, "FakeBitable", _BatchFake)
    monkeypatch.setattr(lib_grouping, "log", lambda *a, **k: None)
    monkeypatch.setattr(app, "snap_find_user_by_openid", lambda oid: USERS.get(oid))
    monkeypatch.setattr(app, "account_gate", lambda oid: None)
    monkeypatch.setattr(app, "active_gate", lambda oid: None)
    monkeypatch.setattr(app, "GROUPING_JOBS_FILE", str(tmp_path / "grouping_jobs.json"))
    monkeypatch.setattr(app, "ONETOONE_JOBS_FILE", str(tmp_path / "onetoone_jobs.json"))
    lib_grouping.configure(lock_dir=str(tmp_path))
    app._rate_store.clear()
    a = Api(monkeypatch, tmp_path)
    monkeypatch.setattr(app, "_snap_live", lambda key: list(a.snapshot.get(key, [])))

    def _search(table_id, filter_conditions=None, *args, **kwargs):
        conds = (filter_conditions or {}).get("conditions", []) \
            if isinstance(filter_conditions, dict) else (filter_conditions or [])
        return a.fake.search_records(table_id, {"conditions": conds}) or []

    monkeypatch.setattr(app.bitable, "search_records", _search)

    def _self_user():
        from flask import g as _g
        return USERS.get(_g.get("yxq_open_id"))

    monkeypatch.setattr(app, "snap_self_user", _self_user)
    return a


@pytest.fixture
def oto_api(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "ONETOONE_SELECT_TABLE", SELECT_TBL)
    monkeypatch.setattr(app, "ONETOONE_RESULT_TABLE", RESULT_TBL)
    monkeypatch.setattr(lib_grouping, "log", lambda *a, **k: None)
    monkeypatch.setattr(lib_onetoone, "log", lambda *a, **k: None)
    monkeypatch.setattr(app, "snap_find_user_by_openid", lambda oid: USERS.get(oid))
    monkeypatch.setattr(app, "account_gate", lambda oid: None)
    monkeypatch.setattr(app, "active_gate", lambda oid: None)
    monkeypatch.setattr(app, "GROUPING_JOBS_FILE", str(tmp_path / "grouping_jobs.json"))
    monkeypatch.setattr(app, "ONETOONE_JOBS_FILE", str(tmp_path / "onetoone_jobs.json"))
    app.configure_grouping_lib(str(tmp_path))
    app.configure_onetoone_lib(str(tmp_path))
    app._rate_store.clear()
    a = OtoApi(monkeypatch, tmp_path)

    def _search(table_id, filter_conditions=None, *args, **kwargs):
        conds = (filter_conditions or {}).get("conditions", []) \
            if isinstance(filter_conditions, dict) else (filter_conditions or [])
        return a.fake.search_records(table_id, {"conditions": conds}) or []

    monkeypatch.setattr(app.bitable, "search_records", _search)

    def _self_user():
        from flask import g as _g
        return USERS.get(_g.get("yxq_open_id"))

    monkeypatch.setattr(app, "snap_self_user", _self_user)
    return a


# ---------------------------------------------------------------- 用户端候选

def test_group_candidates_include_real_name(group_api):
    """分组候选带 real_name：姓名去空白非空用之；无姓名为空串（前端回退昵称）。"""
    signups = [_signup("ou_m0", "小北"), _signup("ou_m1", "阿北"),
               _signup("ou_f0", "阿南")]
    group_api.install(signups=signups)
    r = _me_client("ou_f0").get("/api/activities/" + ACT + "/group/candidates")
    assert r.status_code == 200, r.get_json()
    by_oid = {x["openid"]: x for x in r.get_json()["candidates"]}
    assert set(by_oid) == {"ou_m0", "ou_m1"}
    assert by_oid["ou_m0"]["real_name"] == "陈北"
    assert by_oid["ou_m0"]["user_id"] == "U-0001"
    assert by_oid["ou_m1"]["real_name"] == ""
    assert by_oid["ou_m1"]["nickname"] == "阿北", "既有字段不受影响"


def test_onetoone_candidates_include_real_name(oto_api):
    signups = [_signup("ou_m0", "小北"), _signup("ou_f0", "阿南")]
    oto_api.install(signups=signups)
    r = _me_client("ou_f0").get("/api/activities/" + ACT + "/onetoone/candidates")
    assert r.status_code == 200, r.get_json()
    by_oid = {x["openid"]: x for x in r.get_json()["candidates"]}
    assert set(by_oid) == {"ou_m0"}
    assert by_oid["ou_m0"]["real_name"] == "陈北"
    assert by_oid["ou_m0"]["user_id"] == "U-0001"


# ---------------------------------------------------------------- 用户端结果

def test_group_result_prefers_real_name_with_fallback(group_api):
    """用户端分组结果：真名优先；无姓名回退结果表落库名；落库名也空回退快照昵称。"""
    results = [
        _gr_row("ou_f0", 1, "阿南落库", "女性"),   # me：真名「李南」（去空白）
        _gr_row("ou_m0", 1, "小北落库"),           # 真名「陈北」压过落库名
        _gr_row("ou_m1", 1, "阿北落库"),           # 无姓名 → 落库名
        _gr_row("ou_m2", 1, ""),                   # 无姓名且落库名空 → 快照昵称
        _gr_row("ou_f2", 1, "小东落库", "女性"),   # 纯空白姓名 → 落库名（不把空白当真名）
    ]
    group_api.install(activity=_act(分组状态="已完成"), results=results)
    group_api.snapshot["users"] = list(USERS.values())
    r = _me_client("ou_f0").get("/api/activities/" + ACT + "/group/result")
    assert r.status_code == 200, r.get_json()
    by_oid = {m["openid"]: m for m in r.get_json()["members"]}
    assert by_oid["ou_f0"]["nickname"] == "李南"
    assert by_oid["ou_m0"]["nickname"] == "陈北"
    assert by_oid["ou_m1"]["nickname"] == "阿北落库", "无姓名回退落库名"
    assert by_oid["ou_m2"]["nickname"] == "小中", "落库名也空回退快照昵称"
    assert by_oid["ou_f2"]["nickname"] == "小东落库", "纯空白姓名视为无"
    assert by_oid["ou_m0"]["user_id"] == "U-0001"


def test_onetoone_result_prefers_real_name_with_fallback(oto_api):
    """用户端必聊名单：真名优先；无姓名回退结果表落库名；落库名也空回退昵称。"""
    results = [
        _oto_row("ou_m0", "ou_f0", "阿南落库", 1),   # 真名「李南」
        _oto_row("ou_m0", "ou_f1", "小西落库", 2),   # 无姓名 → 落库名
        _oto_row("ou_m0", "ou_f2", "", 3),           # 纯空白姓名且落库名空 → 快照昵称
    ]
    oto_api.install(activity=_oto_act(oto_status="已完成"), results=results)
    oto_api.snapshot["users"] = list(USERS.values())
    r = _me_client("ou_m0").get("/api/activities/" + ACT + "/onetoone/result")
    assert r.status_code == 200, r.get_json()
    partners = {p["openid"]: p for p in r.get_json()["partners"]}
    assert partners["ou_f0"]["nickname"] == "李南"
    assert partners["ou_f1"]["nickname"] == "小西落库", "无姓名回退落库名"
    assert partners["ou_f2"]["nickname"] == "小东", "落库名也空回退快照昵称"
    assert partners["ou_f0"]["user_id"] == "U-0004"


# ---------------------------------------------------------------- 管理台结果

def test_admin_group_result_has_real_name_and_user_id(group_api):
    """管理台分组结果：member 带 real_name+user_id；display_name 真名优先、缺则原值。"""
    results = [
        _gr_row("ou_m0", 1, "小北落库"),
        _gr_row("ou_f0", 1, "阿南落库", "女性"),
        _gr_row("ou_f1", 1, "小西落库", "女性"),   # 无姓名 → display_name 保持原值
    ]
    group_api.install(results=results)
    group_api.snapshot["users"] = list(USERS.values())
    body = group_api.get("/api/admin/group/result?activity_id=" + ACT).get_json()
    by_oid = {m["open_id"]: m for m in body["groups"][0]["members"]}
    assert set(by_oid["ou_m0"]) == {"open_id", "gender", "display_name",
                                    "real_name", "user_id"}
    assert by_oid["ou_m0"]["real_name"] == "陈北"
    assert by_oid["ou_m0"]["user_id"] == "U-0001"
    assert by_oid["ou_m0"]["display_name"] == "陈北", "真名替换落库名"
    assert by_oid["ou_f0"]["display_name"] == "李南"
    assert by_oid["ou_f1"]["real_name"] == ""
    assert by_oid["ou_f1"]["display_name"] == "小西落库"


def test_admin_onetoone_result_prefers_real_name(oto_api):
    """管理台一对一结果：person/partner 昵称真名优先，无姓名回退原值。"""
    results = [
        _oto_row("ou_m0", "ou_f0", "阿南落库", 1),
        _oto_row("ou_m0", "ou_f1", "小西落库", 2),
        _oto_row("ou_f0", "ou_m0", "小北落库", 1),
    ]
    oto_api.install(results=results)
    oto_api.snapshot["users"] = list(USERS.values())
    body = oto_api.get("/api/admin/onetoone/result?activity_id=" + ACT).get_json()
    people = {p["open_id"]: p for p in body["people"]}
    assert people["ou_m0"]["nickname"] == "陈北"
    assert people["ou_m0"]["user_id"] == "U-0001"
    partners = {t["open_id"]: t for t in people["ou_m0"]["partners"]}
    assert partners["ou_f0"]["nickname"] == "李南"
    assert partners["ou_f1"]["nickname"] == "小西落库", "无姓名回退原值"
    assert partners["ou_f0"]["user_id"] == "U-0004"


# ---------------------------------------------------------------- 管理台名册

def test_admin_group_unsubmitted_roster_has_real_name(group_api):
    """未提交/已提交名册每项带 real_name（沿用取 user_id 的同一份档案，零额外查询）。"""
    signups = [_signup("ou_m0", "小北"), _signup("ou_m1", "阿北"),
               _signup("ou_f0", "阿南")]
    selections = [_sel("ou_m0", "男性", ("ou_f0",))]
    group_api.install(signups=signups, selections=selections)
    group_api.snapshot["users"] = list(USERS.values())
    un = group_api.get("/api/admin/group/unsubmitted?activity_id=" + ACT).get_json()
    by_oid = {x["open_id"]: x for x in un["unsubmitted"]}
    assert set(by_oid) == {"ou_m1", "ou_f0"}
    assert by_oid["ou_f0"]["real_name"] == "李南"
    assert by_oid["ou_m1"]["real_name"] == ""
    submitted = {x["open_id"]: x for x in un["submitted"]}
    assert submitted["ou_m0"]["real_name"] == "陈北"


def test_admin_onetoone_unsubmitted_roster_has_real_name(oto_api):
    signups = [_signup("ou_m0", "小北"), _signup("ou_f0", "阿南")]
    selections = [_oto_sel("ou_m0", "男性", ("ou_f0",))]
    oto_api.install(signups=signups, selections=selections)
    oto_api.snapshot["users"] = list(USERS.values())
    un = oto_api.get("/api/admin/onetoone/unsubmitted?activity_id=" + ACT).get_json()
    by_oid = {x["open_id"]: x for x in un["unsubmitted"]}
    assert set(by_oid) == {"ou_f0"}
    assert by_oid["ou_f0"]["real_name"] == "李南"
    assert un["submitted"][0]["open_id"] == "ou_m0"
    assert un["submitted"][0]["real_name"] == "陈北"


# ---------------------------------------------------------------- 报名名单

def test_activity_signups_include_real_name(group_api):
    """报名名单弹层数据源：payload 带 real_name（无姓名空串，前端回退昵称）。"""
    signups = [_signup("ou_m0", "小北"), _signup("ou_f1", "小西")]
    group_api.install(signups=signups)
    group_api.snapshot["users"] = list(USERS.values())
    r = _me_client("ou_f0").get("/api/activities/" + ACT + "/signups")
    assert r.status_code == 200, r.get_json()
    by_oid = {x["openid"]: x for x in r.get_json()["signups"]}
    assert by_oid["ou_m0"]["real_name"] == "陈北"
    assert by_oid["ou_m0"]["user_id"] == "U-0001"
    assert by_oid["ou_f1"]["real_name"] == ""
    assert by_oid["ou_f1"]["nickname"] == "小西"


# ---------------------------------------------------------------- 现场签到名册（纯空白防漏）

def test_admin_signin_roster_real_name_strips_whitespace(group_api):
    """签到名册 real_name 必须走 _display_real_name：空白剥离、纯空白视为无。

    前端以 `r.real_name || r.nickname` 为主名（'   ' 是 truthy）：纯空白姓名若不
    剥离，暱称显示会被空白盖掉。裸 get_field_text 会返回「 李南 」/「   」（突变
    自查点），本用例同时钉住「去前后空白」与「纯空白回空串」。
    """
    signups = [_signup("ou_f0", "阿南"), _signup("ou_f2", "小东"),
               _signup("ou_m1", "阿北")]
    group_api.install(activity=_act(), signups=signups)
    group_api.snapshot["users"] = list(USERS.values())
    r = group_api.get("/api/admin/activity/signin?activity_id=" + ACT)
    assert r.status_code == 200, r.get_json()
    by_oid = {x["open_id"]: x for x in r.get_json()["roster"]}
    assert by_oid["ou_f0"]["real_name"] == "李南", "「 李南 」→ 去前后空白"
    assert by_oid["ou_f2"]["real_name"] == "", "「   」→ 视为无（不得把空白当真名）"
    assert by_oid["ou_f2"]["nickname"] == "小东", "昵称回退不受影响"
    assert by_oid["ou_m1"]["real_name"] == "", "无姓名 → 空串"


# ---------------------------------------------------------------- 前端绑定（字符串级防回退）

_INDEX_HTML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "web", "frontend", "index.html")


def test_frontend_real_name_bindings_present():
    """字符串级防回退：本批「真名优先」关键绑定在 index.html 至少各出现一次。

    渲染层单测全绿也抓不到前端模板被整批回退（reviewer 突变 P14）。只做 ≥1 次的
    存在性断言（不锁次数/排版），避免误伤后续排版微调：
    - c.*：用户端分组/一对一候选与已选志愿（含「 · 编号」）；
    - r.*：管理台名册（分组×2、一对一×2、签到页×2、现场签到名册）；
    - s.*：报名名单弹层；
    - 「真名为主时仍展示昵称」的签到行副标题；
    - 分组结果成员 chip：名走后端 display_name（后端已按 real_name 优先计算，
      故不是字面 m.real_name），编号追加不能回退。
    """
    with open(_INDEX_HTML, encoding="utf-8") as fh:
        html = fh.read()
    for snippet in (
        "c.real_name || c.nickname",
        "r.real_name || r.nickname",
        "s.real_name || s.nickname",
        "' · ' + r.nickname",
        "m.display_name",
        "' · ' + m.user_id",
    ):
        assert snippet in html, f"index.html 缺少本批绑定 {snippet!r}（整批回退？）"
