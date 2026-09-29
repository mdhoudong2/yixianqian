"""H5 点赞接口：额度、幂等、取消、以及「收到几个赞」不能外泄。

飞书那张表用一个内存假表顶替，过滤逻辑照 `operator: is` 实现——**不桩 lib.praise**：
额度、有效性、归属桶这些规则全在 lib 里，桩掉这文件就只剩在测 mock 了。

这个文件里最该钉死的一条是第 4 条需求：**收到多少赞只有本人可见**。
它错了不会报错，只会让某个人的受欢迎程度在资料页上被所有人看见。
"""
import os
import sys

import pytest

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)

import app  # noqa: E402

from lib import points_config, praise, quota  # noqa: E402

ME = "ou_me"
HER = "ou_her"
HIM = "ou_him"          # 和「我」同性，用来验异性闸
HER2 = "ou_her2"        # 第二个女生，用来验「额度用完 / 退回」

ME_FIELDS = {"用户ID": "U-0001", "昵称": "小北", "账号状态": "单身", "性别": "男"}
HER_FIELDS = {"用户ID": "U-0002", "昵称": "阿南", "账号状态": "单身", "性别": "女"}
HIM_FIELDS = {"用户ID": "U-0003", "昵称": "阿北", "账号状态": "单身", "性别": "男"}
HER2_FIELDS = {"用户ID": "U-0004", "昵称": "小西", "账号状态": "单身", "性别": "女"}

USERS = {ME: ME_FIELDS, HER: HER_FIELDS, HIM: HIM_FIELDS, HER2: HER2_FIELDS}


class FakeTable(object):
    """够用的内存假表：只实现 `operator: is`，因为接线里只用这一种。"""

    def __init__(self):
        self.rows = []
        self._seq = 0
        self.fail_create = False

    def search(self, table_id, conditions=None, *a, **k):
        out = []
        for row in self.rows:
            if all(self._match(row, c) for c in (conditions or [])):
                out.append(row)
        return out

    @staticmethod
    def _match(row, cond):
        want = (cond.get("value") or [""])[0]
        got = row["fields"].get(cond.get("field_name"), "")
        if isinstance(got, list):                    # 文本字段读出来可能是富文本数组
            got = "".join(x.get("text", "") for x in got)
        return got == want

    def create(self, table_id, fields):
        if self.fail_create:
            return None
        self._seq += 1
        row = {"record_id": f"rec_{self._seq}", "fields": dict(fields)}
        self.rows.append(row)
        return row

    def update(self, table_id, record_id, fields):
        for row in self.rows:
            if row["record_id"] == record_id:
                row["fields"].update(fields)
                return row
        return None


@pytest.fixture
def table(monkeypatch):
    t = FakeTable()
    monkeypatch.setattr(app, "PRAISE_TABLE_ID", "tbl_praise")
    monkeypatch.setattr(app.bitable, "search_records", t.search)
    monkeypatch.setattr(app.bitable, "create_record", t.create)
    monkeypatch.setattr(app.bitable, "update_record", t.update)
    # find_praise / prays_today / praise_is_active 都建在 search_records + 常量上，
    # 用真实现——它们正是「状态白名单」和「归属日期」两条口径的落点。
    monkeypatch.setattr(app.bitable, "find_praise",
                        lambda i, tgt: _find_active(t, i, tgt))
    monkeypatch.setattr(app.bitable, "prays_today",
                        lambda oid, day: _prays_today(t, oid, day))
    monkeypatch.setattr(app.bitable, "received_praise",
                        lambda oid, week=None: _received(t, oid))
    return t


def _find_active(t, initiator, target):
    for row in t.search("", [
            {"field_name": app.F_PRAISE_INITIATOR_OPENID, "operator": "is",
             "value": [initiator]},
            {"field_name": app.F_PRAISE_TARGET_OPENID, "operator": "is",
             "value": [target]}]):
        if app.bitable.praise_is_active(row["fields"]):
            return row
    return None


def _prays_today(t, oid, day):
    return [(r["fields"].get(app.F_PRAISE_STATUS), day)
            for r in t.search("", [
                {"field_name": app.F_PRAISE_INITIATOR_OPENID, "operator": "is",
                 "value": [oid]}])]


def _received(t, oid):
    return [(r["fields"].get(app.F_PRAISE_STATUS), r["fields"].get(app.F_PRAISE_WEEK), oid)
            for r in t.search("", [
                {"field_name": app.F_PRAISE_TARGET_OPENID, "operator": "is",
                 "value": [oid]}])]


@pytest.fixture
def client(monkeypatch, table):
    monkeypatch.setattr(app, "snap_self_user",
                        lambda: {"record_id": "rec_me", "fields": dict(ME_FIELDS)})
    monkeypatch.setattr(app, "snap_find_user_by_openid",
                        lambda oid: ({"record_id": "rec_" + oid, "fields": dict(USERS[oid])}
                                     if oid in USERS else None))
    monkeypatch.setattr(app, "_snap", lambda name: [])
    monkeypatch.setattr(app, "refresh_snapshot_table_async", lambda *a, **k: None)
    app._rate_store.clear()
    # 额度的默认值是 10，用例里要调小的话直接改 SPECS 的 default，
    # 不走 points_config.set_value——那要写 SQLite，而这文件不碰库。
    monkeypatch.setitem(points_config.SPECS["daily_praise_limit"], "default", 10)
    points_config.invalidate()

    c = app.app.test_client()
    c.set_cookie("yxq_session", app.create_session(ME))
    return c


