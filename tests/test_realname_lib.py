"""「真名+编号」显示链路与「签到默认打开」的 lib/渲染层单测（不联网，桩表驱动）。

现场活动统一显示「姓名（缺省昵称）＋编号」；边界的昵称语义不动（他人资料页/
留言/点赞/公开引流页/报名通知等仍用昵称）。本文件聚焦本次改动：

- collect_participants / build_participants 读用户表「姓名」（去空白非空才生效）；
- execute_round / execute 落库与回传 display_name 的真名回退＋user_id；
- set_mode（mode≠none）/ set_group_flag（开启方向）的「签到功能默认打开」：
  空→是（同笔写）、已是 是/否→不动、栏位未配置→不写；关闭路径不补默认；
- find_user_by_id_or_name 的「姓名」兜底（不改既有 uid/昵称命中行为）；
- 「查看未提交」回覆与选择卡 label 读真名；
- 管理员「签到」指令回覆统一「真名（用户ID）」。

桩的是「存取」（复用 parity 的 FakeBitable），规则全跑真实现。
"""
import grouping as bot_grouping
import onetoone as bot_onetoone
import points_admin
import pytest
import queries
from constants import (
    ACTIVITY_TABLE_ID,
    GROUP_RESULT_TABLE,
    GROUP_SELECT_TABLE,
    SIGNUP_TABLE_ID,
)

from lib import grouping as lib_grouping
from lib import onetoone as lib_onetoone
from tests.test_grouping_parity import FakeBitable as GroupingFake
from tests.test_grouping_parity import _act, _sel, _signup
from tests.test_onetoone_parity import FakeBitable as OnetooneFake

ACT = "A-0002"
# 一对一那两张表在 CI 缺省为空，钉一组测试表 ID（镜像 test_onetoone_parity 的做法）
OTO_SELECT = "tbl_realname_oto_select"
OTO_RESULT = "tbl_realname_oto_result"


# ---------------------------------------------------------------- 桩数据

def _user(uid, gender, nickname, real_name=None):
    fields = {"用户ID": uid, "性别": gender, "昵称": nickname}
    if real_name is not None:
        fields["姓名"] = real_name
    return {"record_id": f"rec_u_{uid}", "fields": fields}


USERS = {
    "ou_m1": _user("U-0001", "男性", "小北", "陈小北"),
    "ou_m2": _user("U-0002", "男性", "阿北", "   "),    # 「姓名」只有空白 → 回退昵称
    "ou_f1": _user("U-0003", "女性", "阿南", "林阿南"),
    "ou_f2": _user("U-0004", "女性", "小西", "周小西"),
}

# 落库/回传期望：真名去空白非空才用，否则回退昵称
EXPECTED = {"ou_m1": "陈小北", "ou_m2": "阿北", "ou_f1": "林阿南", "ou_f2": "周小西"}


def _find_user(oid):
    rec = USERS.get(oid)
    return [] if rec is None else [{"record_id": rec["record_id"], "fields": dict(rec["fields"])}]


def _find_act(fake, activity_id):
    for r in fake.tables.get(ACTIVITY_TABLE_ID, []):
        if r["fields"].get("活动ID") == activity_id:
            return r
    return None


@pytest.fixture
def configured(monkeypatch, tmp_path):
    """注入表 ID / 栏位名 / 锁目录（走 bot 的同一个入口）。"""
    monkeypatch.setattr(lib_grouping, "log", lambda *a, **k: None)
    monkeypatch.setattr(lib_onetoone, "log", lambda *a, **k: None)
    bot_grouping.configure_grouping_lib(str(tmp_path))
    bot_onetoone.configure_onetoone_lib(str(tmp_path))
    return monkeypatch


@pytest.fixture
def configured_oto(configured):
    """一对一的两张测试表在 CI 缺省为空，同样从 bot 入口注入。"""
    configured.setattr(bot_onetoone, "ONETOONE_SELECT_TABLE", OTO_SELECT)
    configured.setattr(bot_onetoone, "ONETOONE_RESULT_TABLE", OTO_RESULT)
    bot_onetoone.configure_onetoone_lib(None)
    return configured


