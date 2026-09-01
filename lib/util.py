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


def order_cards_seeded(cards, liked_me_openids, seed_str):
    """牵线卡片确定性排序：整副牌随机打乱；喜欢我的人随机散落前 30% 区域。

    与 app.order_cards 规则一致，但用确定性随机源，保证同一 (用户+快照+筛选) 下
    翻页切片不重不漏。返回排序后的新列表（不修改入参）。
    """
    rng = seeded_rand(seed_str)
    if not cards:
        return []
    if not liked_me_openids:
        out = list(cards)
        rng.shuffle(out)
        return out
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
    return front + rest
