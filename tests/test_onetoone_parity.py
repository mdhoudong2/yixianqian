"""等价性安全网：一对一逻辑迁到 lib.onetoone 后，bot 指令回传文字与副作用必须一字不差。

同一个桩表、同一个输入，旧路（tests/_golden_onetoone_impl.py，自 HEAD 9f7f2f5 冻结）
与新路（lib.onetoone + bot 渲染层）各跑一遍，断言：

    回传字符串完全相同  且  写入调用序列与最终表内容完全相同

桩的只是「存取」，规则（幂等判断、状态流转、算法、回滚）全跑真实现——桩掉规则就
只剩在测 mock 了（与 tests/test_grouping_parity.py 同一取舍）。

案例覆盖全部 6 个 handler 的每个 return 分支。C2 审核修复刻意改了少数失败分支的
行为（读失败不再当空表、写入失败不再报成功、锁内查重等），那些案例单独列在
「刻意分歧」一节，不回入逐字比对；其余案例必须保持全绿。
"""
import onetoone as bot_onetoone
import pytest
from constants import (
    ACTIVITY_TABLE_ID,
    FIELD_OTO_ACTIVITY_ID,
    FIELD_OTO_CHOICES,
    FIELD_OTO_RANK,
    FIELD_OTO_SELECTOR_GENDER,
    FIELD_OTO_SELECTOR_OID,
    FIELD_OTO_TARGET_OID,
    FIELD_OTO_USER_OID,
    SIGNUP_TABLE_ID,
)

import grouping as bot_grouping
from lib import grouping as lib_grouping
from lib import onetoone as lib_onetoone
from tests import _golden_onetoone_impl as golden

ACT = "A-0002"
ACT2 = "A-0003"

# CI 无 bot/local_config.py（gitignore）时，一对一那两张表的常量默认值为空；
# 这里钉一组测试表 ID，并注入两条路（配置注入与桩表必须对得上同一组）。
ONETOONE_SELECT_TABLE = "tbl_test_oto_select"
ONETOONE_RESULT_TABLE = "tbl_test_oto_result"


# ---------------------------------------------------------------- 假表

class FakeBitable:
    """够用的内存假表：只实现一对一流程用到的 `operator: is` / `isNot`。

    真实的 lib.bitable_client 不桩——它只做 HTTP，桩掉等于没测。
    """

    def __init__(self, tables=None, fail_create_after=None, search_fails=None):
        # 必须连 fields 一起深拷贝：同一个 Case 会被建两次（旧路一次、新路一次），
        # 只拷外层 dict 的话两条路共用同一批 fields，第一条跑完就把第二条的起点改了。
        self.tables = {k: [{"record_id": r["record_id"], "fields": dict(r["fields"])}
                           for r in v] for k, v in (tables or {}).items()}
        self.fail_create_after = fail_create_after
        self.search_fails = search_fails or set()
        self.calls = []
        self._seq = 0

    # -- 读 --
    def search_records(self, table_id, filter_conditions=None, *a, **k):
        self.calls.append(("search", table_id))
        if table_id in self.search_fails:
            return None          # 模拟飞书限流/网络抖动时 raw client 的返回
        conds = filter_conditions or {}
        if isinstance(conds, dict):
            conds = conds.get("conditions", [])
        return [r for r in self.tables.get(table_id, []) if self._match(r, conds)]

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

    # -- 写 --
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

    def batch_create_records(self, table_id, records_fields):
        self.calls.append(("batch_create", table_id, [dict(r) for r in records_fields]))
        rows = self.tables.setdefault(table_id, [])
        created = 0
        for fields in records_fields:
            if self.fail_create_after is not None and created >= self.fail_create_after:
                break
            self._seq += 1
            rows.append({"record_id": f"rec_new{self._seq}", "fields": dict(fields)})
            created += 1
        return created

    def batch_delete_records(self, table_id, record_ids):
        self.calls.append(("batch_delete", table_id, list(record_ids)))
        before = len(self.tables.get(table_id, []))
        keep = [r for r in self.tables.get(table_id, []) if r["record_id"] not in record_ids]
        self.tables[table_id] = keep
        return before - len(keep)

    # -- 断言辅助 --
    def writes(self):
        """只留写操作；search 是读，比它没意义，还会被调用次数差异误伤。"""
        return [c for c in self.calls if c[0] != "search"]


# ---------------------------------------------------------------- 桩数据

def _user(oid, uid, gender, nickname):
    return {"record_id": f"rec_u_{oid}",
            "fields": {"飞书用户ID": oid, "用户ID": uid, "性别": gender, "昵称": nickname}}