def _g_tables():
    return {
        ACTIVITY_TABLE_ID: [_act()],
        SIGNUP_TABLE_ID: [_signup(o, USERS[o]["fields"]["昵称"]) for o in USERS],
        GROUP_SELECT_TABLE: [
            _sel("ou_m1", "男性", ("ou_f1",)), _sel("ou_m2", "男性", ("ou_f2",)),
            _sel("ou_f1", "女性", ("ou_m1",)), _sel("ou_f2", "女性", ("ou_m2",)),
        ],
        GROUP_RESULT_TABLE: [],
    }


def _oto_sel(oid, gender):
    fields = {"活动ID": ACT, "选择人open_id": oid, "选择人性别": gender}
    for i in range(1, 8):
        fields[f"第{i}志愿"] = ""
    return {"record_id": f"rec_oto_{oid}", "fields": fields}


def _oto_tables():
    return {
        ACTIVITY_TABLE_ID: [{"record_id": "rec_act",
                             "fields": {"活动ID": ACT, "活动名称": "关系自洽",
                                        "一对一状态": "收集中"}}],
        SIGNUP_TABLE_ID: [_signup(o, USERS[o]["fields"]["昵称"]) for o in USERS],
        OTO_SELECT: [
            _oto_sel("ou_m1", "男性"), _oto_sel("ou_m2", "男性"),
            _oto_sel("ou_f1", "女性"), _oto_sel("ou_f2", "女性"),
        ],
        OTO_RESULT: [],
    }


# ---------------------------------------------------------------- ① 落库真名（分组）

def test_grouping_execute_round_stores_real_name_and_falls_back_nickname(configured):
    fake = GroupingFake(_g_tables())

    res = lib_grouping.execute_round(ACT, 1, client=fake, activity=_find_act(fake, ACT),
                                     find_user_by_openid=_find_user,
                                     males_per_group=2, females_per_group=2)

    assert res["code"] == "OK"
    stored = {r["fields"]["用户open_id"]: r["fields"]["用户昵称"]
              for r in fake.tables[GROUP_RESULT_TABLE]}
    assert stored == EXPECTED, "姓名去空白非空落真名，否则回退报名昵称"
    members = {m["open_id"]: m for g in res["groups"] for m in g["members"]}
    assert members["ou_m1"]["display_name"] == "陈小北"
    assert members["ou_m1"]["user_id"] == "U-0001"
    assert members["ou_m2"]["display_name"] == "阿北", "空白姓名回退昵称"
    assert members["ou_f1"]["user_id"] == "U-0003"


def test_grouping_execute_round_member_falls_back_to_oid_when_name_missing(configured):
    """报名人不在用户表（或昵称也空）时 display_name 仍退回 open_id（旧兜底不动）。"""
    tables = _g_tables()
    tables[SIGNUP_TABLE_ID] = [
        _signup("ou_m1", "小北"), _signup("ou_m2", "阿北"),
        _signup("ou_f1", "阿南"), _signup("ou_f2", ""),
    ]
    fake = GroupingFake(tables)

    res = lib_grouping.execute_round(ACT, 1, client=fake, activity=_find_act(fake, ACT),
                                     find_user_by_openid=lambda oid: (
                                         [] if oid == "ou_f2" else _find_user(oid)),
                                     males_per_group=2, females_per_group=2)

    members = {m["open_id"]: m for g in res["groups"] for m in g["members"]}
    assert members["ou_f2"]["display_name"] == "ou_f2"
    assert members["ou_f2"]["user_id"] == ""


# ---------------------------------------------------------------- ② 落库真名（一对一）

