"""一對一多輪：lib 層冪等/取結果/預覽/重開（樁表驅動，不聯網）。

- 同 (活動, 輪次) 重跑被 ALREADY_DONE 攔；換輪次可跑，且歷史輪次結果保留；
- 遷移前的空輪次行 = 第 1 輪（回填由 scripts/dev/add_onetoone_result_round.py
  落地；lib 端以 round_of 統一，見 tests/test_onetoone_parity.py 的刻意分歧節）；
- fetch_result：round=None 取最新、指定輪次取指定輪；輪次列表升序；
- start_collection(clear_results=False)：只寫狀態、歷史不讀不刪（連 search 都不發）；
- start_collection(clear_results=True 默認)：清全部輪次歷史（破壞性語義不變）。
"""
import pytest

import onetoone as bot_onetoone

from lib import onetoone as lib_onetoone
from tests.test_onetoone_parity import (
    ACT,
    FakeBitable,
    ONETOONE_RESULT_TABLE,
    ONETOONE_SELECT_TABLE,
    _default_tables,
    _find_act,
    _find_user,
    _res,
)


@pytest.fixture
def configured(monkeypatch, tmp_path):
    """注入表 ID / 栏位名 / 锁目录（走 bot 的同一个入口）。"""
    monkeypatch.setattr(bot_onetoone, "ONETOONE_SELECT_TABLE", ONETOONE_SELECT_TABLE)
    monkeypatch.setattr(bot_onetoone, "ONETOONE_RESULT_TABLE", ONETOONE_RESULT_TABLE)
    monkeypatch.setattr(lib_onetoone, "log", lambda *a, **k: None)
    bot_onetoone.configure_onetoone_lib(str(tmp_path))


def _execute(fake, round_no):
    return lib_onetoone.execute(ACT, round_no, client=fake, activity=_find_act(fake, ACT),
                                find_user_by_openid=_find_user, female_chats=2)


def _row(record_id, oid, target, rank=1, round_no="1"):
    fields = {**_res(oid, target, rank)["fields"], "轮次": round_no}
    return {"record_id": record_id, "fields": fields}


def _round_rows(fake, round_no):
    return [r for r in fake.tables[ONETOONE_RESULT_TABLE]
            if r["fields"].get("轮次") == str(round_no)]


def test_same_round_blocked_new_round_runs_and_preserves_history(configured):
    """同轮重跑被拒（零写入）；新轮可跑并保留旧轮；新轮再重跑又被拒。"""
    fake = FakeBitable({**_default_tables(),
                        ONETOONE_RESULT_TABLE: [_row("rec_r1", "ou_m1", "ou_f1")]})

    res1 = _execute(fake, 1)
    assert res1["code"] == "ALREADY_DONE" and res1["round"] == 1
    assert fake.writes() == [], "被幂等拦下的重跑零写入"

    res2 = _execute(fake, 2)
    assert res2["code"] == "OK" and res2["round"] == 2
    assert len(_round_rows(fake, 1)) == 1, "第1轮历史保留"
    r2 = _round_rows(fake, 2)
    assert len(r2) == 8, "2男2女各出 2 条必聊"
    assert _find_act(fake, ACT)["fields"]["一对一状态"] == "已完成"

    writes_before = len(fake.writes())
    res3 = _execute(fake, 2)
    assert res3["code"] == "ALREADY_DONE"
    assert len(fake.writes()) == writes_before, "第2轮重跑不得再写"


def test_legacy_rows_without_round_are_round_one(configured):
    """迁移前的空轮次行视为第 1 轮：拦第1轮、放行第2轮（与回填后的数据同语义）。"""
    fake = FakeBitable({**_default_tables(),
                        ONETOONE_RESULT_TABLE: [_res("ou_m1", "ou_f1")]})  # 无轮次字段值

    res1 = _execute(fake, 1)
    assert res1["code"] == "ALREADY_DONE"
    res2 = _execute(fake, 2)
    assert res2["code"] == "OK"
    rows = fake.tables[ONETOONE_RESULT_TABLE]
    legacy = [r for r in rows if r["record_id"] == "rec_res_A-0002_ou_m1_ou_f1"]
    assert len(legacy) == 1 and not legacy[0]["fields"].get("轮次"), \
        "旧行原样保留（轮次回填由迁移脚本负责，lib 只按第1轮语义拦截）"
    assert len(_round_rows(fake, 2)) == 8


def test_execute_writes_round_field_on_every_row(configured):
    fake = FakeBitable(_default_tables())
    res = _execute(fake, 3)
    assert res["code"] == "OK"
    rows = _round_rows(fake, 3)
    assert len(rows) == 8 and all(r["fields"]["活动ID"] == ACT for r in rows)


def test_execute_round_out_of_range_is_rejected_with_zero_writes(configured):
    """轮次越界（0/11/负数/非整数）在写入路径收口：ROUND_OUT_OF_RANGE、零读写。

    preview 的「上限」blocker 只是提示，bot/H5 都可能绕过；这里钉死 execute 自身
    的入口防线——越界时连一次表操作（含只读搜索）都不许发，更不能动状态。
    """
    for bad in (0, 11, -1, 100, "2", 2.0, True, None):
        fake = FakeBitable(_default_tables())
        res = _execute(fake, bad)
        assert res["code"] == "ROUND_OUT_OF_RANGE", f"round={bad!r}"
        assert res["round"] == bad
        assert res["max_round"] == 10
        assert fake.calls == [], f"越界 {bad!r} 不得有任何表操作（含只读搜索）"
        assert _find_act(fake, ACT)["fields"].get("一对一状态") == "收集中"


