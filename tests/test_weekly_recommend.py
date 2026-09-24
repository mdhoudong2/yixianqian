"""每周推荐位生成（bot/auto_tasks.generate_weekly_recommendations）。

这个任务的输出直接决定「谁暗恋你」会不会被看出来，所以除了功能本身，
还要钉死两条不变量：
- 必显的人必须出现在名单里、且排在最前（否则这个功能就白做了）
- 名单之外的一切（pinned 字段）绝不能混进给前端的东西里
- 推荐位**不写多维表格**——旧版就是把数字红娘推荐表灌爆的
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BOT_DIR = os.path.join(_ROOT, "bot")
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

import auto_tasks as at

from lib import quota


def _user(rid, nick, oid, gender, **fields):
    f = {
        at.FIELD_NICKNAME: nick,
        at.FIELD_FEISHU_ID: oid,
        at.FIELD_GENDER: gender,
        at.FIELD_ACCOUNT_STATUS: "单身",
    }
    f.update(fields)
    return {"record_id": rid, "fields": f}


def _like(src, dst, status="单向喜欢", like_type="匿名", month=None):
    f = {
        at.FIELD_LIKE_INITIATOR_OPENID: src,
        at.FIELD_LIKE_TARGET_OPENID: dst,
        at.FIELD_LIKE_STATUS: status,
        at.FIELD_LIKE_TYPE: like_type,
    }
    if month is not None:
        f[at.FIELD_LIKE_MONTH] = month
    return {"record_id": f"l_{src}_{dst}", "fields": f}


# 我的爱好是 h0..h9。第 i 位候选人和我的重合度由 _hobbies_for 给出，
# 单调且互不相同——这样候选池的排序完全由下标决定，测试不用猜分数。
_MY_HOBBIES = [f"h{i}" for i in range(10)]


def _hobbies_for(i):
    """1..10 逐步贴合到满分，10 以后加自己的独有爱好逐渐变淡。"""
    if i <= 10:
        return _MY_HOBBIES[:i]
    return _MY_HOBBIES + [f"unique{i}_{j}" for j in range(i - 10)]


def _pool(n, prefix="女", start=1, gender="女性"):
    return [_user(f"r_{prefix}{i}", f"{prefix}{i}", f"ou_{prefix}{i}", gender,
                  **{at.FIELD_SELF_HOBBIES: _hobbies_for(i)})
            for i in range(start, start + n)]


def _me():
    return _user("r_me", "我", "ou_me", "男性",
                 **{at.FIELD_SELF_HOBBIES: _MY_HOBBIES})


def _run(monkeypatch, users, likes, prev=None, week="2026-W38"):
    saved = {}

    def fake_search(table_id, filter_conditions=None):
        if table_id == at.USER_TABLE_ID:
            return users
        if table_id == at.LIKE_TABLE_ID:
            return likes
        raise AssertionError(f"推荐位不该查这张表: {table_id}")

    monkeypatch.setattr(at, "search_records", fake_search)
    # 失效防护改走 raw 客户端（要区分「查询失败」与「空表」），一并 patch：
    monkeypatch.setattr(at.bitable, "search_records", fake_search)
    monkeypatch.setattr(at.storage, "load_json", lambda path, default=None:
                        prev if prev is not None else default)
    monkeypatch.setattr(at.storage, "save_json", lambda path, data: saved.update(data))
    monkeypatch.setattr(at, "create_record",
                        lambda *a, **k: pytest.fail("推荐位不该写多维表格"))
    monkeypatch.setattr(at.recommend, "week_key", lambda dt=None: week)
    monkeypatch.setattr(at, "log", lambda *a, **k: None)

    at.generate_weekly_recommendations()
    return saved or None


def _algo_of(doc, oid="ou_me"):
    return doc["users"][oid]["algo"]


def test_recommends_seven_when_nothing_pinned(monkeypatch):
    doc = _run(monkeypatch, [_me()] + _pool(12), [])
    entry = doc["users"]["ou_me"]
    assert len(entry["list"]) == 7
    assert entry["pinned"] == []
    # 分数最高的排最前（重合度最高的那位）
    assert entry["algo"][0] == "ou_女10"


def test_anonymous_liker_is_pinned_to_the_front(monkeypatch):
    """必显优先于分数：暗恋我的人爱好跟我完全不同，也必须排第一。"""
    liker = _user("r_liker", "暗恋者", "ou_liker", "女性",
                  **{at.FIELD_SELF_HOBBIES: ["完全不相干"]})
    likes = [_like("ou_liker", "ou_me", month=quota.month_key())]
    doc = _run(monkeypatch, [_me(), liker] + _pool(12), likes)
    entry = doc["users"]["ou_me"]
    assert entry["pinned"] == ["ou_liker"]
    assert entry["list"][0] == "ou_liker"
    assert len(entry["list"]) == 7
    # 必显的人不能再出现在算法位，否则推荐位里同一个人出现两次
    assert "ou_liker" not in entry["algo"]


def test_mutual_like_is_not_pinned(monkeypatch):
    """互相喜欢已经不是秘密了，两个人早在聊，不需要再推。"""
    likes = [_like("ou_女1", "ou_me", status="相互喜欢", month=quota.month_key())]
    doc = _run(monkeypatch, [_me()] + _pool(12), likes)
    assert doc["users"]["ou_me"]["pinned"] == []


def test_real_name_like_is_not_pinned(monkeypatch):
    """必显只认匿名喜欢。实名喜欢本来就告诉对方了。"""
    likes = [_like("ou_女1", "ou_me", like_type="实名", month=quota.month_key())]
    doc = _run(monkeypatch, [_me()] + _pool(12), likes)
    assert doc["users"]["ou_me"]["pinned"] == []


def test_expired_anonymous_like_drops_out(monkeypatch):
    """满 3 个月的匿名喜欢静默失效，位置由算法补上，不需要任何清理任务。"""
    likes = [_like("ou_女1", "ou_me", month="2020-01")]
    doc = _run(monkeypatch, [_me()] + _pool(12), likes)
    entry = doc["users"]["ou_me"]
    assert entry["pinned"] == []
    assert len(entry["list"]) == 7


def test_people_i_already_liked_are_not_candidates(monkeypatch):
    likes = [_like("ou_me", "ou_女12", month=quota.month_key())]
    doc = _run(monkeypatch, [_me()] + _pool(12), likes)
    assert "ou_女12" not in doc["users"]["ou_me"]["list"]


def test_same_gender_and_unknown_gender_are_excluded(monkeypatch):
    users = [_me()] + _pool(8) + _pool(3, prefix="男", gender="男性")
    users.append(_user("r_x", "无性别", "ou_x", "", **{at.FIELD_SELF_HOBBIES: _MY_HOBBIES}))
    doc = _run(monkeypatch, users, [])
    entry = doc["users"]["ou_me"]
    assert all(o.startswith("ou_女") for o in entry["list"])
    assert "ou_x" not in doc["users"]  # 没填性别的人不配名单，也不进别人的候选池


def test_duplicate_archive_of_the_same_person_never_self_pairs(monkeypatch):
    """同一个人重复注册会有多条档案，归并后只能出现一次。"""
    dup = _user("r_dup2", "我", "ou_me", "男性",
                **{at.FIELD_SELF_HOBBIES: _MY_HOBBIES})
    doc = _run(monkeypatch, [_me(), dup] + _pool(12), [])
    entry = doc["users"]["ou_me"]
    assert "ou_me" not in entry["list"]
    assert len(set(entry["list"])) == len(entry["list"])


def test_same_week_is_stable_and_does_not_rewrite(monkeypatch):
    users = [_me()] + _pool(12)
    first = _run(monkeypatch, users, [], week="2026-W38")
    again = _run(monkeypatch, users, [], prev=first, week="2026-W38")
    assert again is None, "同周没变化就不该重写文件、不该刷日志"


def test_next_week_keeps_four_and_replaces_three(monkeypatch):
    users = [_me()] + _pool(20)
    first = _run(monkeypatch, users, [], week="2026-W38")
    old_algo = _algo_of(first)
    second = _run(monkeypatch, users, [], prev=first, week="2026-W39")
    new_algo = _algo_of(second)
    assert len(new_algo) == 7
    assert new_algo[:4] == old_algo[:4]           # 保留 4 个
    fresh = new_algo[4:]
    assert len(fresh) == 3
    assert not set(fresh) & set(old_algo)         # 换掉的 3 个是全新的面孔
    assert second["week"] == "2026-W39"


def test_pinned_survives_the_weekly_rotation(monkeypatch):
    """必显是事实不是选出来的，轮换周也必须还在、还在最前。"""
    liker = _user("r_liker", "暗恋者", "ou_liker", "女性")
    likes = [_like("ou_liker", "ou_me", month=quota.month_key())]
    users = [_me(), liker] + _pool(20)
    first = _run(monkeypatch, users, likes, week="2026-W38")
    second = _run(monkeypatch, users, likes, prev=first, week="2026-W39")
    assert second["users"]["ou_me"]["list"][0] == "ou_liker"


def test_lone_user_gets_no_entry(monkeypatch):
    """站里只有我一个人，名单是空的——不写文件也好，写了也是空的，但不能报错。"""
    doc = _run(monkeypatch, [_me()], [])
    assert doc["users"] == {}