def test_onetoone_execute_stores_real_name_for_user_and_target(configured_oto):
    fake = OnetooneFake(_oto_tables())

    res = lib_onetoone.execute(ACT, 1, client=fake, activity=_find_act(fake, ACT),
                               find_user_by_openid=_find_user, female_chats=2)

    assert res["code"] == "OK"
    rows = fake.tables[OTO_RESULT]
    assert rows, "2男2女 K=2 必须有结果行"
    assert all(r["fields"]["用户昵称"] == EXPECTED[r["fields"]["用户open_id"]] for r in rows)
    assert all(r["fields"]["必聊对象昵称"] == EXPECTED[r["fields"]["必聊对象open_id"]]
               for r in rows)


# ---------------------------------------------------------------- ③ 签到默认（开启功能路径）

def test_set_group_flag_signin_default_matrix(configured):
    """开启分组功能：空 → 同笔写「是」；已是 是/否 → 不动；关闭方向不补默认。"""
    fake = GroupingFake(_g_tables())
    res = lib_grouping.set_group_flag(ACT, True, client=fake, activity=_find_act(fake, ACT))
    assert res["code"] == "OK"
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert len(updates) == 1
    assert updates[0][3] == {"分组功能开启": "是", "签到功能开启": "是"}, \
        "签到默认必须与 flag 在同一笔 update"
    assert _find_act(fake, ACT)["fields"]["签到功能开启"] == "是"

    for value in ("是", "否"):
        tables = _g_tables()
        tables[ACTIVITY_TABLE_ID] = [_act(签到功能开启=value)]
        fake2 = GroupingFake(tables)
        lib_grouping.set_group_flag(ACT, True, client=fake2, activity=_find_act(fake2, ACT))
        assert _find_act(fake2, ACT)["fields"]["签到功能开启"] == value, "已配置值不得被动"
        updates2 = [c for c in fake2.writes() if c[0] == "update"]
        assert updates2[0][3] == {"分组功能开启": "是"}

    # 关闭方向：只写自身，不补签到默认（即使签到为空）
    tables = _g_tables()
    tables[ACTIVITY_TABLE_ID] = [_act(分组功能开启="是")]
    fake3 = GroupingFake(tables)
    lib_grouping.set_group_flag(ACT, False, client=fake3, activity=_find_act(fake3, ACT))
    updates3 = [c for c in fake3.writes() if c[0] == "update"]
    assert updates3[0][3] == {"分组功能开启": "否"}
    assert "签到功能开启" not in _find_act(fake3, ACT)["fields"]


def test_set_mode_signin_default_matrix(configured):
    """开启任一侧功能（mode≠none）：同笔补签到默认；mode=none 不补。"""
    # mode=group：空签到 → 补「是」
    fake = GroupingFake(_g_tables())
    res = lib_grouping.set_mode(ACT, "group", client=fake, activity=_find_act(fake, ACT))
    assert res["code"] == "OK"
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert updates[0][3] == {"分组功能开启": "是", "一对一功能开启": "否", "签到功能开启": "是"}

    # mode=onetoone：空签到 → 补「是」
    fake2 = GroupingFake(_g_tables())
    res2 = lib_grouping.set_mode(ACT, "onetoone", client=fake2, activity=_find_act(fake2, ACT))
    assert res2["code"] == "OK"
    updates2 = [c for c in fake2.writes() if c[0] == "update"]
    assert updates2[0][3] == {"分组功能开启": "否", "一对一功能开启": "是", "签到功能开启": "是"}

    # 已是 是/否 → 不动
    for value in ("是", "否"):
        tables = _g_tables()
        tables[ACTIVITY_TABLE_ID] = [_act(签到功能开启=value)]
        fake3 = GroupingFake(tables)
        lib_grouping.set_mode(ACT, "group", client=fake3, activity=_find_act(fake3, ACT))
        assert _find_act(fake3, ACT)["fields"]["签到功能开启"] == value

    # mode=none：不补签到默认
    tables = _g_tables()
    tables[ACTIVITY_TABLE_ID] = [_act(一对一功能开启="是")]
    fake4 = GroupingFake(tables)
    res4 = lib_grouping.set_mode(ACT, "none", client=fake4, activity=_find_act(fake4, ACT))
    assert res4["code"] == "OK"
    updates4 = [c for c in fake4.writes() if c[0] == "update"]
    assert updates4[0][3] == {"分组功能开启": "否", "一对一功能开启": "否"}
    assert "签到功能开启" not in _find_act(fake4, ACT)["fields"]


