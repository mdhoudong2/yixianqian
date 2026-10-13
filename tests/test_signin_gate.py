"""C4 现场签到（R3）安全网：闸门 + 执行池过滤 + 管理端点 + 用户侧三态（不联网）。

红线与产品决策：
- 开关关闭（无栏位/栏位≠是）＝ 全旧行为：候选/提交/执行池都不过滤；
- 开关开启：候选池只留已到异性、提交被挡（SIGNIN_REQUIRED）、执行池过滤——
  关键案例：早提交志愿但未签到的人，执行结果里必须不出现（只挡提交不够）；
- 判定 D13：「已到」= 值 ∈ {按时, 迟到}（历史遗留的迟到也算到）；未到/空 = 否；
- fail-closed：attendance 查询失败 → 拒绝而非放行（业务准入，fail-open 会放未到场者进来）；
- 管理端点：GET 名册 fresh 直读、POST 单笔/批次只补空、flag 幂等且检查写返回值。

桩法沿用 tests/test_grouping_parity.py 的 FakeBitable（桩存取，规则跑真实现）与
tests/test_group_admin_api.py 的 Api 脚手架（Flask test client + 会话 + 快照桩）。
"""
import os
import sys

import pytest

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402
import grouping as bot_grouping  # noqa: E402
import onetoone as bot_onetoone  # noqa: E402

from lib import grouping as lib_grouping  # noqa: E402
from lib import onetoone as lib_onetoone  # noqa: E402
from tests.test_group_admin_api import Api, _BatchFake  # noqa: E402
from tests.test_grouping_parity import (  # noqa: E402
    ACTIVITY_TABLE_ID,
    GROUP_RESULT_TABLE,
    GROUP_SELECT_TABLE,
    SIGNUP_TABLE_ID,
    FakeBitable,
    _find_act,
    _sel,
)

ACT = "A-0002"
ATT = "签到状态"
FLAG = "签到功能开启"
OTO_SELECT = "tbl_test_oto_select"
OTO_RESULT = "tbl_test_oto_result"


# ---------------------------------------------------------------- 桩数据

def _user(oid, uid, gender, nickname):
    return {"record_id": f"rec_u_{oid}",
            "fields": {"飞书用户ID": oid, "用户ID": uid, "性别": gender, "昵称": nickname}}


USER_DIR = {
    "ou_m0": _user("ou_m0", "U-0001", "男性", "小北"),
    "ou_m1": _user("ou_m1", "U-0002", "男性", "阿北"),
    "ou_m2": _user("ou_m2", "U-0003", "男性", "小中"),
    "ou_f0": _user("ou_f0", "U-0011", "女性", "阿南"),
    "ou_f1": _user("ou_f1", "U-0012", "女性", "小西"),
    "ou_f2": _user("ou_f2", "U-0013", "女性", "小东"),
    "ou_f3": _user("ou_f3", "U-0014", "女性", "小南"),
    "ou_f4": _user("ou_f4", "U-0015", "女性", "小中"),
    "ou_f5": _user("ou_f5", "U-0016", "女性", "小六"),
    "ou_f6": _user("ou_f6", "U-0017", "女性", "小七"),
}


def _find_user(open_id):
    """lib 契约：返回记录列表。"""
    u = USER_DIR.get(open_id)
    return [] if u is None else [u]


def _find_user_record(open_id):
    """app.snap_find_user_by_openid 契约：返回单条记录 dict 或 None。"""
    return USER_DIR.get(open_id)


def _find_user_by_id_or_name(keyword):
    return [u for u in USER_DIR.values()
            if u["fields"]["用户ID"] == keyword or u["fields"]["昵称"] == keyword]


def _signup_att(oid, state, nick="某人", status="已报名", activity=ACT):
    """带签到状态的报名记录。"""
    f = {"活动ID": activity, "报名人open_id": oid, "报名人昵称": nick, "状态": status}
    if state is not None:
        f[ATT] = state
    return {"record_id": f"s_{activity}_{oid}", "fields": f}


def _act_group(**over):
    fields = {"活动ID": ACT, "活动名称": "关系自洽", "分组状态": "收集中",
              "每组男生数": 1, "每组女生数": 1, "分组功能开启": "否", FLAG: "是"}
    fields.update(over)
    return {"record_id": "rec_act", "fields": fields}


def _act_oto(**over):
    fields = {"活动ID": ACT, "活动名称": "关系自洽", "一对一状态": "收集中", FLAG: "是"}
    fields.update(over)
    return {"record_id": "rec_act", "fields": fields}


def _sel_oto(oid, gender, choices=()):
    from constants import FIELD_OTO_CHOICES, FIELD_OTO_SELECTOR_GENDER
    fields = {"活动ID": ACT, "选择人open_id": oid, FIELD_OTO_SELECTOR_GENDER: gender}
    for i, cf in enumerate(FIELD_OTO_CHOICES):
        fields[cf] = choices[i] if i < len(choices) else ""
    return {"record_id": f"oto_s_{oid}", "fields": fields}


# ---------------------------------------------------------------- 纯函数

def test_is_checked_in_d13():
    """D13：按时/迟到=True；未到/空/None=False。"""
    assert lib_grouping.is_checked_in("按时") is True
    assert lib_grouping.is_checked_in("迟到") is True
    assert lib_grouping.is_checked_in("未到") is False
    assert lib_grouping.is_checked_in("") is False
    assert lib_grouping.is_checked_in(None) is False


def test_signin_enabled_reads_flag():
    assert lib_grouping.signin_enabled({"签到功能开启": "是"}) is True
    assert lib_grouping.signin_enabled({"签到功能开启": "否"}) is False
    assert lib_grouping.signin_enabled({}) is False
    assert lib_grouping.signin_enabled(None) is False


# ---------------------------------------------------------------- lib 执行池过滤

