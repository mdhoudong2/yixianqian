"""calculate_match_score 数字红娘评分纯函数单测（不联网）。

覆盖：满分路径、无交集、部分交集 Jaccard、中英文逗号等价、
缺爱好/缺学历默认值、学历差分档、100 上限、非法类型防御、未知学历。
"""
import sys

sys.path.insert(0, "bot")

from auto_tasks import calculate_match_score  # noqa: E402


def test_same_hobbies_same_edu():
    score, reasons = calculate_match_score(
        {"hobbies": ["篮球", "音乐"], "学历": "本科"},
        {"hobbies": ["篮球", "音乐"], "学历": "本科"},
    )
    assert score == 75
    assert any("共同兴趣" in r for r in reasons)
    assert "学历相当" in reasons


def test_no_common_hobbies():
    score, _ = calculate_match_score(
        {"hobbies": ["篮球"], "学历": "本科"},
        {"hobbies": ["足球"], "学历": "硕士"},
    )
    assert score == 30


def test_partial_hobbies():
    # 交集 1/并集 3 -> int(1/3*40)=13；学历差 2 -> 8；基础 15
    score, _ = calculate_match_score(
        {"hobbies": ["a", "b"], "学历": "本科"},
        {"hobbies": ["b", "c"], "学历": "高中及以下"},
    )
    assert score == 36


def test_comma_variants_equal():
    s1, _ = calculate_match_score(
        {"hobbies": "篮球，音乐", "学历": "本科"},
        {"hobbies": "篮球,音乐", "学历": "本科"},
    )
    assert s1 == 75


def test_missing_hobbies_and_edu():
    # 无爱好 +10；无学历 +8；基础 +15
    score, _ = calculate_match_score(
        {"hobbies": [], "学历": ""}, {"hobbies": ["x"], "学历": ""}
    )
    assert score == 33


def test_edu_gap_large():
    # 博士 vs 高中及以下差 4 档 -> 10+3+15
    score, _ = calculate_match_score(
        {"hobbies": [], "学历": "博士"}, {"hobbies": [], "学历": "高中及以下"}
    )
    assert score == 28


def test_score_capped_at_100():
    score, _ = calculate_match_score(
        {"hobbies": ["a"], "学历": "本科"},
        {"hobbies": ["a"], "学历": "本科"},
    )
    assert score <= 100


def test_invalid_hobby_types_defensive():
    score, _ = calculate_match_score(
        {"hobbies": None, "学历": "本科"}, {"hobbies": 123, "学历": "本科"}
    )
    assert score == 45


def test_unknown_edu_treated_as_missing():
    score, _ = calculate_match_score(
        {"hobbies": [], "学历": "未知学历"}, {"hobbies": [], "学历": "本科"}
    )
    assert score == 33