USER_FIELDS = {
    "ou_m1": _user("ou_m1", "U-0001", "男性", "小北")["fields"],
    "ou_m2": _user("ou_m2", "U-0002", "男性", "阿北")["fields"],
    "ou_f1": _user("ou_f1", "U-0003", "女性", "阿南")["fields"],
    "ou_f2": _user("ou_f2", "U-0004", "女性", "小西")["fields"],
    "ou_f3": _user("ou_f3", "U-0005", "女性", "小东")["fields"],
    "ou_f4": _user("ou_f4", "U-0006", "女性", "小南")["fields"],
    "ou_f5": _user("ou_f5", "U-0007", "女性", "小中")["fields"],
    "ou_f6": _user("ou_f6", "U-0008", "女性", "小西二")["fields"],
    "ou_f7": _user("ou_f7", "U-0009", "女性", "小七")["fields"],
    "ou_nouid": {"飞书用户ID": "ou_nouid", "用户ID": "", "性别": "男性", "昵称": "无号"},
    "ou_dup1": {"飞书用户ID": "ou_dup1", "用户ID": "U-0101", "性别": "女性", "昵称": "佳佳"},
    "ou_dup2": {"飞书用户ID": "ou_dup2", "用户ID": "U-0102", "性别": "女性", "昵称": "佳佳"},
}


def _find_user(oid):
    fields = USER_FIELDS.get(oid)
    return [] if fields is None else [{"record_id": f"rec_u_{oid}", "fields": dict(fields)}]


def _find_user_by_id_or_name(keyword):
    hits = [oid for oid, f in USER_FIELDS.items()
            if f.get("用户ID") == keyword or f.get("昵称") == keyword]
    return [{"record_id": f"rec_u_{oid}", "fields": dict(USER_FIELDS[oid])} for oid in hits]


def _act(**over):
    fields = {"活动ID": ACT, "活动名称": "关系自洽", "一对一状态": "收集中"}
    fields.update(over)
    return {"record_id": "rec_act", "fields": fields}


def _signup(oid, nick="某人", activity=ACT):
    return {"record_id": f"rec_s_{oid}_{activity}",
            "fields": {"活动ID": activity, "报名人open_id": oid,
                       "报名人昵称": nick, "状态": "已报名"}}


def _sel(oid, gender, choices=(), activity=ACT):
    fields = {"活动ID": activity, "选择人open_id": oid, FIELD_OTO_SELECTOR_GENDER: gender}
    for i, cf in enumerate(FIELD_OTO_CHOICES):
        fields[cf] = choices[i] if i < len(choices) else ""
    return {"record_id": f"rec_oto_{activity}_{oid}", "fields": fields}


def _res(oid, target, rank=1, activity=ACT):
    return {"record_id": f"rec_res_{activity}_{oid}_{target}",
            "fields": {FIELD_OTO_ACTIVITY_ID: activity, FIELD_OTO_RANK: rank,
                       FIELD_OTO_USER_OID: oid, FIELD_OTO_TARGET_OID: target}}


def _find_act(fake, activity_id):
    for r in fake.tables.get(ACTIVITY_TABLE_ID, []):
        if r["fields"].get("活动ID") == activity_id:
            return r
    return None


def _default_tables():
    """2 男 2 女已报名且都交了一份志愿——足够跑通一次成功匹配。"""
    return {
        ACTIVITY_TABLE_ID: [_act()],
        SIGNUP_TABLE_ID: [_signup(o, USER_FIELDS[o]["昵称"])
                          for o in ("ou_m1", "ou_m2", "ou_f1", "ou_f2")],
        ONETOONE_SELECT_TABLE: [
            _sel("ou_m1", "男性", ("ou_f1",)), _sel("ou_m2", "男性", ("ou_f2",)),
            _sel("ou_f1", "女性", ("ou_m1",)), _sel("ou_f2", "女性", ("ou_m2",)),
        ],
        ONETOONE_RESULT_TABLE: [],
    }


def _rich_tables():
    """1 男 7 女：足够提交 7 个合法志愿。"""
    members = ["ou_m1"] + [f"ou_f{i}" for i in range(1, 8)]
    return {
        ACTIVITY_TABLE_ID: [_act()],
        SIGNUP_TABLE_ID: [_signup(o, USER_FIELDS[o]["昵称"]) for o in members],
        ONETOONE_SELECT_TABLE: [],
        ONETOONE_RESULT_TABLE: [],
    }


