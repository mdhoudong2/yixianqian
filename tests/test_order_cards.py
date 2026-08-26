"""order_cards 牵线卡片排序单测（不联网，回归：筛选后卡片为空时 500）。"""
import os
import sys

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend"
)
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

from app import order_cards


def test_order_cards_empty_cards_with_liked_me():
    """筛选结果为空且存在「喜欢我」记录时不崩溃（回归 IndexError）"""
    assert order_cards([], {"ou_liked_me"}) == []


def test_order_cards_no_liked_me_shuffles_in_place():
    cards = [{"openid": f"ou_{i}"} for i in range(5)]
    result = order_cards(cards, set())
    assert result is cards
    assert sorted(c["openid"] for c in result) == [f"ou_{i}" for i in range(5)]


def test_order_cards_keeps_all_and_fronts_liked():
    """所有卡片保留、无丢失；「喜欢我的人」散落前 30% 区域"""
    for n in range(1, 40):
        cards = [{"openid": f"ou_{i}"} for i in range(n)]
        liked_me = {f"ou_{i}" for i in range(0, n, 3)}
        result = order_cards([dict(c) for c in cards], liked_me)
        assert len(result) == n
        assert {c["openid"] for c in result} == {c["openid"] for c in cards}
        front_n = max(1, int(n * 0.3))
        expected_front = min(len(liked_me), front_n)
        assert sum(1 for c in result[:front_n] if c["openid"] in liked_me) == expected_front