def _unconfigure_signin(monkeypatch):
    """模拟未注入 signin_flag 的旧配置：_cfg 与必填名单同时摘掉该键。"""
    fields = dict(lib_grouping._cfg["fields"])
    fields.pop("signin_flag", None)
    monkeypatch.setitem(lib_grouping._cfg, "fields", fields)
    monkeypatch.setattr(
        lib_grouping, "_REQUIRED_FIELDS",
        tuple(k for k in lib_grouping._REQUIRED_FIELDS if k != "signin_flag"))


def test_set_group_flag_signin_default_skipped_when_field_not_configured(configured):
    _unconfigure_signin(configured)
    fake = GroupingFake(_g_tables())
    lib_grouping.set_group_flag(ACT, True, client=fake, activity=_find_act(fake, ACT))
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert updates[0][3] == {"分组功能开启": "是"}
    assert "签到功能开启" not in _find_act(fake, ACT)["fields"]


def test_set_mode_signin_default_skipped_when_field_not_configured(configured):
    _unconfigure_signin(configured)
    fake = GroupingFake(_g_tables())
    lib_grouping.set_mode(ACT, "group", client=fake, activity=_find_act(fake, ACT))
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert updates[0][3] == {"分组功能开启": "是", "一对一功能开启": "否"}
    assert "签到功能开启" not in _find_act(fake, ACT)["fields"]


def test_set_onetoone_flag_enable_adds_signin_via_set_mode(configured_oto):
    """开启一对一功能走 set_mode（同笔补签到默认）；关闭路径只写自身、不补默认。"""
    fake = GroupingFake(_g_tables())
    res = lib_onetoone.set_onetoone_flag(ACT, True, client=fake, activity=_find_act(fake, ACT))
    assert res["code"] == "OK"
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert updates[0][3] == {"分组功能开启": "否", "一对一功能开启": "是", "签到功能开启": "是"}

    tables = _g_tables()
    tables[ACTIVITY_TABLE_ID] = [_act(一对一功能开启="是")]
    fake2 = GroupingFake(tables)
    res2 = lib_onetoone.set_onetoone_flag(ACT, False, client=fake2,
                                          activity=_find_act(fake2, ACT))
    assert res2["code"] == "OK"
    updates2 = [c for c in fake2.writes() if c[0] == "update"]
    assert updates2[0][3] == {"一对一功能开启": "否"}
    assert "签到功能开启" not in _find_act(fake2, ACT)["fields"]


# ---------------------------------------------------------------- ④ 姓名兜底查找

def _search_recorder(results, calls):
    def _search(table_id, payload=None, *a, **k):
        cond = (payload or {}).get("conditions", [{}])[0]
        calls.append((cond.get("field_name"), (cond.get("value") or [""])[0]))
        return results.get((cond.get("field_name"), (cond.get("value") or [""])[0]), [])
    return _search


def test_find_user_by_id_or_name_prefers_uid_and_nickname(monkeypatch):
    """既有命中行为不变：uid 命中不再查昵称/姓名；昵称命中不触发姓名兜底。"""
    uid_rec = {"record_id": "rec_uid", "fields": {"用户ID": "U-0003"}}
    nick_rec = {"record_id": "rec_nick", "fields": {"昵称": "佳佳"}}
    results = {("用户ID", "0003"): [uid_rec], ("昵称", "佳佳"): [nick_rec]}
    calls = []
    monkeypatch.setattr(queries, "search_records", _search_recorder(results, calls))

    assert queries.find_user_by_id_or_name("U-0003") == [uid_rec]
    assert calls == [("用户ID", "0003")], "uid 命中后不得再查昵称/姓名"

    calls.clear()
    assert queries.find_user_by_id_or_name("佳佳") == [nick_rec]
    assert calls == [("昵称", "佳佳")], "昵称命中后不得触发姓名兜底"


