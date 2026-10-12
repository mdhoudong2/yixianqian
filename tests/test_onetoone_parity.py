"""等价性安全网：一对一逻辑迁到 lib.onetoone 后，bot 指令回传文字与副作用必须一字不差。

同一个桩表、同一个输入，旧路（tests/_golden_onetoone_impl.py，自 HEAD 9f7f2f5 冻结）
与新路（lib.onetoone + bot 渲染层）各跑一遍，断言：

    回传字符串完全相同  且  写入调用序列与最终表内容完全相同

桩的只是「存取」，规则（幂等判断、状态流转、算法、回滚）全跑真实现——桩掉规则就
只剩在测 mock 了（与 tests/test_grouping_parity.py 同一取舍）。

案例覆盖全部 6 个 handler 的每个 return 分支。C2 审核修复刻意改了少数失败分支的
行为（读失败不再当空表、写入失败不再报成功、锁内查重等），那些案例单独列在
「刻意分歧」一节，不回入逐字比对；其余案例必须保持全绿。多轮功能（回复带轮次、
幂等按 (活动, 轮次)、结果写入带「轮次」栏位、状态显示已生成轮次）另开
「本次功能（多轮）：刻意分歧」一节，同样旧/新双断言。
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

    def __init__(self, tables=None, fail_create_after=None, search_fails=None,
                 write_fails=None, search_fail_plan=None, delete_limit=None,
                 write_fail_plan=None):
        # 必须连 fields 一起深拷贝：同一个 Case 会被建两次（旧路一次、新路一次），
        # 只拷外层 dict 的话两条路共用同一批 fields，第一条跑完就把第二条的起点改了。
        self.tables = {k: [{"record_id": r["record_id"], "fields": dict(r["fields"])}
                           for r in v] for k, v in (tables or {}).items()}
        self.fail_create_after = fail_create_after
        self.search_fails = search_fails or set()
        # 细粒度注入（C2 审核修复的分歧案例用）：
        #   write_fails：这些表的 update/create 一律返回 None（写失败）
        #   write_fail_plan：{表: {第 N 次 update 失败（1 起）}}
        #   search_fail_plan：{表: {第 N 次 search 失败（1 起）}}
        #   delete_limit：批量删除最多删几条（模拟删除数不足）
        self.write_fails = write_fails or set()
        self.write_fail_plan = write_fail_plan or {}
        self.search_fail_plan = search_fail_plan or {}
        self.delete_limit = delete_limit
        self.calls = []
        self._seq = 0
        self._search_counts = {}
        self._update_counts = {}

    # -- 读 --
    def search_records(self, table_id, filter_conditions=None, *a, **k):
        self.calls.append(("search", table_id))
        count = self._search_counts.get(table_id, 0) + 1
        self._search_counts[table_id] = count
        if table_id in self.search_fails or count in self.search_fail_plan.get(table_id, set()):
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
        count = self._update_counts.get(table_id, 0) + 1
        self._update_counts[table_id] = count
        if table_id in self.write_fails or count in self.write_fail_plan.get(table_id, set()):
            return None
        for r in self.tables.get(table_id, []):
            if r["record_id"] == record_id:
                r["fields"].update(fields)
                return r
        return None

    def create_record(self, table_id, fields):
        self.calls.append(("create", table_id, dict(fields)))
        if table_id in self.write_fails:
            return None
        self._seq += 1
        row = {"record_id": f"rec_new{self._seq}", "fields": dict(fields)}
        self.tables.setdefault(table_id, []).append(row)
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
        ids = list(record_ids)
        if self.delete_limit is not None:
            ids = ids[:self.delete_limit]
        before = len(self.tables.get(table_id, []))
        keep = [r for r in self.tables.get(table_id, []) if r["record_id"] not in ids]
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
    # 「stop/格式错误」「stop/幂等拦截」「stop/执行成功」「stop/成功无结果」
    # 「stop/写入不完整」因本次多轮功能刻意分歧（回复带轮次、结果写入带「轮次」
    # 栏位），移到文件末尾的「本次功能（多轮）：刻意分歧」一节做旧/新双断言。
    Case("stop/活动不存在", "stop", "A-9999"),
    Case("stop/无参与", "stop", ACT,
         tables={**_default_tables(), ONETOONE_SELECT_TABLE: []}),
    Case("stop/全无性别", "stop", ACT,
         tables={**_default_tables(),
                 ONETOONE_SELECT_TABLE: [_sel("ou_m1", ""), _sel("ou_f1", "")]}),
    Case("stop/算法异常", "stop", ACT, algorithm=_raise(ValueError("炸了"))),

    # ---- 管理员状态（3）----
    Case("status/格式错误", "status", "   "),
    Case("status/活动不存在", "status", "A-9999"),
    # 「status/成功」因新增「已生成轮次」行刻意分歧，同样移到末尾一节。

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


# ================================================================
# C2 审核修复：刻意分歧（旧路行为 ≠ 新路行为）
# ----------------------------------------------------------------
# 下列案例是审核确定的「必须变」的行为：读失败不再当空表、写失败不再报成功、
# 志愿放宽为 1..7、提交走锁内查重等。它们不能回入上面的逐字比对，所以每条都
# 同时钉旧行为与新行为——防止哪天有人把修复回退成「和旧路一致」还以为是保真。
# ================================================================

def _golden_run(configured, case, fake=None):
    fake = fake if fake is not None else _build(case)
    _patch_golden(configured, fake, case)
    return fake, _run_golden(case)


def _new_run(configured, case, fake=None):
    fake = fake if fake is not None else _build(case)
    _patch_new(configured, fake, case)
    return fake, _run_new(case)


def test_submit_allows_one_to_seven_choices_intentional_fix(configured):
    """审核修复：小活动凑不满 7 人时 bot 也能提交（旧路只认恰好 7 个）。

    COUNT 分支的文案随之改为「请选择1-7位（当前N位）」——1..7 全范围都要能过，
    8 个仍然拦下（文案是新口径）。
    """
    case = Case("sub/单个志愿", "submit", tables=_rich_tables(),
                form_value=_form("ou_f1"))

    _, old = _golden_run(configured, case)
    assert old == {"toast": {"type": "error", "content": "请选择7位（当前1位）"}}

    fake, new = _new_run(configured, case)
    assert new == {"toast": {"type": "success", "content": "选择已提交"}}
    created = [c for c in fake.writes() if c[0] == "create"]
    assert len(created) == 1

    case8 = Case("sub/八个志愿", "submit", tables=_rich_tables(),
                 action_value={"activity_id": ACT, "selections": SEVEN + ["ou_f1"]})
    _, new8 = _new_run(configured, case8)
    assert new8 == {"toast": {"type": "error", "content": "请选择1-7位（当前8位）"}}


def test_submit_write_failure_is_not_reported_as_success(configured):
    """审核修复 #4：写失败不得报成功（旧实现不检查 update/create 返回值）。"""
    case = Case("sub/写失败", "submit", tables=_rich_tables(),
                form_value=_form(*SEVEN))

    _, old = _golden_run(configured, case,
                         FakeBitable(case.tables, write_fails={ONETOONE_SELECT_TABLE}))
    assert old == {"toast": {"type": "success", "content": "选择已提交"}}, "旧实现写失败仍报成功"

    fake, new = _new_run(configured, case,
                         FakeBitable(case.tables, write_fails={ONETOONE_SELECT_TABLE}))
    assert new == {"toast": {"type": "error", "content": "提交失败，请稍后再试"}}
    writes = fake.writes()
    assert len(writes) == 1 and writes[0][0] == "create", "只应有一次失败的 create 尝试"
    assert fake.tables.get(ONETOONE_SELECT_TABLE, []) == [], "写失败不得留下记录"


