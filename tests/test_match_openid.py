"""数字红娘推荐 open_id 关联单测（不联网，monkeypatch 替换 bitable 调用）。

覆盖：
- 生成时写入 推荐给用户open_id / 被推荐用户open_id
- 去重优先 open_id 对：用户改名后（昵称变了、open_id 不变）不重复推荐
- 存量旧记录无 open_id 时回退昵称对去重
- 同 open_id 的用户不互相候选
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BOT_DIR = os.path.join(_ROOT, "bot")
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

import auto_tasks as at


def _user(rid, nick, oid, gender, hobbies):
    return {
        "record_id": rid,
        "fields": {
            at.FIELD_NICKNAME: nick,
            at.FIELD_FEISHU_ID: oid,
            at.FIELD_GENDER: gender,
            at.FIELD_EDUCATION: "本科",
            at.FIELD_SELF_HOBBIES: hobbies,
        },
    }


def _run(monkeypatch, tmp_path, users, existing):
    created = []

    def fake_search(table_id, filter_conditions=None):
        if table_id == at.USER_TABLE_ID:
            return users
        if table_id == at.MATCH_TABLE_ID:
            return existing
        return []

    def fake_create(table_id, fields):
        created.append((table_id, dict(fields)))
        return {"record_id": f"c{len(created)}"}

    monkeypatch.setattr(at, "search_records", fake_search)
    monkeypatch.setattr(at, "create_record", fake_create)
    monkeypatch.setattr(at, "SHARED_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(at, "log", lambda *a, **k: None)
    monkeypatch.setattr(at.time, "strftime", lambda fmt: "2026-08-28 00:00:00")
    at.auto_generate_match_recommendations()
    return created


def test_creates_with_openid_fields(monkeypatch, tmp_path):
    users = [
        _user("r1", "甲", "ou_a", "男性", ["唱歌"]),
        _user("r2", "乙", "ou_b", "女性", ["唱歌"]),
        _user("r3", "丙", "ou_c", "女性", ["唱歌"]),
    ]
    created = _run(monkeypatch, tmp_path, users, [])
    assert created, "应生成推荐"
    for _tid, fields in created:
        assert fields[at.FIELD_MATCH_FOR_OPENID]
        assert fields[at.FIELD_MATCH_TARGET_OPENID]


def test_rename_no_duplicate_by_openid(monkeypatch, tmp_path):
    users = [
        _user("r1", "林壹", "ou_a", "男性", ["唱歌"]),
        _user("r2", "乙", "ou_b", "女性", ["唱歌"]),
    ]
    existing = [{
        "record_id": "e1",
        "fields": {
            at.FIELD_MATCH_FOR_USER: "林先生",  # 改名前的昵称快照
            at.FIELD_MATCH_TARGET_USER: "乙",
            at.FIELD_MATCH_FOR_OPENID: "ou_a",
            at.FIELD_MATCH_TARGET_OPENID: "ou_b",
        },
    }]
    created = _run(monkeypatch, tmp_path, users, existing)
    pairs = {(f[at.FIELD_MATCH_FOR_OPENID], f[at.FIELD_MATCH_TARGET_OPENID]) for _t, f in created}
    assert ("ou_a", "ou_b") not in pairs
    assert ("ou_b", "ou_a") in pairs


def test_legacy_record_without_openid_falls_back_to_nickname(monkeypatch, tmp_path):
    users = [
        _user("r1", "甲", "ou_a", "男性", ["唱歌"]),
        _user("r2", "乙", "ou_b", "女性", ["唱歌"]),
    ]
    existing = [{
        "record_id": "e1",
        "fields": {
            at.FIELD_MATCH_FOR_USER: "甲",
            at.FIELD_MATCH_TARGET_USER: "乙",
        },
    }]
    created = _run(monkeypatch, tmp_path, users, existing)
    pairs = {(f[at.FIELD_MATCH_FOR_OPENID], f[at.FIELD_MATCH_TARGET_OPENID]) for _t, f in created}
    assert ("ou_a", "ou_b") not in pairs
    assert ("ou_b", "ou_a") in pairs


def test_same_openid_never_pairs(monkeypatch, tmp_path):
    users = [
        _user("r1", "旧甲", "ou_a", "男性", ["唱歌"]),
        _user("r2", "甲", "ou_a", "女性", ["唱歌"]),
    ]
    created = _run(monkeypatch, tmp_path, users, [])
    assert created == []
