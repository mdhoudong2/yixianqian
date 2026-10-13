"""自动报名可靠性修复（FIX-A）单测：重试队列 / 返回码矩阵 / 轮询入队 / 管理员路径。

背景：approval_sent 去重位先于通知发送占下、失败不回滚；自动报名若遇上飞书瞬时
错误，旧实现每轮整条 continue，永远不再尝试。修复 = 返回码化 + 重试队列 +
轮询末尾无条件补偿。桩法沿用 tests/test_grouping_parity.py 的 FakeBitable（只桩存取）。

bot/local_config.py 由 conftest 注入桩配置；NOTIFIED_FILE 指向 tmp_path，
本文件不读不写仓库 data/ 下的任何真实文件。
"""
import json
from types import SimpleNamespace

import pytest

import auto_tasks
import commands
import store
from constants import (
    ACTIVITY_TABLE_ID,
    FIELD_ACCOUNT_STATUS,
    FIELD_ACTIVITY_ID,
    FIELD_ACTIVITY_STATUS,
    FIELD_FEISHU_ID,
    FIELD_GENDER,
    FIELD_NICKNAME,
    FIELD_SIGNUP_ACTIVITY_ID,
    FIELD_SIGNUP_NICKNAME,
    FIELD_SIGNUP_OPENID,
    FIELD_SIGNUP_STATUS,
    FIELD_WECHAT_PAYMENT,
    SIGNUP_TABLE_ID,
    USER_TABLE_ID,
    WECHAT_PAY_PAID,
)
from lib import storage
from tests.test_grouping_parity import FakeBitable

ACT = "A-0002"
ACT_NUM = "2"  # 活动ID 是自动编号字段：按数字查询，记录里读到的也是编号文本


class SignupFake(FakeBitable):
    """在 parity 的假表上补 get_record 与「单条 create 失败」开关。"""

    def __init__(self, tables=None, search_fails=None, create_fails=False):
        super().__init__(tables=tables, search_fails=search_fails)
        self.create_fails = create_fails

    def create_record(self, table_id, fields):
        if self.create_fails:
            self.calls.append(("create_failed", table_id, dict(fields)))
            return None
        return super().create_record(table_id, fields)

    def get_record(self, table_id, record_id):
        self.calls.append(("get", table_id, record_id))
        for r in self.tables.get(table_id, []):
            if r["record_id"] == record_id:
                return {"record_id": r["record_id"], "fields": dict(r["fields"])}
        return None


def _act(status="报名中", capacity=0):
    return {"record_id": "rec_act", "fields": {
        FIELD_ACTIVITY_ID: ACT_NUM,
        FIELD_ACTIVITY_STATUS: status,
        "报名人数上限": capacity,
    }}


def _signup(oid, status="已报名"):
    return {"record_id": f"rec_s_{oid}", "fields": {
        FIELD_SIGNUP_ACTIVITY_ID: ACT_NUM,
        FIELD_SIGNUP_OPENID: oid,
        FIELD_SIGNUP_NICKNAME: oid,
        FIELD_SIGNUP_STATUS: status,
    }}


def _user(record_id, oid="ou_1", nick="张三", pay=WECHAT_PAY_PAID):
    return {"record_id": record_id, "fields": {
        FIELD_FEISHU_ID: oid, FIELD_NICKNAME: nick, FIELD_WECHAT_PAYMENT: pay,
    }}


def _fake(activity=None, signups=(), search_fails=None, create_fails=False):
    return SignupFake(
        tables={ACTIVITY_TABLE_ID: [activity] if activity is not None else [],
                SIGNUP_TABLE_ID: list(signups)},
        search_fails=search_fails or set(),
        create_fails=create_fails)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """临时 NOTIFIED_FILE + 桩活动ID + 静音日志。"""
    notified = tmp_path / "notified.json"
    monkeypatch.setattr(store, "NOTIFIED_FILE", str(notified))
    monkeypatch.setattr(auto_tasks, "AUTO_SIGNUP_ACTIVITY_ID", ACT)
    monkeypatch.setattr(auto_tasks, "log", lambda *a, **k: None)
    monkeypatch.setattr(commands, "log", lambda *a, **k: None)
    return notified


# ---------------------------------------------------------------- 1. store 队列

def test_store_queue_add_is_idempotent(env):
    store.add_signup_retry("rec_a")
    store.add_signup_retry("rec_a")            # 重复 add 不重置计数
    assert store.get_signup_retry("rec_a") == 0
    store.bump_signup_retry("rec_a")
    store.add_signup_retry("rec_a")            # 已 bump 后再 add 也不清零
    assert store.get_signup_retry("rec_a") == 1