def test_submit_lookup_failure_aborts_instead_of_creating(configured):
    """审核修复 #5：查重读失败不得当空表去 create（重复志愿行的源头）。"""
    case = Case("sub/查重读失败", "submit", tables=_rich_tables(),
                form_value=_form(*SEVEN))

    _, old = _golden_run(configured, case,
                         FakeBitable(case.tables, search_fails={ONETOONE_SELECT_TABLE}))
    assert old == {"toast": {"type": "success", "content": "选择已提交"}}, "旧实现读失败当空表照样插行"

    fake, new = _new_run(configured, case,
                         FakeBitable(case.tables, search_fails={ONETOONE_SELECT_TABLE}))
    assert new == {"toast": {"type": "error", "content": "提交失败，请稍后再试"}}
    assert fake.writes() == [], "读失败必须零写入"


def test_admin_stop_idempotency_read_failure_aborts_zero_writes(configured):
    """审核修复 #5①：幂等查历史结果读失败 → 中止（零写入），不得重跑覆盖。"""
    case = Case("stop/幂等读失败", "stop", ACT)
    fake = FakeBitable(case.tables, search_fails={ONETOONE_RESULT_TABLE})

    # 旧路：读失败当空表 → 继续跑并写结果
    fake_g = FakeBitable(case.tables, search_fails={ONETOONE_RESULT_TABLE})
    _patch_golden(configured, fake_g, case)
    old = _run_golden(case)
    assert "一对一匹配完成" in old
    assert [c for c in fake_g.writes() if c[0] == "batch_create"], "旧路读失败仍重跑"

    _patch_new(configured, fake, case)
    new = _run_new(case)
    assert new == "查询历史结果失败，无法确认是否已执行过，已中止（本次未写入任何数据），请稍后重试。"
    assert fake.writes() == [], "读失败必须零写入"


