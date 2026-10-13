"""auto_bind_from_creator 跳过「微信访客预填记录」的回归（不联网，桩表驱动）。

背景：外部访客经微信问卷提交的记录，创建人为「访客 XXXX」（外部访客身份）。
auto_bind_from_creator 若把这类无邀请码的记录绑定到访客身份，用户在飞书 APP
注册的新身份再来认领时就匹配不到（认领要求飞书用户ID为空）→ 404。业务要求
「访客」创建的记录不得自动绑定，保持空档留待认领；带邀请码的访客记录走原分支。

桩法沿用 tests/test_grouping_parity.py / test_auto_bind_skip_self.py 的
FakeBitable：只桩「存取」，规则全跑真实现。

  ① 访客+无码 → 分毫不动、计入跳过日志；
  ② 访客+有码 → 进入原分支（有效码→观察员；错码→删除），不跳过；
  ③ 创建人名像访客但 creator id 空 → 原 continue，不计数；
  ④ 非访客（真实用户/用户XXXX/应用名）→ 照常绑定（回归）；
  ⑤ 创建人=应用自身优先于访客判定；
  ⑥ 跳过日志节流：同计数短期不重复、计数变化立即记、≥1800s 补记。
"""
import auto_tasks
import pytest
from constants import (
    FIELD_ACCOUNT_STATUS,
    FIELD_CREATOR,
    FIELD_FEISHU_ID,
    FIELD_INVITE_CODE,
    FIELD_NICKNAME,
    USER_TABLE_ID,
)

SELF_ID = "ou_app_self"
REAL_ID = "ou_real_user"
GUEST_ID = "ou_guest_external1"
GUEST_ID2 = "ou_guest_external2"


def _text(value):
    """把桩表里的字段值（文本列表 / 裸字符串）归一成字符串。"""
    if isinstance(value, list):
        return "".join(x.get("text", "") for x in value if isinstance(x, dict))
    return value if isinstance(value, str) else ""


def _row(record_id, creator=None, *, creator_name="创建人", nickname="张三", status="单身",
         feishu=None, invite=None):
    fields = {FIELD_NICKNAME: nickname, FIELD_ACCOUNT_STATUS: status}
    if creator is not None:
        fields[FIELD_CREATOR] = [{"id": creator, "name": creator_name}]
    if feishu is not None:
        fields[FIELD_FEISHU_ID] = [{"type": "text", "text": feishu}]
    if invite is not None:
        fields[FIELD_INVITE_CODE] = [{"type": "text", "text": invite}]
    return {"record_id": record_id, "fields": fields}


class FakeBitable:
    """够用的内存假表：实现 auto_bind_from_creator 用到的 isEmpty/isNotEmpty/is。"""

    def __init__(self, rows=None):
        self.rows = [{"record_id": r["record_id"], "fields": dict(r["fields"])}
                     for r in (rows or [])]

    def search_records(self, table_id, filter_conditions=None, *a, **k):
        conds = (filter_conditions or {}).get("conditions", [])
        return [r for r in self.rows if self._match(r, conds)]

    @staticmethod
    def _match(row, conds):
        for c in conds:
            want = (c.get("value") or [""])[0]
            got = _text(row["fields"].get(c.get("field_name"), ""))
            if c.get("operator") == "isEmpty":
                if got:
                    return False
            elif c.get("operator") == "isNotEmpty":
                if not got:
                    return False
            elif c.get("operator") == "is":
                if got != want:
                    return False
        return True


