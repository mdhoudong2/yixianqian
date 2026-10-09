"""run_onetoone_matching 双向奔赴 top-N 必聊名单单测（不联网）。

覆盖：我的志愿按优先级排序（无人反向选我时）、未选择但选了我的人「惊喜位」升位、
0 志愿纯按对方意愿、异性不足按实际人数、性别缺失跳过、各自独立。
"""
import sys

sys.path.insert(0, "bot")

from onetoone import run_onetoone_matching  # noqa: E402


def _ids(n, prefix, gender):
    return [{"id": f"{prefix}{i}", "gender": gender} for i in range(n)]


def _sel(selector, targets_priorities):
    """targets_priorities: [(target_id, priority), ...]"""
    return {selector: [{"id": t, "priority": p} for t, p in targets_priorities]}


def _ranked(result, oid):
    return [target for target, _ in result[oid]]


def test_my_choices_ranked_by_priority_when_no_mutual():
    # 1 男 + 10 女，男选前 7 个志愿；无人反向选他 → 严格按我的优先级排序
    ps = [{"id": "m0", "gender": "male"}] + _ids(10, "f", "female")
    sel = _sel("m0", [(f"f{i}", i + 1) for i in range(7)])
    result = run_onetoone_matching(ps, sel, top_n=10)

    assert len(result["m0"]) == 10
    # 前 7 = 志愿 f0..f6 按优先级；后 3 = f7/f8/f9（未选择，同分 24，稳定序）
    assert _ranked(result, "m0") == [f"f{i}" for i in range(10)]


def test_unselected_who_chose_me_gets_surprise_slot():
    # 男选 f0..f6；f7（男没选）反向第 1 志愿选男 → f7 升到第 4 名（45 分 > f3 的 44 分）
    ps = [{"id": "m0", "gender": "male"}] + _ids(10, "f", "female")
    sel = _sel("m0", [(f"f{i}", i + 1) for i in range(7)])
    sel["f7"] = [{"id": "m0", "priority": 1}]
    result = run_onetoone_matching(ps, sel, top_n=10)

    got = _ranked(result, "m0")
    assert got[:3] == ["f0", "f1", "f2"]       # 我的前 3 志愿恒在最前
    assert got[3] == "f7"                       # 惊喜位：没选但选了我（第1志愿）
    assert got[4:] == ["f3", "f4", "f5", "f6", "f8", "f9"]


def test_zero_choices_ranked_by_their_will():
    # 0 志愿：纯按「谁选我、把我排第几」排序
    ps = [{"id": "m0", "gender": "male"},
          {"id": "f0", "gender": "female"},
          {"id": "f1", "gender": "female"},
          {"id": "f2", "gender": "female"}]
    sel = {"f0": [{"id": "m0", "priority": 1}],
           "f1": [{"id": "m0", "priority": 3}]}
    result = run_onetoone_matching(ps, sel, top_n=10)
    assert _ranked(result, "m0") == ["f0", "f1", "f2"]


def test_insufficient_returns_actual_count():
    ps = [{"id": "m0", "gender": "male"}] + _ids(3, "f", "female")
    sel = _sel("m0", [("f0", 1), ("f1", 2), ("f2", 3)])
    assert len(run_onetoone_matching(ps, sel, top_n=10)["m0"]) == 3

    solo = run_onetoone_matching([{"id": "m0", "gender": "male"}], {}, top_n=10)
    assert solo["m0"] == []


def test_gender_missing_skipped():
    ps = [{"id": "m0", "gender": "male"},
          {"id": "f0", "gender": "female"},
          {"id": "x0", "gender": ""}]
    result = run_onetoone_matching(ps, {}, top_n=10)
    assert result["x0"] == []
    assert _ranked(result, "m0") == ["f0"]
    assert _ranked(result, "f0") == ["m0"]


def test_each_person_independent():
    # 各自独立：A 选 B、B 选 C，互不影响各自名单
    ps = [{"id": "m0", "gender": "male"},
          {"id": "m1", "gender": "male"},
          {"id": "f0", "gender": "female"},
          {"id": "f1", "gender": "female"}]
    sel = {"m0": [{"id": "f0", "priority": 1}],
           "f0": [{"id": "m1", "priority": 1}]}
    result = run_onetoone_matching(ps, sel, top_n=10)
    assert _ranked(result, "m0")[0] == "f0"   # 我的第一志愿
    assert _ranked(result, "f0")[0] == "m1"   # 她的第一志愿
    # m1 无志愿，但 f0 选了 m1 → m1 名单里 f0 因「对方意愿」排最前
    assert _ranked(result, "m1")[0] == "f0"