def test_admin_stop_selections_read_failure_restores_and_reports(configured):
    """审核修复 #5③：志愿读失败 → 恢复「收集中」+ 明说失败（旧路伪装「没有参与者」）。"""
    case = Case("stop/志愿读失败", "stop", ACT)

    _, old = _golden_run(configured, case,
                         FakeBitable(case.tables, search_fails={ONETOONE_SELECT_TABLE}))
    assert old == "没有已提交志愿的参与者，状态已恢复为「收集中」。", \
        "旧路把读失败伪装成「没有参与者」"

    fake, new = _new_run(configured, case,
                         FakeBitable(case.tables, search_fails={ONETOONE_SELECT_TABLE}))
    assert new == "读取一对一志愿失败，已中止匹配，状态已恢复为「收集中」，请稍后重试。"
    assert _find_act(fake, ACT)["fields"]["一对一状态"] == "收集中"
    assert not [c for c in fake.writes() if c[0] == "batch_create"]


def test_partial_write_rollback_verify_failure_never_claims_rollback(configured):
    """审核修复 #5⑥：部分写入 + 回查失败 → 不得宣称已回滚，明说可能残留。"""
    case = Case("stop/回滚校验失败", "stop", ACT)

    # 旧路：回查失败当空表 → 不删残留，却回「已回滚」
    fake_g = FakeBitable(case.tables, fail_create_after=1,
                         search_fail_plan={ONETOONE_RESULT_TABLE: {2}})
    _patch_golden(configured, fake_g, case)
    old = _run_golden(case)
    assert "已回滚" in old
    assert len(fake_g.tables[ONETOONE_RESULT_TABLE]) == 1, "旧路实际残留 1 条却谎称已回滚"

    fake, new = _new_run(configured, case,
                         FakeBitable(case.tables, fail_create_after=1,
                                     search_fail_plan={ONETOONE_RESULT_TABLE: {2}}))
    assert new == ("结果写入不完整（成功 1/8）且回滚校验失败，可能残留结果；"
                   "状态已恢复收集中，请先『开始一对一』清理残留后再执行")
    assert len(fake.tables[ONETOONE_RESULT_TABLE]) == 1, "无法确认回滚时如实保留残留"
    assert _find_act(fake, ACT)["fields"]["一对一状态"] == "收集中"


