"""validate_suggestion 意见反馈校验单测（不联网）。

覆盖：正常提交、类型缺失/非法、内容为空/纯空白、超长上限边界、
首尾空白裁剪、None/非字符串字段防御。
"""
import os
import sys

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend"
)
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

from app import SUGGESTION_MAX_LEN, validate_suggestion


def test_valid_suggestion():
    err, sg_type, content = validate_suggestion({"type": "功能建议", "content": "希望增加筛选"})
    assert err is None
    assert sg_type == "功能建议"
    assert content == "希望增加筛选"


def test_valid_all_types():
    for t in ("功能建议", "问题反馈", "其他"):
        err, sg_type, _ = validate_suggestion({"type": t, "content": "x"})
        assert err is None
        assert sg_type == t


def test_missing_or_invalid_type_rejected():
    assert validate_suggestion({})[0] == "请选择反馈类型"
    assert validate_suggestion({"content": "x"})[0] == "请选择反馈类型"
    assert validate_suggestion({"type": "", "content": "x"})[0] == "请选择反馈类型"
    assert validate_suggestion({"type": "乱写的", "content": "x"})[0] == "请选择反馈类型"


def test_empty_content_rejected():
    assert validate_suggestion({"type": "其他"})[0] == "请填写反馈内容"
    assert validate_suggestion({"type": "其他", "content": ""})[0] == "请填写反馈内容"
    assert validate_suggestion({"type": "其他", "content": "   \n  "})[0] == "请填写反馈内容"


def test_content_too_long_rejected():
    long_text = "好" * (SUGGESTION_MAX_LEN + 1)
    assert validate_suggestion({"type": "其他", "content": long_text})[0] == f"反馈内容最多 {SUGGESTION_MAX_LEN} 字"


def test_content_at_limit_accepted():
    text = "好" * SUGGESTION_MAX_LEN
    err, _, content = validate_suggestion({"type": "问题反馈", "content": text})
    assert err is None
    assert content == text


def test_whitespace_trimmed():
    err, _, content = validate_suggestion({"type": "其他", "content": "  卡片加载慢  "})
    assert err is None
    assert content == "卡片加载慢"


def test_non_string_fields_defensive():
    # 非字符串不抛异常，走"请选择/请填写"分支
    assert validate_suggestion({"type": 123, "content": ["x"]})[0] == "请选择反馈类型"
    assert validate_suggestion({"type": "其他", "content": {"a": 1}})[0] == "请填写反馈内容"
