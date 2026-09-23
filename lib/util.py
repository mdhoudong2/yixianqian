"""轻量工具：统一的日志输出（bot 与 H5 后端共用）。"""
import hashlib
import random
import time


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def seeded_rand(seed_str):
    """由字符串种子生成确定性随机源（同一种子同一序列）。"""
    h = int(hashlib.md5(seed_str.encode("utf-8")).hexdigest(), 16)
    return random.Random(h & 0xFFFFFFFFFFFFFFFF)


def order_cards_seeded(cards, liked_me_openids, seed_str, pinned_openids=None):
    """牵线卡片确定性排序：推荐位置顶，其余整副牌随机打乱，喜欢我的人随机散落前 30%。

    与 app.order_cards 规则一致，但用确定性随机源，保证同一 (用户+快照+筛选) 下
    翻页切片不重不漏。返回排序后的新列表（不修改入参）。

    `pinned_openids` 是机器人生成的推荐位名单（lib/recommend.py），按给定顺序
    原样置顶——顺序本身就是产品语义（暗恋你的人排最前），不能跟着 seed 乱动。
    这些人是`liked_me_openids`的子集，从散落逻辑里摘出去，免得同一张卡出现两次。
    """
    rng = seeded_rand(seed_str)
    if not cards:
        return []
    head, head_oids = [], set()
    if pinned_openids:
        by_oid = {}
        for c in cards:
            by_oid.setdefault(c.get("openid"), c)
        for oid in pinned_openids:
            card = by_oid.get(oid)
            if card is not None and oid not in head_oids:
                head.append(card)
                head_oids.add(oid)
    cards = [c for c in cards if c.get("openid") not in head_oids]
    if not cards:
        return head
    if not liked_me_openids:
        out = list(cards)
        rng.shuffle(out)
        return head + out
    liked = [c for c in cards if c.get("openid") in liked_me_openids]
    others = [c for c in cards if c.get("openid") not in liked_me_openids]
    rng.shuffle(liked)
    rng.shuffle(others)
    n = len(cards)
    front_n = max(1, int(n * 0.3))
    take_liked = min(len(liked), front_n)
    positions = set(rng.sample(range(front_n), take_liked))
    front = []
    li = oi = 0
    for i in range(front_n):
        if i in positions and li < len(liked):
            front.append(liked[li])
            li += 1
        elif oi < len(others):
            front.append(others[oi])
            oi += 1
        elif li < len(liked):
            front.append(liked[li])
            li += 1
    rest = liked[li:] + others[oi:]
    rng.shuffle(rest)
    return head + front + rest
