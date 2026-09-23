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


def test_pinned_goes_first_in_given_order():
    """推荐位原样置顶：顺序本身就是产品语义（暗恋你的人排最前），不能跟 seed 乱动。"""
    cards = _cards(50)
    pinned = ["ou_030", "ou_007", "ou_041"]
    ordered = order_cards_seeded(cards, {"ou_030", "ou_007", "ou_041"}, "s", pinned)
    assert [c["openid"] for c in ordered[:3]] == pinned
    assert len(ordered) == 50
    assert len({c["openid"] for c in ordered}) == 50


def test_pinned_is_not_scattered_twice():
    """置顶的人同时也在 liked_me 里，不能既在前面又散落一次。"""
    for n in (5, 20, 100):
        cards = _cards(n)
        pinned = [f"ou_{i:03d}" for i in range(min(4, n))]
        ordered = order_cards_seeded(cards, set(pinned), "s", pinned)
        assert len(ordered) == n
        assert len({c["openid"] for c in ordered}) == n
        assert [c["openid"] for c in ordered[:len(pinned)]] == pinned


def test_unknown_pinned_ids_are_ignored():
    """推荐名单里的成员可能已被筛选条件排除或已注销，跳过就好，不能丢卡片。"""
    cards = _cards(10)
    ordered = order_cards_seeded(cards, set(), "s", ["ou_999", "ou_003"])
    assert [c["openid"] for c in ordered[0:1]] == ["ou_003"]
    assert len(ordered) == 10


def test_pinned_covers_everything():
    """全站就这几个人时，推荐位就是全部，不能因为 rest 为空就漏掉。"""
    cards = _cards(3)
    pinned = ["ou_002", "ou_000", "ou_001"]
    ordered = order_cards_seeded(cards, set(), "s", pinned)
    assert [c["openid"] for c in ordered] == pinned


def test_seeded_rand_reproducible():
    r1 = seeded_rand("x")
    r2 = seeded_rand("x")
    assert [r1.random() for _ in range(10)] == [r2.random() for _ in range(10)]
