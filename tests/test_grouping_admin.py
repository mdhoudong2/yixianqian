"""lib/grouping.py 的管理员分组业务单测（不联网，桩表驱动）。

覆盖：执行分组（幂等拦截、失败零状态变更、单笔「已完成」写、逐人字段、轮次参数）、
清空分组结果（fail-closed：读失败/删除不足/状态写失败）、轮次推演 helper、
preview 的零写入红线。

桩的是「存取」（tests/test_grouping_parity.py 的 FakeBitable），规则全跑真实现。
"""
import grouping as bot_grouping
import pytest

from lib import grouping as lib_grouping
from tests.test_grouping_parity import (
    ACTIVITY_TABLE_ID,
    GROUP_RESULT_TABLE,
    GROUP_SELECT_TABLE,
    SIGNUP_TABLE_ID,
    FakeBitable,
    _act,
    _find_act,
    _find_user,
    _res,
    _sel,
    _signup,
)

ACT = "A-0002"


def _tables(activity=None, signups=(), selections=(), results=()):
    return {
        ACTIVITY_TABLE_ID: [activity if activity is not None else _act()],
        SIGNUP_TABLE_ID: list(signups),
        GROUP_SELECT_TABLE: list(selections),
        GROUP_RESULT_TABLE: list(results),
    }


def _balanced(n_males, n_females, prefix="p"):
    """造 n 男 n 女、每人交 1 份志愿的报名与志愿记录（性别齐全）。"""
    signups, selections = [], []
    for i in range(n_males):
        oid = f"ou_{prefix}m{i}"
        signups.append(_signup(oid, f"男{i}"))
        selections.append(_sel(oid, "男性", ()))
    for i in range(n_females):
        oid = f"ou_{prefix}f{i}"
        signups.append(_signup(oid, f"女{i}"))
        selections.append(_sel(oid, "女性", ()))
    return signups, selections


@pytest.fixture
def configured(monkeypatch, tmp_path):
    """注入表 ID / 栏位名 / 锁目录。走 bot 的同一个入口，保证测的就是线上那套接线。"""
    monkeypatch.setattr(lib_grouping, "log", lambda *a, **k: None)
    bot_grouping.configure_grouping_lib(str(tmp_path))
    return bot_grouping


def _activity(fake):
    return _find_act(fake, ACT)


def _execute(fake, round_no=1, males_per_group=2, females_per_group=2,
             treat_search_failure_as_empty=True):
    return lib_grouping.execute_round(
        ACT, round_no, client=fake, activity=_activity(fake),
        find_user_by_openid=_find_user,
        males_per_group=males_per_group, females_per_group=females_per_group,
        treat_search_failure_as_empty=treat_search_failure_as_empty)


# ---------------------------------------------------------------- 执行分组：分支覆盖

def test_not_enough_people_leaves_zero_state_changes(configured):
    """T1：人数不够 → 零状态变更（不再有「恢复收集中」的恢复写），结果表零写入。"""
    signups, selections = _balanced(1, 1)
    fake = FakeBitable(_tables(signups=signups, selections=selections))

    res = _execute(fake)

    assert res["code"] == "NOT_ENOUGH_PEOPLE"
    assert (res["males"], res["females"]) == (1, 1)
    assert fake.writes() == [], "失败不得写任何表"
    assert _activity(fake)["fields"]["分组状态"] == "收集中", "活动栏位原样不动"
    assert fake.tables[GROUP_RESULT_TABLE] == []


def test_not_enough_people_message_includes_skipped(configured):
    """T2：跳过人数要单独报出来——管理员需要知道是「人不够」还是「有人资料没填」。"""
    signups, selections = _balanced(1, 1)
    selections.append(_sel("ou_unknown", ""))          # 性别缺失
    fake = FakeBitable(_tables(signups=signups, selections=selections))

    res = _execute(fake)

    assert res["code"] == "NOT_ENOUGH_PEOPLE"
    assert res["skipped"] == 1
    assert res["males"] == 1 and res["females"] == 1, "跳过的那个不算进人数"