def test_execute_accepts_round_one_and_max(configured):
    """边界内轮次照常执行：1 与 10 都能写入本轮结果（防收口误伤合法边界）。"""
    for rnd in (1, 10):
        fake = FakeBitable(_default_tables())
        res = _execute(fake, rnd)
        assert res["code"] == "OK" and res["round"] == rnd
        assert len(_round_rows(fake, rnd)) == 8


def test_fetch_result_latest_and_specific_round(configured):
    rows = [
        _row("rec_1_f1", "ou_m1", "ou_f1", 1, "1"),
        _row("rec_1_f2", "ou_m1", "ou_f2", 2, "1"),
        _row("rec_2_f1", "ou_m1", "ou_f1", 1, "2"),
    ]
    fake = FakeBitable({**_default_tables(), ONETOONE_RESULT_TABLE: rows})

    latest = lib_onetoone.fetch_result(ACT, None, client=fake, activity=_find_act(fake, ACT))
    assert latest["round"] == 2 and latest["rounds"] == [1, 2]
    assert latest["result_count"] == 1 and latest["matched_count"] == 1
    assert [p["open_id"] for p in latest["people"]] == ["ou_m1"]
    assert [t["open_id"] for t in latest["people"][0]["partners"]] == ["ou_f1"]

    first = lib_onetoone.fetch_result(ACT, 1, client=fake, activity=_find_act(fake, ACT))
    assert first["round"] == 1 and first["result_count"] == 2
    assert [t["open_id"] for t in first["people"][0]["partners"]] == ["ou_f1", "ou_f2"]

    miss = lib_onetoone.fetch_result(ACT, 5, client=fake, activity=_find_act(fake, ACT))
    assert miss["round"] == 5 and miss["people"] == []


def test_start_collection_keep_history_when_clear_results_false(configured):
    """开启新一轮：只写状态，历史轮次结果不读不删（连 search 都不发）。"""
    fake = FakeBitable({**_default_tables(),
                        ONETOONE_RESULT_TABLE: [
                            _row("rec_r1", "ou_m1", "ou_f1", 1, "1"),
                            _row("rec_r2", "ou_m1", "ou_f1", 1, "2")]})

    res = lib_onetoone.start_collection(ACT, client=fake, activity=_find_act(fake, ACT),
                                        clear_results=False)
    assert res["code"] == "OK" and res["cleared_results"] == 0
    assert len(fake.tables[ONETOONE_RESULT_TABLE]) == 2, "历史轮次结果保留"
    writes = fake.writes()
    assert [c[0] for c in writes] == ["update"], "只写状态，不删结果"
    assert writes[0][3] == {"一对一状态": "收集中"}
    assert not [c for c in fake.calls if c[0] == "search" and c[1] == ONETOONE_RESULT_TABLE], \
        "开启新一轮不该读结果表（保留历史，零搜索失败面）"


def test_start_collection_clears_all_rounds_by_default(configured):
    """默认（clear_results=True）：清空全部轮次历史——破坏性语义与迁移前一致。"""
    fake = FakeBitable({**_default_tables(),
                        ONETOONE_RESULT_TABLE: [
                            _row("rec_r1", "ou_m1", "ou_f1", 1, "1"),
                            _row("rec_r2", "ou_m1", "ou_f1", 1, "2")]})

    res = lib_onetoone.start_collection(ACT, client=fake, activity=_find_act(fake, ACT))
    assert res["code"] == "OK" and res["cleared_results"] == 2
    assert fake.tables[ONETOONE_RESULT_TABLE] == []
    assert _find_act(fake, ACT)["fields"]["一对一状态"] == "收集中"


def test_preview_reports_round_state_and_preserve_counts(configured):
    fake = FakeBitable({**_default_tables(),
                        ONETOONE_RESULT_TABLE: [
                            _row("rec_r1", "ou_m1", "ou_f1", 1, "1"),
                            _row("rec_r2", "ou_m1", "ou_f1", 1, "2")]})

    pv = lib_onetoone.preview(ACT, 3, client=fake, activity=_find_act(fake, ACT),
                              find_user_by_openid=_find_user, female_chats=2)
    assert pv["can_execute"] is True
    assert pv["round"] == 3
    assert pv["existing_rounds"] == [1, 2]
    assert pv["existing_round_results"] == 0
    assert pv["historical_results_to_clear"] == 2
    assert pv["rounds_preserved"] == [1, 2] and pv["results_preserved"] == 2
    assert pv["male_count"] == 2 and pv["female_count"] == 2

    pv2 = lib_onetoone.preview(ACT, 1, client=fake, activity=_find_act(fake, ACT),
                               find_user_by_openid=_find_user, female_chats=2)
    assert pv2["can_execute"] is False
    assert any("第1轮已生成" in b for b in pv2["blockers"])
    assert pv2["existing_round_results"] == 1

    pv3 = lib_onetoone.preview(ACT, 11, client=fake, activity=_find_act(fake, ACT),
                               find_user_by_openid=_find_user, female_chats=2)
    assert any("上限" in b for b in pv3["blockers"])


def test_round_of_normalizes_empty_and_legacy_values(configured):
    assert lib_onetoone.round_of({}) == 1
    assert lib_onetoone.round_of({"轮次": ""}) == 1
    assert lib_onetoone.round_of({"轮次": "abc"}) == 1
    assert lib_onetoone.round_of({"轮次": "1"}) == 1
    assert lib_onetoone.round_of({"轮次": "10"}) == 10
