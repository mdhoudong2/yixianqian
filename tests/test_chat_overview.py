"""管理员「一对一聊天情况总览」API 契约（不联网，桩表驱动）。

覆盖任务书要求的判定面：
- _require_admin：未登录 401 / 非管理员 403 / 缺 activity_id 400；
- 每人 assigned/chatted/msg_count、pairs/unchatted；
- 总览 totals（rate/zero/done/mutual/messages）与互聊口径；
- 轮次缺省最新、指定轮次、非法轮次回落最新；
- 排序：unchatted 降序，再 user_id；
- 「已聊」只认本人给必聊对象发的非删除留言（他人留言、非必聊对象留言、已删除留言不计）；
- 零写入：端点只读，create/update/delete 一次都不许有。

桩法比照 tests/test_onetoone_admin_api.py：Flask test client + cookie session +
monkeypatch 快照；假表复用 tests/test_onetoone_parity.py 的 FakeBitable。
"""
import os
import sys

import pytest

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402

ADMIN = "ou_test_admin"
ME = "ou_me"
ACT = "A-0002"
URL = "/api/admin/onetoone/chat-overview?activity_id=" + ACT


def _user(oid, uid, real="", nick=""):
    return {"record_id": f"rec_u_{oid}",
            "fields": {"飞书用户ID": oid, "用户ID": uid, "姓名": real, "昵称": nick}}


def _act():
    return {"record_id": "rec_act",
            "fields": {"活动ID": ACT, "活动名称": "关系自洽"}}


def _res(person, target, rank=1, round_no=2, user_name="", target_name=""):
    fields = {
        "活动ID": ACT,
        "排名": rank,
        "用户open_id": person,
        "用户昵称": user_name,
        "必聊对象open_id": target,
        "必聊对象昵称": target_name,
    }
    if round_no is not None:
        fields["轮次"] = str(round_no)
    return {"record_id": f"rec_res_{person}_{target}_{rank}_{round_no}",
            "fields": fields}


_MSG_SEQ = [0]


def _msg(author, target, status="正常", ts=1700000000000):
    _MSG_SEQ[0] += 1
    return {"record_id": f"rec_msg_{_MSG_SEQ[0]}",
            "fields": {"留言人open_id": author, "目标用户open_id": target,
                       "状态": status, "创建时间": ts, "内容": "hi"}}


# 人员：p1/p2 真名或昵称齐全；p4 只有编号（名字回退结果表落库名）
USERS = [
    _user("ou_p1", "U-0001", real="甲真", nick="甲昵"),
    _user("ou_p2", "U-0002", nick="乙昵"),
    _user("ou_p3", "U-0003", nick="丙昵"),
    _user("ou_p4", "U-0004"),
]

RESULTS = [
    # 第 2 轮（最新）
    _res("ou_p1", "ou_p2", 1, 2, user_name="甲落库", target_name="乙落库"),
    _res("ou_p1", "ou_p3", 2, 2, user_name="甲落库", target_name="丙落库"),
    _res("ou_p2", "ou_p1", 1, 2, user_name="乙落库", target_name="甲落库"),
    _res("ou_p3", "ou_p1", 1, 2, user_name="丙落库", target_name="甲落库"),
    _res("ou_p4", "ou_p3", 1, 2, user_name="丁落库", target_name="丙落库"),
    # 第 1 轮：p1 必聊 p4，且 p1->p4 的留言只在第 1 轮算「必聊对象」
    _res("ou_p1", "ou_p4", 1, 1, user_name="甲落库", target_name="丁落库"),
]

