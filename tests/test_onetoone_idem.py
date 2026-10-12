"""一对一执行状态机（lib.onetoone，桩表驱动，不联网）：审核修复 #5 的防回归。

三条实证故障路径：
1. 幂等查历史结果瞬失败 → 旧实现把它当空表继续跑（结果 8→16 重复）；
   新实现中止、零写入；
2. 部分写入 + 回滚回查失败 → 旧实现残留 1 条却回「已回滚」；新实现明说
   「可能残留」且不宣称已回滚，状态如实恢复；
3. 残留结果后的重试被幂等明确拦住，并指向「开始一对一」清理（不会被无声叠加）。

另覆盖：状态写失败不得继续（#4）、结果已写但终态写失败要明确报错、删除数不足
如实报告残留。桩只桩「存取」，状态机/回滚逻辑全跑 lib 真实现。
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


def _execute(fake):
    return lib_onetoone.execute(ACT, 1, client=fake, activity=_find_act(fake, ACT),
                                find_user_by_openid=_find_user)


def _status(fake):
    return _find_act(fake, ACT)["fields"]["一对一状态"]


def test_idempotency_read_failure_aborts_with_zero_writes(configured):
    """① 幂等查历史结果读失败 → 中止（零写入），绝不当空表重跑。"""
    fake = FakeBitable(_default_tables(), search_fails={ONETOONE_RESULT_TABLE})
    res = _execute(fake)
    assert res["code"] == "SEARCH_FAILED"
    assert fake.writes() == []
    assert _status(fake) == "收集中"


def test_selections_read_failure_restores_collecting(configured):
    """③ 志愿读失败 → 恢复「收集中」＋明说失败，且不写任何结果。"""
    fake = FakeBitable(_default_tables(), search_fails={ONETOONE_SELECT_TABLE})
    res = _execute(fake)
    assert res["code"] == "SELECTIONS_SEARCH_FAILED"
    assert res["restored"] is True
    assert _status(fake) == "收集中"
    assert not [c for c in fake.writes() if c[0] == "batch_create"]
    text = bot_onetoone._render_execute_result(res, ACT)
    assert text == "读取一对一志愿失败，已中止匹配，状态已恢复为「收集中」，请稍后重试。"


def test_restore_failure_is_reported_not_disguised(configured):
    """③ 恢复「收集中」也失败 → 不得说「状态已恢复」。"""
    fake = FakeBitable(_default_tables(), search_fails={ONETOONE_SELECT_TABLE},
                       write_fail_plan={ACTIVITY_TABLE_ID: {2}})
    res = _execute(fake)
    assert res["code"] == "SELECTIONS_SEARCH_FAILED"
    assert res["restored"] is False
    text = bot_onetoone._render_execute_result(res, ACT)
    assert "状态恢复为「收集中」失败" in text
    assert "已恢复" not in text


def test_partial_write_rollback_verify_failure_says_residual(configured):
    """⑥ 部分写入 + 回查失败 → 明说可能残留；重试被幂等明确拦住。"""
    fake = FakeBitable(_default_tables(), fail_create_after=1,
                       search_fail_plan={ONETOONE_RESULT_TABLE: {2}})
    res = _execute(fake)
    assert res["code"] == "PARTIAL_WRITE_VERIFY_FAILED"
    assert len(fake.tables[ONETOONE_RESULT_TABLE]) == 1, "无法确认回滚时残留如实可见"
    assert _status(fake) == "收集中"
    text = bot_onetoone._render_execute_result(res, ACT)
    assert "已回滚" not in text, "不得宣称已回滚"
    assert "回滚校验失败" in text and "残留" in text
    assert "『开始一对一』" in text

    # 残留结果后的重试：幂等明确拦截，指向「开始一对一」清理，且不再写入
    writes_before = len(fake.writes())
    res2 = _execute(fake)
    assert res2["code"] == "ALREADY_DONE"
    text2 = bot_onetoone._render_execute_result(res2, ACT)
    assert "已生成过一对一结果" in text2 and "开始一对一" in text2
    assert len(fake.tables[ONETOONE_RESULT_TABLE]) == 1
    assert len(fake.writes()) == writes_before, "被挡下的重试不得再写"


def test_clean_partial_rollback_removes_rows(configured):
    """⑥ 回查成功且删除足数 → 残留清零、恢复「收集中」、保留原文案。"""
    fake = FakeBitable(_default_tables(), fail_create_after=3)
    res = _execute(fake)
    assert res["code"] == "PARTIAL_WRITE"
    assert res["created"] == 3 and res["total"] == 8
    assert fake.tables[ONETOONE_RESULT_TABLE] == []
    assert _status(fake) == "收集中"
    assert "已回滚" in bot_onetoone._render_execute_result(res, ACT)


def test_delete_incomplete_reports_residual_count(configured):
    """⑥ 删除数不足 → 明报残留条数（不说「已回滚」）。"""
    fake = FakeBitable(_default_tables(), fail_create_after=3, delete_limit=1)
    res = _execute(fake)
    assert res["code"] == "PARTIAL_WRITE_INCOMPLETE"
    assert res["deleted"] == 1 and res["residual"] == 2
    assert len(fake.tables[ONETOONE_RESULT_TABLE]) == 2
    text = bot_onetoone._render_execute_result(res, ACT)
    assert "残留 2 条结果" in text
    assert "已回滚" not in text


def test_close_status_write_failure_aborts_before_reading_selections(configured):
    """② 状态切「已截止」写失败 → 中止；不能带着「收集中」继续写结果。"""
    fake = FakeBitable(_default_tables(), write_fail_plan={ACTIVITY_TABLE_ID: {1}})
    res = _execute(fake)
    assert res["code"] == "CLOSE_FAILED"
    assert len(fake.writes()) == 1
    assert not [c for c in fake.calls if c[0] == "search" and c[1] == ONETOONE_SELECT_TABLE]
    assert bot_onetoone._render_execute_result(res, ACT) == \
        "活动状态写入失败（无法切换为「已截止」），已中止匹配，请稍后重试。"


def test_done_status_write_failure_keeps_results_and_reports(configured):
    """⑦ 结果已写但「已完成」写失败 → 明确错误，绝不报成功。"""
    fake = FakeBitable(_default_tables(), write_fail_plan={ACTIVITY_TABLE_ID: {2}})
    res = _execute(fake)
    assert res["code"] == "DONE_STATUS_FAILED"
    assert fake.tables[ONETOONE_RESULT_TABLE], "结果确实已写入"
    assert _status(fake) == "已截止", "终态未写成，如实停在「已截止」"
    assert bot_onetoone._render_execute_result(res, ACT) == \
        "结果已生成，但状态写入失败（当前状态：已截止），请稍后重试或让管理员检查"


def test_algorithm_error_restores_status(configured, monkeypatch):
    """⑤ 算法异常 → 恢复「收集中」；不得留下「已截止」。"""
    def _boom(*a, **k):
        raise ValueError("炸了")

    monkeypatch.setattr(lib_onetoone, "run_onetoone_matching", _boom)
    fake = FakeBitable(_default_tables())
    res = _execute(fake)
    assert res["code"] == "ALGORITHM_ERROR"
    assert _status(fake) == "收集中"
    assert not [c for c in fake.writes() if c[0] == "batch_create"]
