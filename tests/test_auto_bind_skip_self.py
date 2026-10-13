"""auto_bind_from_creator 跳过「创建人=应用自身」记录的回归（不联网，桩表驱动）。

背景：任何经接口/脚本创建的记录，「创建人」会被飞书记为应用自身（API 无法清空/改写）。
auto_bind_from_creator 若把它当普通注册绑定，预填记录会被占走，真实用户再也无法认领
（认领要求飞书用户ID为空）。这组用例钉死：

  ① 创建人=自身 → 记录分毫不动（不更新/不删除/不写字段/不进邀请码与去重分支）；
  ② 创建人=真实用户 → 照常绑定（回归）；
  ③ 自身 open_id 取不到（None）→ 不跳过任何人（与改动前行为一致）；
  ④ 创建人 None / 空字符串 / self_id 空字符串的防御。

桩法沿用 tests/test_grouping_parity.py 的 FakeBitable：只桩「存取」，规则全跑真实现。
"""
import types

import auto_tasks
import clients
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


def _text(value):
    """把桩表里的字段值（文本列表 / 裸字符串）归一成字符串。"""
    if isinstance(value, list):
        return "".join(x.get("text", "") for x in value if isinstance(x, dict))
    return value if isinstance(value, str) else ""


def _row(record_id, creator=SELF_ID, *, nickname="张三", status="单身",
         feishu=None, invite=None):
    fields = {FIELD_NICKNAME: nickname, FIELD_ACCOUNT_STATUS: status}
    if creator is not None:
        fields[FIELD_CREATOR] = [{"id": creator, "name": "创建人"}]
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
             "cards": [], "codes_loaded": 0, "logs": []}

    def env(rows, self_id=SELF_ID):
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
            return {}

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
        monkeypatch.setattr(auto_tasks, "OBSERVER_TABLE_ID", "")
        monkeypatch.setattr(auto_tasks, "log", lambda msg: state["logs"].append(str(msg)))
        return table

    return env, state


# ---------------------------------------------------------------- ① 跳过自身

def test_creator_is_self_record_is_skipped_completely(wired):
    """创建人=自身 → 不绑定、不删、不写任何字段、不进邀请码分支。"""
    env, state = wired
    table = env([
        _row("rec_prefill1", creator=SELF_ID, nickname="预填一号", invite="OBS-CODE"),
        _row("rec_prefill2", creator=SELF_ID, nickname="预填二号", status="待审核"),
    ])

    auto_tasks.auto_bind_from_creator()

    assert state["updates"] == [], "不许写字段（含账号状态与飞书用户ID）"
    assert state["deletes"] == [], "不许删记录"
    assert state["bindings"] == [], "不许写绑定表"
    assert state["messages"] == [] and state["cards"] == [], "不许发任何消息"
    assert state["codes_loaded"] == 0, "不许进邀请码分支"
    assert {r["record_id"] for r in table.rows} == {"rec_prefill1", "rec_prefill2"}
    for r in table.rows:
        assert FIELD_FEISHU_ID not in r["fields"]
    assert table.rows[0]["fields"][FIELD_ACCOUNT_STATUS] == "单身", "状态原样保留"
    skip_logs = [m for m in state["logs"] if "应用自建" in m]
    assert len(skip_logs) == 1 and "2 条" in skip_logs[0], "每轮最多汇总一条跳过日志"


def test_mixed_round_skips_only_self(wired):
    """同轮「自建+真实」并存：只跳自建，真实用户照绑（continue 不吞后续记录）。"""
    env, state = wired
    table = env([
        _row("rec_self", creator=SELF_ID, nickname="预填"),
        _row("rec_real", creator=REAL_ID, nickname="真用户"),
    ])

    auto_tasks.auto_bind_from_creator()

    rows = {r["record_id"]: r for r in table.rows}
    assert _text(rows["rec_self"]["fields"].get(FIELD_FEISHU_ID)) == ""
    assert _text(rows["rec_real"]["fields"][FIELD_FEISHU_ID]) == REAL_ID
    assert len(state["bindings"]) == 1
    assert REAL_ID in state["bindings"][0]
    assert SELF_ID not in state["bindings"][0]


# ---------------------------------------------------------------- ② 真实用户回归

def test_real_creator_still_binds(wired):
    """创建人=真实用户 → 照常绑飞书ID + 置待审核 + 写绑定表。"""
    env, state = wired
    table = env([_row("rec_real", creator=REAL_ID, nickname="真用户")])

    auto_tasks.auto_bind_from_creator()

    assert state["updates"] == [
        (USER_TABLE_ID, "rec_real", {FIELD_ACCOUNT_STATUS: "待审核"}),
        (USER_TABLE_ID, "rec_real", {FIELD_FEISHU_ID: REAL_ID}),
    ]
    assert _text(table.rows[0]["fields"][FIELD_FEISHU_ID]) == REAL_ID
    assert state["deletes"] == [] and state["messages"] == []
    assert list(state["bindings"][0]) == [REAL_ID]
    assert state["bindings"][0][REAL_ID]["bind_type"] == "auto"


