"""牵线页「推荐」位的选人规则（纯逻辑，与飞书/IO 无关，便于单测）。

## 要解决的两个互相拉扯的目标

1. **暗恋我的人必须出现在推荐位里**（「必显」）——这是这个功能存在的理由。
2. **不能让人从推荐位反推出「这些人喜欢我」**——一旦推荐位全是暗恋自己的人，
   用户看几次就明白了，匿名性当场失效。

所以规则是：推荐位随必显人数扩展，但**永远留至少 RECOMMEND_MIN_ALGO 个算法位**。
必显 12 个人时推荐位有 12 个，其中 9 个必显、3 个算法——外人分不出哪 3 个是陪跑的。

## 每周轮换

算法位每周换掉一批，但保留 RECOMMEND_KEEP 个不动。保留数先让必显占：
必显已经有 4 个以上时，算法位一个都不留，整批换新。

必显位本身**不参与轮换**——它是事实（谁喜欢我），不是选出来的。
「匿名喜欢」满 3 个月作废后自然掉出必显，位置由算法位补上。
"""
from lib import quota

# 推荐位总数下限。必显更多时推荐位跟着变长（见 build_slots）。
RECOMMEND_MIN_TOTAL = 7
# 算法位下限。永远留这么多，否则推荐位可能全是必显、匿名性被反推。
RECOMMEND_MIN_ALGO = 3
# 每周保留不动的名额（先被必显占）。
RECOMMEND_KEEP = 4


def build_slots(pinned_count):
    """算出 (推荐位总数, 必显位, 算法位)。

    必显比 7 个多时推荐位跟着变长，但算法位只有被压到下限这一种可能，
    不会消失——下限就是匿名性的保护垫。
    """
    pinned_count = max(0, int(pinned_count))
    total = max(RECOMMEND_MIN_TOTAL, pinned_count)
    algo = max(RECOMMEND_MIN_ALGO, total - pinned_count)
    algo = min(algo, total)  # 站很小、候选池空时算法位不该超过总数
    return total, total - algo, algo


def next_algo(prev_algo, candidates, pinned, new_week):
    """本轮的算法位名单。

    - `prev_algo`  上轮算法位（按分数降序）
    - `candidates` 本轮算法候选（按分数降序，已排除必显对象）
    - `pinned`     本轮必显（按最近优先）
    - `new_week`   是否到了轮换的周一

    非轮换周只做「剔除失效 + 补齐空位」：某人临时脱单或已互相喜欢时，留着他是错的；
    但**不因为这种个别变动就把整批换掉**——推荐位天天变的话，用户根本看不出
    这是个「每周一批」的东西，也就不会觉得被认真推荐过。
    """
    _, pinned_slots, algo_slots = build_slots(len(pinned))
    pinned_take = list(pinned[:pinned_slots])
    for_slots = max(0, RECOMMEND_KEEP - len(pinned_take))  # 本周保留几个算法位

    cand_set = set(candidates)
    pinned_set = set(pinned_take)
    valid_prev = [o for o in prev_algo if o in cand_set and o not in pinned_set]

    kept = valid_prev[:for_slots] if new_week else valid_prev[:algo_slots]
    picked = list(kept)
    # seen 必须连必显一起算：候选池里当然有必显那些人，漏掉就会把他们
    # 又塞进算法位，推荐位里同一个人出现两次。
    seen = set(picked) | pinned_set

    # 轮换周先捞圈子外的生面孔。候选池是按分数降序的**全量**合格异性，
    # 里面当然有上轮那批人——直接顺着填，分数高的旧人立刻把自己填回来，
    # 「换掉 3 个」就成了空话。生面孔不够时（小站）才回头用旧人补位，
    # 宁可旧面孔重复也不让推荐位空着。
    fill = [o for o in candidates if o not in set(prev_algo)] if new_week else []
    for o in fill + list(candidates):
        if len(picked) >= algo_slots:
            break
        if o in seen:
            continue
        picked.append(o)
        seen.add(o)
    return picked[:algo_slots]


def build_list(pinned, algo):
    """推荐位的最终顺序：必显在前，算法在后。

    顺序本身就是产品语义（暗恋的人排在最前），且固定不变——不打乱，
    否则「每周保留 4 个」在用户看来仍然是位置全变。

    这里和 next_algo 一样按 pinned_slots 截断：暗恋你的人多于名额时，
    只有最近的那几个上推荐位（喜欢关系本身不受影响，配对照常）。
    """
    _, pinned_slots, _ = build_slots(len(pinned))
    head = list(pinned[:pinned_slots])
    return head + [o for o in algo if o not in set(head)]


def week_key(dt=None):
    """本周的标识，形如 "2026-W38"（ISO 周，周一是第一天）。

    用 ISO 周而不是「距某天的天数 / 7」：跨年那周 ISO 编号是连续的，
    自己算容易在 12 月底跳回 W01 时触发一次假轮换。
    """
    return quota.now().strftime("%G-W%V") if dt is None else dt.strftime("%G-W%V")