@pytest.fixture
def wired(monkeypatch):
    """把 auto_bind_from_creator 的外部依赖（表/消息/绑定/邀请码/自身id）全换成桩。"""
    state = {"updates": [], "deletes": [], "bindings": [], "messages": [],
             "cards": [], "codes_loaded": 0, "consumed": [], "moved": [], "logs": []}

    def env(rows, self_id=SELF_ID, codes=None):
        table = FakeBitable(rows)

        def _update(table_id, record_id, fields):
            state["updates"].append((table_id, record_id, dict(fields)))
            for r in table.rows:
                if r["record_id"] == record_id:
                    r["fields"].update(fields)
            return True

        def _delete(table_id, record_id):
            state["deletes"].append((table_id, record_id))
            table.rows = [r for r in table.rows if r["record_id"] != record_id]
            return True

        def _update_feishu_id(record_id, open_id):
            state["updates"].append((USER_TABLE_ID, record_id, {FIELD_FEISHU_ID: open_id}))
            for r in table.rows:
                if r["record_id"] == record_id:
                    r["fields"][FIELD_FEISHU_ID] = [{"type": "text", "text": open_id}]
            return True

        def _load_codes():
            state["codes_loaded"] += 1
            return dict(codes or {})

        def _consume(code, nick):
            state["consumed"].append((code, nick))
            return True

        def _move(record_id, fields, open_id):
            state["moved"].append((record_id, open_id))
            return "rec_moved1"

        monkeypatch.setattr(auto_tasks, "search_records", table.search_records)
        monkeypatch.setattr(auto_tasks, "update_record", _update)
        monkeypatch.setattr(auto_tasks, "delete_record", _delete)
        monkeypatch.setattr(auto_tasks, "update_user_feishu_id", _update_feishu_id)
        monkeypatch.setattr(auto_tasks, "get_bot_self_openid", lambda: self_id)
        monkeypatch.setattr(
            auto_tasks, "update_bindings",
            lambda mutator: state["bindings"].append(dict(mutator({}))))
        monkeypatch.setattr(
            auto_tasks, "send_text_message",
            lambda oid, text, **k: state["messages"].append((oid, text)))
        monkeypatch.setattr(
            auto_tasks, "send_main_menu_card",
            lambda oid, **k: state["cards"].append(oid))
        monkeypatch.setattr(auto_tasks, "auto_fill_like_links", lambda: None)
        monkeypatch.setattr(auto_tasks, "load_observer_codes", _load_codes)
        monkeypatch.setattr(auto_tasks, "consume_observer_code", _consume)
        monkeypatch.setattr(auto_tasks, "_move_record_to_observer_table", _move)
        monkeypatch.setattr(auto_tasks, "release_observer_code", lambda code: None)
        # 节流状态是模块级字典：逐测试隔离，免得上个用例的计数影响本用例日志断言
        monkeypatch.setattr(auto_tasks, "_skip_log_state", {})
        monkeypatch.setattr(auto_tasks, "OBSERVER_TABLE_ID", "")
        monkeypatch.setattr(auto_tasks, "log", lambda msg: state["logs"].append(str(msg)))
        return table

    return env, state


def _guest_logs(state):
    return [m for m in state["logs"] if "访客预填" in m]


# ---------------------------------------------------------------- ① 访客+无码：跳过

def test_guest_without_code_record_is_skipped_completely(wired):
    """访客+无码 → 不绑定、不删、不写任何字段、不进邀请码/去重分支。"""
    env, state = wired
    table = env([
        _row("rec_guest1", creator=GUEST_ID, creator_name="访客 A1B2", nickname="访客甲"),
        _row("rec_guest2", creator=GUEST_ID2, creator_name="Guest 乙", nickname="访客乙",
             status="待审核"),
    ])

    auto_tasks.auto_bind_from_creator()

    assert state["updates"] == [], "不许写字段（含账号状态与飞书用户ID）"
    assert state["deletes"] == [], "不许删记录"
    assert state["bindings"] == [], "不许写绑定表"
    assert state["messages"] == [] and state["cards"] == [], "不许发任何消息"
    assert state["codes_loaded"] == 0, "不许进邀请码分支"
    assert state["consumed"] == [] and state["moved"] == []
    assert {r["record_id"] for r in table.rows} == {"rec_guest1", "rec_guest2"}
    for r in table.rows:
        assert FIELD_FEISHU_ID not in r["fields"]
    skip_logs = _guest_logs(state)
    assert len(skip_logs) == 1 and "2 条" in skip_logs[0] and "留待认领" in skip_logs[0]