def test_algorithm_exception_leaves_zero_state_changes(configured, monkeypatch):
    """T3：算法抛异常 → 零状态变更 + 错误文字带上异常内容。"""
    signups, selections = _balanced(4, 4)

    def _boom(*a, **k):
        raise ValueError("炸了")

    monkeypatch.setattr(lib_grouping, "run_grouping_algorithm", _boom)
    fake = FakeBitable(_tables(signups=signups, selections=selections))

    res = _execute(fake)

    assert res["code"] == "ALGORITHM_ERROR"
    assert res["error"] == "炸了"
    assert fake.writes() == []
    assert fake.tables[GROUP_RESULT_TABLE] == []


def test_empty_groups_leaves_zero_state_changes(configured, monkeypatch):
    """T4：人数够但组不出来 → 零状态变更。"""
    signups, selections = _balanced(4, 4)
    monkeypatch.setattr(lib_grouping, "run_grouping_algorithm", lambda *a, **k: [])
    fake = FakeBitable(_tables(signups=signups, selections=selections))

    res = _execute(fake)

    assert res["code"] == "EMPTY_GROUPS"
    assert fake.writes() == []
    assert fake.tables[GROUP_RESULT_TABLE] == []


def test_partial_write_rolls_back_results_without_touching_activity(configured):
    """T5：批量只写进一半 → 必须查回来删掉本轮半截结果；活动表一个栏位都不写。"""
    signups, selections = _balanced(4, 4)
    fake = FakeBitable(_tables(signups=signups, selections=selections), fail_create_after=5)

    res = _execute(fake)

    assert res["code"] == "PARTIAL_WRITE"
    assert (res["created"], res["total"]) == (5, 8)
    assert fake.tables[GROUP_RESULT_TABLE] == [], "半截结果必须被清干净"
    assert [c for c in fake.writes() if c[0] == "update"] == [], "失败不得写活动表"


def test_group_size_validation_rejects_bad_values_with_zero_calls(configured):
    """每组人数防御性校验：非 ≥1 整数 → GROUP_SIZE_INVALID、零表操作、零状态变更。"""
    for m_per, f_per in ((0, 2), (2, 0), (-1, 2), ("2", 2), (True, 2), (2, None), (2.0, 2)):
        fake = FakeBitable(_tables())
        res = _execute(fake, males_per_group=m_per, females_per_group=f_per)
        assert res["code"] == "GROUP_SIZE_INVALID", f"({m_per!r},{f_per!r})"
        assert fake.calls == [], f"({m_per!r},{f_per!r}) 不得有任何表操作"
        assert _activity(fake)["fields"]["分组状态"] == "收集中"


def test_idempotency_blocks_rerun(configured):
    """T6：本轮已有结果 → 一个字都不能写（含活动表）。"""
    signups, selections = _balanced(4, 4)
    fake = FakeBitable(_tables(signups=signups, selections=selections,
                               results=[_res("ou_x", 1, "2")]))

    res = _execute(fake, round_no=2)

    assert res["code"] == "ALREADY_DONE"
    assert fake.writes() == []
    assert _activity(fake)["fields"]["分组状态"] == "收集中"


def test_success_writes_done_and_sizes_in_single_update(configured):
    """T7：成功路径活动表只有一笔 update：已完成 + 每组人数（同笔）。"""
    signups, selections = _balanced(4, 4)
    fake = FakeBitable(_tables(signups=signups, selections=selections))

    res = _execute(fake)

    assert res["code"] == "OK"
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert len(updates) == 1, "成功必须单笔 update"
    assert updates[0][3] == {"分组状态": "已完成", "每组男生数": 2, "每组女生数": 2}
    assert _activity(fake)["fields"]["分组状态"] == "已完成"


def test_execute_uses_command_group_sizes_not_activity_fields(configured):
    """执行入参覆盖活动栏位：活动写 4/4、入参 2/2 → 结果按 2/2 分组、栏位被改成 2/2。"""
    signups, selections = _balanced(2, 2)
    fake = FakeBitable(_tables(activity=_act(每组男生数=4, 每组女生数=4),
                               signups=signups, selections=selections))

    res = _execute(fake, males_per_group=2, females_per_group=2)

    assert res["code"] == "OK"
    assert res["males_per_group"] == 2 and res["females_per_group"] == 2
    assert _activity(fake)["fields"]["每组男生数"] == 2