def _update_tables():
    """与 _rich_tables 同，但 ou_m1 已有一行志愿——提交时命中 update 分支。"""
    tables = _rich_tables()
    tables[ONETOONE_SELECT_TABLE] = [_sel("ou_m1", "男性", ("ou_f1",))]
    return tables


def _raise(exc):
    def _boom(*a, **k):
        raise exc
    return _boom


def _form(*vals):
    return {f"choice_{i}": v for i, v in enumerate(vals)}


SEVEN = ["ou_f1", "ou_f2", "ou_f3", "ou_f4", "ou_f5", "ou_f6", "ou_f7"]


# ---------------------------------------------------------------- 案例

class Case:
    """一个待验分支：跑哪条指令、喂什么输入、桩表长什么样。"""

    def __init__(self, name, handler, keyword="", tables=None, fail_create_after=None,
                 search_fails=None, algorithm=None, operator="ou_m1",
                 action_value=None, form_value=None):
        self.name = name
        self.handler = handler
        self.keyword = keyword
        self.tables = _default_tables() if tables is None else tables
        self.fail_create_after = fail_create_after
        self.search_fails = search_fails
        self.algorithm = algorithm
        self.operator = operator
        self.action_value = action_value if action_value is not None else {"activity_id": ACT}
        self.form_value = form_value


CASES = [
    # ---- 用户指令（5）----
    Case("cmd/未注册", "command", "ou_ghost"),
    Case("cmd/未报名", "command", "ou_m1",
         tables={**_default_tables(), SIGNUP_TABLE_ID: []}),
    Case("cmd/无收集中活动", "command", "ou_m1",
         tables={**_default_tables(),
                 ACTIVITY_TABLE_ID: [_act(一对一状态="已完成")]}),
    Case("cmd/单个活动", "command", "ou_m1"),
    Case("cmd/多个活动", "command", "ou_m1",
         tables={**_default_tables(),
                 ACTIVITY_TABLE_ID: [_act(), _act(
                     活动ID=ACT2, 活动名称="关系自洽二")],
                 SIGNUP_TABLE_ID: [_signup("ou_m1", "小北"),
                                   _signup("ou_m1", "小北", activity=ACT2)]}),

    # ---- 用户提交（10）----
    Case("sub/活动不存在", "submit", action_value={"activity_id": "A-9999"}),
    Case("sub/已截止", "submit",
         tables={**_default_tables(), ACTIVITY_TABLE_ID: [_act(一对一状态="已截止")]}),
    Case("sub/未报名", "submit", operator="ou_m2",
         tables=_rich_tables()),
    Case("sub/用户不存在", "submit", operator="ou_ghost",
         tables={**_rich_tables(),
                 SIGNUP_TABLE_ID: _rich_tables()[SIGNUP_TABLE_ID] + [_signup("ou_ghost", "鬼")]}),
    Case("sub/同名多人", "submit", tables=_rich_tables(),
         form_value=_form("佳佳", *SEVEN[:6])),
    Case("sub/编号解析", "submit", tables=_rich_tables(),
         form_value=_form("U-0003", *SEVEN[1:])),
    Case("sub/志愿不足", "submit", tables=_rich_tables(),
         form_value=_form(*SEVEN[:6])),
    Case("sub/重复志愿", "submit", tables=_rich_tables(),
         form_value=_form(SEVEN[0], *SEVEN[:6])),
    Case("sub/无效参与者", "submit", tables=_rich_tables(),
         form_value=_form("ou_m2", *SEVEN[1:])),
    Case("sub/selections分支", "submit", tables=_rich_tables(),
         action_value={"activity_id": ACT, "selections": SEVEN}),
    Case("sub/新增成功", "submit", tables=_rich_tables(),
         form_value=_form(*SEVEN)),
    Case("sub/更新成功", "submit", tables=_update_tables(),
         form_value=_form(*SEVEN)),

    # ---- 管理员开始（4）----
    Case("start/格式错误", "start", ""),
    Case("start/活动不存在", "start", "A-9999"),
    Case("start/成功无历史", "start", ACT),
    Case("start/成功有历史", "start", ACT,
         tables={**_default_tables(),
                 ONETOONE_RESULT_TABLE: [_res("ou_m1", "ou_f1")]}),

    # ---- 管理员执行（10）----
    Case("stop/格式错误", "stop", ""),
    Case("stop/活动不存在", "stop", "A-9999"),
    Case("stop/幂等拦截", "stop", ACT,
         tables={**_default_tables(),
                 ONETOONE_RESULT_TABLE: [_res("ou_m1", "ou_f1")]}),
    Case("stop/无参与", "stop", ACT,
         tables={**_default_tables(), ONETOONE_SELECT_TABLE: []}),
    Case("stop/全无性别", "stop", ACT,
         tables={**_default_tables(),
                 ONETOONE_SELECT_TABLE: [_sel("ou_m1", ""), _sel("ou_f1", "")]}),
    Case("stop/执行成功", "stop", ACT),
    Case("stop/成功无结果", "stop", ACT,
         tables={**_default_tables(),
                 ONETOONE_SELECT_TABLE: [_sel("ou_m1", "男性"), _sel("ou_m2", "男性")]}),
    Case("stop/写入不完整", "stop", ACT, fail_create_after=1),
    Case("stop/算法异常", "stop", ACT, algorithm=_raise(ValueError("炸了"))),
    Case("stop/幂等查询失败", "stop", ACT, search_fails={ONETOONE_RESULT_TABLE}),
    Case("stop/志愿查询失败", "stop", ACT, search_fails={ONETOONE_SELECT_TABLE}),

    # ---- 管理员状态（3）----
    Case("status/格式错误", "status", "   "),
    Case("status/活动不存在", "status", "A-9999"),
    Case("status/成功", "status", ACT),

    # ---- 管理员未提交（5）----
    Case("unsubmitted/格式错误", "unsubmitted", "   "),
    Case("unsubmitted/活动不存在", "unsubmitted", "A-9999"),
    Case("unsubmitted/全部已提交", "unsubmitted", ACT),
    Case("unsubmitted/部分未提交", "unsubmitted", ACT,
         tables={**_default_tables(),
                 ONETOONE_SELECT_TABLE: [
                     _sel("ou_m1", "男性", ("ou_f1",)), _sel("ou_m2", "男性", ("ou_f2",)),
                     _sel("ou_f1", "女性", ("ou_m1",))]}),
    Case("unsubmitted/缺用户ID", "unsubmitted", ACT,
         tables={**_default_tables(),
                 SIGNUP_TABLE_ID: [_signup("ou_nouid", "无号")],
                 ONETOONE_SELECT_TABLE: []}),
]