def test_find_user_by_id_or_name_real_name_fallback(monkeypatch):
    """昵称查空后才按「姓名」兜底；比对前去掉全部空白。"""
    name_rec = {"record_id": "rec_name", "fields": {"姓名": "陈小北"}}
    results = {("姓名", "陈小北"): [name_rec]}
    calls = []
    monkeypatch.setattr(queries, "search_records", _search_recorder(results, calls))

    assert queries.find_user_by_id_or_name("陈小北") == [name_rec]
    assert calls == [("昵称", "陈小北"), ("姓名", "陈小北")], "先昵称后姓名"

    calls.clear()
    assert queries.find_user_by_id_or_name(" 陈 小 北 ") == [name_rec]
    assert calls == [("昵称", " 陈 小 北 "), ("姓名", "陈小北")], "查姓名前去空白"

    calls.clear()
    assert queries.find_user_by_id_or_name("U-9999") == []
    assert calls == [("用户ID", "9999"), ("昵称", "U-9999"), ("姓名", "U-9999")], \
        "uid 查空后仍走昵称→姓名兜底（返回值与旧口径一致）"

    calls.clear()
    assert queries.find_user_by_id_or_name("  ") == []
    assert calls == [("昵称", "  ")], "去空白后为空则不查询名表"


# ---------------------------------------------------------------- ⑤ 视图/回覆读真名

def test_grouping_unsubmitted_reply_uses_real_name(configured):
    tables = _g_tables()
    # m1/f1 已提交；m2（空白姓名）与 f2（真名）未提交
    tables[GROUP_SELECT_TABLE] = [_sel("ou_m1", "男性"), _sel("ou_f1", "女性")]
    fake = GroupingFake(tables)
    configured.setattr(bot_grouping, "bitable", fake)
    configured.setattr(bot_grouping, "find_activity_by_id", lambda aid: _find_act(fake, aid))
    configured.setattr(bot_grouping, "find_user_by_openid", _find_user)
    configured.setattr(bot_grouping, "log", lambda *a, **k: None)

    res = lib_grouping.fetch_unsubmitted(ACT, client=fake, activity=_find_act(fake, ACT),
                                         find_user_by_openid=_find_user)
    by_oid = {u["open_id"]: u for u in res["unsubmitted"]}
    assert by_oid["ou_m2"]["real_name"] == ""
    assert by_oid["ou_f2"]["real_name"] == "周小西"

    text = bot_grouping.handle_admin_unsubmitted(ACT)
    assert "U-0002 · 男性 · 阿北" in text, "空白姓名回退昵称"
    assert "U-0004 · 女性 · 周小西" in text, "有姓名用真名"


def test_onetoone_unsubmitted_reply_uses_real_name(configured_oto):
    tables = _oto_tables()
    tables[OTO_SELECT] = [_oto_sel("ou_m1", "男性"), _oto_sel("ou_f1", "女性")]
    fake = OnetooneFake(tables)
    configured_oto.setattr(bot_onetoone, "bitable", fake)
    configured_oto.setattr(bot_onetoone, "find_activity_by_id", lambda aid: _find_act(fake, aid))
    configured_oto.setattr(bot_onetoone, "find_user_by_openid", _find_user)
    configured_oto.setattr(bot_onetoone, "log", lambda *a, **k: None)

    res = lib_onetoone.fetch_unsubmitted(ACT, client=fake, activity=_find_act(fake, ACT),
                                         find_user_by_openid=_find_user)
    by_oid = {u["open_id"]: u for u in res["unsubmitted"]}
    assert by_oid["ou_f2"]["real_name"] == "周小西"

    text = bot_onetoone.handle_admin_unsubmitted_onetoone(ACT)
    assert "U-0002 · 男性 · 阿北" in text
    assert "U-0004 · 女性 · 周小西" in text


def _card_options(card):
    for el in card["elements"][0]["elements"]:
        if el.get("options"):
            return [o["text"]["content"] for o in el["options"]]
    return None