def test_success_round_field_is_stored(configured):
    """轮次写入：第 7 轮执行的每条结果行都带「轮次=7」。"""
    signups, selections = _balanced(2, 2)
    fake = FakeBitable(_tables(signups=signups, selections=selections))

    res = _execute(fake, round_no=7)

    assert res["code"] == "OK" and res["round"] == 7
    rows = fake.tables[GROUP_RESULT_TABLE]
    assert rows and all(r["fields"]["轮次"] == "7" for r in rows)


def test_done_status_write_failure_reports_explicit_error(configured):
    """结果已写但「已完成」写失败 → DONE_STATUS_FAILED（绝不报成功）。"""
    signups, selections = _balanced(2, 2)
    fake = FakeBitable(_tables(signups=signups, selections=selections))
    real_update = fake.update_record
    fake.update_record = lambda table_id, rid, fields: (
        None if table_id == ACTIVITY_TABLE_ID else real_update(table_id, rid, fields))

    res = _execute(fake)

    assert res["code"] == "DONE_STATUS_FAILED"
    assert fake.tables[GROUP_RESULT_TABLE], "结果确实已写入"


def test_writes_every_member_of_every_group(configured):
    """T8：3 组 × 8 人 = 24 条，每条的五个字段都要对。"""
    signups, selections = _balanced(12, 12)
    fake = FakeBitable(_tables(signups=signups, selections=selections))

    res = _execute(fake, males_per_group=4, females_per_group=4)

    assert res["code"] == "OK"
    assert res["group_count"] == 3
    assert res["participant_count"] == 24
    rows = fake.tables[GROUP_RESULT_TABLE]
    assert len(rows) == 24
    by_group = {}
    for r in rows:
        f = r["fields"]
        assert set(f) == {"活动ID", "组号", "用户open_id", "用户昵称", "用户性别", "轮次"}
        assert f["活动ID"] == ACT
        assert f["轮次"] == "1"
        assert f["用户昵称"] == next(
            s["fields"]["报名人昵称"] for s in signups
            if s["fields"]["报名人open_id"] == f["用户open_id"])
        by_group.setdefault(f["组号"], []).append((f["用户open_id"], f["用户性别"]))
    assert sorted(by_group) == [1, 2, 3]
    for members in by_group.values():
        assert len(members) == 8
        assert sum(1 for _, g in members if g == "男性") == 4
        assert sum(1 for _, g in members if g == "女性") == 4
    assert len({oid for members in by_group.values() for oid, _ in members}) == 24


# ---------------------------------------------------------------- 清空分组结果（reset）

def test_reset_group_results_clears_all_rounds_and_clears_status(configured):
    """清空要把该活动**所有轮次**的结果一次清干净，状态栏位写空（未开始）。"""
    results = [_res("ou_m1", 1, "1"), _res("ou_m2", 1, "1"),
               _res("ou_f1", 2, "2"), _res("ou_f2", 2, "2")]
    fake = FakeBitable(_tables(results=results))

    res = lib_grouping.reset_group_results(ACT, client=fake, activity=_activity(fake))

    assert res["code"] == "OK"
    assert res["cleared_results"] == 4
    deletes = [c for c in fake.writes() if c[0] == "batch_delete"]
    assert len(deletes) == 1
    assert sorted(deletes[0][2]) == sorted(r["record_id"] for r in results)
    assert fake.tables[GROUP_RESULT_TABLE] == []
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert len(updates) == 1 and updates[0][3] == {"分组状态": ""}
    assert _activity(fake)["fields"]["分组状态"] == ""


def test_reset_group_results_fail_closed_on_search_failure(configured):
    """fail-closed：结果表读失败 → RESULTS_SEARCH_FAILED，零写入、状态原样。"""
    fake = FakeBitable(_tables(results=[_res("ou_m1", 1, "1")]),
                       search_fails={GROUP_RESULT_TABLE})

    res = lib_grouping.reset_group_results(ACT, client=fake, activity=_activity(fake))

    assert res["code"] == "RESULTS_SEARCH_FAILED"
    assert fake.writes() == []
    assert _activity(fake)["fields"]["分组状态"] == "收集中"


