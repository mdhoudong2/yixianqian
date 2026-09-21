"""auto_generate_match_recommendations 生成逻辑单测（不联网，全 mock）。

覆盖：无 open_id 用户跳过（既不为其生成也不被推荐他人）、
去重（openid 对 + 昵称对）、每人最多 3 条、只推异性、
同日幂等（date-gate 不写表）、去重查询只取四列。
"""
import sys

sys.path.insert(0, "bot")

import auto_tasks as M  # noqa: E402
from constants import (  # noqa: E402
    FIELD_MATCH_FOR_OPENID,
    FIELD_MATCH_FOR_USER,
    FIELD_MATCH_TARGET_OPENID,
    FIELD_MATCH_TARGET_USER,
)


def _user(nick, oid, gender):
    return {
        "昵称": nick,
        "飞书用户ID": oid,
        "性别": gender,
        "学历": "本科",
        "我是一个怎样的人-爱好": [{"text": "篮球", "type": "text"}],
    }


def _run(monkeypatch, active, existing_match_rows, match_log):
    created = []
    seen_search = {}

    def fake_search(table_id, filt=None, page_size=100, field_names=None, **kw):
        from constants import MATCH_TABLE_ID, USER_TABLE_ID

        if table_id == USER_TABLE_ID:
            return [{"record_id": f"rec{i}", "fields": u} for i, u in enumerate(active)]
        if table_id == MATCH_TABLE_ID:
            seen_search["field_names"] = field_names
            return existing_match_rows
        return []

    monkeypatch.setattr(M, "search_records", fake_search)
    monkeypatch.setattr(M, "create_record", lambda tid, f: created.append(f) or True)
    monkeypatch.setattr(M.storage, "load_json", lambda *a, **k: dict(match_log))
    saved = {}
    monkeypatch.setattr(M.storage, "save_json", lambda p, d: saved.update(d))
    monkeypatch.setattr(M, "log", lambda *a, **k: None)
    M.auto_generate_match_recommendations()
    return created, seen_search, saved


def test_no_openid_users_skipped(monkeypatch):
    active = [_user("男1", "ou_m1", "男性"), _user("女1", "ou_f1", "女性"),
              _user("幽灵", "", "女性")]
    created, _, _ = _run(monkeypatch, active, [], {})
    for_nicks = [f["推荐给用户"] for f in created]
    target_nicks = [f["被推荐用户"] for f in created]
    assert "幽灵" not in for_nicks
    assert "幽灵" not in target_nicks
    for f in created:
        assert f["推荐给用户open_id"] and f["被推荐用户open_id"]


def test_dedup_and_top3_and_opposite_gender(monkeypatch):
    active = [_user("男1", "ou_m1", "男性")]
    active += [_user(f"女{i}", f"ou_f{i}", "女性") for i in range(5)]
    existing = [{
        "fields": {
            FIELD_MATCH_FOR_OPENID: "ou_m1",
            FIELD_MATCH_TARGET_OPENID: "ou_f0",
            FIELD_MATCH_FOR_USER: "男1",
            FIELD_MATCH_TARGET_USER: "女0",
        }
    }]
    created, seen, _ = _run(monkeypatch, active, existing, {})
    mine = [f for f in created if f["推荐给用户"] == "男1"]
    targets = [f["被推荐用户"] for f in mine]
    assert "女0" not in targets
    assert len(mine) <= 3
    # 去重查询只取四列（5 万行全字段扫描太重）
    assert seen["field_names"] is not None
    assert set(seen["field_names"]) == {
        FIELD_MATCH_FOR_OPENID, FIELD_MATCH_TARGET_OPENID,
        FIELD_MATCH_FOR_USER, FIELD_MATCH_TARGET_USER,
    }


def test_same_day_idempotent(monkeypatch):
    import time

    today = time.strftime("%Y-%m-%d")
    active = [_user("男1", "ou_m1", "男性"), _user("女1", "ou_f1", "女性")]
    created, _, saved = _run(monkeypatch, active, [], {"last_generate_date": today})
    assert created == []
    assert saved == {}
