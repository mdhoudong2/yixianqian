"""客户端重载/恢复痕迹埋点契约（不联网）。

覆盖：鉴权、限流、结构校验、JSONL 落盘字段、轮转、管理端聚合与 days 过滤。
"""
import json
import os
import sys
import time
from datetime import datetime

import pytest

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402

USER = "ou_ce_user"
ADMIN = "ou_test_admin"
OTHER = "ou_ce_other"
SID = "sid-test-0001"


def _client(open_id=USER):
    c = app.app.test_client()
    if open_id is not None:
        c.set_cookie("yxq_session", app.create_session(open_id))
    return c


@pytest.fixture
def events_file(tmp_path, monkeypatch):
    path = tmp_path / "client_events.jsonl"
    monkeypatch.setattr(app, "CLIENT_EVENTS_FILE", str(path))
    monkeypatch.setattr(app, "CLIENT_EVENTS_MAX_BYTES", 20 * 1024 * 1024)
    monkeypatch.setattr(
        app, "_snap",
        lambda name: ([{"fields": {"飞书用户ID": USER, "用户ID": "U-9001"}}]
                      if name == "users" else []))
    app._rate_store.clear()
    return path


def _payload(events=None, **boot_over):
    boot = {
        "nav": "reload",
        "at": 1700000000000,
        "prev_reason": "version",
        "since_boot": 1200,
        "prev_pos": 7,
        "clean_exit": True,
    }
    boot.update(boot_over)
    return {
        "sid": SID,
        "boot": boot,
        "events": events if events is not None else [
            {"t": 1700000000100, "k": "page", "d": {"to": "cards", "from": "likes"}},
        ],
    }


def _post(client, payload):
    return client.post("/api/client-events", json=payload)


def _read_lines(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ---------------------------------------------------------------- 鉴权 / 限流

def test_unauthenticated_is_401_and_writes_nothing(events_file):
    assert _post(_client(None), _payload()).status_code == 401
    assert not events_file.exists()


def test_rate_limit_is_30_per_minute(events_file):
    c = _client()
    for _ in range(30):
        assert _post(c, _payload()).status_code == 200
    assert _post(c, _payload()).status_code == 429
    assert len(_read_lines(events_file)) == 30


# ---------------------------------------------------------------- 合法写入

def test_valid_payload_appends_one_line_per_event(events_file):
    c = _client()
    payload = _payload(events=[
        {"t": 1700000000100, "k": "boot", "d": {"nav": "reload"}},
        {"t": 1700000000200, "k": "page", "d": {"to": "cards", "from": "likes"}},
    ])
    r = _post(c, payload)
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "written": 2}

    rows = _read_lines(events_file)
    assert len(rows) == 2
    now_ms = int(time.time() * 1000)
    for row, ev in zip(rows, payload["events"]):
        assert abs(row["ts"] - now_ms) < 10_000
        assert row["open_id"] == USER
        assert row["uid"] == "U-9001"
        assert row["sid"] == SID
        assert row["boot"] == payload["boot"]
        assert row["ev"] == ev
        assert isinstance(row["ua"], str)


def test_ua_is_truncated_to_200(events_file):
    r = _post(_client(), _payload(), )
    assert r.status_code == 200
    # 上面一次默认 UA；再发一条超长 UA 验证截断
    r = _client().post("/api/client-events", json=_payload(),
                       headers={"User-Agent": "u" * 500})
    assert r.status_code == 200
    rows = _read_lines(events_file)
    assert len(rows[-1]["ua"]) == 200


def test_write_failure_returns_500(events_file, monkeypatch):
    def boom(_lines):
        raise OSError("disk full")

    monkeypatch.setattr(app, "_client_events_append", boom)
    assert _post(_client(), _payload()).status_code == 500


# ---------------------------------------------------------------- 非法结构

@pytest.mark.parametrize("mutate", [
    lambda p: {"sid": SID, "events": []},                       # 缺 boot
    lambda p: _payload(nav="push"),                             # nav 非法
    lambda p: _payload(prev_reason="weird"),                    # reason 非法
    lambda p: _payload(clean_exit="false"),                     # 类型非法
    lambda p: _payload(events=[{"t": 1, "k": "x" * 33}]),       # k 过长
    lambda p: _payload(events=[{"t": 1, "k": "page", "d": {"x": "a" * 201}}]),
    lambda p: _payload(events=[{"t": 1, "k": "page", "d": {"x": {"nested": 1}}}]),
    lambda p: _payload(events=[{"t": 1, "k": "page"}] * 31),    # 超 30 条
])
def test_invalid_payload_is_400(events_file, mutate):
    r = _post(_client(), mutate(_payload()))
    assert r.status_code == 400
    assert not events_file.exists()


# ---------------------------------------------------------------- 轮转

def test_file_rotation_to_dot_one(events_file, monkeypatch):
    events_file.write_text('{"old":1}\n' * 20, encoding="utf-8")
    monkeypatch.setattr(app, "CLIENT_EVENTS_MAX_BYTES", 100)
    assert events_file.stat().st_size > 100

    assert _post(_client(), _payload()).status_code == 200
    backup = events_file.with_name(events_file.name + ".1")
    assert backup.exists()
    assert '{"old":1}' in backup.read_text(encoding="utf-8")
    rows = _read_lines(events_file)
    assert len(rows) == 1
    assert rows[0]["ev"]["k"] == "page"


