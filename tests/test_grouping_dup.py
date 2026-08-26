"""分组卡片同名消歧纯函数单测（不联网）。"""
from grouping import _dup_nicknames, _option_label


def test_dup_nicknames_true():
    ps = [{"nickname": "佳佳"}, {"nickname": "飞哥"}, {"nickname": "佳佳"}]
    assert _dup_nicknames(ps) is True


def test_dup_nicknames_false():
    ps = [{"nickname": "佳佳"}, {"nickname": "飞哥"}]
    assert _dup_nicknames(ps) is False


def test_dup_nicknames_empty():
    assert _dup_nicknames([]) is False


def test_option_label_with_uid():
    assert _option_label("佳佳", "U-0054", True) == "佳佳（U-0054）"


def test_option_label_without_uid():
    assert _option_label("佳佳", "U-0054", False) == "佳佳"


def test_option_label_missing_uid_keeps_nickname():
    assert _option_label("佳佳", "", True) == "佳佳"