def test_real_creator_with_done_status_binds_without_status_write(wired):
    """已脱单的真实用户只写飞书ID，不覆盖账号状态（原逻辑未变）。"""
    env, state = wired
    table = env([_row("rec_done", creator=REAL_ID, nickname="已脱单", status="已脱单")])

    auto_tasks.auto_bind_from_creator()

    assert state["updates"] == [(USER_TABLE_ID, "rec_done", {FIELD_FEISHU_ID: REAL_ID})]
    assert table.rows[0]["fields"][FIELD_ACCOUNT_STATUS] == "已脱单"


# ---------------------------------------------------------------- ③ 自身id取不到

def test_self_id_unavailable_keeps_old_behavior(wired):
    """bot/v3/info 失败（None）→ 不跳过任何人，包括应用自建记录（行为与改动前一致）。"""
    env, state = wired
    table = env([
        _row("rec_app", creator=SELF_ID, nickname="应用自建"),
        _row("rec_real", creator=REAL_ID, nickname="真用户"),
    ], self_id=None)

    auto_tasks.auto_bind_from_creator()

    assert _text(table.rows[0]["fields"][FIELD_FEISHU_ID]) == SELF_ID
    assert _text(table.rows[1]["fields"][FIELD_FEISHU_ID]) == REAL_ID
    assert len(state["bindings"]) == 2


# ---------------------------------------------------------------- ④ 空值防御

def test_none_or_empty_creator_is_left_alone(wired):
    """创建人缺失 / 空字符串 → 维持原样跳过，且不产生任何写入。"""
    env, state = wired
    table = env([
        _row("rec_none", creator=None, nickname="无创建人"),
        _row("rec_empty", creator="", nickname="空创建人"),
    ])

    auto_tasks.auto_bind_from_creator()

    assert state["updates"] == [] and state["deletes"] == []
    assert state["bindings"] == []
    assert all(FIELD_FEISHU_ID not in r["fields"] for r in table.rows)


def test_empty_self_id_never_skips(wired):
    """self_id 为空字符串（防御）→ 不视为命中自身，真实创建人照常绑定。"""
    env, state = wired
    table = env([_row("rec_real", creator=REAL_ID, nickname="真用户")], self_id="")

    auto_tasks.auto_bind_from_creator()

    assert _text(table.rows[0]["fields"][FIELD_FEISHU_ID]) == REAL_ID
    assert len(state["bindings"]) == 1


# ---------------------------------------------------------------- get_bot_self_openid 本体

class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def self_getter(monkeypatch):
    """干净的自取 id 环境：清缓存、桩 token 与 requests.get、捕获 log。"""
    logs = []
    state = {"http_calls": 0, "url": "",
             "payload": {"code": 0, "bot": {"open_id": "ou_bot_self"}}}
    monkeypatch.setattr(clients, "_BOT_SELF_OPENID", None)
    monkeypatch.setattr(clients, "_BOT_SELF_OPENID_READY", False)
    monkeypatch.setattr(clients, "_BOT_SELF_OPENID_WARN_AT", 0.0)
    monkeypatch.setattr(clients, "get_tenant_access_token", lambda: "t-token")
    monkeypatch.setattr(clients, "log", logs.append)

    def _get(url, headers=None, timeout=None):
        state["http_calls"] += 1
        state["url"] = url
        return _Resp(state["payload"])

    monkeypatch.setattr(clients, "requests", types.SimpleNamespace(get=_get))
    return state, logs


def test_getter_returns_open_id_and_caches_success(self_getter):
    state, _logs = self_getter
    assert clients.get_bot_self_openid() == "ou_bot_self"
    assert clients.get_bot_self_openid() == "ou_bot_self"
    assert state["http_calls"] == 1, "成功后进程内缓存，不再请求"
    assert state["url"].endswith("/open-apis/bot/v3/info")


def test_getter_accepts_data_wrapper(self_getter):
    state, _logs = self_getter
    state["payload"] = {"code": 0, "data": {"open_id": "ou_wrapped"}}
    assert clients.get_bot_self_openid() == "ou_wrapped"


def test_getter_returns_none_and_throttles_warning_on_failure(self_getter):
    """接口报错 → 返回 None、失败不缓存（下轮可重试）、warning 每 10 分钟最多一条。"""
    state, logs = self_getter
    state["payload"] = {"code": 99991663, "msg": "no permission"}
    assert clients.get_bot_self_openid() is None
    assert clients.get_bot_self_openid() is None
    assert state["http_calls"] == 2
    warnings = [m for m in logs if "open_id" in m]
    assert len(warnings) == 1, "warning 必须节流，不能每轮刷屏"


def test_getter_returns_none_when_no_token(self_getter, monkeypatch):
    state, logs = self_getter
    monkeypatch.setattr(clients, "get_tenant_access_token", lambda: None)
    assert clients.get_bot_self_openid() is None
    assert state["http_calls"] == 0
    assert len([m for m in logs if "open_id" in m]) == 1


def test_getter_returns_none_when_open_id_missing(self_getter):
    state, _logs = self_getter
    state["payload"] = {"code": 0, "bot": {"app_name": "x"}}
    assert clients.get_bot_self_openid() is None