# ---------------------------------------------------------------- 驱动

def _patch_golden(monkey, fake, case):
    """把冻结旧实作接到假表上。

    `search_records` 外面包一层 None→[]，与迁移前 bot/clients.py 的语义一模一样；
    新路 lib 内部调的是 raw 的 client.search_records，由业务函数自己决定怎么解释
    None。两条路最终行为必须一致——这正是要验的东西。
    """
    noop = lambda *a, **k: None  # noqa: E731
    monkey.setattr(golden, "ONETOONE_SELECT_TABLE", ONETOONE_SELECT_TABLE, raising=False)
    monkey.setattr(golden, "ONETOONE_RESULT_TABLE", ONETOONE_RESULT_TABLE, raising=False)
    # 算法权重常量已随算法搬进 lib，bot/constants.py 那边没有了。黄金副本是从
    # `from constants import *` 拿它们的，不喂就是 NameError → 被当成「算法出错」
    # 吞掉，两条路都返回同一句错误，测试照样绿——那种绿是假的。
    for name in ("WEIGHT_ONETOONE_MY_PRIORITY", "WEIGHT_ONETOONE_THEIR_PRIORITY",
                 "WEIGHT_ONETOONE_MATCH", "ONETOONE_UNSELECTED_SCORE"):
        monkey.setattr(golden, name, getattr(lib_onetoone, name), raising=False)
    monkey.setattr(golden, "search_records",
                   lambda *a, **k: fake.search_records(*a, **k) or [], raising=False)
    monkey.setattr(golden, "update_record", fake.update_record, raising=False)
    monkey.setattr(golden, "create_record", fake.create_record, raising=False)
    monkey.setattr(golden, "batch_create_records", fake.batch_create_records, raising=False)
    monkey.setattr(golden, "batch_delete_records", fake.batch_delete_records, raising=False)
    monkey.setattr(golden, "find_activity_by_id", lambda aid: _find_act(fake, aid), raising=False)
    monkey.setattr(golden, "find_user_by_id_or_name", _find_user_by_id_or_name, raising=False)
    monkey.setattr(golden, "find_user_by_openid", _find_user, raising=False)
    # 迁移前 get_activity_signups 来自 bot/grouping.py → lib.grouping.collect_participants；
    # 用同一份真实实现 + 假表驱动，保持「旧路依赖」的原样。
    monkey.setattr(
        golden, "get_activity_signups",
        lambda aid: lib_grouping.collect_participants(
            aid, client=fake, find_user_by_openid=_find_user),
        raising=False)
    monkey.setattr(golden, "send_card_message", noop, raising=False)
    monkey.setattr(golden, "log", noop, raising=False)
    if case.algorithm is not None:
        monkey.setattr(golden, "run_onetoone_matching", case.algorithm)