def _post(c, url, body=None):
    return c.post(url, json=body or {}, headers={"X-Requested-With": "XMLHttpRequest"})


def _delete(c, url):
    return c.delete(url, headers={"X-Requested-With": "XMLHttpRequest"})


# ---------------------------------------------------------------- 基本路径

def test_a_praise_is_stored_with_every_field_the_stats_need(client, table):
    """统计按「归属日期/归属周/性别」数，任一为空都会让某个桶悄悄漏掉一个人。"""
    r = _post(client, "/api/praise", {"target_openid": HER})
    assert r.status_code == 200, r.get_json()

    row = table.rows[0]["fields"]
    assert row[app.F_PRAISE_INITIATOR_OPENID] == ME
    assert row[app.F_PRAISE_INITIATOR_GENDER] == "男"      # 性别是快照
    assert row[app.F_PRAISE_TARGET_OPENID] == HER
    assert row[app.F_PRAISE_TARGET_GENDER] == "女"
    assert row[app.F_PRAISE_STATUS] == praise.PRAISE_STATUS_ACTIVE
    assert row[app.F_PRAISE_OBJECT_TYPE] == praise.OBJECT_USER_PROFILE
    assert row[app.F_PRAISE_OBJECT_ID] == HER
    assert row[app.F_PRAISE_DAY] == praise.day_key()
    assert row[app.F_PRAISE_WEEK] == praise.week_key()
    assert row[app.F_PRAISE_CREATED_AT]
    assert r.get_json()["left"] == 9


def test_the_daily_limit_is_enforced(client, table, monkeypatch):
    monkeypatch.setitem(points_config.SPECS["daily_praise_limit"], "default", 1)
    points_config.invalidate()

    assert _post(client, "/api/praise", {"target_openid": HER}).status_code == 200
    r = _post(client, "/api/praise", {"target_openid": HER2})
    assert r.status_code == 400 and "点满" in r.get_json()["error"]
    assert len(table.rows) == 1


def test_cancelling_gives_the_slot_back(client, table, monkeypatch):
    """取消掉的赞不占次数——口径是「每天最多 10 个有效赞」。"""
    monkeypatch.setitem(points_config.SPECS["daily_praise_limit"], "default", 1)
    points_config.invalidate()

    _post(client, "/api/praise", {"target_openid": HER})
    assert _post(client, "/api/praise", {"target_openid": HER2}).status_code == 400

    r = _delete(client, f"/api/praise/{HER}")
    assert r.status_code == 200 and r.get_json()["left"] == 1
    assert _post(client, "/api/praise", {"target_openid": HER2}).status_code == 200


def test_cancelling_keeps_the_row(client, table):
    """取消是改状态，不是删记录：删了就查不出「点过又反悔」。"""
    _post(client, "/api/praise", {"target_openid": HER})
    _delete(client, f"/api/praise/{HER}")
    assert len(table.rows) == 1
    assert table.rows[0]["fields"][app.F_PRAISE_STATUS] == praise.PRAISE_STATUS_CANCELLED


def test_a_cancelled_praise_can_be_given_again(client, table):
    """取消之后重新点，应该新建一条而不是被「点过了」挡住。"""
    _post(client, "/api/praise", {"target_openid": HER})
    _delete(client, f"/api/praise/{HER}")
    r = _post(client, "/api/praise", {"target_openid": HER})
    assert r.status_code == 200
    assert len(table.rows) == 2
    active = [x for x in table.rows
              if x["fields"][app.F_PRAISE_STATUS] == praise.PRAISE_STATUS_ACTIVE]
    assert len(active) == 1


# ---------------------------------------------------------------- 幂等 / 重复

def test_praising_twice_is_success_not_an_error(client, table):
    """按钮点完就变「已点」，双击/断网重试都会再打一次。回错误会弹红提示，
    用户以为没点上、实际点上了——照实回成功，而且不能再建一条。"""
    _post(client, "/api/praise", {"target_openid": HER})
    r = _post(client, "/api/praise", {"target_openid": HER})
    assert r.status_code == 200
    assert r.get_json()["already"] is True
    assert len(table.rows) == 1


def test_a_duplicate_does_not_eat_a_slot(client, table):
    for _ in range(3):
        _post(client, "/api/praise", {"target_openid": HER})
    assert _post(client, "/api/praise", {"target_openid": HER}).get_json()["left"] == 9


def test_cancelling_something_never_praised_is_success_too(client):
    r = _delete(client, f"/api/praise/{HER}")
    assert r.status_code == 200 and r.get_json()["already"] is True


# ---------------------------------------------------------------- 谁能点

def test_only_the_opposite_gender(client):
    r = _post(client, "/api/praise", {"target_openid": HIM})
    assert r.status_code == 400 and "异性" in r.get_json()["error"]