def test_reset_group_results_fail_closed_on_delete_incomplete(configured):
    """fail-closed：删除数不足 → DELETE_INCOMPLETE（不报成功、不清状态）。"""
    results = [_res("ou_m1", 1, "1"), _res("ou_f1", 2, "2"), _res("ou_f2", 2, "2")]
    fake = FakeBitable(_tables(results=results), delete_limit=2)

    res = lib_grouping.reset_group_results(ACT, client=fake, activity=_activity(fake))

    assert res["code"] == "DELETE_INCOMPLETE"
    assert res["deleted"] == 2 and res["total"] == 3
    assert [c for c in fake.writes() if c[0] == "update"] == [], "删除不足不得再清状态"
    assert len(fake.tables[GROUP_RESULT_TABLE]) == 1


def test_reset_group_results_status_write_failure_reports(configured):
    """状态清空写失败 → STATUS_FAILED（结果已删，如实报告）。"""
    results = [_res("ou_m1", 1, "1")]
    fake = FakeBitable(_tables(results=results))
    real_update = fake.update_record
    fake.update_record = lambda table_id, rid, fields: (
        None if table_id == ACTIVITY_TABLE_ID else real_update(table_id, rid, fields))

    res = lib_grouping.reset_group_results(ACT, client=fake, activity=_activity(fake))

    assert res["code"] == "STATUS_FAILED"
    assert res["cleared_results"] == 1
    assert fake.tables[GROUP_RESULT_TABLE] == []


def test_reset_group_results_busy_when_lock_held(configured, monkeypatch):
    """抢锁失败 → BUSY、零写入（锁不可重入，这里是另一持有者持锁的外部视角）。"""
    monkeypatch.setattr(lib_grouping, "RESET_LOCK_TIMEOUT", 0.1)
    fake = FakeBitable(_tables(results=[_res("ou_m1", 1, "1")]))

    with lib_grouping.grouping_lock(ACT, timeout=1):
        res = lib_grouping.reset_group_results(ACT, client=fake, activity=_activity(fake))

    assert res["code"] == "BUSY"
    assert fake.writes() == []


# ---------------------------------------------------------------- 轮次推演 helper

def test_latest_round_reads_max_and_handles_empty(configured):
    signups, selections = _balanced(2, 2)
    with_rounds = FakeBitable(_tables(signups=signups, selections=selections, results=[
        _res("ou_pm0", 1, "1"), _res("ou_pm1", 1, "2"), _res("ou_pf0", 1, "2")]))
    assert lib_grouping.latest_round(with_rounds, ACT) == 2

    empty = FakeBitable(_tables())
    assert lib_grouping.latest_round(empty, ACT) == 0


def test_latest_round_ignores_blank_and_non_digit_rounds(configured):
    """空/非数字轮次行不参与（与 execute_round 幂等口径一致）。"""
    fake = FakeBitable(_tables(results=[
        {"record_id": "r_blank", "fields": {"活动ID": ACT, "组号": 1, "用户open_id": "x",
                                             "用户昵称": "", "用户性别": "男性", "轮次": ""}},
        _res("ou_pm0", 1, "3"),
    ]))
    assert lib_grouping.latest_round(fake, ACT) == 3


def test_latest_round_search_failure_returns_none(configured):
    """读失败必须回 None（与「没有结果」的 0 严格区分——调用端据此明报失败）。"""
    fake = FakeBitable(_tables(), search_fails={GROUP_RESULT_TABLE})
    assert lib_grouping.latest_round(fake, ACT) is None


# ---------------------------------------------------------------- preview

def test_preview_round_reports_current_history(configured):
    signups, selections = _balanced(4, 4)
    results = [_res("ou_m1", 1, "1"), _res("ou_m2", 1, "1"), _res("ou_f1", 2, "2")]
    fake = FakeBitable(_tables(signups=signups, selections=selections, results=results))

    res = lib_grouping.preview_round(ACT, 3, client=fake, activity=_activity(fake),
                                     find_user_by_openid=_find_user)

    assert res["historical_results_to_clear"] == 3
    assert res["rounds_preserved"] == ["1", "2"]
    assert res["results_preserved"] == 3
    assert fake.writes() == []