@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setattr(bot_onetoone, "ONETOONE_SELECT_TABLE", OTO_SELECT)
    monkeypatch.setattr(bot_onetoone, "ONETOONE_RESULT_TABLE", OTO_RESULT)
    bot_grouping.configure_grouping_lib(str(tmp_path))
    bot_onetoone.configure_onetoone_lib(str(tmp_path))
    monkeypatch.setattr(lib_grouping, "log", lambda *a, **k: None)
    monkeypatch.setattr(lib_onetoone, "log", lambda *a, **k: None)
    return monkeypatch


def _group_tables(activity, signups, selections, results=()):
    return {ACTIVITY_TABLE_ID: [activity], SIGNUP_TABLE_ID: list(signups),
            GROUP_SELECT_TABLE: list(selections), GROUP_RESULT_TABLE: list(results),
            OTO_SELECT: [], OTO_RESULT: []}


def _flatten_members(res):
    return [m["open_id"] for g in res["groups"] for m in g["members"]]


def test_group_pool_filter_excludes_unchecked_in(configured):
    """关键案例：已提交志愿但未签到（含空状态）→ 执行结果里不出现他。"""
    activity = _act_group(每组男生数=1, 每组女生数=1)
    signups = [
        _signup_att("ou_m0", "按时"), _signup_att("ou_m1", "未到"),
        _signup_att("ou_m2", None),
        _signup_att("ou_f0", "按时"), _signup_att("ou_f1", "迟到"),
        _signup_att("ou_f2", "按时"),
    ]
    selections = [_sel("ou_m0", "男性", ("ou_f0",)), _sel("ou_m1", "男性", ("ou_f0",)),
                  _sel("ou_m2", "男性", ("ou_f1",)),
                  _sel("ou_f0", "女性", ("ou_m0",)), _sel("ou_f1", "女性", ("ou_m1",)),
                  _sel("ou_f2", "女性", ("ou_m2",))]
    fake = FakeBitable(_group_tables(activity, signups, selections))

    res = lib_grouping.execute_round(ACT, 1, client=fake, activity=activity,
                                     find_user_by_openid=_find_user)
    assert res["code"] == "OK"
    members = _flatten_members(res)
    assert "ou_m1" not in members, "未到者必须被执行池过滤（只挡提交不够）"
    assert "ou_m2" not in members, "未签到（空值）同样排除"
    assert "ou_m0" in members and "ou_f1" in members, "已到者（迟到也算）参与"
    # 结果表落库内容同样不含被过滤者
    stored = [r["fields"]["用户open_id"] for r in fake.tables[GROUP_RESULT_TABLE]]
    assert "ou_m1" not in stored and "ou_m2" not in stored


def test_group_pool_not_filtered_when_flag_off(configured):
    """开关关闭（栏位缺失/否）＝ 全旧行为：未到者照常参与。"""
    activity = _act_group(每组男生数=1, 每组女生数=1)
    activity["fields"].pop(FLAG)          # 旧活动表形态：没有签到栏位
    signups = [_signup_att("ou_m0", "未到"), _signup_att("ou_f0", "未到")]
    selections = [_sel("ou_m0", "男性", ("ou_f0",)), _sel("ou_f0", "女性", ("ou_m0",))]
    fake = FakeBitable(_group_tables(activity, signups, selections))

    res = lib_grouping.execute_round(ACT, 1, client=fake, activity=activity,
                                     find_user_by_openid=_find_user)
    assert res["code"] == "OK"
    assert set(_flatten_members(res)) == {"ou_m0", "ou_f0"}


def test_group_pool_fail_closed_on_search_failure(configured):
    """attendance 查询失败 → SIGNIN_QUERY_FAILED，零结果写入、状态恢复。"""
    activity = _act_group()
    signups = [_signup_att("ou_m0", "按时"), _signup_att("ou_f0", "按时")]
    selections = [_sel("ou_m0", "男性", ("ou_f0",)), _sel("ou_f0", "女性", ("ou_m0",))]
    fake = FakeBitable(_group_tables(activity, signups, selections),
                       search_fails={SIGNUP_TABLE_ID})

    res = lib_grouping.execute_round(ACT, 1, client=fake, activity=activity,
                                     find_user_by_openid=_find_user)
    assert res["code"] == "SIGNIN_QUERY_FAILED"
    assert fake.tables[GROUP_RESULT_TABLE] == [], "不是放行，也不是写空结果"
    assert _find_act(fake, ACT)["fields"]["分组状态"] == "收集中"


def _oto_tables(activity, signups, selections, results=()):
    return {ACTIVITY_TABLE_ID: [activity], SIGNUP_TABLE_ID: list(signups),
            OTO_SELECT: list(selections), OTO_RESULT: list(results),
            GROUP_SELECT_TABLE: [], GROUP_RESULT_TABLE: []}


def test_onetoone_pool_filter_excludes_unchecked_in(configured):
    activity = _act_oto()
    signups = [
        _signup_att("ou_m0", "按时"), _signup_att("ou_m1", "未到"),
        _signup_att("ou_f0", "迟到"), _signup_att("ou_f1", None),
    ]
    selections = [_sel_oto("ou_m0", "男性", ("ou_f0",)),
                  _sel_oto("ou_m1", "男性", ("ou_f0",)),
                  _sel_oto("ou_f0", "女性", ("ou_m0",)),
                  _sel_oto("ou_f1", "女性", ("ou_m0",))]
    fake = FakeBitable(_oto_tables(activity, signups, selections))

    res = lib_onetoone.execute(ACT, 1, client=fake, activity=activity,
                               find_user_by_openid=_find_user)
    assert res["code"] == "OK"
    assert res["participant_count"] == 2, "只留已到的 1 男 1 女"
    oids = {r["fields"]["用户open_id"] for r in fake.tables[OTO_RESULT]}
    targets = {r["fields"]["必聊对象open_id"] for r in fake.tables[OTO_RESULT]}
    assert oids == {"ou_m0", "ou_f0"}, "未到/空值者不得出现在结果里"
    assert "ou_m1" not in targets and "ou_f1" not in targets


