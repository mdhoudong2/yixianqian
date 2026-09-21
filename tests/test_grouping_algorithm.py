"""run_grouping_algorithm 卫星滚动分组算法单测（不联网）。

覆盖：参数校验、精确整除、余数均摊无丢失无重复、
人数不足/空/单性别、互选驱动同组、大规模确定性。
"""
import sys

sys.path.insert(0, "bot")

from grouping import _mutual_affinity_score, run_grouping_algorithm  # noqa: E402


def _ids(n, prefix, gender):
    return [{"id": f"{prefix}{i}", "gender": gender} for i in range(n)]


def _all_ids(groups):
    return [x for g in groups for x in g["male_ids"] + g["female_ids"]]


def test_affinity_weights():
    # 单向 1 志愿 100*0.7=70；双向 140；红娘 5 星单向 100*0.3=30
    assert _mutual_affinity_score("a", "b", {"a": [{"id": "b", "priority": 1}]}) == 70
    s = _mutual_affinity_score(
        "a", "b",
        {"a": [{"id": "b", "priority": 1}], "b": [{"id": "a", "priority": 1}]},
    )
    assert s == 140
    s = _mutual_affinity_score(
        "a", "b", {}, [{"person1_id": "a", "person2_id": "b", "stars": 5}]
    )
    assert s == 30


def test_invalid_params_raise():
    import pytest

    with pytest.raises(ValueError):
        run_grouping_algorithm([], {}, 0, 3)
    with pytest.raises(ValueError):
        run_grouping_algorithm([], {}, 3, 0)


def test_exact_groups():
    ps = _ids(4, "m", "male") + _ids(4, "f", "female")
    groups = run_grouping_algorithm(ps, {}, 2, 2)
    assert len(groups) == 2
    assert all(len(g["male_ids"]) == 2 and len(g["female_ids"]) == 2 for g in groups)


def test_remainder_distributed_without_loss():
    ps = _ids(5, "m", "male") + _ids(5, "f", "female")
    groups = run_grouping_algorithm(ps, {}, 2, 2)
    allids = _all_ids(groups)
    assert len(allids) == 10 and len(set(allids)) == 10


def test_insufficient_returns_empty():
    assert run_grouping_algorithm(
        [{"id": "m", "gender": "male"}, {"id": "f", "gender": "female"}], {}, 2, 2
    ) == []
    assert run_grouping_algorithm([], {}, 2, 2) == []
    assert run_grouping_algorithm(_ids(2, "m", "male"), {}, 1, 1) == []


def test_mutual_top_pick_grouped_together():
    ps = [{"id": "m0", "gender": "male"}, {"id": "m1", "gender": "male"},
          {"id": "f0", "gender": "female"}, {"id": "f1", "gender": "female"}]
    sel = {"m0": [{"id": "f0", "priority": 1}], "f0": [{"id": "m0", "priority": 1}]}
    groups = run_grouping_algorithm(ps, sel, 1, 1)
    g0 = [g for g in groups if "m0" in g["male_ids"]][0]
    assert "f0" in g0["female_ids"]


def test_large_scale_deterministic():
    ps = _ids(60, "m", "male") + _ids(60, "f", "female")
    g1 = run_grouping_algorithm(ps, {}, 3, 3)
    g2 = run_grouping_algorithm(ps, {}, 3, 3)
    assert len(g1) == 20
    assert g1 == g2
    allids = _all_ids(g1)
    assert len(allids) == 120 and len(set(allids)) == 120