def test_mixed_round_skips_only_guests(wired):
    """同轮「访客+真实用户」并存：只跳访客，真实用户照绑（continue 不吞后续记录）。"""
    env, state = wired
    table = env([
        _row("rec_guest", creator=GUEST_ID, creator_name="访客 A1B2", nickname="访客甲"),
        _row("rec_real", creator=REAL_ID, creator_name="真实用户", nickname="真用户"),
    ])

    auto_tasks.auto_bind_from_creator()

    rows = {r["record_id"]: r for r in table.rows}
    assert _text(rows["rec_guest"]["fields"].get(FIELD_FEISHU_ID)) == ""
    assert _text(rows["rec_real"]["fields"][FIELD_FEISHU_ID]) == REAL_ID
    assert len(state["bindings"]) == 1 and REAL_ID in state["bindings"][0]
    assert GUEST_ID not in state["bindings"][0]
    assert len(_guest_logs(state)) == 1 and "1 条" in _guest_logs(state)[0]


# ---------------------------------------------------------------- ② 访客+有码：原分支

def test_guest_with_invite_code_keeps_original_branches(wired):
    """访客+有码 → 不跳过：有效码走观察员分支，错码走删除分支。"""
    env, state = wired
    table = env([
        _row("rec_guest_ok", creator=GUEST_ID, creator_name="访客 A1B2",
             nickname="访客甲", invite="OBS1"),
        _row("rec_guest_bad", creator=GUEST_ID2, creator_name="Guest 乙",
             nickname="访客乙", invite="WRONG"),
    ], codes={"OBS1": {"used": False}})

    auto_tasks.auto_bind_from_creator()

    assert _guest_logs(state) == [], "有邀请码的访客记录不得按访客跳过"
    assert state["consumed"] == [("OBS1", "访客甲")]
    assert state["moved"] == [("rec_guest_ok", GUEST_ID)]
    assert state["deletes"] == [(USER_TABLE_ID, "rec_guest_bad")], "错码访客照原逻辑删除"
    assert GUEST_ID in state["bindings"][0]
    assert state["bindings"][0][GUEST_ID]["bind_type"] == "observer"
    assert {r["record_id"] for r in table.rows} == {"rec_guest_ok"}
    assert any("邀请码" in t or "注册" in t for _, t in state["messages"])


# ---------------------------------------------------------------- ③ 访客名但本人 id 空

def test_guest_name_without_creator_id_falls_into_original_continue(wired):
    """创建人名像访客但 creator id 空 → 原 `if not open_id: continue`，不计数不写。"""
    env, state = wired
    env([
        _row("rec_no_id", creator="", creator_name="访客 无名", nickname="无名"),
        _row("rec_missing", creator=None, creator_name="访客 缺失", nickname="缺失"),
    ])

    auto_tasks.auto_bind_from_creator()

    assert state["updates"] == [] and state["deletes"] == []
    assert state["bindings"] == [] and state["messages"] == []
    assert state["codes_loaded"] == 0
    assert _guest_logs(state) == [], "creator id 空的记录不算「跳过的访客记录」"


# ---------------------------------------------------------------- ④ 非访客回归

def test_non_guest_creators_still_bind(wired):
    """真实用户 / 用户XXXX / 应用名创建人 → 一律照常绑定（回归，防误伤）。"""
    env, state = wired
    table = env([
        _row("rec_real", creator=REAL_ID, creator_name="张三", nickname="张三"),
        _row("rec_user_num", creator="ou_user_1234", creator_name="用户1234", nickname="用户1234"),
        _row("rec_app_name", creator="ou_app_user", creator_name="一线牵助手", nickname="应用名"),
    ])

    auto_tasks.auto_bind_from_creator()

    rows = {r["record_id"]: r for r in table.rows}
    for rid, oid in (("rec_real", REAL_ID), ("rec_user_num", "ou_user_1234"),
                     ("rec_app_name", "ou_app_user")):
        assert _text(rows[rid]["fields"][FIELD_FEISHU_ID]) == oid
    assert state["deletes"] == [] and state["messages"] == []
    assert len(state["bindings"]) == 3
    assert _guest_logs(state) == []


# ---------------------------------------------------------------- ⑤ self 跳过优先