def test_onetoone_pool_not_filtered_when_flag_off(configured):
    activity = _act_oto()
    activity["fields"].pop(FLAG)
    signups = [_signup_att("ou_m0", None), _signup_att("ou_f0", "未到")]
    selections = [_sel_oto("ou_m0", "男性", ("ou_f0",)),
                  _sel_oto("ou_f0", "女性", ("ou_m0",))]
    fake = FakeBitable(_oto_tables(activity, signups, selections))

    res = lib_onetoone.execute(ACT, 1, client=fake, activity=activity,
                               find_user_by_openid=_find_user)
    assert res["code"] == "OK"
    assert res["participant_count"] == 2


def test_onetoone_pool_fail_closed_on_search_failure(configured):
    activity = _act_oto()
    signups = [_signup_att("ou_m0", "按时"), _signup_att("ou_f0", "按时")]
    selections = [_sel_oto("ou_m0", "男性", ("ou_f0",)),
                  _sel_oto("ou_f0", "女性", ("ou_m0",))]
    fake = FakeBitable(_oto_tables(activity, signups, selections),
                       search_fails={SIGNUP_TABLE_ID})

    res = lib_onetoone.execute(ACT, 1, client=fake, activity=activity,
                               find_user_by_openid=_find_user)
    assert res["code"] == "SIGNIN_QUERY_FAILED"
    assert fake.tables[OTO_RESULT] == []
    assert _find_act(fake, ACT)["fields"]["一对一状态"] == "收集中"


# ---------------------------------------------------------------- H5 API 脚手架

@pytest.fixture
def api(monkeypatch, tmp_path):
    import tests.test_group_admin_api as _tga
    monkeypatch.setattr(_tga, "FakeBitable", _BatchFake)
    monkeypatch.setattr(lib_grouping, "log", lambda *a, **k: None)
    monkeypatch.setattr(lib_onetoone, "log", lambda *a, **k: None)
    monkeypatch.setattr(app, "snap_find_user_by_openid", _find_user_record)
    monkeypatch.setattr(app, "account_gate", lambda oid: None)
    monkeypatch.setattr(app, "active_gate", lambda oid: None)
    monkeypatch.setattr(app, "GROUPING_JOBS_FILE", str(tmp_path / "grouping_jobs.json"))
    monkeypatch.setattr(app, "ONETOONE_JOBS_FILE", str(tmp_path / "onetoone_jobs.json"))
    lib_grouping.configure(lock_dir=str(tmp_path))
    app._rate_store.clear()
    a = Api(monkeypatch, tmp_path)

    def _search(table_id, filter_conditions=None, *args, **kwargs):
        conds = (filter_conditions or {}).get("conditions", []) \
            if isinstance(filter_conditions, dict) else (filter_conditions or [])
        return a.fake.search_records(table_id, {"conditions": conds}) or []

    monkeypatch.setattr(app.bitable, "search_records", _search)
    monkeypatch.setattr(app.bitable, "create_record",
                        lambda table_id, fields: a.fake.create_record(table_id, fields))
    monkeypatch.setattr(app.bitable, "update_record",
                        lambda table_id, rid, fields: a.fake.update_record(table_id, rid, fields))
    monkeypatch.setattr(app.bitable, "get_user_signup",
                        lambda aid, oid: next(
                            (s for s in a.fake.search_records(
                                SIGNUP_TABLE_ID, {"conditions": [
                                    {"field_name": "活动ID", "operator": "is", "value": [aid]},
                                    {"field_name": "报名人open_id", "operator": "is",
                                     "value": [oid]}]}) or []
                             if s["fields"].get("状态") != "已取消"), None))
    monkeypatch.setattr(app, "_snap_live",
                        lambda key: list(a.snapshot.get(key, [])))

    def _self_user():
        from flask import g as _g
        return USER_DIR.get(_g.get("yxq_open_id"))

    monkeypatch.setattr(app, "snap_self_user", _self_user)
    return a


def _me_client(open_id):
    c = app.app.test_client()
    c.set_cookie("yxq_session", app.create_session(open_id))
    return c


# ---------------------------------------------------------------- H5 候选/提交闸门

def test_candidates_gate_off_returns_everyone(api):
    """开关关（栏位缺失）＝ 旧行为：未到者也出现在候选池。"""
    act = _act_group()
    act["fields"].pop(FLAG)
    signups = [_signup_att("ou_m0", "按时"), _signup_att("ou_f0", "未到"),
               _signup_att("ou_f1", None)]
    api.install(activity=act, signups=signups)
    c = _me_client("ou_m0")
    r = c.get("/api/activities/" + ACT + "/group/candidates")
    assert r.status_code == 200, r.get_json()
    ids = {x["openid"] for x in r.get_json()["candidates"]}
    assert ids == {"ou_f0", "ou_f1"}


def test_candidates_gate_on_filters_pool_and_self(api):
    act = _act_group()
    signups = [_signup_att("ou_m0", "按时"), _signup_att("ou_f0", "未到"),
               _signup_att("ou_f1", "迟到")]
    api.install(activity=act, signups=signups)
    c = _me_client("ou_m0")
    r = c.get("/api/activities/" + ACT + "/group/candidates")
    assert r.status_code == 200
    ids = {x["openid"] for x in r.get_json()["candidates"]}
    assert ids == {"ou_f1"}, "候选池只留已到异性（迟到算已到）"

    # 本人未签到 → 403 SIGNIN_REQUIRED
    c1 = _me_client("ou_f0")
    r1 = c1.get("/api/activities/" + ACT + "/group/candidates")
    assert r1.status_code == 403
    assert r1.get_json()["code"] == "SIGNIN_REQUIRED"

    # 未到（显式）同一文案
    c2 = _me_client("ou_f1")
    api.fake.tables[SIGNUP_TABLE_ID][2]["fields"][ATT] = "未到"
    r2 = c2.get("/api/activities/" + ACT + "/group/candidates")
    assert r2.status_code == 403 and r2.get_json()["code"] == "SIGNIN_REQUIRED"
    assert "现场签到" in r2.get_json()["error"]