def test_store_queue_get_bump_clear(env):
    assert store.get_signup_retry("rec_missing") == 0
    assert store.bump_signup_retry("rec_a") == 1
    assert store.bump_signup_retry("rec_a") == 2
    assert store.get_signup_retry("rec_a") == 2
    store.clear_signup_retry("rec_a")
    assert store.get_signup_retry("rec_a") == 0
    store.clear_signup_retry("rec_a")          # 不在队列，重复 clear 不炸


def test_store_queue_list_sorted_by_attempts(env):
    store.add_signup_retry("rec_a")
    store.add_signup_retry("rec_b")
    store.add_signup_retry("rec_c")
    store.bump_signup_retry("rec_c")
    store.bump_signup_retry("rec_a")
    store.bump_signup_retry("rec_a")
    assert store.list_signup_retries(10) == [("rec_b", 0), ("rec_c", 1), ("rec_a", 2)]
    assert store.list_signup_retries(1) == [("rec_b", 0)]
    store.clear_signup_retry("rec_b")
    assert [r for r, _ in store.list_signup_retries(10)] == ["rec_c", "rec_a"]


def test_store_queue_lives_in_notified_file_under_own_key(env):
    store.add_signup_retry("rec_a")
    store.bump_signup_retry("rec_a")
    data = json.loads(env.read_text())
    assert data["signup_retry"] == {"rec_a": 1}


# ---------------------------------------------------------------- 2. 轮询入队

def _poll_user():
    return [{"record_id": "rec_u1", "fields": {
        FIELD_NICKNAME: "张三", FIELD_GENDER: "男性", FIELD_FEISHU_ID: "ou_poll1",
        FIELD_WECHAT_PAYMENT: WECHAT_PAY_PAID}}]


def _patch_poll(monkeypatch, code):
    """把轮询路径的全部外部依赖换成桩；返回 process_signup_retries 的调用记录。"""
    monkeypatch.setattr(auto_tasks, "search_records", lambda *a, **k: _poll_user())
    monkeypatch.setattr(auto_tasks, "send_text_message", lambda *a, **k: True)
    monkeypatch.setattr(auto_tasks, "send_main_menu_card", lambda *a, **k: True)
    monkeypatch.setattr(auto_tasks, "reward_inviter", lambda *a, **k: (None, ""))
    monkeypatch.setattr(auto_tasks, "_auto_signup_new_user_ex", lambda *a, **k: code)
    processed = []
    monkeypatch.setattr(auto_tasks, "process_signup_retries",
                        lambda limit=10: processed.append(limit))
    return processed


def test_poll_enqueues_on_retry_code(env, monkeypatch):
    processed = _patch_poll(monkeypatch, "retry_query")
    auto_tasks.auto_send_view_after_approval()
    assert store.list_signup_retries(10) == [("rec_u1", 0)]
    assert processed == [10], "轮询末尾必须调用 process_signup_retries"


@pytest.mark.parametrize("code", ["created", "already", "skip_not_paid", "skip_full"])
def test_poll_does_not_enqueue_on_non_retry(env, monkeypatch, code):
    _patch_poll(monkeypatch, code)
    auto_tasks.auto_send_view_after_approval()
    assert store.list_signup_retries(10) == []


def test_poll_runs_retry_processor_even_without_items(env, monkeypatch):
    monkeypatch.setattr(auto_tasks, "search_records", lambda *a, **k: [])
    processed = []
    monkeypatch.setattr(auto_tasks, "process_signup_retries",
                        lambda limit=10: processed.append(limit))
    auto_tasks.auto_send_view_after_approval()
    assert processed == [10]


# ---------------------------------------------------------------- 3. 补偿重试

def _patch_process(monkeypatch, code, user=_user("rec_u1")):
    fake = SignupFake(tables={USER_TABLE_ID: [user] if user else []})
    monkeypatch.setattr(auto_tasks, "bitable", fake)
    monkeypatch.setattr(auto_tasks, "_auto_signup_new_user_ex", lambda *a, **k: code)
    return fake


@pytest.mark.parametrize("code", ["created", "already", "skip_full", "skip_not_paid"])
def test_process_clears_on_business_result(env, monkeypatch, code):
    fake = _patch_process(monkeypatch, code)
    store.add_signup_retry("rec_u1")
    store.bump_signup_retry("rec_u1")
    auto_tasks.process_signup_retries()
    assert store.list_signup_retries(10) == []
    assert ("get", USER_TABLE_ID, "rec_u1") in fake.calls


