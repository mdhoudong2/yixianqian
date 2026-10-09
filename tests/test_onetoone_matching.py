"""run_onetoone_matching 各自独立 top-N 必聊名单单测（不联网）。

覆盖：7 志愿必然进 top-7 且志愿内按优先级排序、非志愿按匹配度补齐第 8~10、
异性不足按实际人数、0 志愿纯按匹配度、性别缺失跳过。
"""
import sys

sys.path.insert(0, "bot")

from onetoone import run_onetoone_matching  # noqa: E402


def _ids(n, prefix, gender):
    return [{"id": f"{prefix}{i}", "gender": gender} for i in range(n)]


def _profiles(participants):
    return {p["id"]: {} for p in participants}


def _choices(selector, targets, priorities):
    return {selector: [{"id": t, "priority": k} for t, k in zip(targets, priorities)]}


def test_seven_choices_always_top7_in_priority_order():
    # 1 男 + 10 女，男选前 7 个志愿，必进 top-7 且按优先级排序
    ps = [{"id": "m0", "gender": "male"}] + _ids(10, "f", "female")
    sel = _choices("m0", [f"f{i}" for i in range(7)], range(1, 8))
    result = run_onetoone_matching(ps, sel, _profiles(ps), top_n=10)

    got = result["m0"]
    assert len(got) == 10
    # 前 7 名 = 志愿 f0..f6，顺序与优先级一致
    assert [oid for oid, _ in got[:7]] == [f"f{i}" for i in range(7)]
    assert [rank for _, rank in got[:7]] == [1, 2, 3, 4, 5, 6, 7]
    # 第 8~10 名由 f7/f8/f9 补齐，且不重复
    rest = [oid for oid, _ in got[7:]]
    assert sorted(rest) == ["f7", "f8", "f9"]
    assert len(set(rest)) == 3


def test_non_choice_filled_by_match_score():
    # 男不选志愿，但 f7 与男有共同爱好 → f7 匹配度最高，补进第 8 名
    ps = [{"id": "m0", "gender": "male"}] + _ids(10, "f", "female")
    sel = _choices("m0", [f"f{i}" for i in range(7)], range(1, 8))
    profiles = _profiles(ps)
    profiles["m0"] = {"hobbies": {"读书"}}
    profiles["f7"] = {"hobbies": {"读书"}}
    result = run_onetoone_matching(ps, sel, profiles, top_n=10)

    got = result["m0"]
    assert [oid for oid, _ in got[:7]] == [f"f{i}" for i in range(7)]
    # 非志愿里 f7 匹配度最高，应排第 8
    assert got[7][0] == "f7"
    assert got[7][1] == 8


def test_insufficient_returns_actual_count():
    # 只有 3 个异性，志愿全选也最多 3 人；无异性则空名单
    ps = [{"id": "m0", "gender": "male"}] + _ids(3, "f", "female")
    sel = _choices("m0", ["f0", "f1", "f2"], [1, 2, 3])
    result = run_onetoone_matching(ps, sel, _profiles(ps), top_n=10)
    assert len(result["m0"]) == 3

    solo = run_onetoone_matching([{"id": "m0", "gender": "male"}], {}, {}, top_n=10)
    assert solo["m0"] == []


def test_zero_choices_pure_match_score():
    # 0 志愿：纯按匹配度排序，f0 有共同爱好 → 排第一
    ps = [{"id": "m0", "gender": "male"},
          {"id": "f0", "gender": "female"},
          {"id": "f1", "gender": "female"}]
    profiles = _profiles(ps)
    profiles["m0"] = {"hobbies": {"读书"}}
    profiles["f0"] = {"hobbies": {"读书"}}
    result = run_onetoone_matching(ps, {}, profiles, top_n=10)
    assert result["m0"][0][0] == "f0"


def test_gender_missing_skipped():
    # 性别缺失者：自己不给名单，也不进别人的候选
    ps = [{"id": "m0", "gender": "male"},
          {"id": "f0", "gender": "female"},
          {"id": "x0", "gender": ""}]
    result = run_onetoone_matching(ps, {}, _profiles(ps), top_n=10)
    assert result["x0"] == []
    # m0 的候选只有 f0（x0 不在），f0 的候选只有 m0
    assert [oid for oid, _ in result["m0"]] == ["f0"]
    assert [oid for oid, _ in result["f0"]] == ["m0"]


def test_each_person_independent():
    # 各自独立：A 选了 B，不影响 B 的名单（B 的名单按 B 自己的志愿算）
    ps = [{"id": "m0", "gender": "male"},
          {"id": "m1", "gender": "male"},
          {"id": "f0", "gender": "female"},
          {"id": "f1", "gender": "female"}]
    sel = {"m0": [{"id": "f0", "priority": 1}],
           "f0": [{"id": "m1", "priority": 1}]}
    result = run_onetoone_matching(ps, sel, _profiles(ps), top_n=10)
    assert result["m0"][0][0] == "f0"
    assert result["f0"][0][0] == "m1"
    # 互相独立：m0 的第一志愿 f0 并不因此进 m0 之外任何人的「被优先」名单
    assert result["m1"][0][0] in ("f0", "f1")  # m1 无志愿，纯匹配度，二者分数相同
