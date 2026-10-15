"""run_offline_onetoone_matching 线下均衡分配单测（不联网）。

覆盖：整除配额、25男/15女不整除、非整除 ±1 配额、双向奔赴偏好优先、
「每个女生恰好 K 场 / 每个男生 base 或 base+1 场」的均衡保证、非法参数、单性别空结果、
同输入确定性。

空 profiles 时每对 match_score 都是同一个中性分，不改变排序——优先级项（0.4×双方优先级）
主导，故偏好类用例只需构造 selections，无需造真实资料。
"""
import sys

import pytest

sys.path.insert(0, "bot")

from lib.onetoone import run_offline_onetoone_matching  # noqa: E402


def _ids(n, prefix, gender):
    return [{"id": f"{prefix}{i}", "gender": gender} for i in range(n)]


def _sel(selector, targets_priorities):
    """targets_priorities: [(target_id, priority), ...]"""
    return {selector: [{"id": t, "priority": p} for t, p in targets_priorities]}


def _male_targets(result, male_oid):
    return [fid for fid, _, _ in result["male_lists"][male_oid]]


def _female_targets(result, female_oid):
    return [mid for mid, _ in result["female_lists"][female_oid]]


def _assert_edges_consistent(result):
    """male_lists 与 female_lists 描述同一条边的两个方向，必须一一对应。"""
    male_edges = {(m, f) for m, lst in result["male_lists"].items()
                  for f, _, _ in lst}
    female_edges = {(m, f) for f, lst in result["female_lists"].items()
                    for m, _ in lst}
    assert male_edges == female_edges


def test_equal_division():
    # 2 男 2 女，K=2 → E=4，base=2，每个男恰好 2 场、每个女恰好 2 场
    ps = _ids(2, "m", "male") + _ids(2, "f", "female")
    result = run_offline_onetoone_matching(ps, {}, {}, female_chats=2)

    assert result["male_quota"] == {"m0": 2, "m1": 2}
    assert all(len(lst) == 2 for lst in result["male_lists"].values())
    assert all(len(lst) == 2 for lst in result["female_lists"].values())
    _assert_edges_consistent(result)


def test_unbalanced_25_15():
    # 25 男 15 女，K=15 → E=225，base=9，每个男恰好 9 场、每个女恰好 15 场
    ps = _ids(25, "m", "male") + _ids(15, "f", "female")
    result = run_offline_onetoone_matching(ps, {}, {}, female_chats=15)

    assert sum(len(lst) for lst in result["male_lists"].values()) == 225
    assert all(len(lst) == 9 for lst in result["male_lists"].values())
    assert all(len(lst) == 15 for lst in result["female_lists"].values())
    assert all(q == 9 for q in result["male_quota"].values())
    _assert_edges_consistent(result)


def test_non_divisible_quota():
    # 3 男 2 女，K=2 → E=4，base=1，rem=1 → 配额 [2,1,1]，每个女恰好 2 场
    ps = _ids(3, "m", "male") + _ids(2, "f", "female")
    result = run_offline_onetoone_matching(ps, {}, {}, female_chats=2)

    quotas = sorted(result["male_quota"].values())
    assert quotas == [1, 1, 2]
    assert sum(len(lst) for lst in result["male_lists"].values()) == 4
    assert all(len(lst) == 2 for lst in result["female_lists"].values())
    _assert_edges_consistent(result)


def test_preference_priority_mutual_pairing():
    # 2 男 2 女，K=1 → 完美匹配。m0↔f0、m1↔f1 互相第一志愿，其余不选。
    # 双向奔赴对称分：互相选的边 0.4×(100+100)=80，互不选的边 0.4×(30+30)=24。
    ps = _ids(2, "m", "male") + _ids(2, "f", "female")
    sel = _sel("m0", [("f0", 1)])
    sel.update(_sel("f0", [("m0", 1)]))
    sel.update(_sel("m1", [("f1", 1)]))
    sel.update(_sel("f1", [("m1", 1)]))
    result = run_offline_onetoone_matching(ps, sel, {}, female_chats=1)

    assert _male_targets(result, "m0") == ["f0"]
    assert _male_targets(result, "m1") == ["f1"]
    assert _female_targets(result, "f0") == ["m0"]
    assert _female_targets(result, "f1") == ["m1"]
    _assert_edges_consistent(result)


def test_female_gets_top_k_visitors_by_score():
    # 3 男 1 女，K=2。女选 m0(1)、m1(2)；m0/m1/m2 都第 1 志愿选她。
    # 女↔m0=0.4×(100+100)，女↔m1=0.4×(90+100)，女↔m2=0.4×(30+100)。
    # 最高分两条是 m0、m1 → 女生来访卡应是 m0、m1（且 m0 排 m1 前）。
    ps = _ids(3, "m", "male") + [{"id": "f0", "gender": "female"}]
    sel = _sel("f0", [("m0", 1), ("m1", 2)])
    for m in ("m0", "m1", "m2"):
        sel.update(_sel(m, [("f0", 1)]))
    result = run_offline_onetoone_matching(ps, sel, {}, female_chats=2)

    visitors = _female_targets(result, "f0")
    assert visitors == ["m0", "m1"]
    # 配额 [1,1,0]：m2 无配额，冷板凳
    assert result["male_quota"]["m2"] == 0
    assert result["male_lists"]["m2"] == []


def test_every_female_has_at_least_one_visitor():
    # 男少女多极端：2 男 5 女，K=1 → E=5，base=2，rem=1 → 配额 [3,2]。
    # 每个女恰好 1 个访客 → 无人坐冷板凳。
    ps = _ids(2, "m", "male") + _ids(5, "f", "female")
    result = run_offline_onetoone_matching(ps, {}, {}, female_chats=1)

    assert all(len(lst) == 1 for lst in result["female_lists"].values())
    assert sorted(result["male_quota"].values()) == [2, 3]
    _assert_edges_consistent(result)


def test_deterministic():
    ps = _ids(5, "m", "male") + _ids(3, "f", "female")
    sel = _sel("m0", [("f0", 1), ("f1", 2)])
    a = run_offline_onetoone_matching(ps, sel, {}, female_chats=2)
    b = run_offline_onetoone_matching(ps, sel, {}, female_chats=2)
    assert a == b


def test_invalid_female_chats_raises():
    ps = _ids(3, "m", "male") + _ids(2, "f", "female")
    with pytest.raises(ValueError):
        run_offline_onetoone_matching(ps, {}, {}, female_chats=0)
    with pytest.raises(ValueError):
        run_offline_onetoone_matching(ps, {}, {}, female_chats=-1)


def test_female_chats_exceeds_male_count_raises():
    # 2 男 3 女，K=3 > M=2：每个女生最多和 2 位男生各聊一次，无法满足 3 场
    ps = _ids(2, "m", "male") + _ids(3, "f", "female")
    with pytest.raises(ValueError):
        run_offline_onetoone_matching(ps, {}, {}, female_chats=3)


def test_single_gender_returns_empty():
    result = run_offline_onetoone_matching(_ids(4, "m", "male"), {}, {}, female_chats=2)
    assert result == {"male_lists": {}, "female_lists": {}, "male_quota": {}}
