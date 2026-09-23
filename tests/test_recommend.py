"""推荐位选人规则。

规则本身不难，但「必显多人时会不会把算法位挤没」这件事没有报错、只有后果：
匿名性靠那 3 个陪跑位撑着，挤没了用户就能反推出谁暗恋自己。所以边界要钉死。
"""
import datetime

from lib.recommend import (
    RECOMMEND_KEEP,
    RECOMMEND_MIN_ALGO,
    RECOMMEND_MIN_TOTAL,
    build_list,
    build_slots,
    match_score,
    next_algo,
    week_key,
)


def _cands(prefix, n):
    return [f"{prefix}{i}" for i in range(n)]


def _profile(**kw):
    """建一份资料。没显式给的字段留空，走中性分。"""
    base = {"hobbies": [], "sports": [], "traits": [],
            "education": "", "mbti": [], "city": "", "church": ""}
    base.update(kw)
    return base


# ==================== 名额分配 ====================

def test_no_pinned_gives_seven_with_three_plus_algo():
    total, pinned, algo = build_slots(0)
    assert (total, pinned, algo) == (7, 0, 7)


def test_slots_for_each_pinned_count():
    """计划里那张表的四种取值，逐一对上。"""
    assert build_slots(0) == (7, 0, 7)
    assert build_slots(3) == (7, 3, 4)
    assert build_slots(4) == (7, 4, 3)
    assert build_slots(12) == (12, 9, 3)


def test_algo_slots_never_drop_below_the_floor():
    """匿名性的底线：无论多少人暗恋你，推荐位里永远混着算法推荐。"""
    for n in range(0, 60):
        assert build_slots(n)[2] >= RECOMMEND_MIN_ALGO
    # 必显远多于 7 时也不塌
    assert build_slots(50)[2] == RECOMMEND_MIN_ALGO


def test_total_never_below_minimum():
    for n in range(0, 20):
        assert build_slots(n)[0] >= RECOMMEND_MIN_TOTAL


# ==================== 每周轮换 ====================

def test_new_week_keeps_four_and_replaces_three():
    """候选池是「全部合格异性」，本来就含着上轮那批人——不是另一批新面孔。"""
    prev = _cands("old", 7)
    fresh = _cands("new", 10)
    cands = prev + fresh
    algo = next_algo(prev, cands, pinned=[], new_week=True)
    assert len(algo) == 7
    assert algo[:RECOMMEND_KEEP] == prev[:RECOMMEND_KEEP]      # 保留 4 个
    assert algo[RECOMMEND_KEEP:] == fresh[:3]                  # 换掉 3 个
    assert not set(algo[RECOMMEND_KEEP:]) & set(prev)


def test_same_week_does_not_churn():
    """非轮换周：名单原样保留，看不出任何变化。"""
    prev = _cands("old", 7)
    cands = prev + _cands("new", 10)
    assert next_algo(prev, cands, pinned=[], new_week=False) == prev


def test_same_week_backfills_only_when_someone_is_gone():
    """个别失效（脱单/已互相喜欢）要剔除并补位，但不能整批换。"""
    prev = ["old0", "old1", "old2", "old3", "old4"]
    cands = ["old0", "old3", "old4", "new0", "new1"]  # old1/old2 已不可选
    algo = next_algo(prev, cands, pinned=[], new_week=False)
    assert algo == ["old0", "old3", "old4", "new0", "new1"]
    # old1/old2 被剔除，但不是「整批换新」——三个老面孔还在
    assert algo[0] == "old0"


def test_pinned_eats_the_keep_quota():
    """必显占满 4 个以上时，算法位一个都不留、整批换新。"""
    pinned = _cands("pin", 4)
    prev = _cands("old", 3)
    cands = _cands("new", 5)
    algo = next_algo(prev, cands, pinned, new_week=True)
    assert algo == cands[:3]
    assert not set(algo) & set(prev)


def test_pinned_is_taken_from_the_front():
    """必显按「最近优先」给，超出名额的从尾部截断。"""
    pinned = _cands("pin", 12)
    algo = next_algo([], _cands("new", 5), pinned, new_week=True)
    assert len(algo) == 3

    _, pinned_slots, _ = build_slots(len(pinned))
    assert pinned_slots == 9


def test_short_candidate_pool_does_not_pad_with_duplicates():
    """候选不够就短一截，绝不重复塞同一个人。"""
    algo = next_algo([], ["a", "b"], pinned=[], new_week=True)
    assert algo == ["a", "b"]
    assert len(set(algo)) == len(algo)


def test_algo_never_contains_a_pinned_person():
    """必显的人同时出现在算法位会导致推荐位里出现两次同一个人。"""
    pinned = ["p1", "p2"]
    cands = ["p1", "p2", "c1", "c2", "c3", "c4", "c5", "c6"]
    algo = next_algo(["p1", "c1"], cands, pinned, new_week=True)
    assert "p1" not in algo
    assert "p2" not in algo


# ==================== 最终顺序 ====================

def test_build_list_puts_pinned_first():
    assert build_list(["p1", "p2"], ["a1", "a2"]) == ["p1", "p2", "a1", "a2"]


def test_build_list_truncates_overflowing_pinned():
    """必显特多时按名额截断，不能把推荐位撑成 15 个。"""
    out = build_list(_cands("pin", 12), ["a1", "a2", "a3"])
    assert len(out) == 12
    assert out[:9] == _cands("pin", 9)