def test_round_out_of_range_is_blocked_by_preview(configured):
    """T11：轮次不限（字段已转文本），preview 只挡「非 ≥1 整数」。"""
    signups, selections = _balanced(4, 4)
    fake = FakeBitable(_tables(signups=signups, selections=selections))

    for bad in (0, -1, "2", None, True):
        res = lib_grouping.preview_round(ACT, bad, client=fake, activity=_activity(fake),
                                         find_user_by_openid=_find_user)
        assert res["can_execute"] is False, f"round={bad!r}"
        assert any("轮次必须是 ≥1 的整数" in b for b in res["blockers"])
    assert fake.writes() == []

    # 不限轮次：50/51/999 都是合法轮次，不得出现任何轮次 blocker
    for rnd in (1, 50, 51, 999):
        res = lib_grouping.preview_round(ACT, rnd, client=fake, activity=_activity(fake),
                                         find_user_by_openid=_find_user)
        assert res["can_execute"] is True, f"round={rnd} 应可执行"
        assert not any("轮次" in b for b in res["blockers"])
    assert fake.writes() == []


def test_duplicate_selector_oid_keeps_first(configured):
    """T12：同一个 open_id 交了两份志愿 → 只取第一份，且不计入「性别缺失」跳过数。"""
    signups, selections = _balanced(4, 4)
    dup = _sel("ou_pm0", "女性", ())                   # 与 ou_pm0 的「男性」重复
    dup["record_id"] = "rec_g_dup"
    fake = FakeBitable(_tables(signups=signups, selections=selections + [dup]))

    participants, _selections, skipped = lib_grouping._collect_selections(fake, ACT)

    assert skipped == 0
    p0 = [p for p in participants if p["id"] == "ou_pm0"]
    assert len(p0) == 1
    assert p0[0]["gender"] == "male", "应该保留第一条（男性）"


def test_summarize_selections_dedupes_dirty_rows(configured):
    """必修 1/2 的共同实现：非空 open_id 去重；首行性别缺失不吞人（后续有效行仍算）。"""
    records = [
        _sel("ou_dup", ""),                      # 首行性别缺失：算已提交，但不吞掉这个人
        _sel("ou_dup", "男性", ("ou_pf0",)),     # 次行有效：参与分组
        _sel("ou_m1", "男性"),
        _sel("", "男性"),                         # 空 open_id：整行忽略
    ]

    summary = lib_grouping.summarize_selections(records)

    assert summary["submitted_count"] == 2
    assert summary["submitted_oids"] == ["ou_dup", "ou_m1"]
    assert (summary["male_count"], summary["female_count"]) == (2, 0)
    assert summary["skipped"] == 1


def test_fetch_status_and_unsubmitted_share_dedup(configured):
    """必修 1 的负向验证（lib 层）：已提交 + 未提交 == 报名总数，重复行只算一次。"""
    signups, selections = _balanced(2, 2)
    dup = _sel("ou_pm0", "女性", ())                   # 同一人的第二行
    dup["record_id"] = "rec_g_dup"
    fake = FakeBitable(_tables(signups=signups, selections=selections + [dup]))

    st = lib_grouping.fetch_status(ACT, client=fake, activity=_activity(fake),
                                   find_user_by_openid=_find_user)
    un = lib_grouping.fetch_unsubmitted(ACT, client=fake, activity=_activity(fake),
                                        find_user_by_openid=_find_user)

    assert st["submitted_count"] == 4, "重复行只算一次（不是 5）"
    assert st["submitted_count"] + un["total"] == st["signup_count"] == 4


def test_first_gender_missing_second_valid_still_participates(configured):
    """必修 2：首行性别缺失、次行有效时，人必须仍参与分组（与 preview 计数一致）。"""
    records = [_sel("ou_pm0", ""), _sel("ou_pm0", "男性"), _sel("ou_pf0", "女性")]
    fake = FakeBitable(_tables(activity=_act(), selections=records))

    summary = lib_grouping.summarize_selections(records)

    assert summary["male_count"] == 1
    assert summary["female_count"] == 1
    preview = lib_grouping.preview_round(ACT, 1, client=fake, activity=_activity(fake),
                                         find_user_by_openid=_find_user)
    assert preview["male_count"] == summary["male_count"] == 1
    assert preview["female_count"] == summary["female_count"] == 1


