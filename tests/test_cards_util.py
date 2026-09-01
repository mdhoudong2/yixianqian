"""卡片确定性排序与分页切片单测。"""
from lib.util import order_cards_seeded, seeded_rand


def _cards(n):
    return [{"openid": f"ou_{i:03d}"} for i in range(n)]


def test_seeded_deterministic():
    cards = _cards(50)
    a = order_cards_seeded(cards, {"ou_005"}, "seed1")
    b = order_cards_seeded(cards, {"ou_005"}, "seed1")
    assert a == b


def test_seeded_diff_seed_differs():
    cards = _cards(50)
    a = order_cards_seeded(cards, set(), "seed1")
    b = order_cards_seeded(cards, set(), "seed2")
    assert a != b


def test_liked_in_front_zone():
    cards = _cards(100)
    liked = {f"ou_{i:03d}" for i in range(10, 20)}
    ordered = order_cards_seeded(cards, liked, "s")
    front = ordered[:30]
    # 喜欢我的人应散落前30%区
    assert any(c["openid"] in liked for c in front)


def test_pagination_no_dup_no_miss():
    cards = _cards(200)
    ordered = order_cards_seeded(cards, set(), "page")
    pages = []
    for off in range(0, 200, 50):
        pages += ordered[off:off + 50]
    assert len(pages) == 200
    assert len({c["openid"] for c in pages}) == 200


def test_seeded_rand_reproducible():
    r1 = seeded_rand("x")
    r2 = seeded_rand("x")
    assert [r1.random() for _ in range(10)] == [r2.random() for _ in range(10)]
