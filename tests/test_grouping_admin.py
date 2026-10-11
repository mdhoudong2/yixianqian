"""lib/grouping.py 的管理员分组业务单测（不联网，桩表驱动）。

迁移前 `_do_grouping` 是零覆盖的。这一组把它的每条分支钉死：状态回滚、写入回滚、
幂等拦截、状态流转顺序、逐人写入的字段、preview 的零写入红线。

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
    """造 n 男 n 女、每人交 1 份志愿的报名与志愿记录（性别齐全）。

    不接收每组人数——那是活动栏位，由各用例自己在 _act(...) 里写死，避免
    「以为设了 4 男 4 女、其实是活动表里那个 2」这种以为设了其实没设的错。
    """
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


# ---------------------------------------------------------------- 分支覆盖

def test_not_enough_people_restores_collecting(configured):
    """T1：人数不够 → 状态必须被写回「收集中」，且分组结果表零写入。

    忘了回滚的话，活动会永远卡在「已截止」，用户再也提交不了志愿。
    """
    signups, selections = _balanced(1, 1)
    fake = FakeBitable(_tables(signups=signups, selections=selections))

    res = lib_grouping.execute_round(ACT, 1, client=fake, activity=_activity(fake),
                                     find_user_by_openid=_find_user)

    assert res["code"] == "NOT_ENOUGH_PEOPLE"
    assert (res["males"], res["females"]) == (1, 1)
    assert _activity(fake)["fields"]["分组状态"] == "收集中"
    assert fake.tables[GROUP_RESULT_TABLE] == []


def test_not_enough_people_message_includes_skipped(configured):
    """T2：跳过人数要单独报出来——管理员需要知道是「人不够」还是「有人资料没填」。"""
    signups, selections = _balanced(1, 1)
    selections.append(_sel("ou_unknown", ""))          # 性别缺失
    fake = FakeBitable(_tables(signups=signups, selections=selections))

    res = lib_grouping.execute_round(ACT, 1, client=fake, activity=_activity(fake),
                                     find_user_by_openid=_find_user)

    assert res["code"] == "NOT_ENOUGH_PEOPLE"
    assert res["skipped"] == 1
    assert res["males"] == 1 and res["females"] == 1, "跳过的那个不算进人数"


def test_algorithm_exception_restores_collecting(configured, monkeypatch):
    """T3：算法抛异常 → 状态回滚 + 错误文字带上异常内容，不能只写一句「失败了」。"""
    signups, selections = _balanced(4, 4)

    def _boom(*a, **k):
        raise ValueError("炸了")

    monkeypatch.setattr(lib_grouping, "run_grouping_algorithm", _boom)
    fake = FakeBitable(_tables(signups=signups, selections=selections))

    res = lib_grouping.execute_round(ACT, 1, client=fake, activity=_activity(fake),
                                     find_user_by_openid=_find_user)

    assert res["code"] == "ALGORITHM_ERROR"
    assert res["error"] == "炸了"
    assert _activity(fake)["fields"]["分组状态"] == "收集中"
    assert fake.tables[GROUP_RESULT_TABLE] == []


def test_empty_groups_restores_collecting(configured, monkeypatch):
    """T4：人数够但组不出来 → 同样是回滚，不能让活动停在「已截止」。"""
    signups, selections = _balanced(4, 4)
    monkeypatch.setattr(lib_grouping, "run_grouping_algorithm", lambda *a, **k: [])
    fake = FakeBitable(_tables(signups=signups, selections=selections))

    res = lib_grouping.execute_round(ACT, 1, client=fake, activity=_activity(fake),
                                     find_user_by_openid=_find_user)

    assert res["code"] == "EMPTY_GROUPS"
    assert _activity(fake)["fields"]["分组状态"] == "收集中"
    assert fake.tables[GROUP_RESULT_TABLE] == []


def test_partial_write_rolls_back(configured):
    """T5：批量只写进一半 → 必须查回来删掉 + 回滚状态。

    否则用户看到的是「已完成」，点进去发现自己那一组根本不存在。
    """
    signups, selections = _balanced(4, 4)
    fake = FakeBitable(_tables(signups=signups, selections=selections), fail_create_after=5)

    res = lib_grouping.execute_round(ACT, 1, client=fake, activity=_activity(fake),
                                     find_user_by_openid=_find_user)

    assert res["code"] == "PARTIAL_WRITE"
    assert (res["created"], res["total"]) == (5, 8)
    assert fake.tables[GROUP_RESULT_TABLE] == [], "半截结果必须被清干净"
    assert _activity(fake)["fields"]["分组状态"] == "收集中"


def test_idempotency_blocks_rerun(configured):
    """T6：本轮已有结果 → 一个字都不能写，连「已截止」都不许先改。

    幂等检查与写入之间的时间窗靠外层的 grouping_lock 补上。
    """
    signups, selections = _balanced(4, 4)
    fake = FakeBitable(_tables(signups=signups, selections=selections,
                               results=[_res("ou_x", 1, "2")]))

    res = lib_grouping.execute_round(ACT, 2, client=fake, activity=_activity(fake),
                                     find_user_by_openid=_find_user)

    assert res["code"] == "ALREADY_DONE"
    assert fake.writes() == []
    assert _activity(fake)["fields"]["分组状态"] == "收集中"


def test_status_transitions_on_success(configured):
    """T7：成功路径的状态流转顺序必须是 收集中 → 已截止 → 已完成。"""
    signups, selections = _balanced(4, 4)
    fake = FakeBitable(_tables(signups=signups, selections=selections))

    res = lib_grouping.execute_round(ACT, 1, client=fake, activity=_activity(fake),
                                     find_user_by_openid=_find_user)

    assert res["code"] == "OK"
    statuses = [c[3]["分组状态"] for c in fake.writes() if c[0] == "update"]
    assert statuses == ["已截止", "已完成"]
    assert _activity(fake)["fields"]["分组状态"] == "已完成"


def test_writes_every_member_of_every_group(configured):
    """T8：3 组 × 8 人 = 24 条，每条的五个字段都要对。

    少写一条就有一个用户在 H5 上查不到自己的组，而且没有任何报错。
    """
    signups, selections = _balanced(12, 12)
    fake = FakeBitable(_tables(activity=_act(每组男生数=4, 每组女生数=4),
                               signups=signups, selections=selections))

    res = lib_grouping.execute_round(ACT, 1, client=fake, activity=_activity(fake),
                                     find_user_by_openid=_find_user)

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


def test_start_group_clears_all_historical_results(configured):
    """T9：重新开放要把该活动**所有轮次**的结果一次清干净，不分轮次。

    只清当轮的话，幂等保护会拦下重跑，管理员会以为指令没生效。
    """
    results = [_res("ou_m1", 1, "1"), _res("ou_m2", 1, "1"),
               _res("ou_f1", 2, "2"), _res("ou_f2", 2, "2")]
    fake = FakeBitable(_tables(results=results))

    res = lib_grouping.start_group(ACT, 3, 3, client=fake, activity=_activity(fake))

    assert res["code"] == "OK"
    assert res["cleared_results"] == 4
    deletes = [c for c in fake.writes() if c[0] == "batch_delete"]
    assert len(deletes) == 1
    assert sorted(deletes[0][2]) == sorted(r["record_id"] for r in results)
    assert fake.tables[GROUP_RESULT_TABLE] == []


def test_start_group_writes_config_fields(configured):
    """T10：活动表只写那三个栏位——多写一个就可能顺手盖掉别人正在改的东西。"""
    fake = FakeBitable(_tables())

    lib_grouping.start_group(ACT, 4, 5, client=fake, activity=_activity(fake))

    updates = [c for c in fake.writes() if c[0] == "update"]
    assert len(updates) == 1
    assert updates[0][3] == {"分组状态": "收集中", "每组男生数": 4, "每组女生数": 5}
    assert _activity(fake)["fields"]["分组状态"] == "收集中"


def test_start_group_keeps_history_when_clear_results_false(configured):
    """开启新一轮：只开放收集，历史结果一条不动，连结果表都不读。

    算法是确定性的，直接重跑旧轮次没有意义——保留历史 + 重开收集才能跑出
    第 N+1 轮。
    """
    results = [_res("ou_m1", 1, "1"), _res("ou_m2", 1, "1"),
               _res("ou_f1", 2, "2"), _res("ou_f2", 2, "2")]
    fake = FakeBitable(_tables(results=results))

    res = lib_grouping.start_group(ACT, 3, 3, client=fake, activity=_activity(fake),
                                   clear_results=False)

    assert res["code"] == "OK"
    assert res["cleared_results"] == 0
    assert _activity(fake)["fields"]["分组状态"] == "收集中"
    # 历史结果原样保留，且结果表零读零删
    assert [r["record_id"] for r in fake.tables[GROUP_RESULT_TABLE]] == \
        [r["record_id"] for r in results]
    assert not any(c[0] == "batch_delete" for c in fake.calls)
    assert not any(c == ("search", GROUP_RESULT_TABLE) for c in fake.calls), \
        "保留模式不得读结果表（省一次全表读，也不进 SEARCH_FAILED）"
    updates = [c for c in fake.writes() if c[0] == "update"]
    assert len(updates) == 1
    assert updates[0][3] == {"分组状态": "收集中", "每组男生数": 3, "每组女生数": 3}


def test_start_group_clear_results_true_matches_default(configured):
    """显式 clear_results=True 与默认路径完全一致（bot 走的仍是默认值）。"""
    results = [_res("ou_m1", 1, "1"), _res("ou_f1", 2, "2")]
    fake_default = FakeBitable(_tables(results=results))
    fake_explicit = FakeBitable(_tables(results=results))

    res_default = lib_grouping.start_group(ACT, 3, 3, client=fake_default,
                                           activity=_activity(fake_default))
    res_explicit = lib_grouping.start_group(ACT, 3, 3, client=fake_explicit,
                                            activity=_activity(fake_explicit),
                                            clear_results=True)

    assert res_default == res_explicit
    assert res_explicit["cleared_results"] == 2
    assert fake_default.writes() == fake_explicit.writes()
    assert fake_default.tables == fake_explicit.tables


def test_preview_round_reports_preserved_history(configured):
    """开启新一轮的加法字段：preview 要能报出「保留 N 轮 / 共 X 条」。

    旧字段 historical_results_to_clear 语义不动（它始终回答「会删多少」），
    rounds_preserved / results_preserved 回答「会留下什么」。
    """
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
    """T11：「轮次」是单选栏位，飞书只配了 1-10。第 11 轮必须在执行前就被挡住，
    且挡住它的那次调用一个栏位都不能写。

    真的写下去会整批失败再触发回滚，管理员看到的是「已截止又收集中」，很难查。
    """
    signups, selections = _balanced(4, 4)
    fake = FakeBitable(_tables(signups=signups, selections=selections))

    res = lib_grouping.preview_round(ACT, 11, client=fake, activity=_activity(fake),
                                     find_user_by_openid=_find_user)

    assert res["can_execute"] is False
    assert any("超出单选字段上限" in b for b in res["blockers"])
    assert fake.writes() == []


def test_duplicate_selector_oid_keeps_first(configured):
    """T12：同一个 open_id 交了两份志愿 → 只取第一份，且不计入「性别缺失」跳过数。

    取到两份的话同一个人会被塞进两个组；错记成跳过则「人数不足」的提示会误导。
    """
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
    """必修 1/2 的共同实现：非空 open_id 去重；首行性别缺失不吞人（后续有效行仍算）。

    同一实现同时喂 bot「分组状态」、preview 与 H5 端点，三处口径不可能再漂移。
    """
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
    """RFC R8：preview 绝不能碰「分组状态」。

    执行路径一进门就把活动改成「已截止」；preview 若沿用同一条路径，管理员只是
    「看一眼预览」就把活动截止掉，用户立刻不能提交志愿。这是本次最重的一条红线。
    """
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
    """四种事前拦截各要能单独触发，且每组人数为 0 时不再重复报「人数不足」。

    两条一起报等于把同一个根因说两遍，管理员会去改错的那个设置。
    """
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

    over = FakeBitable(_tables(signups=signups, selections=selections))
    res = lib_grouping.preview_round(ACT, 11, client=over, activity=_activity(over),
                                     find_user_by_openid=_find_user)
    assert res["can_execute"] is False
    assert res["blockers"] == ["第11轮超出单选字段上限（轮次字段只支持 1-10）"]


def test_search_failure_is_not_treated_as_empty_when_asked(configured):
    """RFC R7：幂等检查读不到飞书时，H5 侧要中止而不是当成「没跑过」。

    当成没跑过就会重跑并覆盖既有结果——那正是这个开关要挡的事。
    bot 侧保持老行为（当空表继续），所以默认参数必须是 True。
    """
    signups, selections = _balanced(4, 4)
    fake = FakeBitable(_tables(signups=signups, selections=selections),
                       search_fails={GROUP_RESULT_TABLE})

    res = lib_grouping.execute_round(ACT, 1, client=fake, activity=_activity(fake),
                                     find_user_by_openid=_find_user,
                                     treat_search_failure_as_empty=False)
    assert res["code"] == "SEARCH_FAILED"
    assert fake.writes() == []

    fake2 = FakeBitable(_tables(signups=signups, selections=selections),
                        search_fails={GROUP_RESULT_TABLE})
    res2 = lib_grouping.execute_round(ACT, 1, client=fake2, activity=_activity(fake2),
                                      find_user_by_openid=_find_user)
    assert res2["code"] == "OK", "bot 侧默认必须沿用「读不到当空表」的老行为"


def test_start_group_reports_search_failure_when_asked(configured):
    signups, selections = _balanced(4, 4)
    fake = FakeBitable(_tables(signups=signups, selections=selections),
                       search_fails={GROUP_RESULT_TABLE})

    res = lib_grouping.start_group(ACT, 2, 2, client=fake, activity=_activity(fake),
                                   treat_search_failure_as_empty=False)
    assert res["code"] == "SEARCH_FAILED"


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
    """只注入一部分（这里只给 activity_table_id）时，要点名缺哪几项。

    否则报错要等到业务跑深了才炸成 KeyError('group_result_table')，看不出根因；
    Phase 2 接 H5 端点时最容易踩这个。
    """
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


def test_bot_start_group_refuses_to_claim_success_on_failure(configured, monkeypatch):
    """bot 不能在「清历史失败」后仍回「志愿填写已开始」。

    今天 bot 走默认 treat_search_failure_as_empty=True，SEARCH_FAILED 不可达（parity
    佐证），所以这条用桩把 lib 的回传码换成失败，钉住 bot/grouping.py 里那道
    「回传码不能被丢」的断言——否则日后照 H5 改成传 False，就会静默回一条
    与事实相反的成功回执。
    """
    signups, selections = _balanced(2, 2)
    fake = FakeBitable(_tables(signups=signups, selections=selections))
    monkeypatch.setattr(bot_grouping, "bitable", fake)
    monkeypatch.setattr(bot_grouping, "find_activity_by_id", lambda aid: _find_act(fake, aid))
    monkeypatch.setattr(bot_grouping, "log", lambda *a, **k: None)

    real_start = lib_grouping.start_group
    monkeypatch.setattr(
        lib_grouping, "start_group",
        lambda *a, **k: dict(real_start(*a, **k), code="SEARCH_FAILED"))

    with pytest.raises(RuntimeError, match="start_group 未成功"):
        bot_grouping.handle_admin_start_group(f"{ACT} 2 2")


def test_bot_start_group_still_returns_verbatim_success_reply(configured, monkeypatch):
    """对照组：正常路径的回执一字都不能变（红线：parity 84 条全绿靠它）。"""
    signups, selections = _balanced(2, 2)
    fake = FakeBitable(_tables(signups=signups, selections=selections))
    monkeypatch.setattr(bot_grouping, "bitable", fake)
    monkeypatch.setattr(bot_grouping, "find_activity_by_id", lambda aid: _find_act(fake, aid))
    monkeypatch.setattr(bot_grouping, "log", lambda *a, **k: None)

    reply = bot_grouping.handle_admin_start_group(f"{ACT} 2 2")
    assert reply.startswith("志愿填写已开始：") and "已开放，未发送群发通知。" in reply