def test_fetch_result_returns_rounds_and_isomorphic_groups(configured):
    """必修 4：结果读取按轮次过滤、默认最新轮，结构与 execute_round 的 groups 同构。"""
    def _result(oid, group_no, round_no, gender):
        rec = _res(oid, group_no, round_no)
        rec["record_id"] = f"rec_res_{round_no}_{group_no}_{oid}"
        rec["fields"]["用户性别"] = gender
        return rec

    results = [
        _result("ou_pm0", 1, "1", "男性"), _result("ou_pf0", 1, "1", "女性"),
        _result("ou_pf0", 1, "2", "女性"), _result("ou_pm0", 1, "2", "男性"),
    ]
    fake = FakeBitable(_tables(results=results))

    latest = lib_grouping.fetch_result(ACT, None, client=fake, activity=_activity(fake))
    assert latest["name"] == "关系自洽"
    assert latest["rounds"] == [1, 2] and latest["round"] == 2
    assert len(latest["groups"]) == 1
    g = latest["groups"][0]
    assert set(g) == {"group_id", "members"}
    # 成员键与 job result.groups 同构；男先女后；昵称空串退回 oid
    assert [m["open_id"] for m in g["members"]] == ["ou_pm0", "ou_pf0"]
    assert all(set(m) == {"open_id", "gender", "display_name"} for m in g["members"])
    assert g["members"][0]["display_name"] == "ou_pm0"

    first = lib_grouping.fetch_result(ACT, 1, client=fake, activity=_activity(fake))
    assert first["round"] == 1
    assert [m["open_id"] for m in first["groups"][0]["members"]] == ["ou_pm0", "ou_pf0"]

    missing = lib_grouping.fetch_result(ACT, 9, client=fake, activity=_activity(fake))
    assert missing["round"] == 9 and missing["groups"] == []
    assert missing["rounds"] == [1, 2]


def test_fetch_result_empty_table(configured):
    fake = FakeBitable(_tables())
    res = lib_grouping.fetch_result(ACT, None, client=fake, activity=_activity(fake))
    assert res["round"] is None and res["rounds"] == [] and res["groups"] == []


# ---------------------------------------------------------------- 迁移红线

def test_preview_round_writes_nothing(configured):
    """preview 绝不能写任何栏位（管理台「看一眼」不得产生副作用）。"""
    signups, selections = _balanced(4, 4)
    fake = FakeBitable(_tables(signups=signups, selections=selections,
                               results=[_res("ou_m1", 1, "1"), _res("ou_f2", 1, "2")]))

    res = lib_grouping.preview_round(ACT, 3, client=fake, activity=_activity(fake),
                                     find_user_by_openid=_find_user)

    assert fake.writes() == [], "preview 一次写都不能有"
    assert fake.calls == [("search", GROUP_SELECT_TABLE), ("search", GROUP_RESULT_TABLE)]
    assert _activity(fake)["fields"]["分组状态"] == "收集中"
    assert res["group_status"] == "收集中"
    assert res["historical_results_to_clear"] == 2
    assert res["historical_rounds_to_clear"] == ["1", "2"]
    assert res["participant_count"] == 8
    assert res["expected_groups"] == 2
    assert res["can_execute"] is True