def test_process_bumps_counter_on_retry(env, monkeypatch):
    _patch_process(monkeypatch, "retry_create")
    store.add_signup_retry("rec_u1")
    auto_tasks.process_signup_retries()
    assert store.get_signup_retry("rec_u1") == 1
    auto_tasks.process_signup_retries()
    assert store.get_signup_retry("rec_u1") == 2


def test_process_drops_after_max_attempts(env, monkeypatch):
    _patch_process(monkeypatch, "retry_activity")
    logs = []
    monkeypatch.setattr(auto_tasks, "log", logs.append)
    storage.save_json(store.NOTIFIED_FILE,
                      {"signup_retry": {"rec_u1": store.SIGNUP_RETRY_MAX}})
    auto_tasks.process_signup_retries()
    assert store.list_signup_retries(10) == []
    assert any("自动报名重试放弃" in m for m in logs), logs


def test_process_clears_when_user_record_gone(env, monkeypatch):
    fake = SignupFake(tables={USER_TABLE_ID: []})
    monkeypatch.setattr(auto_tasks, "bitable", fake)
    called = []
    monkeypatch.setattr(auto_tasks, "_auto_signup_new_user_ex",
                        lambda *a, **k: called.append(1) or "created")
    store.add_signup_retry("rec_u1")
    auto_tasks.process_signup_retries()
    assert store.list_signup_retries(10) == []
    assert called == [], "用户记录不存在时不应再调报名"


def test_process_clears_when_openid_missing(env, monkeypatch):
    _patch_process(monkeypatch, "created", user=_user("rec_u1", oid=""))
    store.add_signup_retry("rec_u1")
    auto_tasks.process_signup_retries()
    assert store.list_signup_retries(10) == []


# ---------------------------------------------------------------- 4. 返回码矩阵

def test_ex_pay_gate_first_no_network(env, monkeypatch):
    fake = SignupFake()
    monkeypatch.setattr(auto_tasks, "bitable", fake)
    assert auto_tasks._auto_signup_new_user_ex("ou1", "张三", None) == "skip_not_paid"
    assert auto_tasks._auto_signup_new_user_ex("ou1", "张三", "") == "skip_not_paid"
    assert auto_tasks._auto_signup_new_user_ex("ou1", "张三", "还没交钱") == "skip_not_paid"
    assert fake.calls == [], "缴费门禁最先，任何网络调用之前就该返回"


def test_ex_no_activity_configured(env, monkeypatch):
    monkeypatch.setattr(auto_tasks, "AUTO_SIGNUP_ACTIVITY_ID", "")
    fake = SignupFake()
    monkeypatch.setattr(auto_tasks, "bitable", fake)
    assert auto_tasks._auto_signup_new_user_ex("ou1", "张三", WECHAT_PAY_PAID) == "skip_no_activity"
    assert fake.calls == []


def test_ex_activity_query_failure_retries(env, monkeypatch):
    fake = _fake(activity=_act(), search_fails={ACTIVITY_TABLE_ID})
    monkeypatch.setattr(auto_tasks, "bitable", fake)
    assert auto_tasks._auto_signup_new_user_ex("ou1", "张三", WECHAT_PAY_PAID) == "retry_activity"


def test_ex_activity_missing_skips(env, monkeypatch):
    fake = _fake(activity=None)
    monkeypatch.setattr(auto_tasks, "bitable", fake)
    assert auto_tasks._auto_signup_new_user_ex("ou1", "张三", WECHAT_PAY_PAID) == "skip_no_activity"


def test_ex_activity_not_open_skips(env, monkeypatch):
    fake = _fake(activity=_act(status="未开始报名"))
    monkeypatch.setattr(auto_tasks, "bitable", fake)
    assert auto_tasks._auto_signup_new_user_ex("ou1", "张三", WECHAT_PAY_PAID) == "skip_not_open"


def test_ex_signup_query_failure_retries(env, monkeypatch):
    fake = _fake(activity=_act(), search_fails={SIGNUP_TABLE_ID})
    monkeypatch.setattr(auto_tasks, "bitable", fake)
    assert auto_tasks._auto_signup_new_user_ex("ou1", "张三", WECHAT_PAY_PAID) == "retry_query"


def test_ex_already_signed_up(env, monkeypatch):
    fake = _fake(activity=_act(), signups=[_signup("ou1")])
    monkeypatch.setattr(auto_tasks, "bitable", fake)
    assert auto_tasks._auto_signup_new_user_ex("ou1", "张三", WECHAT_PAY_PAID) == "already"


