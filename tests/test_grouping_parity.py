"""等价性安全网：bot 分组指令（重构后）与冻结旧实作的分工比对。

同一个桩表、同一个输入，旧路（tests/_golden_*_impl.py，从 base commit 8d68f41 冻结）
与新路（lib.grouping + bot 渲染层）各跑一遍，断言：

    回传字符串完全相同  且  写入调用序列与最终表内容完全相同

桩的只是「存取」，规则（幂等判断、回滚、算法、状态流转）全跑真实现——桩掉规则就
只剩在测 mock 了（与 tests/test_praise_api.py 的 FakeTable 同一取舍）。

【本次重构「移除志愿收集阶段」后的比对范围】
- 「开始填志愿」指令已删除：相关案例整体移除（金标不动，函数变历史产物）；
- 「执行分组」：命令格式改为「活动ID 每组男 每组女 [轮次]」、每组人数改为执行入参、
  轮次缺省=最新+1、失败一律零状态变更、成功单笔写「已完成」——全部为刻意分歧，
  单独列在文件末尾的「刻意分歧」一节（自断言固定文案 + 副作用），不再回入金标比对；
- 未变路径（分组状态 / 查看未提交 / 分组功能开关）仍走上面的逐字比对。

案例覆盖每个保留 handler 的每个 return 分支。新增字串不在金标范围内（黄金副本里
本来就没有），必须在 PR 说明里单独列出。
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

    def __init__(self, tables=None, fail_create_after=None, search_fails=None,
                 delete_limit=None):
        # 必须连 fields 一起深拷贝：同一个 Case 会被建两次（旧路一次、新路一次），
        # 只拷外层 dict 的话两条路共用同一批 fields，第一条跑完就把第二条的起点改了。
        self.tables = {k: [{"record_id": r["record_id"], "fields": dict(r["fields"])}
                           for r in v] for k, v in (tables or {}).items()}
        self.fail_create_after = fail_create_after
        self.search_fails = search_fails or set()
        self.delete_limit = delete_limit
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
    "ou_m3": {"用户ID": "U-0006", "性别": "男性"},
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


def _balanced(n_males, n_females):
    """n 男 n 女报名 + 志愿（性别齐全），供新语义的成功路径用例使用。"""
    signups, selections = [], []
    for i in range(1, n_males + 1):
        oid = f"ou_m{i}"
        signups.append(_signup(oid, f"男{i}"))
        selections.append(_sel(oid, "男性", ()))
    for i in range(1, n_females + 1):
        oid = f"ou_f{i}"
        signups.append(_signup(oid, f"女{i}"))
        selections.append(_sel(oid, "女性", ()))
    return {
        ACTIVITY_TABLE_ID: [_act()],
        SIGNUP_TABLE_ID: signups,
        GROUP_SELECT_TABLE: selections,
        GROUP_RESULT_TABLE: [],
    }


# ---------------------------------------------------------------- 金标案例（未变路径）

CASES = [
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
    if case.handler == "status":
        return golden_grouping.handle_admin_group_status(case.keyword)
    return golden_grouping.handle_admin_unsubmitted(case.keyword)


def _run_new(case):
    if case.handler == "flag":
        return bot_commands.handle_admin_toggle_group_flag(case.keyword)
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

# 「开启分组功能」成功案例本次刻意新增的副作用：同笔 update 里多写「签到功能开启=是」
# （飞书单选栏位没有 API 级默认值，见 lib.grouping.signin_default_payload；
# 「签到默认」自本次重构从开始动作迁移到开启功能路径）。
# 除该栏位外其余副作用必须逐字相同；「多写的就是它」由
# test_flag_on_signin_default_adds_exactly_one_field_write 专门钉死。
_SIGNIN_DEFAULT_CASES = {f"flag/开启-{a}" for a in ("开", "on", "1", "是", "true")}


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


def test_flag_on_signin_default_adds_exactly_one_field_write(monkey_and_lockdir):
    """「签到默认打开」：开启分组功能成功案例唯一多出的副作用 = 同笔 update 里的「签到功能开启=是」。

    飞书单选栏位不支持默认值：开启分组功能时把「是」和 flag 写进同一笔 update；
    管理员之后手动改「否」仍是后写者胜。旧路没有这个写入——刻意的行为变化。
    """
    case = Case("flag/开启-开", "flag", f"{ACT} 开")
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
    assert new_updates[0]["分组功能开启"] == "是", "签到默认必须与 flag 在同一笔 update 里"
    assert {k: v for k, v in new_updates[0].items() if k != SIGNIN_FIELD} == old_updates[0]
    assert _find_act(new, ACT)["fields"][SIGNIN_FIELD] == "是"
    assert SIGNIN_FIELD not in _find_act(old, ACT)["fields"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_bot_text_is_identical_to_frozen_impl(monkey_and_lockdir, case):
    """新旧两条路的回传字符串必须逐字相同——这是保留路径的红线。"""
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

    flag 开启案例是唯一白名单：「签到默认打开」刻意多写「签到功能开启=是」
    （旧路没有这个逻辑）——剔除该栏位后其余写入与表内容必须逐条全等；
    「多写的就是签到默认」由 test_flag_on_signin_default_adds_exactly_one_field_write
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
# 本次重构（移除志愿收集阶段）：刻意分歧（旧路行为 ≠ 新路行为）
# ----------------------------------------------------------------
# 「开始填志愿」指令整体删除；「执行分组」命令格式改为
# 「执行分组 活动ID 每组男 每组女 [轮次]」（每组人数为执行入参、轮次缺省=最新+1）；
# 失败一律零状态变更（不再有「恢复收集中」的恢复写）；成功单笔 update 写
# 「已完成」+ 每组人数。
# 金标（冻结旧实作）不再持有可比的 stop 路径，以下用例全部自断言固定文案与副作用。
# ================================================================

_STOP_GROUP_FORMAT_HINT = ("格式：执行分组 活动ID 每组男 每组女 [轮次]\n"
                           "例如：执行分组 A-0002 3 3 或 执行分组 A-0002 3 3 2")


def _new_stop(fake_tables=None, keyword="A-0002 2 2", fail_create_after=None,
              search_fails=None, monkey=None, algorithm=None):
    """跑新路 handle_admin_stop_group，返回 (fake, text)。"""
    case = Case("stop", "stop", keyword, tables=fake_tables,
                fail_create_after=fail_create_after, search_fails=search_fails,
                algorithm=algorithm)
    fake = _build(case)
    _patch_new(monkey, fake, case)
    return fake, bot_grouping.handle_admin_stop_group(case.keyword)


def test_stop_command_format_and_validation(monkey_and_lockdir):
    """新格式解析：缺参/非数字给新格式提示；每组≤0 与轮次非法分别报错。"""
    fake, text = _new_stop(monkey=monkey_and_lockdir, keyword="")
    assert text == _STOP_GROUP_FORMAT_HINT
    assert fake.writes() == []

    fake, text = _new_stop(monkey=monkey_and_lockdir, keyword="A-0002")
    assert text == _STOP_GROUP_FORMAT_HINT
    assert fake.writes() == []

    fake, text = _new_stop(monkey=monkey_and_lockdir, keyword="A-0002 x 3")
    assert text == _STOP_GROUP_FORMAT_HINT
    assert fake.writes() == []

    fake, text = _new_stop(monkey=monkey_and_lockdir, keyword="A-0002 0 3")
    assert text == "每组男女人数必须大于0"
    assert fake.writes() == []

    fake, text = _new_stop(monkey=monkey_and_lockdir, keyword="A-0002 2 2 0")
    assert text == "轮次必须为大于0的整数"
    assert fake.writes() == []

    fake, text = _new_stop(monkey=monkey_and_lockdir, keyword="A-0002 2 2 第三轮")
    assert text == "轮次必须为数字，当前：第三轮"
    assert fake.writes() == []

    fake, text = _new_stop(monkey=monkey_and_lockdir, keyword="A-9999 2 2")
    assert text == "未找到活动：A-9999"
    assert fake.writes() == []


def test_stop_success_single_update_with_done_and_sizes(monkey_and_lockdir):
    """成功路径：命令入参 3男3女覆盖活动栏位（2/2），单笔 update 写「已完成」+每组人数。

    组员名统一「名字（U-xxxx）」；结果行带轮次；状态不再经过「已截止」。
    """
    tables = _balanced(3, 3)
    fake, text = _new_stop(tables, keyword="A-0002 3 3", monkey=monkey_and_lockdir)

    assert text.startswith("🎉 活动「关系自洽」第1轮 分组结果")
    assert "共1组，参与6人，每组3男3女。" in text
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert len(updates) == 1, "成功路径必须单笔 update"
    assert updates[0][3] == {"分组状态": "已完成", "每组男生数": 3, "每组女生数": 3}
    assert _find_act(fake, ACT)["fields"]["分组状态"] == "已完成"

    rows = fake.tables[GROUP_RESULT_TABLE]
    assert len(rows) == 6 and all(r["fields"]["轮次"] == "1" for r in rows)
    assert "男1（U-0001）" in text and "女1（U-0003）" in text, "组员名必须带编号"


def test_stop_explicit_round_runs_and_keeps_history(monkey_and_lockdir):
    """显式轮次：第7轮可执行、历史轮保留；本轮写入行带「轮次=7」。"""
    tables = _balanced(2, 2)
    tables[GROUP_RESULT_TABLE] = [_res("ou_x", 1, "1")]
    fake, text = _new_stop(tables, keyword="A-0002 2 2 7", monkey=monkey_and_lockdir)

    assert text.startswith("🎉 活动「关系自洽」第7轮 分组结果")
    rows = fake.tables[GROUP_RESULT_TABLE]
    assert len([r for r in rows if r["fields"]["轮次"] == "1"]) == 1
    assert len([r for r in rows if r["fields"]["轮次"] == "7"]) == 4


def test_stop_default_round_is_latest_plus_one(monkey_and_lockdir):
    """轮次缺省=最新已有轮次+1：已有第2轮 → 缺省执行第3轮，历史原样保留。"""
    tables = _balanced(2, 2)
    tables[GROUP_RESULT_TABLE] = [_res("ou_x", 1, "2")]
    fake, text = _new_stop(tables, keyword="A-0002 2 2", monkey=monkey_and_lockdir)

    assert text.startswith("🎉 活动「关系自洽」第3轮 分组结果")
    rows = fake.tables[GROUP_RESULT_TABLE]
    assert len([r for r in rows if r["fields"]["轮次"] == "2"]) == 1
    assert len([r for r in rows if r["fields"]["轮次"] == "3"]) == 4


def test_stop_default_round_search_failure_reports_zero_writes(monkey_and_lockdir):
    """缺省轮次的推演读失败 → 明报失败、零写入（不得拿 0 当「没跑过」）。"""
    tables = _balanced(2, 2)
    fake, text = _new_stop(tables, keyword="A-0002 2 2",
                           search_fails={GROUP_RESULT_TABLE}, monkey=monkey_and_lockdir)

    assert text == "查询历史分组结果失败，无法确定默认轮次，请稍后重试或显式指定轮次。"
    assert fake.writes() == []
    assert _find_act(fake, ACT)["fields"]["分组状态"] == "收集中", "零状态变更（活动栏位原样）"


def test_stop_already_done_points_to_reset(monkey_and_lockdir):
    """幂等拦截：文案指向「清空分组结果」，不再指向已删除的开始指令。"""
    tables = _balanced(2, 2)
    tables[GROUP_RESULT_TABLE] = [_res("ou_x", 1, "2")]
    fake, text = _new_stop(tables, keyword="A-0002 2 2 2", monkey=monkey_and_lockdir)

    assert text == ("活动「关系自洽」第2轮已生成过分组结果，请勿重复执行。"
                    "如需重新分组，请先发送「清空分组结果 A-0002」清除已有结果，再执行分组。")
    assert fake.writes() == []


def test_stop_failures_leave_zero_state_changes(monkey_and_lockdir):
    """失败一律零状态变更：人数不足 / 写入不完整 / 算法异常 / 空组都不写活动表。"""
    # 人数不足（1男1女 < 2男2女）
    tables = _balanced(1, 1)
    fake, text = _new_stop(tables, keyword="A-0002 2 2", monkey=monkey_and_lockdir)
    assert text == ("人数不足，无法分组：1男1女，每组需要2男2女。\n"
                    "本次未写入任何数据，请补充报名或调整每组人数后重试。")
    assert fake.writes() == []
    assert _find_act(fake, ACT)["fields"]["分组状态"] == "收集中"

    # 写入不完整：回滚本轮 + 零状态变更（放在算法 monkeypatch 之前，避免串台）
    fake, text = _new_stop(_balanced(2, 2), keyword="A-0002 2 2",
                           fail_create_after=2, monkey=monkey_and_lockdir)
    assert text == ("分组结果写入失败（成功 2/4 条），已回滚本轮已写入部分，"
                    "状态未变，请稍后重试。")
    assert [c for c in fake.writes() if c[0] == "update"] == [], "失败不得写活动表"
    assert fake.tables[GROUP_RESULT_TABLE] == [], "半截结果必须被清干净"

    # 算法异常
    def _boom(*a, **k):
        raise ValueError("炸了")

    fake, text = _new_stop(_balanced(2, 2), keyword="A-0002 2 2",
                           monkey=monkey_and_lockdir, algorithm=_boom)
    assert text == "分组算法出错：炸了。本次未写入任何数据，请稍后重试。"
    assert fake.writes() == []

    # 算法返回空
    fake, text = _new_stop(_balanced(2, 2), keyword="A-0002 2 2",
                           monkey=monkey_and_lockdir, algorithm=lambda *a, **k: [])
    assert text == ("分组失败，人数不足以组成完整小组（本次未写入任何数据，"
                    "请补充报名或调整每组人数后重试）。")
    assert fake.writes() == []


def test_stop_done_status_write_failure_reports(monkey_and_lockdir):
    """成功写入结果后「已完成」单笔 update 失败 → 明确报错，绝不报成功。"""
    case = Case("stop", "stop", "A-0002 2 2", tables=_balanced(2, 2))
    fake = _build(case)
    _patch_new(monkey_and_lockdir, fake, case)
    real_update = fake.update_record
    fake.update_record = lambda table_id, rid, fields: (
        None if table_id == ACTIVITY_TABLE_ID else real_update(table_id, rid, fields))

    text = bot_grouping.handle_admin_stop_group("A-0002 2 2")
    assert text == ("分组结果已生成，但状态写入失败（未标记为「已完成」），"
                    "用户可能看不到结果；请稍后重试或让管理员检查。")
    assert fake.tables[GROUP_RESULT_TABLE], "结果确实已写入"


def test_stop_success_without_uid_prints_name_only(monkey_and_lockdir):
    """编号缺失时只写名字（与旧渲染同一口径）。"""
    tables = {
        ACTIVITY_TABLE_ID: [_act()],
        SIGNUP_TABLE_ID: [_signup(o, n) for o, n in
                          (("ou_x1", "小北"), ("ou_x2", "阿北"),
                           ("ou_x3", "阿南"), ("ou_x4", "小西"))],
        GROUP_SELECT_TABLE: [_sel("ou_x1", "男性"), _sel("ou_x2", "男性"),
                             _sel("ou_x3", "女性"), _sel("ou_x4", "女性")],
        GROUP_RESULT_TABLE: [],
    }
    fake, text = _new_stop(tables, keyword="A-0002 2 2", monkey=monkey_and_lockdir)
    assert "小北" in text and "（U-" not in text


def test_group_command_selects_by_flag_intentional_change(monkey_and_lockdir):
    """用户卡片通道：flag=是即可开卡（旧路按状态收集中），与 H5 入口同口径。"""
    monkey = monkey_and_lockdir
    noop = lambda *a, **k: None  # noqa: E731

    def _run_cmd(fake, mod, is_golden):
        if is_golden:
            monkey.setattr(golden_grouping, "send_card_message", noop, raising=False)
            return golden_grouping.handle_group_command("ou_m1")
        monkey.setattr(bot_grouping, "send_card_message", noop)
        monkey.setattr(bot_grouping, "search_records",
                       lambda *a, **k: fake.search_records(*a, **k) or [])
        return bot_grouping.handle_group_command("ou_m1")

    # 状态=收集中、无 flag：旧路发卡；新路按 flag 拒
    case = Case("cmd/对照", "command", "ou_m1")
    old, new = _build(case), _build(case)
    _patch_golden(monkey, old, case)
    old_text = _run_cmd(old, golden_grouping, True)
    _patch_new(monkey, new, case)
    new_text = _run_cmd(new, bot_grouping, False)
    assert old_text == ""
    assert new_text == "当前没有已开启分组功能的活动。\n分组功能由管理员开启，请关注通知。"

    # flag=是（状态仍收集中）：两边都发卡、回传空串——保留路径的等价点
    case2 = Case("cmd/flag开", "command", "ou_m1",
                 tables={**_default_tables(),
                         ACTIVITY_TABLE_ID: [_act(分组功能开启="是")]})
    old2, new2 = _build(case2), _build(case2)
    _patch_golden(monkey, old2, case2)
    old2_text = _run_cmd(old2, golden_grouping, True)
    _patch_new(monkey, new2, case2)
    new2_text = _run_cmd(new2, bot_grouping, False)
    assert old2_text == "" and new2_text == ""

    # flag=是 + 状态=已完成：旧路不发卡；新路照发（志愿随时可改，不看状态）
    case3 = Case("cmd/已完成", "command", "ou_m1",
                 tables={**_default_tables(),
                         ACTIVITY_TABLE_ID: [_act(分组功能开启="是", 分组状态="已完成")]})
    old3, new3 = _build(case3), _build(case3)
    _patch_golden(monkey, old3, case3)
    old3_text = _run_cmd(old3, golden_grouping, True)
    _patch_new(monkey, new3, case3)
    new3_text = _run_cmd(new3, bot_grouping, False)
    assert old3_text == "当前没有正在进行分组选择的活动。\n分组选择由管理员在活动前开启，请关注通知。"
    assert new3_text == ""


def test_removed_start_apis_stay_removed():
    """「移除志愿收集阶段」：开始类 lib 函数、常量与 bot handler 必须保持删除。"""
    import onetoone as bot_onetoone  # noqa: PLC0415

    from lib import onetoone as lib_onetoone  # noqa: PLC0415

    assert not hasattr(lib_grouping, "start_group")
    assert not hasattr(lib_grouping, "STATUS_COLLECTING")
    assert not hasattr(lib_grouping, "STATUS_CLOSED")
    assert not hasattr(bot_grouping, "handle_admin_start_group")
    assert not hasattr(lib_onetoone, "start_collection")
    assert not hasattr(bot_onetoone, "handle_admin_start_onetoone")