def test_preview_blockers_cover_all_four_causes(configured):
    """四种事前拦截各要能单独触发，且每组人数为 0 时不再重复报「人数不足」。"""
    signups, selections = _balanced(4, 4)

    zero = FakeBitable(_tables(activity=_act(每组男生数=0, 每组女生数=0),
                               signups=signups, selections=selections))
    res = lib_grouping.preview_round(ACT, 1, client=zero, activity=_activity(zero),
                                     find_user_by_openid=_find_user)
    assert res["can_execute"] is False
    assert len(res["blockers"]) == 1 and "必须大于0" in res["blockers"][0]

    thin, thin_sel = _balanced(1, 1)
    not_enough = FakeBitable(_tables(signups=thin, selections=thin_sel))
    res = lib_grouping.preview_round(ACT, 1, client=not_enough,
                                     activity=_activity(not_enough),
                                     find_user_by_openid=_find_user)
    assert res["can_execute"] is False
    assert len(res["blockers"]) == 1 and "人数不足" in res["blockers"][0]
    assert res["blockers"][0] == "人数不足：当前 1男1女，每组需要 2男2女（至少 2男2女）"

    done = FakeBitable(_tables(signups=signups, selections=selections,
                                results=[_res("ou_x", 1, "1")]))
    res = lib_grouping.preview_round(ACT, 1, client=done, activity=_activity(done),
                                     find_user_by_openid=_find_user)
    assert res["can_execute"] is False
    assert res["blockers"] == ["第1轮已生成分组结果，请勿重复执行"]

    bad_round = FakeBitable(_tables(signups=signups, selections=selections))
    res = lib_grouping.preview_round(ACT, 0, client=bad_round, activity=_activity(bad_round),
                                     find_user_by_openid=_find_user)
    assert res["can_execute"] is False
    assert res["blockers"] == ["轮次必须是 ≥1 的整数，当前 0"]


def test_search_failure_is_not_treated_as_empty_when_asked(configured):
    """幂等检查读不到飞书时，H5 侧要中止而不是当成「没跑过」。

    当成没跑过就会重跑并覆盖既有结果——那正是这个开关要挡的事。
    bot 侧保持老行为（当空表继续），所以默认参数必须是 True。
    """
    signups, selections = _balanced(4, 4)
    fake = FakeBitable(_tables(signups=signups, selections=selections),
                       search_fails={GROUP_RESULT_TABLE})

    res = _execute(fake, treat_search_failure_as_empty=False)
    assert res["code"] == "SEARCH_FAILED"
    assert fake.writes() == []

    fake2 = FakeBitable(_tables(signups=signups, selections=selections),
                        search_fails={GROUP_RESULT_TABLE})
    res2 = _execute(fake2)
    assert res2["code"] == "OK", "bot 侧默认必须沿用「读不到当空表」的老行为"


def test_scores_survived_the_move():
    """算法评分常量从 bot/constants.py 搬进 lib，数值一个都不能变。"""
    assert lib_grouping.PRIORITY_SCORES == {
        1: 100, 2: 90, 3: 80, 4: 70, 5: 65, 6: 60, 7: 55, 8: 50, 9: 45, 10: 40}
    assert lib_grouping.MATCHMAKER_STAR_SCORES == {5: 100, 4: 90, 3: 80, 2: 75, 1: 70}
    assert lib_grouping.WEIGHT_USER_SELECTION == 0.7
    assert lib_grouping.WEIGHT_MATCHMAKER_PICK == 0.3


def test_configure_rejects_missing_field_names(monkeypatch):
    """栏位名缺一个就在 configure 时炸，别等到跑分组时才发现写进了错误的表。"""
    monkeypatch.setitem(lib_grouping._cfg, "fields", {})
    with pytest.raises(RuntimeError, match="缺少栏位名"):
        lib_grouping.configure(fields={"activity_name": "活动名称"})


def test_configure_rejects_unknown_field_names():
    with pytest.raises(RuntimeError, match="未知栏位名"):
        lib_grouping.configure(fields={"根本没有这个栏位": "x"})


def test_business_functions_require_configure(monkeypatch):
    monkeypatch.setitem(lib_grouping._cfg, "tables", {})
    monkeypatch.setitem(lib_grouping._cfg, "fields", {})
    with pytest.raises(RuntimeError, match="未 configure"):
        lib_grouping.fetch_status(ACT, client=object(), activity={}, find_user_by_openid=_find_user)