MESSAGES = [
    # 计入第 2 轮：p1 已聊 p2/p3、p2 已聊 p1
    _msg("ou_p1", "ou_p2"), _msg("ou_p1", "ou_p2", ts=1700000005000),
    _msg("ou_p1", "ou_p3"),
    _msg("ou_p2", "ou_p1"),
    # 非必聊对象（第 2 轮 p1 不负责 p4）——不得计入 p1 的 msg_count
    _msg("ou_p1", "ou_p4"), _msg("ou_p1", "ou_p4"), _msg("ou_p1", "ou_p4"),
    _msg("ou_p1", "ou_p4"), _msg("ou_p1", "ou_p4"),
    # 非必聊双向留言也不得算「互聊」（p1/p4 第 2 轮互不在对方必聊名单）
    _msg("ou_p4", "ou_p1"),
    # 他人留言（非本人发出）——不得算成 p3/ p2 已聊
    _msg("ou_x", "ou_p3"), _msg("ou_p2", "ou_p3"),
    # 已删除——不得计入（作者是 p1，对象是必聊 p2）
    _msg("ou_p1", "ou_p2", status="已删除"),
    _msg("ou_p1", "ou_p4", status="已删除"),
]


class Api:
    def __init__(self, monkeypatch):
        from tests.test_onetoone_parity import FakeBitable

        self.snapshot = {
            "activities": [_act()],
            "users": list(USERS),
            "onetoone_results": list(RESULTS),
            "messages": list(MESSAGES),
        }
        self.fresh_calls = []
        self.fake = FakeBitable({})
        monkeypatch.setattr(app, "_grouping_client", self.fake)
        monkeypatch.setattr(app, "_snap_fresh", self._fresh)
        monkeypatch.setattr(app, "_snap", lambda key: list(self.snapshot.get(key, [])))
        monkeypatch.setattr(app, "_snap_live", lambda key: list(self.snapshot.get(key, [])))
        self.client = app.app.test_client()
        self.client.set_cookie("yxq_session", app.create_session(ADMIN))
        self.client_me = app.app.test_client()
        self.client_me.set_cookie("yxq_session", app.create_session(ME))

    def _fresh(self, key):
        self.fresh_calls.append(key)
        return list(self.snapshot.get(key, []))

    def get(self, path=URL, client=None):
        return (client or self.client).get(path)


@pytest.fixture
def api(monkeypatch):
    return Api(monkeypatch)


def _summary(body):
    return body["summary"]


# ---------------------------------------------------------------- 鉴权

def test_unauthenticated_is_401(api):
    assert app.app.test_client().get(URL).status_code == 401


def test_non_admin_is_403(api):
    assert api.get(client=api.client_me).status_code == 403


def test_missing_activity_id_is_400(api):
    assert api.get("/api/admin/onetoone/chat-overview").status_code == 400


def test_unknown_activity_is_404(api):
    assert api.get("/api/admin/onetoone/chat-overview?activity_id=A-9999").status_code == 404


# ---------------------------------------------------------------- 轮次与总览

def test_latest_round_totals_and_people(api):
    r = api.get()
    assert r.status_code == 200
    body = r.get_json()
    assert body["name"] == "关系自洽"
    assert body["round"] == 2 and body["rounds"] == [1, 2]
    assert _summary(body) == {
        "people": 4,
        "pairs": 5,
        "chatted_pairs": 3,
        "rate": 0.6,
        "zero_people": 2,
        "done_people": 2,
        "mutual_pairs": 1,
        "messages": 4,
    }

    people = {p["open_id"]: p for p in body["people"]}
    assert set(people) == {"ou_p1", "ou_p2", "ou_p3", "ou_p4"}

    p1 = people["ou_p1"]
    assert p1["assigned"] == 2 and p1["chatted"] == 2 and p1["msg_count"] == 3
    assert p1["unchatted"] == []
    assert [(t["open_id"], t["rank"], t["chatted"], t["msg_count"]) for t in p1["pairs"]] == [
        ("ou_p2", 1, True, 2),
        ("ou_p3", 2, True, 1),
    ]
    # 毫秒时间戳转成可读文本（不是裸数字）
    assert p1["pairs"][0]["last_at"]
    assert not p1["pairs"][0]["last_at"].isdigit()

    p2 = people["ou_p2"]
    assert p2["assigned"] == 1 and p2["chatted"] == 1 and p2["msg_count"] == 1

    p3 = people["ou_p3"]
    assert p3["assigned"] == 1 and p3["chatted"] == 0 and p3["msg_count"] == 0
    assert p3["unchatted"] == [
        {"open_id": "ou_p1", "user_id": "U-0001", "name": "甲真", "rank": 1}
    ]

    # 非必聊对象留言（p1->p4 五条）不计入 p1 的 msg_count；已删除也不计
    assert people["ou_p4"]["chatted"] == 0 and people["ou_p4"]["msg_count"] == 0