def test_partial_write_delete_incomplete_reports_residual(configured):
    """审核修复 #5⑥：回查成功但删除数不足 → 明报残留条数，不得说「已回滚」。"""
    case = Case("stop/删除不完整", "stop", ACT)
    fake, new = _new_run(configured, case,
                         FakeBitable(case.tables, fail_create_after=3, delete_limit=1))
    assert new == ("一对一结果写入失败（成功 3/8 条），回滚不完整（残留 2 条结果），"
                   "状态已恢复为「收集中」，请先发送「开始一对一」清理残留后再执行。")
    assert len(fake.tables[ONETOONE_RESULT_TABLE]) == 2


def test_partial_write_clean_rollback_keeps_original_message(configured):
    """回滚干净时保留原文案（与旧路逐字相同），只有回滚异常才换措辞。"""
    case = Case("stop/写入不完整", "stop", ACT)
    fake, new = _new_run(configured, case, FakeBitable(case.tables, fail_create_after=1))
    assert new == ("一对一结果写入失败（成功 1/8 条），已回滚，"
                   "状态恢复为「收集中」，请稍后重试。")
    assert fake.tables[ONETOONE_RESULT_TABLE] == []


def test_admin_stop_close_status_write_failure_aborts(configured):
    """审核修复 #4②：状态切「已截止」写失败 → 中止（旧路忽略返回值继续写结果）。"""
    case = Case("stop/截止写失败", "stop", ACT)

    _, old = _golden_run(configured, case,
                         FakeBitable(case.tables, write_fail_plan={ACTIVITY_TABLE_ID: {1}}))
    assert "一对一匹配完成" in old, "旧路状态写失败也一样报成功"

    fake, new = _new_run(configured, case,
                         FakeBitable(case.tables, write_fail_plan={ACTIVITY_TABLE_ID: {1}}))
    assert new == "活动状态写入失败（无法切换为「已截止」），已中止匹配，请稍后重试。"
    writes = fake.writes()
    assert len(writes) == 1 and writes[0][0] == "update", "截止失败后不得再有任何写入"
    assert not [c for c in writes if c[0] == "batch_create"]


def test_admin_stop_done_status_write_failure_reports_explicit_error(configured):
    """审核修复 #4⑦：结果已生成但「已完成」写失败 → 明确错误（不说成功）。"""
    case = Case("stop/完成写失败", "stop", ACT)
    fake, new = _new_run(configured, case,
                         FakeBitable(case.tables, write_fail_plan={ACTIVITY_TABLE_ID: {2}}))
    assert new == "结果已生成，但状态写入失败（当前状态：已截止），请稍后重试或让管理员检查"
    assert fake.tables[ONETOONE_RESULT_TABLE], "结果确实已写入"
    assert _find_act(fake, ACT)["fields"]["一对一状态"] == "已截止"


def test_admin_start_status_write_failure_reports_error(configured):
    """审核修复 #4/#8：开始一对一状态写失败 → 明确错误（旧路照样回「已开始」）。"""
    case = Case("start/状态写失败", "start", ACT)

    _, old = _golden_run(configured, case,
                         FakeBitable(case.tables, write_fail_plan={ACTIVITY_TABLE_ID: {1}}))
    assert "一对一志愿填写已开始" in old

    fake, new = _new_run(configured, case,
                         FakeBitable(case.tables, write_fail_plan={ACTIVITY_TABLE_ID: {1}}))
    assert new == "一对一志愿填写开始失败：活动「关系自洽」状态写入失败，请稍后重试。"
    writes = fake.writes()
    assert len(writes) == 1 and writes[0][0] == "update"