def test_incomplete_config_names_the_missing_items(monkeypatch):
    """只注入一部分（这里只给 activity_table_id）时，要点名缺哪几项。"""
    # 栏位名补齐，只留三张表不注入
    monkeypatch.setitem(lib_grouping._cfg, "tables", {"activity_table_id": ACTIVITY_TABLE_ID})
    monkeypatch.setitem(lib_grouping._cfg, "fields",
                        {k: f"F_{k}" for k in lib_grouping._REQUIRED_FIELDS})
    with pytest.raises(RuntimeError) as ei:
        lib_grouping.fetch_status(ACT, client=object(), activity={},
                                  find_user_by_openid=_find_user)
    msg = str(ei.value)
    for missing in ("group_select_table", "group_result_table", "signup_table_id"):
        assert missing in msg, f"报错应点名缺少 {missing}，实际：{msg}"
    assert "activity_table_id" not in msg, "已注入的表不该被列进缺失清单"


# ---------------------------------------------------------------- bot 渲染（清空指令）

def _patch_bot(monkeypatch, fake):
    monkeypatch.setattr(bot_grouping, "bitable", fake)
    monkeypatch.setattr(bot_grouping, "find_activity_by_id", lambda aid: _find_act(fake, aid))
    monkeypatch.setattr(bot_grouping, "log", lambda *a, **k: None)


def test_bot_reset_group_reports_success_and_failures(configured, monkeypatch):
    """清空指令回执：成功报「已清空 N 条」；读失败/删除不足如实报错、不吞成成功。"""
    results = [_res("ou_m1", 1, "1"), _res("ou_f1", 2, "2")]
    fake = FakeBitable(_tables(results=results))
    _patch_bot(monkeypatch, fake)

    reply = bot_grouping.handle_admin_reset_group(ACT)
    assert reply == "已清空活动「关系自洽」的历史分组结果 2 条，分组状态已重置为未开始。"
    assert fake.tables[GROUP_RESULT_TABLE] == []
    assert _activity(fake)["fields"]["分组状态"] == ""

    # 读失败
    fake2 = FakeBitable(_tables(results=results), search_fails={GROUP_RESULT_TABLE})
    _patch_bot(monkeypatch, fake2)
    reply2 = bot_grouping.handle_admin_reset_group(ACT)
    assert reply2 == "清空失败：读取历史分组结果失败，未做任何删除，请稍后重试。"
    assert fake2.writes() == []

    # 删除不完整
    fake3 = FakeBitable(_tables(results=results), delete_limit=1)
    _patch_bot(monkeypatch, fake3)
    reply3 = bot_grouping.handle_admin_reset_group(ACT)
    assert reply3 == "清空失败：分组结果删除不完整（成功删除 1/2 条），请稍后重试。"
    assert [c for c in fake3.writes() if c[0] == "update"] == []

    # 活动不存在 / 格式错误
    assert bot_grouping.handle_admin_reset_group("") == \
        "格式：清空分组结果 活动ID\n例如：清空分组结果 A-0002"
    assert bot_grouping.handle_admin_reset_group("A-9999") == "未找到活动：A-9999"


def test_bot_reset_group_busy_reports_busy(configured, monkeypatch):
    """清空指令自持锁：锁被占时明确回执，绝不无限等待。"""
    fake = FakeBitable(_tables(results=[_res("ou_m1", 1, "1")]))
    _patch_bot(monkeypatch, fake)
    monkeypatch.setattr(lib_grouping, "RESET_LOCK_TIMEOUT", 0.1)

    with lib_grouping.grouping_lock(ACT, timeout=1):
        reply = bot_grouping.handle_admin_reset_group(ACT)

    assert reply == bot_grouping.GROUPING_BUSY_REPLY
    assert fake.writes() == []


def test_bot_stop_group_refuses_to_claim_success_on_failure(configured, monkeypatch):
    """bot 渲染层对未知/失败码不得伪装成成功（以 DONE_STATUS_FAILED 为例）。"""
    signups, selections = _balanced(2, 2)
    fake = FakeBitable(_tables(signups=signups, selections=selections))
    _patch_bot(monkeypatch, fake)
    real_update = fake.update_record
    fake.update_record = lambda table_id, rid, fields: (
        None if table_id == ACTIVITY_TABLE_ID else real_update(table_id, rid, fields))

    reply = bot_grouping.handle_admin_stop_group(f"{ACT} 2 2")
    assert "已生成" in reply and "状态写入失败" in reply
    assert "🎉" not in reply, "不得伪装成成功回执"