def test_select_cards_show_real_name_with_uid_always():
    """选择卡 label = 真名（缺省昵称）＋编号；永远带编号，缺编号时只名字。"""
    participants = [
        {"open_id": "ou_m1", "nickname": "小北", "gender": "男性",
         "user_id": "U-0001", "real_name": "陈小北"},
        {"open_id": "ou_m2", "nickname": "阿北", "gender": "男性",
         "user_id": "U-0002", "real_name": ""},
        {"open_id": "ou_m3", "nickname": "无号", "gender": "男性",
         "user_id": "", "real_name": ""},
    ]
    expected = ["陈小北（U-0001）", "阿北（U-0002）", "无号"]
    assert _card_options(bot_grouping.build_group_select_card(
        ACT, "关系自洽", participants, "女性")) == expected
    assert _card_options(bot_onetoone.build_onetoone_select_card(
        ACT, "关系自洽", participants, "女性")) == expected


# ---------------------------------------------------------------- 签到指令回覆

def test_admin_attendance_replies_use_real_name_and_uid(monkeypatch):
    user = {"record_id": "rec_u", "fields": {
        "用户ID": "U-0003", "昵称": "阿南", "姓名": "林阿南", "飞书用户ID": "ou_f1"}}
    monkeypatch.setattr(points_admin, "find_activity_by_id",
                        lambda aid: {"record_id": "rec_act", "fields": {"活动名称": "关系自洽"}})
    monkeypatch.setattr(points_admin, "find_user_by_id_or_name", lambda kw: [user])
    monkeypatch.setattr(points_admin, "search_records",
                        lambda *a, **k: [{"record_id": "rec_s", "fields": {"状态": "已报名"}}])
    monkeypatch.setattr(points_admin, "update_record", lambda *a, **k: {"record_id": "rec_s"})
    logs = []
    monkeypatch.setattr(points_admin, "log", lambda msg: logs.append(msg))

    text = points_admin.handle_admin_attendance("签到 A-0002 U-0003 按时")
    assert text == "已记录「林阿南（U-0003）」在「关系自洽」的考勤：按时"
    assert "林阿南（U-0003）" in logs[0], "log 行也带真名+uid"


def test_admin_attendance_falls_back_to_nickname_and_name_only(monkeypatch):
    """真名缺省昵称；用户ID取不到时只名字。"""
    user = {"record_id": "rec_u2", "fields": {
        "昵称": "阿北", "飞书用户ID": "ou_m2"}}
    monkeypatch.setattr(points_admin, "find_activity_by_id",
                        lambda aid: {"record_id": "rec_act", "fields": {"活动名称": "关系自洽"}})
    monkeypatch.setattr(points_admin, "find_user_by_id_or_name", lambda kw: [user])
    monkeypatch.setattr(points_admin, "search_records",
                        lambda *a, **k: [{"record_id": "rec_s", "fields": {"状态": "已报名"}}])
    monkeypatch.setattr(points_admin, "update_record", lambda *a, **k: {"record_id": "rec_s"})
    monkeypatch.setattr(points_admin, "log", lambda msg: None)

    text = points_admin.handle_admin_attendance("签到 A-0002 阿北 迟到")
    assert text == "已记录「阿北」在「关系自洽」的考勤：迟到"


# ---------------------------------------------------------------- 结果读取不动（边界）

def test_fetch_result_keeps_stored_names(configured):
    """结果表读取原样回显落库的显示名（不二次加工）；fetch_result 结构不动。"""
    tables = _g_tables()
    tables[GROUP_RESULT_TABLE] = [{
        "record_id": "rec_r1",
        "fields": {"活动ID": ACT, "组号": 1, "用户open_id": "ou_m1",
                   "用户昵称": "陈小北", "用户性别": "男性", "轮次": "1"}}]
    fake = GroupingFake(tables)
    res = lib_grouping.fetch_result(ACT, None, client=fake, activity=_find_act(fake, ACT))
    member = res["groups"][0]["members"][0]
    assert member["display_name"] == "陈小北"
    assert set(member) == {"open_id", "gender", "display_name"}, "fetch_result 键集不动"