def test_admin_start_results_search_failure_reports_residual_risk(configured):
    """审核修复 #8：清历史结果读失败 → 明说可能残留（不得报「已开始」）。"""
    case = Case("start/清历史读失败", "start", ACT)
    fake, new = _new_run(configured, case,
                         FakeBitable(case.tables, search_fails={ONETOONE_RESULT_TABLE}))
    assert new == ("一对一志愿填写开始失败：活动「关系自洽」状态已写入「收集中」，"
                   "但读取历史结果失败，历史结果可能残留；请稍后重试「开始一对一」清理后再执行。")
    assert _find_act(fake, ACT)["fields"]["一对一状态"] == "收集中"


def test_admin_start_delete_incomplete_reports_failure(configured):
    """审核修复 #8：删除数不足 → 明报失败（旧路忽略删除返回值照样报成功）。"""
    tables = {**_default_tables(),
              ONETOONE_RESULT_TABLE: [_res("ou_m1", "ou_f1"),
                                      _res("ou_m2", "ou_f2"),
                                      _res("ou_f1", "ou_m1")]}
    case = Case("start/删除不完整", "start", ACT, tables=tables)

    _, old = _golden_run(configured, case, FakeBitable(tables, delete_limit=2))
    assert "一对一志愿填写已开始" in old

    fake, new = _new_run(configured, case, FakeBitable(tables, delete_limit=2))
    assert new == ("一对一志愿填写开始失败：活动「关系自洽」历史结果清理不完整"
                   "（成功删除 2/3 条），请稍后重试「开始一对一」清理残留。")
    assert len(fake.tables[ONETOONE_RESULT_TABLE]) == 1


def test_admin_start_busy_lock_reports_busy(configured, monkeypatch):
    """审核修复 #8：整体持跨进程活动锁；锁被占时明确回执，绝不无限等待。"""
    case = Case("start/忙", "start", ACT)
    fake = _build(case)
    _patch_new(configured, fake, case)
    monkeypatch.setattr(lib_onetoone, "START_LOCK_TIMEOUT", 0.1)
    with lib_grouping.grouping_lock(ACT, timeout=1):
        text = bot_onetoone.handle_admin_start_onetoone(ACT)
    assert text == "当前已有一对一任务在执行中，请稍候再试。"
    assert fake.writes() == []


def test_submit_busy_lock_reports_busy(configured, monkeypatch):
    """审核修复 #3：提交也走同一把活动锁；锁被占时错误提示（旧路无锁直接写）。"""
    case = Case("sub/忙", "submit", tables=_rich_tables(), form_value=_form(*SEVEN))
    fake = _build(case)
    _patch_new(configured, fake, case)
    monkeypatch.setattr(lib_onetoone, "SUBMIT_LOCK_TIMEOUT", 0.1)
    with lib_grouping.grouping_lock(ACT, timeout=1):
        result = bot_onetoone.handle_onetoone_submit("ou_m1", case.action_value, case.form_value)
    assert result == {"toast": {"type": "error", "content": "当前已有一对一操作正在进行，请稍后重试。"}}
    assert fake.writes() == []


def test_status_submitted_count_uses_dedupe_intentional_fix(configured):
    """审核修复 #14：状态指令「已提交选择」按非空 open_id 去重（旧路按行数）。"""
    tables = {**_default_tables(),
              ONETOONE_SELECT_TABLE: [
                  _sel("ou_m1", "男性", ("ou_f1",)),
                  _sel("ou_m1", "男性", ("ou_f2",)),   # 同一人的重复行（脏数据）
                  _sel("ou_f1", "女性", ("ou_m1",)),
              ]}
    case = Case("status/重复行", "status", ACT, tables=tables)

    _, old = _golden_run(configured, case)
    assert "已提交选择：3人" in old, "旧口径按记录行数"

    _, new = _new_run(configured, case)
    assert "已提交选择：2人" in new, "去重口径只算 2 人（与分组端统一）"