# ==================== 相似度打分（100 分制） ====================
# 「相似原理」：只比「像不像」，不做互补性——互补是红娘的活，不是算法的。
# 每一项的权重就是它的满分，加起来正好 100。
W_HOBBIES = 25   # 爱好 Jaccard
W_SPORTS = 15    # 运动 Jaccard
W_TRAITS = 15    # 性格 Jaccard
W_EDUCATION = 15 # 学历档位
W_MBTI = 15      # MBTI 相同字母数
W_CITY = 10      # 同城
W_CHURCH = 5     # 同教堂
# 一侧没填该字段时的「中性分」比例。给 0 太狠（资料填得少的人直接出局），
# 给满分又变成「空白也是优点」，取一半：不奖励空白，也不惩罚。
NEUTRAL_RATIO = 0.5

_EDU_ORDER = {"高中及以下": 1, "大专": 2, "本科": 3, "硕士": 4, "博士": 5}
# 学历差一档给几分（满分 15）。差得越多越不相干，但不断崖。
_EDU_BY_DIFF = {0: 15, 1: 11, 2: 6}


# 会被 _as_set 转换的多选字段。prepare_profile 就是把这几个键先转好。
_MULTI_KEYS = ("hobbies", "sports", "traits", "mbti")


def _as_set(v):
    """多选字段可能是逗号串也可能已经是 list，统一成 frozenset。"""
    if isinstance(v, frozenset):
        return v  # 快路径：prepare_profile 转过的直接复用
    if isinstance(v, str):
        return frozenset(x.strip() for x in v.replace("，", ",").replace("、", ",").split(",") if x.strip())
    if isinstance(v, (list, tuple, set)):
        return frozenset(str(x).strip() for x in v if str(x).strip())
    return frozenset()


def prepare_profile(p):
    """把资料里的多选字段预转成 frozenset。

    推荐是全量两两打分（O(N²)），每次比较都重新解析一遍「看书,爬山」这种
    字符串的话，几百人就要多花好几秒。转一次、反复用。
    """
    out = dict(p)
    for k in _MULTI_KEYS:
        if k in out:
            out[k] = _as_set(out[k])
    return out


def _jaccard(a, b):
    """交集/并集。任一为空返回 None（= 没法比，走中性分）。"""
    a, b = _as_set(a), _as_set(b)
    if not a or not b:
        return None
    return len(a & b) / len(a | b)


def _text(v):
    return (v or "").strip() if isinstance(v, str) else ""


def _scale(ratio, weight):
    return int(round(ratio * weight))


def match_score(a, b):
    """两个人的相似度，返回 (分数, 理由列表)。

    分数 0..100。理由只是日志/管理员看的，**不上前端**——推荐角标只有
    「推荐」两个字，写出理由等于告诉用户「我们按爱好推的」，反而让人
    以为没被推的人是不合适，而真正的必显理由（有人暗恋你）绝不能露。
    """
    score = 0
    reasons = []

    for key, label, weight in (("hobbies", "爱好", W_HOBBIES),
                               ("sports", "运动", W_SPORTS),
                               ("traits", "性格", W_TRAITS)):
        r = _jaccard(a.get(key), b.get(key))
        if r is None:
            score += _scale(NEUTRAL_RATIO, weight)
            continue
        score += _scale(r, weight)
        if r > 0:
            common = _as_set(a.get(key)) & _as_set(b.get(key))
            reasons.append(f"共同{label}：{'、'.join(sorted(common)[:3])}")

    edu_a = _EDU_ORDER.get(_text(a.get("education")), 0)
    edu_b = _EDU_ORDER.get(_text(b.get("education")), 0)
    if not edu_a or not edu_b:
        score += _scale(NEUTRAL_RATIO, W_EDUCATION)
    else:
        diff = abs(edu_a - edu_b)
        score += _EDU_BY_DIFF.get(diff, 2)
        if diff == 0:
            reasons.append("学历相当")

    # MBTI 是 4 组字母各选一个，比「相同的字母有几个」；填了 E 又填 I 的人
    # 也照样能算，只是分母按 4 算，最多拿满 15 分。
    mbti_a, mbti_b = _as_set(a.get("mbti")), _as_set(b.get("mbti"))
    if not mbti_a or not mbti_b:
        score += _scale(NEUTRAL_RATIO, W_MBTI)
    else:
        common = mbti_a & mbti_b
        score += _scale(min(len(common), 4) / 4, W_MBTI)
        if common:
            reasons.append(f"MBTI 相同{'、'.join(sorted(common))}")

    for key, label, weight in (("city", "同城", W_CITY), ("church", "同教堂", W_CHURCH)):
        va, vb = _text(a.get(key)), _text(b.get(key))
        if not va or not vb:
            score += _scale(NEUTRAL_RATIO, weight)
        elif va == vb:
            score += weight
            reasons.append(label)

    return min(score, 100), reasons
