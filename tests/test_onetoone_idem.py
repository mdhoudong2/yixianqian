"""一对一执行状态机（lib.onetoone，桩表驱动，不联网）：审核修复 #5 的防回归。

三条实证故障路径：
1. 幂等查历史结果瞬失败 → 旧实现把它当空表继续跑（结果 8→16 重复）；
   新实现中止、零写入；
2. 部分写入 + 回滚回查失败 → 旧实现残留 1 条却回「已回滚」；新实现明说
   「可能残留」且不宣称已回滚；
3. 残留结果后的重试被幂等明确拦住，并指向「清空一对一结果」清理（不会被无声叠加）。

本次重构「移除志愿收集阶段」后：失败一律零状态变更（不再有「已截止」前置写、
也不再有「恢复收集中」的恢复写）。另覆盖：结果已写但终态写失败要明确报错、
删除数不足如实报告残留。桩只桩「存取」，状态机/回滚逻辑全跑 lib 真实现。
"""
import pytest

import onetoone as bot_onetoone

from lib import onetoone as lib_onetoone
from tests.test_onetoone_parity import (
    ACT,
    ACTIVITY_TABLE_ID,
    FakeBitable,
    ONETOONE_RESULT_TABLE,
    ONETOONE_SELECT_TABLE,
    _default_tables,
    _find_act,
    _find_user,
)


@pytest.fixture
def configured(monkeypatch, tmp_path):
    """注入表 ID / 栏位名 / 锁目录（走 bot 的同一个入口）。"""
    monkeypatch.setattr(bot_onetoone, "ONETOONE_SELECT_TABLE", ONETOONE_SELECT_TABLE)
    monkeypatch.setattr(bot_onetoone, "ONETOONE_RESULT_TABLE", ONETOONE_RESULT_TABLE)
    monkeypatch.setattr(lib_onetoone, "log", lambda *a, **k: None)
    bot_onetoone.configure_onetoone_lib(str(tmp_path))


def _execute(fake, female_chats=2):
    return lib_onetoone.execute(ACT, 1, client=fake, activity=_find_act(fake, ACT),
                                find_user_by_openid=_find_user, female_chats=female_chats)


def _status(fake):
    return _find_act(fake, ACT)["fields"]["一对一状态"]


def test_idempotency_read_failure_aborts_with_zero_writes(configured):
    """① 幂等查历史结果读失败 → 中止（零写入），绝不当空表重跑。"""
    fake = FakeBitable(_default_tables(), search_fails={ONETOONE_RESULT_TABLE})
    res = _execute(fake)
    assert res["code"] == "SEARCH_FAILED"
    assert fake.writes() == []
    assert _status(fake) == "收集中"


def test_selections_read_failure_aborts_zero_writes(configured):
    """③ 志愿读失败 → 中止＋明说失败，零写入（不再有恢复「收集中」的恢复写）。"""
    fake = FakeBitable(_default_tables(), search_fails={ONETOONE_SELECT_TABLE})
    res = _execute(fake)
    assert res["code"] == "SELECTIONS_SEARCH_FAILED"
    assert "restored" not in res, "restored 字段已随恢复语义删除"
    assert fake.writes() == [], "读失败必须零写入"
    assert _status(fake) == "收集中", "活动栏位原样不动"
    text = bot_onetoone._render_execute_result(res, ACT)
    assert text == "读取一对一志愿失败，已中止匹配，本次未写入任何数据，请稍后重试。"


def test_no_activity_write_before_success(configured):
    """零状态变更不变量：从进门到结果写完，活动表一笔 update 都没有。

    旧状态机一进门就写「已截止」，中途失败再写「收集中」恢复；新状态机只在
    成功末尾单笔写「已完成」。这里在结果写入前注入活动表写失败探针来验证：
    前段路径（读幂等/读志愿/读报名/算法）根本不会去写活动表。
    """
    fake = FakeBitable(_default_tables())
    fake.update_record = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("结果写完前不得写活动表"))
    # 用「写入不完整」把执行停在结果阶段（成功末尾的已完成写会被探针拦下——
    # 但那之前的所有失败分支都不该碰活动表）
    fake.fail_create_after = 1
    res = _execute(fake)
    assert res["code"] == "PARTIAL_WRITE"


def test_partial_write_rollback_verify_failure_says_residual(configured):
    """⑥ 部分写入 + 回查失败 → 明说可能残留；重试被幂等明确拦住。"""
    fake = FakeBitable(_default_tables(), fail_create_after=1,
                       search_fail_plan={ONETOONE_RESULT_TABLE: {2}})
    res = _execute(fake)
    assert res["code"] == "PARTIAL_WRITE_VERIFY_FAILED"
    assert len(fake.tables[ONETOONE_RESULT_TABLE]) == 1, "无法确认回滚时残留如实可见"
    assert _status(fake) == "收集中", "失败零状态变更（活动栏位原样）"
    assert not [c for c in fake.writes() if c[0] == "update"], "失败不写活动表"
    text = bot_onetoone._render_execute_result(res, ACT)
    assert "已回滚" not in text, "不得宣称已回滚"
    assert "回滚校验失败" in text and "残留" in text
    assert "「清空一对一结果 A-0002」" in text

    # 残留结果后的重试：幂等明确拦截，指向「清空一对一结果」清理，且不再写入
    writes_before = len(fake.writes())
    res2 = _execute(fake)
    assert res2["code"] == "ALREADY_DONE"
    text2 = bot_onetoone._render_execute_result(res2, ACT)
    assert "已生成过一对一结果" in text2 and "清空一对一结果" in text2
    assert len(fake.tables[ONETOONE_RESULT_TABLE]) == 1
    assert len(fake.writes()) == writes_before, "被挡下的重试不得再写"