# ================================================================
# 本次功能（一對一多輪）：刻意分歧（旧路行为 ≠ 新路行为）
# ----------------------------------------------------------------
# 「执行一对一」新增可选轮次（默认第 1 轮）、幂等按 (活动, 轮次)（迁移前的空轮次
# 行 = 第 1 轮）、结果写入带「轮次」栏位；「一对一状态」新增「已生成轮次」行。
# 冻结旧实作没有轮次概念，以下路径刻意分歧；未变路径仍在上面的 strict 案例集里
# 逐字比对（文案 + 写入调用序列）。每条同时钉旧行为与新行为，防止日后有人把多轮
# 回退成「和旧路一致」还以为是保真。
# ================================================================

def test_admin_stop_help_line_mentions_round(configured):
    """空参格式说明补 [轮次]（旧路没有轮次）。"""
    case = Case("stop/格式错误", "stop", "")
    _, old = _golden_run(configured, case)
    assert old == "格式：执行一对一 活动ID\n例如：执行一对一 A-0002"

    _, new = _new_run(configured, case)
    assert new == ("格式：执行一对一 活动ID [轮次]\n"
                   "例如：执行一对一 A-0002 或 执行一对一 A-0002 2")


def test_admin_stop_round1_legacy_row_still_idempotent_text_has_round(configured):
    """迁移前的空轮次结果行 = 第 1 轮：默认第 1 轮仍被幂等拦住，文案带「第1轮」。"""
    case = Case("stop/幂等拦截", "stop", ACT,
                tables={**_default_tables(),
                        ONETOONE_RESULT_TABLE: [_res("ou_m1", "ou_f1")]})

    old_fake, old = _golden_run(configured, case)
    assert old == ("活动「关系自洽」已生成过一对一结果，请勿重复执行。如需重新匹配，请先发送"
                   "「开始一对一 A-0002」重新开放，再执行。")
    assert old_fake.writes() == []

    new_fake, new = _new_run(configured, case)
    assert new == ("活动「关系自洽」第1轮已生成过一对一结果，请勿重复执行。如需重新匹配，请先发送"
                   "「开始一对一 A-0002」重新开放，再执行。")
    assert new_fake.writes() == [], "被挡下的重跑不得再写"


def test_admin_stop_success_text_and_writes_carry_round(configured):
    """成功路径：回执带「第1轮」；每条结果都带「轮次=1」栏位（旧路结果行没有轮次）。"""
    case = Case("stop/执行成功", "stop", ACT)

    old_fake, old = _golden_run(configured, case)
    assert old == ("🎉 活动「关系自洽」一对一匹配完成：\n"
                   "参与 4 人，共生成 8 条必聊关系。\n"
                   "用户可自查 H5「我的一对一」查看名单。")
    old_created = [c for c in old_fake.writes() if c[0] == "batch_create"][0]
    assert old_created[2] and all("轮次" not in r for r in old_created[2])

    new_fake, new = _new_run(configured, case)
    assert new == ("🎉 活动「关系自洽」第1轮 一对一匹配完成：\n"
                   "参与 4 人，共生成 8 条必聊关系。\n"
                   "用户可自查 H5「我的一对一」查看名单。")
    new_created = [c for c in new_fake.writes() if c[0] == "batch_create"][0]
    assert new_created[2] and all(r.get("轮次") == "1" for r in new_created[2])


def test_admin_stop_success_without_pairs_text_has_round(configured):
    """成功但 0 条必聊（无异性参与者）：回执仍带轮次（旧路无轮次）。"""
    case = Case("stop/成功无结果", "stop", ACT,
                tables={**_default_tables(),
                        ONETOONE_SELECT_TABLE: [_sel("ou_m1", "男性"), _sel("ou_m2", "男性")]})

    _, old = _golden_run(configured, case)
    assert old == ("🎉 活动「关系自洽」一对一匹配完成：\n"
                   "参与 2 人，共生成 0 条必聊关系。\n"
                   "用户可自查 H5「我的一对一」查看名单。")

    _, new = _new_run(configured, case)
    assert new == ("🎉 活动「关系自洽」第1轮 一对一匹配完成：\n"
                   "参与 2 人，共生成 0 条必聊关系。\n"
                   "用户可自查 H5「我的一对一」查看名单。")