def test_not_yourself(client):
    r = _post(client, "/api/praise", {"target_openid": ME})
    assert r.status_code == 400 and "自己" in r.get_json()["error"]


def test_a_missing_gender_is_refused(client, monkeypatch):
    """没填性别就判不了「是不是异性」。放行的话同性之间也能点。"""
    fields = dict(HER_FIELDS)
    fields["性别"] = ""
    monkeypatch.setattr(app, "snap_find_user_by_openid",
                        lambda oid: {"record_id": "rec_x", "fields": fields})
    r = _post(client, "/api/praise", {"target_openid": HER})
    assert r.status_code == 400 and "性别" in r.get_json()["error"]


def test_an_unknown_target_is_404(client):
    assert _post(client, "/api/praise", {"target_openid": "ou_ghost"}).status_code == 404


def test_a_write_failure_is_reported_not_swallowed(client, table):
    """建记录失败要回错误，不能回「点赞成功」——用户以为点上了，实际没有。"""
    table.fail_create = True
    r = _post(client, "/api/praise", {"target_openid": HER})
    assert r.status_code == 502


# ---------------------------------------------------------------- 表没配

def test_an_unconfigured_table_says_so(client, monkeypatch):
    """生产还没建表时必须明确说「没配置」，绝不往空表 ID 里写。"""
    monkeypatch.setattr(app, "PRAISE_TABLE_ID", "")
    r = _post(client, "/api/praise", {"target_openid": HER})
    assert r.status_code == 503 and "配置" in r.get_json()["error"]


# ---------------------------------------------------------------- 隐私（第 4 条）

def test_the_public_profile_never_exposes_a_praise_count(client):
    """资料页公开接口里不能出现任何「收到几个赞」的数字。

    这条错了不会报错：某个人的受欢迎程度会静静地挂在页面上给所有人看。
    所以这里查的是**整个响应体**里有没有这三个键，而不是只看某一处。
    """
    _post(client, "/api/praise", {"target_openid": HER})
    # 换成对方来看我的资料页：我收到过几个赞，她一个都不该看见。
    c2 = app.app.test_client()
    c2.set_cookie("yxq_session", app.create_session(HER))
    body = c2.get(f"/api/users/{ME}/public").get_json()
    for leaked in ("praised_count", "received", "received_week", "praise_count"):
        assert leaked not in body


def test_the_public_profile_carries_praised_for_the_hint(client):
    """需求第 8 条要显示「要不要表示喜欢？」，所以「我点过赞没有」得给前端。"""
    assert client.get(f"/api/users/{HER}/public").get_json()["praised"] is False
    _post(client, "/api/praise", {"target_openid": HER})
    assert client.get(f"/api/users/{HER}/public").get_json()["praised"] is True


def test_a_cancelled_praise_clears_the_hint(client):
    _post(client, "/api/praise", {"target_openid": HER})
    _delete(client, f"/api/praise/{HER}")
    assert client.get(f"/api/users/{HER}/public").get_json()["praised"] is False


def test_my_praise_count_is_visible_to_me(client):
    """收到几个赞只在本人接口里出现。"""
    c2 = app.app.test_client()
    c2.set_cookie("yxq_session", app.create_session(HER))
    assert c2.get("/api/praise/me").get_json()["received_week"] == 0
    _post(client, "/api/praise", {"target_openid": HER})
    assert c2.get("/api/praise/me").get_json()["received_week"] == 1


def test_my_state_lists_who_i_praised(client, table):
    _post(client, "/api/praise", {"target_openid": HER})
    st = client.get("/api/praise/me").get_json()
    assert st["praised"] == [HER]
    assert st["used"] == 1 and st["limit"] == 10


def test_cancelling_removes_it_from_my_list(client):
    _post(client, "/api/praise", {"target_openid": HER})
    _delete(client, f"/api/praise/{HER}")
    assert client.get("/api/praise/me").get_json()["praised"] == []


# ---------------------------------------------------------------- 门禁

def test_reading_my_praise_needs_login():
    assert app.app.test_client().get("/api/praise/me").status_code == 401


def test_writing_needs_the_csrf_header(client):
    """跨站表单能带上 cookie，所以写接口额外要一个自定义头。"""
    assert client.post("/api/praise", json={"target_openid": HER}).status_code == 403


def test_praise_does_not_touch_the_points_ledger(client, points_db):
    """第 10 条：点赞不和麦穗挂钩。点十个赞，账本一行都不该多。"""
    from lib import points
    for _ in range(3):
        _post(client, "/api/praise", {"target_openid": HER})
    assert points.balance(ME) == 0


def test_the_week_bucket_matches_the_lib(client, table):
    """写进去的归属周必须是 lib 认的那个桶，否则周汇总会一条都数不到。"""
    _post(client, "/api/praise", {"target_openid": HER})
    week = table.rows[0]["fields"][app.F_PRAISE_WEEK]
    assert praise.received_by_week(_received(table, HER), week=week) == {HER: 1}
    assert week == praise.week_key(quota.now())
