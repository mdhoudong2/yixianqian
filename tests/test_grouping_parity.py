"""等价性安全网：迁移后的 bot 分组指令，回传文字与副作用必须与旧实作**一字不差**。

同一个桩表、同一个输入，旧路（tests/_golden_*_impl.py，从 base commit 8d68f41 冻结）
与新路（lib.grouping + bot 渲染层）各跑一遍，断言：

注意 keyword 是**去掉指令前缀之后**的尾巴（bot/yixianqian_bot_ws.py 用
`text[len("开始填志愿"):].strip()` 切出来再丢进来），不是整句聊天原文。
写错了不会让测试红，只会让所有案例都掉进同一个早退分支——看起来全绿，
其实什么都没测。改这里之前先跑 /tmp/dump 类的脚本把实际回传印出来核对。

    回传字符串完全相同  且  写入调用序列与最终表内容完全相同

桩的只是「存取」，规则（幂等判断、回滚、算法、状态流转）全跑真实现——桩掉规则就
只剩在测 mock 了（与 tests/test_praise_api.py 的 FakeTable 同一取舍）。

案例覆盖每个 handle_* 的每个 return 分支。新增字串不在这个范围内（黄金副本里本来
就没有），必须在 PR 说明里单独列出。
"""
import re

import commands as bot_commands
import grouping as bot_grouping
import pytest
from constants import (
    ACTIVITY_TABLE_ID,
    FIELD_GS_CHOICES,
    FIELD_GS_SELECTOR_GENDER,
    GROUP_RESULT_TABLE,
    GROUP_SELECT_TABLE,
    SIGNUP_TABLE_ID,
)

from lib import grouping as lib_grouping
from tests import _golden_commands_impl as golden_commands
from tests import _golden_grouping_impl as golden_grouping

ACT = "A-0002"


# ---------------------------------------------------------------- 假表