def test_group_select_gate_on_blocks_unchecked(api):
    act = _act_group()
    signups = [_signup_att("ou_m0", "未到"), _signup_att("ou_f0", "按时")]
    api.install(activity=act, signups=signups)
    c = _me_client("ou_m0")
    r = api.post if False else c.post(
        "/api/activities/" + ACT + "/group/select", json={"choices": ["ou_f0"]},
        headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 403
    assert r.get_json()["code"] == "SIGNIN_REQUIRED"
    assert api.fake.tables[GROUP_SELECT_TABLE] == [], "被挡时零写入"


def test_group_select_gate_off_allows_unchecked(api):
    act = _act_group()
    act["fields"].pop(FLAG)
    signups = [_signup_att("ou_m0", "未到"), _signup_att("ou_f0", "按时")]
    api.install(activity=act, signups=signups)
    c = _me_client("ou_m0")
    r = c.post("/api/activities/" + ACT + "/group/select", json={"choices": ["ou_f0"]},
               headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 200
    rows = api.fake.tables[GROUP_SELECT_TABLE]
    assert len(rows) == 1 and rows[0]["fields"]["选择人open_id"] == "ou_m0"


def test_gate_fail_closed_when_attendance_search_fails(api):
    act = _act_group()
    signups = [_signup_att("ou_m0", "按时"), _signup_att("ou_f0", "按时")]
    api.install(activity=act, signups=signups)
    # 报名检查（带报名人open_id）照常可读；attendance 的宽查询失败
    _fail_broad_signup_search(api.fake)
    c = _me_client("ou_m0")
    r = c.get("/api/activities/" + ACT + "/group/candidates")
    assert r.status_code == 503
    assert r.get_json()["code"] == "SIGNIN_QUERY_FAILED"
    r2 = c.post("/api/activities/" + ACT + "/group/select", json={"choices": ["ou_f0"]},
                headers={"X-Requested-With": "XMLHttpRequest"})
    assert r2.status_code == 503
    assert r2.get_json()["code"] == "SIGNIN_QUERY_FAILED"


def test_onetoone_select_gate_and_pool(api):
    act = _act_oto()
    signups = [_signup_att("ou_m0", "按时"), _signup_att("ou_f0", "按时"),
               _signup_att("ou_f1", "未到")]
    api.install(activity=act, signups=signups)
    c = _me_client("ou_m0")
    # 已到目标可提交
    r = c.post("/api/activities/" + ACT + "/onetoone/select", json={"choices": ["ou_f0"]},
               headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 200
    # 未到目标不在候选池
    r2 = c.post("/api/activities/" + ACT + "/onetoone/select", json={"choices": ["ou_f1"]},
                headers={"X-Requested-With": "XMLHttpRequest"})
    assert r2.status_code == 400 and "可选列表" in r2.get_json()["error"]
    # 本人未签到 → 403
    c1 = _me_client("ou_f1")
    r3 = c1.post("/api/activities/" + ACT + "/onetoone/select", json={"choices": ["ou_m0"]},
                 headers={"X-Requested-With": "XMLHttpRequest"})
    assert r3.status_code == 403 and r3.get_json()["code"] == "SIGNIN_REQUIRED"


# ---------------------------------------------------------------- 用户侧三态

def test_activity_detail_my_attendance_three_states(api):
    act = _act_group()
    api.install(activity=act, signups=[_signup_att("ou_m0", "按时")])
    c = _me_client("ou_m0")
    r = c.get("/api/activities/" + ACT)
    assert r.status_code == 200
    body = r.get_json()
    assert body["signin_enabled"] is True and body["my_attendance"] == "按时"

    # 未到
    api.fake.tables[SIGNUP_TABLE_ID][0]["fields"][ATT] = "未到"
    body = c.get("/api/activities/" + ACT).get_json()
    assert body["my_attendance"] == "未到"
    # 未签到（空）
    api.fake.tables[SIGNUP_TABLE_ID][0]["fields"][ATT] = ""
    body = c.get("/api/activities/" + ACT).get_json()
    assert body["my_attendance"] == ""
    # 开关关：signin_enabled=False（三态不显示）
    act2 = _act_group()
    act2["fields"].pop(FLAG)
    api.install(activity=act2, signups=[_signup_att("ou_m0", "按时")])
    body = c.get("/api/activities/" + ACT).get_json()
    assert body["signin_enabled"] is False


def test_my_groups_carries_my_attendance(api, monkeypatch):
    act = _act_group()
    signup = _signup_att("ou_m0", "未到")
    api.install(activity=act, signups=[signup])
    monkeypatch.setattr(app, "snap_active_signups_by_openid", lambda oid: [signup])
    monkeypatch.setattr(app, "snap_group_selections_by_selector", lambda oid: [])
    c = _me_client("ou_m0")
    r = c.get("/api/activities/mine/groups")
    assert r.status_code == 200
    items = r.get_json()["activities"]
    assert len(items) == 1
    assert items[0]["signin_enabled"] is True and items[0]["my_attendance"] == "未到"

    # 一对一列表同口径
    act_oto = _act_oto()
    api.install(activity=act_oto, signups=[signup])
    monkeypatch.setattr(app, "snap_active_signups_by_openid", lambda oid: [signup])
    monkeypatch.setattr(app, "snap_onetoone_selections_by_selector", lambda oid: [])
    r2 = c.get("/api/activities/mine/onetoone")
    items2 = r2.get_json()["activities"]
    assert len(items2) == 1
    assert items2[0]["signin_enabled"] is True and items2[0]["my_attendance"] == "未到"


def test_my_activities_carries_signin_state(api):
    """「我的活动」卡片与「活动」页一致：带 signin_enabled / my_attendance，且活动状态实时读。"""
    act = _act_group()
    api.install(activity=act, signups=[_signup_att("ou_m0", "按时")])
    c = _me_client("ou_m0")
    r = c.get("/api/activities/mine")
    assert r.status_code == 200, r.get_json()
    items = r.get_json()["activities"]
    assert len(items) == 1
    assert items[0]["signin_enabled"] is True and items[0]["my_attendance"] == "按时"
    assert items[0]["group_flag"] == "否", "「我的活动」也要带模式 flag（关闭全部后隐藏入口）"
    assert "activities" in api.fresh_calls, "活动状态必须实时读（bot 写入，快照会过期）"


def _first_activity(r):
    assert r.status_code == 200, r.get_json()
    return r.get_json()["activities"][0]


def test_activities_list_carries_my_attendance(api):
    """活动列表（首页/活动页卡片）也要带现场签到三态：已报名+收集中才查签到表。"""
    act = _act_group()
    api.install(activity=act, signups=[_signup_att("ou_m0", "按时")])
    c = _me_client("ou_m0")
    item = _first_activity(c.get("/api/activities"))
    assert item["my_signup"] is True
    assert item["signin_enabled"] is True and item["my_attendance"] == "按时"


def test_activities_list_attendance_empty_when_not_signed_up(api):
    """未报名的活动不查签到表：my_attendance 恒为空串（卡片入口也不显示）。"""
    act = _act_group()
    api.install(activity=act, signups=[_signup_att("ou_f0", "按时")])
    c = _me_client("ou_m0")
    item = _first_activity(c.get("/api/activities"))
    assert item["my_signup"] is False
    assert item["signin_enabled"] is True and item["my_attendance"] == ""


def test_activities_list_attendance_carried_when_not_collecting(api):
    """志愿随时可改（2026-10-13）：非「收集中」状态也照常带签到态（卡片闸门判断用）。"""
    act = _act_group(分组状态="未开始")
    api.install(activity=act, signups=[_signup_att("ou_m0", "按时")])
    c = _me_client("ou_m0")
    item = _first_activity(c.get("/api/activities"))
    assert item["my_signup"] is True
    assert item["signin_enabled"] is True and item["my_attendance"] == "按时"


def test_activities_list_carries_mode_flags(api):
    """卡片入口由模式开关控制（「关闭全部」后前端整块隐藏）：列表必须带两个 flag。"""
    act = _act_group()  # 默认「分组功能开启=否」（= 关闭全部后的状态）
    api.install(activity=act, signups=[_signup_att("ou_m0", "按时")])
    c = _me_client("ou_m0")
    item = _first_activity(c.get("/api/activities"))
    assert item["group_flag"] == "否" and item.get("onetoone_flag", "") != "是"

    act2 = _act_group(分组功能开启="是")
    api.install(activity=act2, signups=[_signup_att("ou_m0", "按时")])
    item2 = _first_activity(c.get("/api/activities"))
    assert item2["group_flag"] == "是"


def test_group_select_allowed_when_not_collecting(api):
    """志愿随时可改：状态非「收集中」也能提交（2026-10-13），两个快照键都刷。"""
    act = _act_group(分组状态="已完成")
    signups = [_signup_att("ou_m0", "按时"), _signup_att("ou_f0", "按时")]
    api.install(activity=act, signups=signups)
    c = _me_client("ou_m0")
    r = c.post("/api/activities/" + ACT + "/group/select", json={"choices": ["ou_f0"]},
               headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 200, r.get_json()
    rows = api.fake.tables[GROUP_SELECT_TABLE]
    assert len(rows) == 1 and rows[0]["fields"]["选择人open_id"] == "ou_m0"
    assert set(api.refresh_calls) >= {"group_selections", "onetoone_selections"}


def test_group_candidates_allowed_when_not_collecting(api):
    """志愿随时可改：非「收集中」状态候选列表也能打开。"""
    act = _act_group(分组状态="未开始")
    signups = [_signup_att("ou_m0", "按时"), _signup_att("ou_f0", "按时")]
    api.install(activity=act, signups=signups)
    c = _me_client("ou_m0")
    r = c.get("/api/activities/" + ACT + "/group/candidates")
    assert r.status_code == 200, r.get_json()
    assert {x["openid"] for x in r.get_json()["candidates"]} == {"ou_f0"}


def test_onetoone_select_allowed_when_not_collecting(api):
    """志愿随时可改：一对一状态非「收集中」也能提交。"""
    act = _act_oto(一对一状态="已完成")
    signups = [_signup_att("ou_m0", "按时"), _signup_att("ou_f0", "按时")]
    api.install(activity=act, signups=signups)
    c = _me_client("ou_m0")
    r = c.post("/api/activities/" + ACT + "/onetoone/select", json={"choices": ["ou_f0"]},
               headers={"X-Requested-With": "XMLHttpRequest"})
    assert r.status_code == 200, r.get_json()


def test_activities_list_fail_closed_on_attendance_error(api):
    """签到查询失败 → my_attendance 回空串（按未签到处理），列表不报错。"""
    act = _act_group()
    api.install(activity=act, signups=[_signup_att("ou_m0", "按时")])
    _fail_broad_signup_search(api.fake)
    c = _me_client("ou_m0")
    item = _first_activity(c.get("/api/activities"))
    assert item["signin_enabled"] is True and item["my_attendance"] == ""


# ---------------------------------------------------------------- 管理端点：名册

def _admin_signin_url(aid=ACT):
    return "/api/admin/activity/signin?activity_id=" + aid


def test_admin_signin_roster_fresh_and_counts(api):
    act = _act_group()
    signups = [
        _signup_att("ou_m0", "按时", "小北"),
        _signup_att("ou_m1", "迟到", "阿北"),
        _signup_att("ou_f0", "未到", "阿南"),
        _signup_att("ou_f1", None, "小西"),
        _signup_att("ou_f2", "", "小东", status="已取消"),   # 已取消不入名册
    ]
    api.install(activity=act, signups=signups)
    r = api.get(_admin_signin_url())
    assert r.status_code == 200
    body = r.get_json()
    assert body["signin_enabled"] is True
    assert body["stats"] == {"checked_in": 2, "signed_up": 4, "not_checked_in": 2}
    by_oid = {x["open_id"]: x for x in body["roster"]}
    assert set(by_oid) == {"ou_m0", "ou_m1", "ou_f0", "ou_f1"}, "已取消不入名册"
    assert by_oid["ou_m0"]["checked_in"] is True and by_oid["ou_m0"]["state"] == "按时"
    assert by_oid["ou_m1"]["checked_in"] is True, "迟到算已到（D13）"
    assert by_oid["ou_f0"]["checked_in"] is False
    assert by_oid["ou_m0"]["user_id"] == "U-0001" and by_oid["ou_m0"]["gender"] == "男性"

    # fresh：原件变化后直接可见（不走快照）
    api.fake.tables[SIGNUP_TABLE_ID][3]["fields"][ATT] = "按时"
    body2 = api.get(_admin_signin_url()).get_json()
    assert body2["stats"]["checked_in"] == 3


def test_admin_signin_roster_includes_real_name(api, monkeypatch):
    """名册带真实姓名：现场核对身份用；取自已有用户档案（零额外查询），无档案回空串。

    前端搜索有 real_name 匹配分支（无自动化测试），这里锁定后端字段契约。
    """
    monkeypatch.setitem(USER_DIR["ou_m0"]["fields"], "姓名", "陈小北")
    api.install(activity=_act_group(),
                signups=[_signup_att("ou_m0", None, "小北"),
                         _signup_att("ou_f0", "按时", "阿南")])
    body = api.get(_admin_signin_url()).get_json()
    by_oid = {x["open_id"]: x for x in body["roster"]}
    assert by_oid["ou_m0"]["real_name"] == "陈小北"
    assert by_oid["ou_m0"]["user_id"] == "U-0001"      # 既有字段不受影响
    assert by_oid["ou_m0"]["nickname"] == "小北"
    assert by_oid["ou_f0"]["real_name"] == ""          # 档案无姓名 → 空串（不报错）


def test_admin_signin_roster_search_failure_500(api):
    api.install(activity=_act_group(), signups=[_signup_att("ou_m0", "按时")])
    api.fake.search_fails = {SIGNUP_TABLE_ID}
    r = api.get(_admin_signin_url())
    assert r.status_code == 500 and r.get_json()["code"] == "SIGNIN_QUERY_FAILED"


# ---------------------------------------------------------------- 管理端点：单笔/批次

def test_admin_signin_single_write(api):
    api.install(activity=_act_group(),
                signups=[_signup_att("ou_m0", None), _signup_att("ou_f0", "未到")])
    r = api.post("/api/admin/activity/signin",
                 {"activity_id": ACT, "open_id": "ou_m0", "state": "按时"})
    assert r.status_code == 200 and r.get_json()["state"] == "按时"
    row = [x for x in api.fake.tables[SIGNUP_TABLE_ID]
           if x["fields"]["报名人open_id"] == "ou_m0"][0]
    assert row["fields"][ATT] == "按时"

    # UI 两态之外的「迟到」显式拒绝（D13；历史值仍按已到判定）
    r2 = api.post("/api/admin/activity/signin",
                  {"activity_id": ACT, "open_id": "ou_m0", "state": "迟到"})
    assert r2.status_code == 400
    r3 = api.post("/api/admin/activity/signin",
                  {"activity_id": ACT, "open_id": "ou_m0", "state": "别的"})
    assert r3.status_code == 400
    # 没报名 404；多条报名 400
    r4 = api.post("/api/admin/activity/signin",
                  {"activity_id": ACT, "open_id": "ou_f2", "state": "按时"})
    assert r4.status_code == 404
    api.fake.tables[SIGNUP_TABLE_ID].append(_signup_att("ou_m0", "按时", "重复行"))
    r5 = api.post("/api/admin/activity/signin",
                  {"activity_id": ACT, "open_id": "ou_m0", "state": "按时"})
    assert r5.status_code == 400 and "多条" in r5.get_json()["error"]


def test_admin_signin_single_write_failure_500(api, monkeypatch):
    api.install(activity=_act_group(), signups=[_signup_att("ou_m0", None)])
    monkeypatch.setattr(api.fake, "update_record", lambda *a, **k: None)
    r = api.post("/api/admin/activity/signin",
                 {"activity_id": ACT, "open_id": "ou_m0", "state": "按时"})
    assert r.status_code == 500


def test_admin_signin_batch_fills_only_empty(api):
    act = _act_group()
    signups = [
        _signup_att("ou_m0", "按时"), _signup_att("ou_m1", "未到"),
        _signup_att("ou_f0", None), _signup_att("ou_f1", ""),
        _signup_att("ou_f2", None, status="已取消"),
    ]
    api.install(activity=act, signups=signups)
    r = api.post("/api/admin/activity/signin", {"activity_id": ACT, "all_pending": True})
    assert r.status_code == 200
    body = r.get_json()
    assert body["updated"] == 2 and body["skipped"] == 2 and body["failed"] == 0
    by_oid = {x["fields"]["报名人open_id"]: x["fields"].get(ATT)
              for x in api.fake.tables[SIGNUP_TABLE_ID]}
    assert by_oid["ou_m0"] == "按时", "已标记的不得被覆盖"
    assert by_oid["ou_m1"] == "未到", "未到不得被批次改写"
    assert by_oid["ou_f0"] == "按时" and by_oid["ou_f1"] == "按时"
    assert by_oid["ou_f2"] is None, "已取消不参与批次"


def test_admin_signin_batch_search_failure_500(api):
    api.install(activity=_act_group(), signups=[_signup_att("ou_m0", None)])
    api.fake.search_fails = {SIGNUP_TABLE_ID}
    r = api.post("/api/admin/activity/signin", {"activity_id": ACT, "all_pending": True})
    assert r.status_code == 500 and r.get_json()["code"] == "SIGNIN_QUERY_FAILED"


def test_admin_signin_clear_all_clears_marked(api):
    """clear_all：只清非空的「签到状态」，空值/已取消记录不动，返回值如实。"""
    signups = [
        _signup_att("ou_m0", "按时"), _signup_att("ou_m1", "未到"),
        _signup_att("ou_f0", "迟到"), _signup_att("ou_f1", None),
        _signup_att("ou_f2", ""), _signup_att("ou_f3", "按时", status="已取消"),
    ]
    api.install(activity=_act_group(), signups=signups)
    r = api.post("/api/admin/activity/signin", {"activity_id": ACT, "clear_all": True})
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "activity_id": ACT,
                            "cleared": 3, "failed": 0, "total": 5}
    by_oid = {x["fields"]["报名人open_id"]: x["fields"].get(ATT)
              for x in api.fake.tables[SIGNUP_TABLE_ID]}
    assert by_oid["ou_m0"] == "" and by_oid["ou_m1"] == "" and by_oid["ou_f0"] == ""
    assert by_oid["ou_f1"] is None and by_oid["ou_f2"] in (None, "")
    assert by_oid["ou_f3"] == "按时", "已取消记录不参与清除"
    assert api.refresh_calls == ["signups"]


def test_admin_signin_clear_all_reports_partial_failure(api, monkeypatch):
    """batch_update 少写多少就报多少 failed，不谎报全清。"""
    api.install(activity=_act_group(),
                signups=[_signup_att("ou_m0", "按时"), _signup_att("ou_m1", "未到")])
    monkeypatch.setattr(api.fake, "batch_update_records", lambda *a, **k: 1)
    r = api.post("/api/admin/activity/signin", {"activity_id": ACT, "clear_all": True})
    assert r.status_code == 200
    body = r.get_json()
    assert body["cleared"] == 1 and body["failed"] == 1 and body["total"] == 2


def test_admin_signin_clear_all_total_failure(api, monkeypatch):
    api.install(activity=_act_group(), signups=[_signup_att("ou_m0", "按时")])
    monkeypatch.setattr(api.fake, "batch_update_records", lambda *a, **k: 0)
    r = api.post("/api/admin/activity/signin", {"activity_id": ACT, "clear_all": True})
    body = r.get_json()
    assert body["cleared"] == 0 and body["failed"] == 1
    assert api.refresh_calls == [], "全失败不刷新快照"


def test_admin_signin_clear_all_nothing_marked_no_write(api):
    api.install(activity=_act_group(), signups=[_signup_att("ou_m0", None)])
    r = api.post("/api/admin/activity/signin", {"activity_id": ACT, "clear_all": True})
    assert r.get_json() == {"ok": True, "activity_id": ACT,
                            "cleared": 0, "failed": 0, "total": 1}
    assert api.fake.writes() == [] and api.refresh_calls == []


def test_admin_signin_clear_all_search_failure_500(api):
    api.install(activity=_act_group(), signups=[_signup_att("ou_m0", "按时")])
    api.fake.search_fails = {SIGNUP_TABLE_ID}
    r = api.post("/api/admin/activity/signin", {"activity_id": ACT, "clear_all": True})
    assert r.status_code == 500 and r.get_json()["code"] == "SIGNIN_QUERY_FAILED"


def test_admin_signin_flag_writes_and_idempotent(api):
    fake = api.install(activity=_act_group())
    r = api.post("/api/admin/activity/signin/flag", {"activity_id": ACT, "enabled": True})
    assert r.status_code == 200 and r.get_json()["signin_flag"] == "是"
    assert fake.writes() == [], "已是目标值不重写"

    r = api.post("/api/admin/activity/signin/flag", {"activity_id": ACT, "enabled": False})
    assert r.status_code == 200 and r.get_json()["signin_flag"] == "否"
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert len(updates) == 1 and updates[0][3] == {FLAG: "否"}
    assert api.refresh_calls == ["activities"]
    # 同步桩快照模拟 fresh 读到新值 → 幂等
    api.snapshot["activities"][0]["fields"][FLAG] = "否"
    api.post("/api/admin/activity/signin/flag", {"activity_id": ACT, "enabled": False})
    assert len([c for c in fake.writes() if c[0] == "update"]) == 1


def test_admin_signin_flag_write_failure_500(api, monkeypatch):
    api.install(activity=_act_group(**{FLAG: "否"}))
    monkeypatch.setattr(api.fake, "update_record", lambda *a, **k: None)
    r = api.post("/api/admin/activity/signin/flag", {"activity_id": ACT, "enabled": True})
    assert r.status_code == 500 and r.get_json()["code"] == "WRITE_FAILED"


def test_admin_signin_payload_validation(api):
    api.install(activity=_act_group())
    assert api.get("/api/admin/activity/signin").status_code == 400
    assert api.get(_admin_signin_url("A-9999")).status_code == 404
    assert api.post("/api/admin/activity/signin", {}).status_code == 400
    assert api.post("/api/admin/activity/signin/flag",
                    {"activity_id": ACT, "enabled": "yes"}).status_code == 400


# ---------------------------------------------------------------- bot 卡片提交闸门

def _fail_broad_signup_search(fake):
    """让报名表的「宽查询」（不带报名人open_id，即 attendance_map 的形状）返回 None。

    bot 的报名检查与 attendance 查询都打同一张表；直接 search_fails 会先把报名
    检查打死（变成「你未报名」），测不到 fail-closed 分支。这里只按条件形状区分。
    """
    real = fake.search_records

    def flaky(table_id, filter_conditions=None, *a, **k):
        conds = (filter_conditions or {}).get("conditions", []) \
            if isinstance(filter_conditions, dict) else (filter_conditions or [])
        names = [c.get("field_name") for c in conds]
        if table_id == SIGNUP_TABLE_ID and "报名人open_id" not in names:
            return None
        return real(table_id, filter_conditions, *a, **k)

    fake.search_records = flaky


def _patch_bot_grouping(monkeypatch, fake):
    monkeypatch.setattr(bot_grouping, "bitable", fake)
    monkeypatch.setattr(bot_grouping, "search_records",
                        lambda *a, **k: fake.search_records(*a, **k) or [])
    monkeypatch.setattr(bot_grouping, "update_record", fake.update_record)
    monkeypatch.setattr(bot_grouping, "create_record", fake.create_record)
    monkeypatch.setattr(bot_grouping, "find_activity_by_id",
                        lambda aid: _find_act(fake, aid))
    monkeypatch.setattr(bot_grouping, "find_user_by_openid", _find_user)
    monkeypatch.setattr(bot_grouping, "find_user_by_id_or_name", _find_user_by_id_or_name)


def _patch_bot_onetoone(monkeypatch, fake):
    monkeypatch.setattr(bot_onetoone, "bitable", fake)
    monkeypatch.setattr(bot_onetoone, "search_records",
                        lambda *a, **k: fake.search_records(*a, **k) or [])
    monkeypatch.setattr(bot_onetoone, "find_activity_by_id",
                        lambda aid: _find_act(fake, aid))
    monkeypatch.setattr(bot_onetoone, "find_user_by_openid", _find_user)
    monkeypatch.setattr(bot_onetoone, "find_user_by_id_or_name", _find_user_by_id_or_name)


def test_bot_group_submit_gate(configured):
    activity = _act_group()
    signups = [_signup_att("ou_m0", "未到"), _signup_att("ou_f0", "按时")]
    selections = []
    fake = FakeBitable(_group_tables(activity, signups, selections))
    _patch_bot_grouping(configured, fake)

    res = bot_grouping.handle_group_submit(
        "ou_m0", {"activity_id": ACT, "selections": ["ou_f0"]}, None)
    assert res["toast"]["type"] == "error" and "现场签到" in res["toast"]["content"]
    assert fake.writes() == [], "被挡时零写入"

    # 查询失败 fail-closed：只让 attendance 的宽查询失败（报名人 check 仍可读）
    _fail_broad_signup_search(fake)
    res2 = bot_grouping.handle_group_submit(
        "ou_m0", {"activity_id": ACT, "selections": ["ou_f0"]}, None)
    assert "签到信息查询失败" in res2["toast"]["content"]


def test_bot_group_submit_pool_filters_targets(configured):
    activity = _act_group()
    signups = [
        _signup_att("ou_m0", "按时"),
        _signup_att("ou_f0", "按时"), _signup_att("ou_f1", "未到"),
        _signup_att("ou_f2", "按时"), _signup_att("ou_f3", "按时"),
        _signup_att("ou_f4", "按时"), _signup_att("ou_f5", "按时"),
        _signup_att("ou_f6", "按时"),
    ]
    fake = FakeBitable(_group_tables(activity, signups, []))
    _patch_bot_grouping(configured, fake)

    # 恰好 7 位（bot 分组提交口径），f1 未到 → 必须被候选池过滤
    choices = ["ou_f0", "ou_f2", "ou_f3", "ou_f4", "ou_f5", "ou_f6", "ou_f1"]
    res = bot_grouping.handle_group_submit(
        "ou_m0", {"activity_id": ACT, "selections": choices}, None)
    assert "无效参与者" in res["toast"]["content"]
    assert fake.writes() == []


def test_bot_onetoone_submit_gate(configured):
    activity = _act_oto()
    signups = [_signup_att("ou_m0", None), _signup_att("ou_f0", "按时")]
    fake = FakeBitable(_oto_tables(activity, signups, []))
    _patch_bot_onetoone(configured, fake)

    res = bot_onetoone.handle_onetoone_submit(
        "ou_m0", {"activity_id": ACT, "selections": ["ou_f0"]}, None)
    assert res["toast"]["type"] == "error" and "现场签到" in res["toast"]["content"]
    assert fake.writes() == []

    _fail_broad_signup_search(fake)
    res2 = bot_onetoone.handle_onetoone_submit(
        "ou_m0", {"activity_id": ACT, "selections": ["ou_f0"]}, None)
    assert "签到信息查询失败" in res2["toast"]["content"]


def test_bot_onetoone_submit_pool_filters_targets(configured):
    activity = _act_oto()
    signups = [_signup_att("ou_m0", "按时"), _signup_att("ou_f1", "未到")]
    fake = FakeBitable(_oto_tables(activity, signups, []))
    _patch_bot_onetoone(configured, fake)

    res = bot_onetoone.handle_onetoone_submit(
        "ou_m0", {"activity_id": ACT, "selections": ["ou_f1"]}, None)
    assert "无效参与者" in res["toast"]["content"]
    assert fake.writes() == []
