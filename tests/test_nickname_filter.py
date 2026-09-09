"""pass_card_filters 昵称/文本筛选大小写不敏感单测（不联网）。

筛选昵称可能是英文（如 Alice），大小写不应影响匹配；
与前端分组搜索的 toLowerCase 口径对齐。
"""
import os
import sys

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend"
)
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

from app import pass_card_filters


def test_nickname_case_insensitive():
    assert pass_card_filters({"昵称": "Alice"}, {"nickname": "alice"}) is True
    assert pass_card_filters({"昵称": "Alice"}, {"nickname": "ALICE"}) is True
    assert pass_card_filters({"昵称": "alice"}, {"nickname": "Alice"}) is True
    assert pass_card_filters({"昵称": "Alice"}, {"nickname": "AlIcE"}) is True


def test_nickname_substring_and_mismatch():
    assert pass_card_filters({"昵称": "AliceWonder"}, {"nickname": "licew"}) is True
    assert pass_card_filters({"昵称": "Alice"}, {"nickname": "bob"}) is False
    assert pass_card_filters({"昵称": ""}, {"nickname": "alice"}) is False
    assert pass_card_filters({}, {"nickname": "alice"}) is False


def test_nickname_chinese_unaffected():
    assert pass_card_filters({"昵称": "张三"}, {"nickname": "张"}) is True
    assert pass_card_filters({"昵称": "张三"}, {"nickname": "李"}) is False


def test_user_id_case_insensitive():
    assert pass_card_filters({"用户ID": "U-AB12"}, {"user_id": "u-ab12"}) is True
    assert pass_card_filters({"用户ID": "U-AB12"}, {"user_id": "U-ab"}) is True
    assert pass_card_filters({"用户ID": "U-AB12"}, {"user_id": "U-CD"}) is False


def test_empty_filter_passes_all():
    assert pass_card_filters({"昵称": "Alice"}, {}) is True
    assert pass_card_filters({"昵称": "Alice"}, {"nickname": ""}) is True