def _patch_new(monkey, fake, case):
    """新路：lib 拿到的是 client 实例与 find_user_by_openid 注入。"""
    noop = lambda *a, **k: None  # noqa: E731
    monkey.setattr(bot_onetoone, "bitable", fake)
    monkey.setattr(bot_onetoone, "search_records",
                   lambda *a, **k: fake.search_records(*a, **k) or [])
    monkey.setattr(bot_onetoone, "find_activity_by_id", lambda aid: _find_act(fake, aid))
    monkey.setattr(bot_onetoone, "find_user_by_id_or_name", _find_user_by_id_or_name)
    monkey.setattr(bot_onetoone, "find_user_by_openid", _find_user)
    monkey.setattr(bot_onetoone, "send_card_message", noop)
    monkey.setattr(lib_onetoone, "log", noop)
    if case.algorithm is not None:
        monkey.setattr(lib_onetoone, "run_onetoone_matching", case.algorithm)


def _run_golden(case):
    if case.handler == "command":
        return golden.handle_onetoone_command(case.keyword)
    if case.handler == "submit":
        return golden.handle_onetoone_submit(case.operator, case.action_value, case.form_value)
    if case.handler == "start":
        return golden.handle_admin_start_onetoone(case.keyword)
    if case.handler == "stop":
        return golden.handle_admin_stop_onetoone(case.keyword)
    if case.handler == "status":
        return golden.handle_admin_onetoone_status(case.keyword)
    if case.handler == "unsubmitted":
        return golden.handle_admin_unsubmitted_onetoone(case.keyword)
    raise AssertionError(f"未知 handler: {case.handler}")


def _run_new(case):
    if case.handler == "command":
        return bot_onetoone.handle_onetoone_command(case.keyword)
    if case.handler == "submit":
        return bot_onetoone.handle_onetoone_submit(case.operator, case.action_value, case.form_value)
    if case.handler == "start":
        return bot_onetoone.handle_admin_start_onetoone(case.keyword)
    if case.handler == "stop":
        return bot_onetoone.handle_admin_stop_onetoone(case.keyword)
    if case.handler == "status":
        return bot_onetoone.handle_admin_onetoone_status(case.keyword)
    if case.handler == "unsubmitted":
        return bot_onetoone.handle_admin_unsubmitted_onetoone(case.keyword)
    raise AssertionError(f"未知 handler: {case.handler}")


@pytest.fixture
def configured(monkeypatch, tmp_path):
    """两条路共用同一份注入：表 ID / 栏位名 / 锁目录都走同一个入口，别各配各的。"""
    monkeypatch.setattr(bot_onetoone, "ONETOONE_SELECT_TABLE", ONETOONE_SELECT_TABLE)
    monkeypatch.setattr(bot_onetoone, "ONETOONE_RESULT_TABLE", ONETOONE_RESULT_TABLE)
    bot_grouping.configure_grouping_lib(str(tmp_path))
    bot_onetoone.configure_onetoone_lib(str(tmp_path))
    return monkeypatch


def _build(case):
    return FakeBitable(case.tables, case.fail_create_after, case.search_fails)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_bot_text_is_identical_to_frozen_impl(configured, case):
    """新旧两条路的回传字符串必须逐字相同——这是本次的红线。"""
    old, new = _build(case), _build(case)
    _patch_golden(configured, old, case)
    old_text = _run_golden(case)
    _patch_new(configured, new, case)
    new_text = _run_new(case)

    assert new_text == old_text, (
        f"文案漂移\n--- 旧 ---\n{old_text!r}\n--- 新 ---\n{new_text!r}")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_side_effects_are_identical(configured, case):
    """文字相同还不够：写进飞书的每一条也要相同，否则表已经被改了话才算对。"""
    old, new = _build(case), _build(case)
    _patch_golden(configured, old, case)
    _run_golden(case)
    _patch_new(configured, new, case)
    _run_new(case)

    assert new.writes() == old.writes(), (
        f"副作用漂移\n--- 旧 ---\n{old.writes()}\n--- 新 ---\n{new.writes()}")
    assert new.tables == old.tables