def test_people_sorted_by_unchatted_desc_then_user_id(api):
    body = api.get().get_json()
    assert [p["user_id"] for p in body["people"]] == ["U-0003", "U-0004", "U-0001", "U-0002"]
    assert [len(p["unchatted"]) for p in body["people"]] == [1, 1, 0, 0]


def test_round_filter_uses_same_messages(api):
    body = api.get(URL + "&round=1").get_json()
    assert body["round"] == 1 and body["rounds"] == [1, 2]
    assert _summary(body) == {
        "people": 1,
        "pairs": 1,
        "chatted_pairs": 1,
        "rate": 1.0,
        "zero_people": 0,
        "done_people": 1,
        "mutual_pairs": 0,
        "messages": 5,
    }
    person = body["people"][0]
    assert person["open_id"] == "ou_p1"
    assert person["assigned"] == 1 and person["chatted"] == 1 and person["msg_count"] == 5
    # p4 不在 users 快照的姓名/昵称里：回退结果表落库名
    assert person["pairs"][0]["name"] == "丁落库"


@pytest.mark.parametrize("round_arg", ["99", "x", "0", "-1"])
def test_invalid_round_falls_back_to_latest(api, round_arg):
    body = api.get(URL + "&round=" + round_arg).get_json()
    assert body["round"] == 2
    assert _summary(body)["pairs"] == 5


def test_no_results_returns_empty_shape(api):
    api.snapshot["onetoone_results"] = []
    body = api.get().get_json()
    assert body["round"] is None and body["rounds"] == []
    assert body["people"] == []
    assert _summary(body) == {
        "people": 0,
        "pairs": 0,
        "chatted_pairs": 0,
        "rate": 0.0,
        "zero_people": 0,
        "done_people": 0,
        "mutual_pairs": 0,
        "messages": 0,
    }


# ---------------------------------------------------------------- 名字 / 口径

def test_name_fallbacks_and_message_scope(api):
    body = api.get().get_json()
    people = {p["open_id"]: p for p in body["people"]}
    # 真名 > 昵称 > 结果表落库名
    assert people["ou_p1"]["name"] == "甲真"
    assert people["ou_p2"]["name"] == "乙昵"
    assert people["ou_p3"]["name"] == "丙昵"
    assert people["ou_p4"]["name"] == "丁落库"
    # 结果表落库名兜底后，编号仍来自 users 快照
    assert people["ou_p4"]["user_id"] == "U-0004"

    # 他人留言（ou_x -> ou_p3 / ou_p2 -> ou_p3）不能让 p3 变「已聊」
    assert people["ou_p3"]["chatted"] == 0
    # 已删除留言不能算 p1->p2 的已聊条数（剩 2 条正常）
    p1_p2 = [t for t in people["ou_p1"]["pairs"] if t["open_id"] == "ou_p2"][0]
    assert p1_p2["chatted"] is True and p1_p2["msg_count"] == 2


def test_reads_results_and_messages_fresh(api):
    api.get()
    assert "onetoone_results" in api.fresh_calls
    assert "messages" in api.fresh_calls
    # 留言只读一次（全表 fresh search）
    assert api.fresh_calls.count("messages") == 1


# ---------------------------------------------------------------- 零写入

def test_endpoint_performs_zero_writes(api, monkeypatch):
    calls = []
    for name in ("create_record", "update_record", "delete_record",
                 "batch_update_records", "batch_create_records"):
        if hasattr(app.bitable, name):
            monkeypatch.setattr(app.bitable, name,
                                lambda *a, _n=name, **k: calls.append((_n, a, k)))
    r = api.get()
    assert r.status_code == 200
    assert calls == []
    assert api.fake.writes() == []