def test_clean_partial_rollback_removes_rows(configured):
    """⑥ 回查成功且删除足数 → 残留清零、零状态变更（对比旧路「恢复收集中」）。"""
    fake = FakeBitable(_default_tables(), fail_create_after=3)
    res = _execute(fake)
    assert res["code"] == "PARTIAL_WRITE"
    assert res["created"] == 3 and res["total"] == 8
    assert fake.tables[ONETOONE_RESULT_TABLE] == []
    assert _status(fake) == "收集中", "失败零状态变更"
    assert not [c for c in fake.writes() if c[0] == "update"]
    assert "已回滚本轮已写入部分" in bot_onetoone._render_execute_result(res, ACT)


def test_delete_incomplete_reports_residual_count(configured):
    """⑥ 删除数不足 → 明报残留条数（不说「已回滚」）。"""
    fake = FakeBitable(_default_tables(), fail_create_after=3, delete_limit=1)
    res = _execute(fake)
    assert res["code"] == "PARTIAL_WRITE_INCOMPLETE"
    assert res["deleted"] == 1 and res["residual"] == 2
    assert len(fake.tables[ONETOONE_RESULT_TABLE]) == 2
    assert not [c for c in fake.writes() if c[0] == "update"]
    text = bot_onetoone._render_execute_result(res, ACT)
    assert "残留 2 条结果" in text
    assert "已回滚" not in text


def test_done_status_write_failure_keeps_results_and_reports(configured):
    """⑦ 结果已写但「已完成」写失败 → 明确错误，绝不报成功。

    新状态机不在执行前写任何状态：write_fail_plan 第 1 笔活动 update 就是
    成功末尾那笔「已完成」。失败零状态变更：状态停在原值（收集中）。
    """
    fake = FakeBitable(_default_tables(), write_fail_plan={ACTIVITY_TABLE_ID: {1}})
    res = _execute(fake)
    assert res["code"] == "DONE_STATUS_FAILED"
    assert fake.tables[ONETOONE_RESULT_TABLE], "结果确实已写入"
    assert _status(fake) == "收集中", "终态未写成，活动栏位原样"
    assert bot_onetoone._render_execute_result(res, ACT) == \
        "结果已生成，但状态写入失败（未标记为「已完成」），用户可能看不到结果；请稍后重试或让管理员检查。"


def test_algorithm_error_leaves_zero_state_changes(configured, monkeypatch):
    """⑤ 算法异常 → 中止、零状态变更（不再有恢复写）。

    均衡算法（run_offline_onetoone_matching）抛非 ValueError 异常 → ALGORITHM_ERROR；
    抛 ValueError（如女生场次 > 男生数）走 FEMALE_CHATS_INVALID，另测。
    """
    def _boom(*a, **k):
        raise RuntimeError("炸了")

    monkeypatch.setattr(lib_onetoone, "run_offline_onetoone_matching", _boom)
    fake = FakeBitable(_default_tables())
    res = _execute(fake)
    assert res["code"] == "ALGORITHM_ERROR"
    assert _status(fake) == "收集中", "活动栏位原样不动"
    assert fake.writes() == [], "算法失败零写入"


def test_female_chats_entry_validation_zero_writes(configured):
    """女生场次入口校验：缺省 / 非正整数 → 对应码，零读零写（连锁都不等）。

    缺省回 FEMALE_CHATS_REQUIRED、非正整数（0/字符串/布尔/负数）回
    FEMALE_CHATS_INVALID；都在任何表操作之前收口，管理员跑之前就被拦下。
    """
    for fc, code in ((None, "FEMALE_CHATS_REQUIRED"),
                     (0, "FEMALE_CHATS_INVALID"),
                     ("2", "FEMALE_CHATS_INVALID"),
                     (True, "FEMALE_CHATS_INVALID"),
                     (-1, "FEMALE_CHATS_INVALID")):
        fake = FakeBitable(_default_tables())
        res = lib_onetoone.execute(ACT, 1, client=fake, activity=_find_act(fake, ACT),
                                   find_user_by_openid=_find_user, female_chats=fc)
        assert res["code"] == code, f"female_chats={fc!r}"
        assert fake.calls == [], f"入口校验 {fc!r} 不得有任何表操作（含只读）"
        assert _status(fake) == "收集中", f"入口校验 {fc!r} 不得动状态"


def test_female_chats_over_male_count_zero_state_changes(configured):
    """女生场次 > 男生数 → 算法 ValueError → FEMALE_CHATS_INVALID、零状态变更。"""
    fake = FakeBitable(_default_tables())   # 2 男 2 女
    res = _execute(fake, female_chats=3)    # 3 > 男生数 2
    assert res["code"] == "FEMALE_CHATS_INVALID"
    assert "女生场次" in res["error"] and "男生人数" in res["error"]
    assert _status(fake) == "收集中", "活动栏位原样不动"
    assert not [c for c in fake.writes() if c[0] == "batch_create"]
    assert not [c for c in fake.writes() if c[0] == "update"]