def test_build_list_dedupes_a_pinned_person_from_algo():
    """防守：算法位里若混进必显的人（历史文件、人工改数据），最终名单不能重复。"""
    assert build_list(["p1"], ["p1", "a1"]) == ["p1", "a1"]


def test_full_week_is_seven_and_stable():
    """端到端一轮：7 个位置、必显在前、无重复。"""
    pinned = ["p1", "p2"]
    prev = _cands("old", 5)
    cands = ["p1", "p2"] + _cands("new", 8)
    algo = next_algo(prev, cands, pinned, new_week=True)
    out = build_list(pinned, algo)
    assert len(out) == 7
    assert out[:2] == pinned
    assert len(set(out)) == len(out)


def test_small_pool_reuses_old_faces_rather_than_shrinking():
    """小站：全员就那几个人，轮换周也得把位置填满，不能缩水成 4 个。"""
    prev = _cands("old", 7)
    algo = next_algo(prev, prev, pinned=[], new_week=True)
    assert len(algo) == 7
    assert set(algo) == set(prev)


# ==================== 周标识 ====================

def test_week_key_is_iso_week():
    # 2026-01-01 是周四，ISO 周属于 2025-W53 那一周之后的 2026-W01
    assert week_key(datetime.date(2026, 1, 1)) == "2026-W01"
    # 跨年那周：12-31 与次年 01-01 必须在同一周，否则会多触发一次轮换
    assert week_key(datetime.date(2025, 12, 29)) == week_key(datetime.date(2026, 1, 1))


def test_week_key_changes_on_monday():
    assert week_key(datetime.date(2026, 9, 20)) != week_key(datetime.date(2026, 9, 21))
    # 周一与周日同周，周与周之间才变
    assert week_key(datetime.date(2026, 9, 21)) == week_key(datetime.date(2026, 9, 27))


# ==================== 相似度打分 ====================

def test_identical_full_profiles_score_100():
    p = _profile(hobbies=["看书", "爬山"], sports=["跑步"], traits=["性格沉稳"],
                 education="本科", mbti=["I", "S", "T", "J"], city="上海", church="徐汇堂")
    score, reasons = match_score(p, p)
    assert score == 100
    assert reasons


def test_score_is_symmetric():
    a = _profile(hobbies=["看书"], education="硕士", mbti=["E", "N", "F", "P"], city="北京")
    b = _profile(hobbies=["跑步", "看书"], education="本科", mbti=["E", "N", "T", "J"], city="北京")
    assert match_score(a, b)[0] == match_score(b, a)[0]


def test_more_overlap_scores_higher():
    me = _profile(hobbies=["看书", "爬山", "看电影"], education="本科", city="上海")
    near = _profile(hobbies=["看书", "爬山"], education="本科", city="上海")
    far = _profile(hobbies=["滑雪", "冲浪"], education="高中及以下", city="广州")
    assert match_score(me, near)[0] > match_score(me, far)[0]


def test_blank_profile_gets_neutral_not_zero():
    """资料没填全的人不该被判死刑，但也不该比「真的像」更高。"""
    me = _profile(hobbies=["看书"], education="本科", city="上海")
    blank = _profile()
    twin = _profile(hobbies=["看书"], education="本科", city="上海")
    s_blank = match_score(me, blank)[0]
    assert 0 < s_blank < match_score(me, twin)[0]


def test_list_and_comma_string_are_equivalent():
    """多选字段读回来时是 list，历史数据里可能是逗号串，得分不能因此不同。"""
    a = _profile(hobbies=["看书", "爬山"])
    as_str = _profile(hobbies="看书，爬山")
    b = _profile(hobbies=["看书"])
    assert match_score(a, b)[0] == match_score(as_str, b)[0]


def test_mbti_counts_shared_letters():
    a = _profile(mbti=["E", "N", "F", "J"])
    all_same = _profile(mbti=["E", "N", "F", "J"])
    three = _profile(mbti=["E", "N", "F", "P"])
    none = _profile(mbti=["I", "S", "T", "P"])
    s_all, _ = match_score(a, all_same)
    s_three, _ = match_score(a, three)
    s_none, _ = match_score(a, none)
    assert s_all > s_three > s_none


def test_education_gap_lowers_score():
    a = _profile(education="博士")
    scores = [match_score(a, _profile(education=e))[0]
              for e in ("博士", "硕士", "本科", "大专", "高中及以下")]
    assert scores == sorted(scores, reverse=True)


def test_same_city_and_church_add_up():
    base = _profile(city="上海", church="徐汇堂")
    other = _profile(city="上海", church="徐汇堂")
    diff_city = _profile(city="广州", church="徐汇堂")
    diff_both = _profile(city="广州", church="天河堂")
    assert (match_score(base, other)[0]
            > match_score(base, diff_city)[0]
            > match_score(base, diff_both)[0])


def test_score_stays_within_bounds():
    """任何组合都不能越界——前端拿它排序，越界会让推荐位排得莫名其妙。"""
    import itertools
    moods = [_profile(), _profile(hobbies=["看书"], education="本科", mbti=["E"]),
             _profile(hobbies="滑雪,冲浪", education="博士", mbti=["I", "N"], city="广州"),
             _profile(hobbies=["看书", "爬山"], sports=["跑步"], traits=["积极乐观"],
                      education="硕士", mbti=["E", "N", "F", "J"], city="上海", church="徐汇堂")]
    for a, b in itertools.product(moods, repeat=2):
        s, _ = match_score(a, b)
        assert 0 <= s <= 100