class FakeBitable:
    """够用的内存假表：只实现分组流程用到的 `operator: is` / `isNot`。

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
            return None          # 模拟飞书限流/网络抖动时 lib 的原始返回
        conds = (filter_conditions or {}).get("conditions", [])
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
        keep = [r for r in self.tables.get(table_id, []) if r["record_id"] not in record_ids]
        self.tables[table_id] = keep
        return len(keep)

    # -- 断言辅助 --
    def writes(self):
        """只留写操作；search 是读，比它没意义，还会被调用次数差异误伤。"""
        return [c for c in self.calls if c[0] != "search"]


def _act(**over):
    fields = {"活动ID": ACT, "活动名称": "关系自洽", "分组状态": "收集中",
              "每组男生数": 2, "每组女生数": 2, "分组功能开启": "否"}
    fields.update(over)
    return {"record_id": "rec_act", "fields": fields}


def _signup(oid, nick="某人"):
    return {"record_id": f"rec_s_{oid}",
            "fields": {"活动ID": ACT, "报名人open_id": oid,
                       "报名人昵称": nick, "状态": "已报名"}}


def _sel(oid, gender, choices=()):
    fields = {"活动ID": ACT, "选择人open_id": oid, FIELD_GS_SELECTOR_GENDER: gender}
    for i, cf in enumerate(FIELD_GS_CHOICES):
        fields[cf] = choices[i] if i < len(choices) else ""
    return {"record_id": f"rec_g_{oid}", "fields": fields}


def _res(oid, group_no, round_no="1"):
    return {"record_id": f"rec_r_{group_no}_{oid}",
            "fields": {"活动ID": ACT, "组号": group_no, "用户open_id": oid,
                       "用户昵称": "", "用户性别": "男性", "轮次": round_no}}


USERS = {
    "ou_m1": {"用户ID": "U-0001", "性别": "男性"},
    "ou_m2": {"用户ID": "U-0002", "性别": "男性"},
    "ou_f1": {"用户ID": "U-0003", "性别": "女性"},
    "ou_f2": {"用户ID": "U-0004", "性别": "女性"},
    "ou_f3": {"用户ID": "U-0005", "性别": "女性"},
    "ou_ng": {},                      # 用户表查得到、但用户ID与性别都空
    "ou_nogender": {"用户ID": "U-0007"},
    "ou_missing": {},                 # 用户表查不到
}


def _find_user(oid):
    fields = USERS.get(oid)
    return [] if fields is None else [{"record_id": f"rec_u_{oid}", "fields": dict(fields)}]


def _find_act(fake, activity_id):
    for r in fake.tables.get(ACTIVITY_TABLE_ID, []):
        if r["fields"].get("活动ID") == activity_id:
            return r
    return None


# ---------------------------------------------------------------- 案例

class Case:
    """一个待验分支：跑哪条指令、喂什么输入、桩表长什么样。"""

    def __init__(self, name, handler, keyword, tables=None, fail_create_after=None,
                 search_fails=None, algorithm=None):
        self.name = name
        self.handler = handler
        self.keyword = keyword
        self.tables = _default_tables() if tables is None else tables
        self.fail_create_after = fail_create_after
        self.search_fails = search_fails
        self.algorithm = algorithm


def _default_tables():
    """4 男 1 女已交志愿——足够跑通一次成功分组。"""
    return {
        ACTIVITY_TABLE_ID: [_act()],
        SIGNUP_TABLE_ID: [_signup(o, n) for o, n in (
            ("ou_m1", "小北"), ("ou_m2", "阿北"), ("ou_f1", "阿南"), ("ou_f2", "小西"),
            ("ou_f3", "小东"), ("ou_ng", "无号"), ("ou_nogender", "无性别"))],
        GROUP_SELECT_TABLE: [
            _sel("ou_m1", "男性", ("ou_f1",)), _sel("ou_m2", "男性", ("ou_f2",)),
            _sel("ou_f1", "女性", ("ou_m1",)), _sel("ou_f2", "女性", ("ou_m2",)),
            _sel("ou_f3", "女性", ()),
        ],
        GROUP_RESULT_TABLE: [],
    }


def _raise(exc):
    def _boom(*a, **k):
        raise exc
    return _boom


def _empty_groups(*a, **k):
    return []


CASES = [
    # ---- 开始填志愿（6）----
    Case("start/格式错误", "start", ""),
    Case("start/非数字", "start", "A-0002 x 3"),
    Case("start/每组为零", "start", "A-0002 0 3"),
    Case("start/活动不存在", "start", "A-9999 2 2"),
    Case("start/成功有历史", "start", "A-0002 3 3",
         tables={**_default_tables(),
                 GROUP_RESULT_TABLE: [_res("ou_m1", 1, "1"), _res("ou_m2", 1, "1"),
                                      _res("ou_f1", 2, "2")]}),
    Case("start/成功无历史", "start", "A-0002 3 3"),

    # ---- 执行分组（11；「执行成功/执行成功第二轮/幂等查询读失败」因组员名
    #      追加编号刻意分歧，移到文件末尾「本次功能（现场显示名）：刻意分歧」一节）----
    Case("stop/格式错误", "stop", ""),
    Case("stop/轮次非数字", "stop", "A-0002 第三轮"),
    Case("stop/轮次为零", "stop", "A-0002 0"),
    Case("stop/活动不存在", "stop", "A-9999"),
    Case("stop/每组为零", "stop", "A-0002",
         tables={**_default_tables(),
                 ACTIVITY_TABLE_ID: [_act(每组男生数=0, 每组女生数=0)]}),
    Case("stop/幂等拦截", "stop", "A-0002 2",
         tables={**_default_tables(),
                 GROUP_RESULT_TABLE: [_res("ou_m1", 1, "2")]}),
    Case("stop/人数不足", "stop", "A-0002",
         tables={**_default_tables(),
                 GROUP_SELECT_TABLE: [_sel("ou_m1", "男性"), _sel("ou_f1", "女性")]}),
    Case("stop/人数不足含跳过", "stop", "A-0002",
         tables={**_default_tables(), GROUP_SELECT_TABLE: [
             _sel("ou_m1", "男性"), _sel("ou_f1", "女性"), _sel("ou_nogender", "")]}),
    Case("stop/算法异常", "stop", "A-0002",
         algorithm=_raise(ValueError("炸了"))),
    Case("stop/算法返回空", "stop", "A-0002", algorithm=_empty_groups),
    Case("stop/写入不完整", "stop", "A-0002", fail_create_after=3),

    # ---- 分组状态（3）----
    Case("status/格式错误", "status", "   "),
    Case("status/活动不存在", "status", "A-9999"),
    Case("status/成功", "status", ACT),

    # ---- 查看未提交（6）----
    Case("unsubmitted/格式错误", "unsubmitted", "   "),
    Case("unsubmitted/活动不存在", "unsubmitted", "A-9999"),
    Case("unsubmitted/全部已提交", "unsubmitted", ACT,
         tables={**_default_tables(),
                 SIGNUP_TABLE_ID: [_signup("ou_m1", "小北"), _signup("ou_f1", "阿南")]}),
    Case("unsubmitted/部分未提交", "unsubmitted", ACT),
    Case("unsubmitted/缺用户ID", "unsubmitted", ACT,
         tables={**_default_tables(), SIGNUP_TABLE_ID: [_signup("ou_ng", "无号")]}),
    Case("unsubmitted/缺性别", "unsubmitted", ACT,
         tables={**_default_tables(), SIGNUP_TABLE_ID: [_signup("ou_nogender", "无性别")],
                 GROUP_SELECT_TABLE: []}),

    # ---- 分组功能开关（13）----
    Case("flag/格式错误", "flag", ""),
    Case("flag/非法状态", "flag", "A-0002 也许"),
    Case("flag/活动不存在", "flag", "A-9999 开"),
]

for _alias in ("开", "on", "1", "是", "true"):
    CASES.append(Case(f"flag/开启-{_alias}", "flag", f"{ACT} {_alias}"))
for _alias in ("关", "off", "0", "否", "false"):
    CASES.append(Case(f"flag/关闭-{_alias}", "flag", f"{ACT} {_alias}"))


# ---------------------------------------------------------------- 驱动

def _patch_golden(monkey, fake, case):
    """把冻结旧实作接到假表上。

    `search_records` 外面包一层 None→[]，与 bot/clients.py 原本的语义一模一样；
    新路 lib 内部调的是 raw 的 client.search_records，由 execute_round 自己决定怎么
    解释 None。两条路最终行为必须一致——这正是要验的东西。
    """
    noop = lambda *a, **k: None  # noqa: E731
    # 算法评分常量已随算法搬进 lib，bot/constants.py 那边没有了。黄金副本是从
    # `from constants import *` 拿它们的，不喂就是 NameError → 被当成「算法出错」
    # 吞掉，两条路都返回「算法出错」，测试照样全绿——那种绿是假的。
    # 喂的就是 lib 现在用的那一份，两边算法输入完全一致（数值由
    # tests/test_grouping_admin.py 的 test_scores_survived_the_move 钉死）。
    for name in ("PRIORITY_SCORES", "MATCHMAKER_STAR_SCORES",
                 "WEIGHT_USER_SELECTION", "WEIGHT_MATCHMAKER_PICK"):
        monkey.setattr(golden_grouping, name, getattr(lib_grouping, name), raising=False)
    for mod in (golden_grouping, golden_commands):
        monkey.setattr(mod, "search_records",
                       lambda *a, **k: fake.search_records(*a, **k) or [], raising=False)
        monkey.setattr(mod, "update_record", fake.update_record, raising=False)
        monkey.setattr(mod, "batch_create_records", fake.batch_create_records, raising=False)
        monkey.setattr(mod, "batch_delete_records", fake.batch_delete_records, raising=False)
        monkey.setattr(mod, "create_record", fake.create_record, raising=False)
        monkey.setattr(mod, "find_activity_by_id", lambda aid: _find_act(fake, aid), raising=False)
        monkey.setattr(mod, "find_user_by_openid", _find_user, raising=False)
        monkey.setattr(mod, "log", noop, raising=False)
    if case.algorithm is not None:
        monkey.setattr(golden_grouping, "run_grouping_algorithm", case.algorithm)


def _patch_new(monkey, fake, case):
    """新路：lib 拿到的是 client 实例与 find_user_by_openid 注入。"""
    monkey.setattr(bot_grouping, "bitable", fake)
    monkey.setattr(bot_commands, "bitable", fake)
    monkey.setattr(bot_grouping, "find_activity_by_id", lambda aid: _find_act(fake, aid))
    monkey.setattr(bot_commands, "find_activity_by_id", lambda aid: _find_act(fake, aid))
    monkey.setattr(bot_grouping, "find_user_by_openid", _find_user)
    monkey.setattr(lib_grouping, "log", lambda *a, **k: None)
    if case.algorithm is not None:
        monkey.setattr(lib_grouping, "run_grouping_algorithm", case.algorithm)


def _run_golden(case):
    if case.handler == "flag":
        return golden_commands.handle_admin_toggle_group_flag(case.keyword)
    if case.handler == "start":
        return golden_grouping.handle_admin_start_group(case.keyword)
    if case.handler == "stop":
        return golden_grouping.handle_admin_stop_group(case.keyword)
    if case.handler == "status":
        return golden_grouping.handle_admin_group_status(case.keyword)
    return golden_grouping.handle_admin_unsubmitted(case.keyword)


def _run_new(case):
    if case.handler == "flag":
        return bot_commands.handle_admin_toggle_group_flag(case.keyword)
    if case.handler == "start":
        return bot_grouping.handle_admin_start_group(case.keyword)
    if case.handler == "stop":
        return bot_grouping.handle_admin_stop_group(case.keyword)
    if case.handler == "status":
        return bot_grouping.handle_admin_group_status(case.keyword)
    return bot_grouping.handle_admin_unsubmitted(case.keyword)


@pytest.fixture
def monkey_and_lockdir(monkeypatch, tmp_path):
    """两条路共用同一份注入：表 ID / 栏位名 / 锁目录都走同一个入口，别各配各的。"""
    bot_grouping.configure_grouping_lib(str(tmp_path))
    return monkeypatch


def _build(case):
    return FakeBitable(case.tables, case.fail_create_after, case.search_fails)


# ---------------------------------------------------------------- 签到默认（刻意新增）

SIGNIN_FIELD = "签到功能开启"

# 「开始填志愿」成功案例本次刻意新增的副作用：同笔 update 里多写「签到功能开启=是」
# （飞书单选栏位没有 API 级默认值，见 lib.grouping.signin_default_payload）。
# 除该栏位外其余副作用必须逐字相同；「多写的就是它」由
# test_start_signin_default_adds_exactly_one_field_write 专门钉死。
_SIGNIN_DEFAULT_CASES = {"start/成功有历史", "start/成功无历史"}


def _strip_signin(writes):
    """剔除「签到默认打开」追加的那一栏，供白名单化的副作用比对。"""
    out = []
    for c in writes:
        if c[0] == "update" and c[1] == ACTIVITY_TABLE_ID and SIGNIN_FIELD in c[3]:
            out.append((c[0], c[1], c[2], {k: v for k, v in c[3].items() if k != SIGNIN_FIELD}))
        else:
            out.append(c)
    return out


def _tables_without_signin(tables):
    """表快照去掉活动表的「签到功能开启」（与 _strip_signin 配对）。"""
    return {key: [{"record_id": r["record_id"],
                   "fields": {k: v for k, v in r["fields"].items() if k != SIGNIN_FIELD}}
                  for r in rows]
            for key, rows in tables.items()}


def test_start_signin_default_adds_exactly_one_field_write(monkey_and_lockdir):
    """「签到默认打开」：start 成功案例唯一多出的副作用 = 同笔 update 里的「签到功能开启=是」。

    飞书单选栏位不支持默认值：开始填志愿时把「是」和状态写进同一笔 update；
    管理员之后手动改「否」仍是后写者胜。旧路没有这个写入——刻意的行为变化。
    """
    case = Case("start/成功无历史", "start", "A-0002 3 3")
    old, new = _build(case), _build(case)
    _patch_golden(monkey_and_lockdir, old, case)
    _run_golden(case)
    _patch_new(monkey_and_lockdir, new, case)
    _run_new(case)

    old_updates = [c[3] for c in old.writes() if c[0] == "update"]
    new_updates = [c[3] for c in new.writes() if c[0] == "update"]
    assert len(old_updates) == len(new_updates) == 1
    added = {k: v for k, v in new_updates[0].items() if k not in old_updates[0]}
    assert added == {SIGNIN_FIELD: "是"}, "多写的必须且只能是「签到功能开启=是」"
    assert new_updates[0]["分组状态"] == "收集中", "签到默认必须与状态在同一笔 update 里"
    assert {k: v for k, v in new_updates[0].items() if k != SIGNIN_FIELD} == old_updates[0]
    assert _find_act(new, ACT)["fields"][SIGNIN_FIELD] == "是"
    assert SIGNIN_FIELD not in _find_act(old, ACT)["fields"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_bot_text_is_identical_to_frozen_impl(monkey_and_lockdir, case):
    """新旧两条路的回传字符串必须逐字相同——这是本次的红线。"""
    old, new = _build(case), _build(case)
    _patch_golden(monkey_and_lockdir, old, case)
    old_text = _run_golden(case)
    _patch_new(monkey_and_lockdir, new, case)
    new_text = _run_new(case)

    assert new_text == old_text, (
        f"文案漂移\n--- 旧 ---\n{old_text!r}\n--- 新 ---\n{new_text!r}")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_side_effects_are_identical(monkey_and_lockdir, case):
    """文字相同还不够：写进飞书的每一条也要相同，否则表已经被改了话才算对。

    start 成功案例是唯一白名单：「签到默认打开」刻意多写「签到功能开启=是」
    （旧路没有这个逻辑）——剔除该栏位后其余写入与表内容必须逐条全等；
    「多写的就是签到默认」由 test_start_signin_default_adds_exactly_one_field_write
    另行钉死。
    """
    old, new = _build(case), _build(case)
    _patch_golden(monkey_and_lockdir, old, case)
    _run_golden(case)
    _patch_new(monkey_and_lockdir, new, case)
    _run_new(case)

    if case.name in _SIGNIN_DEFAULT_CASES:
        assert _strip_signin(new.writes()) == old.writes(), (
            f"副作用漂移（剔除签到默认后仍不等）\n--- 旧 ---\n{old.writes()}\n--- 新 ---\n{new.writes()}")
        assert _tables_without_signin(new.tables) == old.tables
        return
    assert new.writes() == old.writes(), (
        f"副作用漂移\n--- 旧 ---\n{old.writes()}\n--- 新 ---\n{new.writes()}")
    assert new.tables == old.tables


def test_dirty_duplicate_row_counts_add_up_intentional_fix(monkey_and_lockdir):
    """【此處為刻意的 bug fix，非回歸】重复志愿行下，bot 两条指令的数字必须同口径。

    旧实现「分组状态」按记录**行数**报已提交（3 行 → 3 人），而「查看未提交」按
    去重 open_id 报（2 人已交 → 1 人未交），3+1 超过报名总数 3，自相矛盾。
    本次修正后两处都走去重口径：已提交 2 + 未提交 1 == 报名 3。

    本用例之外，「执行分组成功」两案例（组员名追加编号）与「开始填志愿」两案例
    （签到默认写入）也在本文件单独列为刻意分歧，见对应専用测试；其余 CASES
    必须保持逐字全绿。
    """
    tables = {
        ACTIVITY_TABLE_ID: [_act()],
        SIGNUP_TABLE_ID: [_signup("ou_m1", "小北"), _signup("ou_f1", "阿南"),
                          _signup("ou_f2", "小西")],
        GROUP_SELECT_TABLE: [
            _sel("ou_m1", "男性", ("ou_f1",)),
            _sel("ou_m1", "男性", ("ou_f2",)),      # 同一人的重复志愿行（脏数据）
            _sel("ou_f1", "女性", ("ou_m1",)),
        ],
        GROUP_RESULT_TABLE: [],
    }
    case_status = Case("status/重复志愿行", "status", ACT, tables=tables)
    case_unsub = Case("unsubmitted/重复志愿行", "unsubmitted", ACT, tables=tables)

    # 旧实现（黄金副本）确实按行数报 3——记录在案，证明这是刻意的行为变化
    old_fake = _build(case_status)
    _patch_golden(monkey_and_lockdir, old_fake, case_status)
    old_status = _run_golden(case_status)
    assert "已提交选择：3人" in old_status

    new_fake = _build(case_status)
    _patch_new(monkey_and_lockdir, new_fake, case_status)
    new_status = _run_new(case_status)

    unsub_fake = _build(case_unsub)
    _patch_new(monkey_and_lockdir, unsub_fake, case_unsub)
    new_unsub = _run_new(case_unsub)

    assert "已提交选择：2人" in new_status, "去重后只算 2 人（旧口径是 3）"
    submitted = int(re.search(r"已提交选择：(\d+)人", new_status).group(1))
    unsubmitted = int(re.search(r"未提交志愿（(\d+)人）", new_unsub).group(1))
    assert submitted + unsubmitted == 3, "已提交（去重）+ 未提交 == 报名总数"


# ================================================================
# 本次功能（现场显示名）：刻意分歧（旧路行为 ≠ 新路行为）
# ----------------------------------------------------------------
# 「执行分组」成功回复的组员名改为「名字（U-xxxx）」：名字 = 姓名去空白缺省昵称
# （lib.grouping.execute_round 的 oid_to_name），缺编号时只名字。stub 用户没有
# 「姓名」→ 名字本身与旧路相同，唯一变化是追加「（编号）」。两个成功案例移出
# CASES，旧/新双断言钉死文案变换与副作用不变。
# ================================================================

_STOP_SUCCESS_CASES = (
    ("stop/执行成功", "A-0002", None),
    ("stop/执行成功第二轮", "A-0002 2",
     {**_default_tables(), GROUP_RESULT_TABLE: [_res("ou_m1", 1, "1")]}),
    # bot 侧把幂等查询的读失败当空表继续（treat_search_failure_as_empty=True），
    # 这个案例实际走成功路径并渲染组员名单，因此同样列入本分歧。
    ("stop/幂等查询读失败", "A-0002", None),
)


def test_admin_stop_success_appends_uid_intentional_change(monkey_and_lockdir):
    """执行分组成功：组员名统一追加「（U-xxxx）」——剥掉编号后缀后与旧路逐字相同。

    编号是抽查对象：只断「（U-」模式的话，实现把编号写死成任意值（如 U-0000）
    也能过（reviewer 突变 P13）。这里把编号集合与「人↔编号」配对都写死：
    stub USERS 里本案例组员 ou_m1/ou_m2/ou_f1/ou_f2/ou_f3 → U-0001..U-0005。
    """
    for name, keyword, tables in _STOP_SUCCESS_CASES:
        case = Case(name, "stop", keyword, tables=tables)
        if name == "stop/幂等查询读失败":
            case.search_fails = {GROUP_RESULT_TABLE}
        old, new = _build(case), _build(case)
        _patch_golden(monkey_and_lockdir, old, case)
        old_text = _run_golden(case)
        _patch_new(monkey_and_lockdir, new, case)
        new_text = _run_new(case)

        assert "（U-" not in old_text, "旧路组员名不带编号"
        assert "（U-" in new_text, "新路组员名必须带编号"
        assert re.sub(r"（U-\d+）", "", new_text) == old_text, (
            f"{name} 除编号后缀外文案漂移\n--- 旧 ---\n{old_text!r}\n--- 新 ---\n{new_text!r}")
        assert set(re.findall(r"（U-\d+）", new_text)) == {
            "（U-0001）", "（U-0002）", "（U-0003）", "（U-0004）", "（U-0005）"}, (
            f"{name} 编号集合不符（写死/缺失/串号都会被抓）\n--- 新 ---\n{new_text!r}")
        assert "小北（U-0001）、阿北（U-0002）、阿南（U-0003）、小西（U-0004）、小东（U-0005）" in new_text, (
            f"{name} 组员与人编号配对不符\n--- 新 ---\n{new_text!r}")
        # 落库内容不变：stub 无「姓名」→ 用户昵称栏仍写昵称
        assert new.writes() == old.writes(), f"{name} 副作用漂移"
        assert new.tables == old.tables