# ---------------------------------------------------------------- summary

def _rec(ts, sid, *, at=None, nav="navigate", prev_reason="", since=None,
         prev_pos=None, clean_exit=True, k="boot", d=None, open_id=USER):
    return {
        "ts": ts,
        "open_id": open_id,
        "uid": "U-9001",
        "ua": "UA",
        "sid": sid,
        "boot": {
            "nav": nav,
            "at": ts if at is None else at,
            "prev_reason": prev_reason,
            "since_boot": since,
            "prev_pos": prev_pos,
            "clean_exit": clean_exit,
        },
        "ev": {"t": ts, "k": k, "d": d or {}},
    }


def _write_jsonl(path, records):
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8")


@pytest.fixture
def summary_file(events_file):
    now = int(time.time() * 1000)
    boot_a = now - 300_000
    _write_jsonl(events_file, [
        _rec(now - 300_000, "sid-a", at=boot_a, nav="reload", prev_reason="version",
             since=60_000, prev_pos=3, clean_exit=False, k="boot"),
        _rec(now - 290_000, "sid-a", at=boot_a, nav="reload", prev_reason="version",
             since=60_000, prev_pos=3, clean_exit=False, k="page",
             d={"to": "cards", "from": "likes"}),
        _rec(now - 200_000, "sid-b", nav="navigate", prev_reason="", since=None,
             clean_exit=True, k="boot"),
        _rec(now - 100_000, "sid-c", nav="back_forward", prev_reason="unknown",
             since=400_000, clean_exit=False, k="boot"),
        _rec(now - 90_000, "sid-a", at=boot_a, nav="reload", prev_reason="version",
             k="restore", d={"ok": True, "saved_pos": 3, "restored_pos": 3,
                             "total": 20, "reason": "ok"}),
        _rec(now - 80_000, "sid-a", at=boot_a, nav="reload", prev_reason="version",
             k="restore", d={"ok": False, "saved_pos": 30, "restored_pos": None,
                             "total": 20, "reason": "out_of_range"}),
        _rec(now - 70_000, "sid-b", at=now - 200_000, nav="navigate", k="restore",
             d={"ok": False, "saved_pos": None, "restored_pos": None,
                "total": 20, "reason": "no_state"}),
        _rec(now - 4 * 86400_000, "sid-old", nav="reload", prev_reason="version",
             since=1000, clean_exit=False, k="boot"),
    ])
    return events_file, now


def test_summary_aggregates(summary_file):
    path, now = summary_file
    d = _client(ADMIN).get("/api/admin/client-events/summary?days=3").get_json()
    day = datetime.fromtimestamp(now / 1000).strftime("%Y-%m-%d")

    assert d["days"] == 3
    assert d["total_events"] == 7
    assert d["boots"] == {"navigate": 1, "reload": 1, "back_forward": 1, "unknown": 0}
    assert d["reload_reasons"] == {"version": 1, "unknown": 1}
    assert d["restore"] == {"ok": 1, "fail": 1, "no_state": 1}
    assert len(d["crash_suspects"]) == 1
    assert d["crash_suspects"][0]["sid"] == "sid-a"
    assert d["crash_suspects"][0]["prev_pos"] == 3
    assert sum(d["by_day"].values()) == 7
    assert d["by_day"].get(day, 0) >= 1
    assert len(d["recent"]) == 7
    assert d["recent"][0]["ts"] == now - 70_000        # 最近一条在最前
    assert d["recent"][0]["k"] == "restore"
    assert all("ts" in r and "ev" not in r for r in d["recent"])


def test_summary_days_filter(summary_file):
    d = _client(ADMIN).get("/api/admin/client-events/summary?days=1").get_json()
    assert d["days"] == 1
    assert d["total_events"] == 7
    # 4 天前的记录已在 days=3 外；再造一条 2 天前的验证 days=1 也过滤
    path, now = summary_file
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(_rec(now - 2 * 86400_000, "sid-2d"), ensure_ascii=False) + "\n")
    d = _client(ADMIN).get("/api/admin/client-events/summary?days=1").get_json()
    assert d["total_events"] == 7


def test_summary_recent_capped_at_50(events_file):
    now = int(time.time() * 1000)
    _write_jsonl(events_file, [
        _rec(now - 1000 + i, "sid-%d" % i, at=now - 1000 + i) for i in range(60)
    ])
    d = _client(ADMIN).get("/api/admin/client-events/summary?days=3").get_json()
    assert d["total_events"] == 60
    assert len(d["recent"]) == 50
    assert d["recent"][0]["ts"] == now - 1000 + 59


def test_summary_invalid_days_is_400(events_file):
    c = _client(ADMIN)
    assert c.get("/api/admin/client-events/summary?days=0").status_code == 400
    assert c.get("/api/admin/client-events/summary?days=abc").status_code == 400


def test_summary_requires_admin(events_file):
    assert _client(None).get("/api/admin/client-events/summary").status_code == 401
    assert _client(OTHER).get("/api/admin/client-events/summary").status_code == 403