def test_ex_full_skips(env, monkeypatch):
    fake = _fake(activity=_act(capacity=1), signups=[_signup("ou_other")])
    monkeypatch.setattr(auto_tasks, "bitable", fake)
    assert auto_tasks._auto_signup_new_user_ex("ou1", "张三", WECHAT_PAY_PAID) == "skip_full"


def test_ex_create_failure_retries(env, monkeypatch):
    fake = _fake(activity=_act(), create_fails=True)
    monkeypatch.setattr(auto_tasks, "bitable", fake)
    assert auto_tasks._auto_signup_new_user_ex("ou1", "张三", WECHAT_PAY_PAID) == "retry_create"


def test_ex_created_and_compat_shell(env, monkeypatch):
    fake = _fake(activity=_act())
    monkeypatch.setattr(auto_tasks, "bitable", fake)
    assert auto_tasks._auto_signup_new_user_ex("ou1", "张三", WECHAT_PAY_PAID) == "created"
    assert auto_tasks.auto_signup_new_user("ou2", "李四", WECHAT_PAY_PAID) is True
    assert auto_tasks.auto_signup_new_user("ou1", "张三", WECHAT_PAY_PAID) is False  # 幂等


def test_shell_returns_false_on_retry_and_skip(env, monkeypatch):
    fake = _fake(activity=_act(), create_fails=True)
    monkeypatch.setattr(auto_tasks, "bitable", fake)
    assert auto_tasks.auto_signup_new_user("ou1", "张三", WECHAT_PAY_PAID) is False
    assert auto_tasks.auto_signup_new_user("ou2", "李四", "") is False


# ---------------------------------------------------------------- 5. 管理员审核路径

def _patch_admin(monkeypatch, code):
    rec = {"record_id": "rec_u9", "fields": {
        FIELD_NICKNAME: "李四", "用户ID": "U-0009",
        FIELD_ACCOUNT_STATUS: "待审核", FIELD_FEISHU_ID: "ou_admin1",
        FIELD_GENDER: "男性", FIELD_WECHAT_PAYMENT: WECHAT_PAY_PAID}}
    monkeypatch.setattr(commands, "find_user_by_id_or_name", lambda kw: [rec])
    monkeypatch.setattr(commands, "search_records", lambda *a, **k: [])
    monkeypatch.setattr(commands, "update_record", lambda *a, **k: True)
    monkeypatch.setattr(commands, "reserve_notified", lambda *a, **k: True)
    monkeypatch.setattr(commands, "generate_h5_url", lambda oid: "https://example.test/h5")
    monkeypatch.setattr(commands, "send_text_message", lambda *a, **k: True)
    monkeypatch.setattr(commands, "_auto_signup_new_user_ex", lambda *a, **k: code)


@pytest.mark.parametrize("code,queued", [
    ("retry_query", True), ("retry_create", True), ("created", False), ("skip_full", False)])
def test_admin_approve_transient_enqueues(env, monkeypatch, code, queued):
    _patch_admin(monkeypatch, code)
    reply = commands.handle_admin_approve("U-0009")
    assert "已审核通过" in reply
    assert bool(store.list_signup_retries(10)) is queued


# ---------------------------------------------------------------- 6. 认领指令前缀（A5）

def test_claim_command_prefix_matching(monkeypatch):
    """「认领」类指令改为前缀匹配；CI（装了 lark）执行，本地 web venv 无 lark 自动跳过。"""
    ws = pytest.importorskip("yixianqian_bot_ws")
    replies = []

    def _capture(oid, chat, text):
        replies.append(text)
        return True

    monkeypatch.setattr(ws, "is_admin", lambda oid: False)
    monkeypatch.setattr(ws, "update_p2p_chat", lambda *a, **k: None)
    monkeypatch.setattr(ws, "log", lambda *a, **k: None)
    monkeypatch.setattr(ws, "handle_claim_command", lambda sid: "CLAIM")
    monkeypatch.setattr(ws, "_reply_inbound", _capture)

    def _event(text, msg_id):
        return SimpleNamespace(event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_claim_tester"),
                sender_type="user"),
            message=SimpleNamespace(
                chat_type="p2p", chat_id="oc_claim", message_type="text",
                content=json.dumps({"text": text}), message_id=msg_id)))

    for i, text in enumerate(("认领", "认领资料 张三", "认领报名 13800000000",
                              "现场认领", "claim 13800000000", "Claim")):
        ws.do_p2_im_message_receive_v1(_event(text, f"om_claim_{i}"))

    assert replies == ["CLAIM"] * 6