def test_admin_stop_partial_write_writes_carry_round(configured):
    """写入不完整回滚：文案不变，但写入行带轮次（回滚只针对本轮）。"""
    case = Case("stop/写入不完整", "stop", ACT, fail_create_after=1)

    old_fake, old = _golden_run(configured, case)
    assert "已回滚" in old
    old_created = [c for c in old_fake.writes() if c[0] == "batch_create"][0]
    assert old_created[2] and all("轮次" not in r for r in old_created[2])

    new_fake, new = _new_run(configured, case)
    assert new == old, "回滚文案不变（只有写入行多了轮次）"
    new_created = [c for c in new_fake.writes() if c[0] == "batch_create"][0]
    assert new_created[2] and all(r.get("轮次") == "1" for r in new_created[2])
    assert new_fake.tables[ONETOONE_RESULT_TABLE] == [], "本轮部分写入回滚干净"


def test_admin_stop_round2_runs_and_keeps_round1(configured):
    """旧路完全不认「A-0002 2」（整串当活动ID找不到）；新路跑第2轮且保留第1轮结果。"""
    tables = {**_default_tables(),
              ONETOONE_RESULT_TABLE: [
                  {"record_id": "rec_r1",
                   "fields": {**_res("ou_m1", "ou_f1")["fields"], "轮次": "1"}}]}
    case = Case("stop/第2轮", "stop", f"{ACT} 2", tables=tables)

    _, old = _golden_run(configured, case)
    assert old == "未找到活动：A-0002 2"

    fake, new = _new_run(configured, case)
    assert new == ("🎉 活动「关系自洽」第2轮 一对一匹配完成：\n"
                   "参与 4 人，共生成 8 条必聊关系。\n"
                   "用户可自查 H5「我的一对一」查看名单。")
    rows = fake.tables[ONETOONE_RESULT_TABLE]
    r1 = [r for r in rows if r["fields"].get("轮次") == "1"]
    r2 = [r for r in rows if r["fields"].get("轮次") == "2"]
    assert len(r1) == 1 and len(r2) == 8, "第1轮历史保留，第2轮新写入"


def test_admin_stop_round_argument_validation(configured):
    """非法轮次：新路明确报轮次错误（旧路把整串当活动ID）。"""
    for keyword, expected in ((f"{ACT} 0", "轮次必须为大于0的整数"),
                              (f"{ACT} x", "轮次必须为数字，当前：x")):
        case = Case("stop/坏轮次", "stop", keyword)
        _, old = _golden_run(configured, case)
        assert old == f"未找到活动：{keyword}"
        _, new = _new_run(configured, case)
        assert new == expected


def test_admin_status_rounds_line_intentional_addition(configured):
    """「一对一状态」新增「已生成轮次」行（旧路无此信息）；迁移前行显示为第1轮。"""
    case = Case("status/成功", "status", ACT)
    _, old = _golden_run(configured, case)
    assert old == ("活动「关系自洽」一对一状态：\n"
                   "状态：收集中\n"
                   "报名人数：4\n"
                   "已提交选择：4人")

    _, new = _new_run(configured, case)
    assert new == ("活动「关系自洽」一对一状态：\n"
                   "状态：收集中\n"
                   "报名人数：4\n"
                   "已提交选择：4人\n"
                   "已生成轮次：无")

    case2 = Case("status/有轮次", "status", ACT,
                 tables={**_default_tables(), ONETOONE_RESULT_TABLE: [_res("ou_m1", "ou_f1")]})
    _, new2 = _new_run(configured, case2)
    assert new2.endswith("已生成轮次：第1轮")