def test_self_skip_takes_priority_over_guest_name(wired):
    """创建人=应用自身且名字带「访客」→ 走 self 跳过，不进访客计数。"""
    env, state = wired
    table = env([
        _row("rec_self_guest_name", creator=SELF_ID, creator_name="访客 应用自建",
             nickname="预填", invite="OBS1"),
    ])

    auto_tasks.auto_bind_from_creator()

    assert state["updates"] == [] and state["deletes"] == [] and state["bindings"] == []
    assert state["codes_loaded"] == 0, "self 跳过在邀请码解析之前，优先级最高"
    assert _guest_logs(state) == []
    self_logs = [m for m in state["logs"] if "应用自建" in m]
    assert len(self_logs) == 1 and "1 条" in self_logs[0]
    assert table.rows[0]["fields"][FIELD_ACCOUNT_STATUS] == "单身", "状态原样保留"


def test_self_name_normally_not_guest(wired):
    """正常应用名不是访客前缀 → 行为与既有 skip_self 用例一致（回归）。"""
    env, state = wired
    table = env([_row("rec_self", creator=SELF_ID, creator_name="一线牵助手", nickname="预填")])

    auto_tasks.auto_bind_from_creator()

    assert state["updates"] == [] and state["deletes"] == []
    assert [m for m in state["logs"] if "应用自建" in m] and _guest_logs(state) == []
    assert FIELD_FEISHU_ID not in table.rows[0]["fields"]


# ---------------------------------------------------------------- ⑥ 节流机制

class _Clock:
    """只换 auto_tasks 里的 time 引用，不动全局 time 模块。"""
    now = 1000.0

    @classmethod
    def time(cls):
        return cls.now


def test_skip_log_throttle_same_count_silent_change_and_timeout_log(monkeypatch):
    logs = []
    monkeypatch.setattr(auto_tasks, "log", logs.append)
    monkeypatch.setattr(auto_tasks, "_skip_log_state", {})
    monkeypatch.setattr(auto_tasks, "time", _Clock)
    _Clock.now = 1000.0

    auto_tasks._log_skip_throttled("k", 3, "skip=3")
    auto_tasks._log_skip_throttled("k", 3, "skip=3")
    assert logs == ["skip=3"], "同计数短期内不得重复打日志"

    auto_tasks._log_skip_throttled("k", 4, "skip=4")
    assert logs == ["skip=3", "skip=4"], "计数变化立即记一条"

    _Clock.now += 1799
    auto_tasks._log_skip_throttled("k", 4, "skip=4")
    assert logs == ["skip=3", "skip=4"], "距上次不足 1800 秒仍静默"

    _Clock.now += 1
    auto_tasks._log_skip_throttled("k", 4, "skip=4")
    assert logs == ["skip=3", "skip=4", "skip=4"], "满 1800 秒补记一条"


def test_skip_log_throttle_keys_are_independent(monkeypatch):
    """guest 与 self 两条日志互不干扰。"""
    logs = []
    monkeypatch.setattr(auto_tasks, "log", logs.append)
    monkeypatch.setattr(auto_tasks, "_skip_log_state", {})
    monkeypatch.setattr(auto_tasks, "time", _Clock)
    _Clock.now = 2000.0

    auto_tasks._log_skip_throttled("self", 1, "self=1")
    auto_tasks._log_skip_throttled("guest", 1, "guest=1")
    auto_tasks._log_skip_throttled("self", 1, "self=1")
    assert logs == ["self=1", "guest=1"]


def test_guest_log_goes_through_same_throttle(wired):
    """轮末访客日志走节流：连续两轮同计数只记一次。"""
    env, state = wired
    env([_row("rec_guest", creator=GUEST_ID, creator_name="访客 A1B2", nickname="访客甲")])

    auto_tasks.auto_bind_from_creator()
    auto_tasks.auto_bind_from_creator()

    assert len(_guest_logs(state)) == 1, "同计数连续轮询不得刷屏"


# ---------------------------------------------------------------- get_creator_name 本体

def test_get_creator_name_parses_creator_field():
    import queries

    fields = {FIELD_CREATOR: [{"id": "ou_x", "name": "访客 A1B2"}]}
    assert queries.get_creator_name(fields) == "访客 A1B2"
    assert queries.get_creator_name({}) is None
    assert queries.get_creator_name({FIELD_CREATOR: []}) is None
    assert queries.get_creator_name({FIELD_CREATOR: [{"id": "ou_y"}]}) is None
