"""账本里的兑换单 → 多维表格的单据（`auto_tasks.sync_points_documents`）。

单据的真相在 SQLite，表格只是管理员看得见的那一面。这一层考的就是这一跳：
**字段名写错、或者读数时把列表当字典用，后果都是「什么都没有」——**
不报错、不重试，管理员那边只是永远看不到这张单子。

飞书接口全部打桩。真正要钉的是「写进去的字段名和值对不对」。
"""
import auto_tasks
from constants import (
    FIELD_MM_CONDITION,
    FIELD_MM_REDEMPTION_ID,
    FIELD_MM_STATUS,
    FIELD_MM_USER_NAME,
    FIELD_MM_USER_OPENID,
    FIELD_WISH_ACTIVITY_ID,
    FIELD_WISH_REDEMPTION_ID,
    FIELD_WISH_STATUS,
    FIELD_WISH_TARGET_NAME,
    FIELD_WISH_TARGET_OPENID,
    FIELD_WISH_USER_NAME,
    FIELD_WISH_USER_OPENID,
    WISH_STATUS_PENDING,
)

from lib import points, points_config, points_redeem

ACT = {"id": "A-0001", "title": "周末桌游", "quota": 10, "fee": 0,
       "start_at": "2026-10-01 19:00:00", "open": True}
USER = "ou_me"
TARGET = "ou_target"


def _fund(oid=USER, n=200):
    points.grant(oid, n, points.KIND_ADMIN, reason="测试预置")


def _doc_env(monkeypatch, *, users=None):
    """桩掉表 ID、建记录接口、用户表和「建过没有」的文件记账。"""
    written = []
    monkeypatch.setattr(auto_tasks, "WISH_TABLE_ID", "tbl_wish")
    monkeypatch.setattr(auto_tasks, "MATCHMAKER_ORDER_TABLE_ID", "tbl_mm")
    monkeypatch.setattr(auto_tasks, "create_record",
                        lambda tid, fields: written.append((tid, fields)) or {"record_id": "r1"})
    # find_user_by_openid 回的是**列表**，不是一条记录——照真实形状桩，
    # 否则「把列表当字典用」这个 bug 在测试里永远露不出来。
    monkeypatch.setattr(auto_tasks, "find_user_by_openid",
                        lambda oid: ([{"record_id": "rec_u",
                                       "fields": {"昵称": users[oid]}}]
                                     if oid in (users or {}) else []))
    seen = set()
    monkeypatch.setattr(auto_tasks, "reserve_notified",
                        lambda scope, key: key not in seen and not seen.add(key))
    monkeypatch.setattr(auto_tasks, "unreserve_notified",
                        lambda scope, key: seen.discard(key))
    return written


def test_a_wish_reaches_the_table_with_every_field_the_admin_edits(points_db,
                                                                   monkeypatch):
    """心愿单是管理员唯一能看见「谁想认识谁」的地方，字段缺一个就判不了。"""
    users = {USER: "小北", TARGET: "阿南"}
    written = _doc_env(monkeypatch, users=users)
    _fund()
    points_redeem.redeem_wish(USER, ACT, TARGET)

    assert auto_tasks.sync_points_documents() == 1
    tid, fields = written[0]
    assert tid == "tbl_wish"
    assert fields[FIELD_WISH_ACTIVITY_ID] == "A-0001"
    assert fields[FIELD_WISH_USER_OPENID] == USER
    assert fields[FIELD_WISH_USER_NAME] == "小北"      # 昵称查不到就只有 open_id 可看
    assert fields[FIELD_WISH_TARGET_OPENID] == TARGET
    assert fields[FIELD_WISH_TARGET_NAME] == "阿南"
    assert fields[FIELD_WISH_STATUS] == WISH_STATUS_PENDING
    assert fields[FIELD_WISH_REDEMPTION_ID]                # 表格改状态要能回写账本


def test_a_nickname_is_read_from_a_single_primary_record(points_db, monkeypatch):
    """`find_user_by_openid` 回列表。当字典用会炸在「查到了人」和「写单据」中间，
    单据一条都建不出来，只留一行循环异常日志。"""
    written = _doc_env(monkeypatch, users={USER: "小北"})
    _fund()
    points_redeem.redeem_wish(USER, ACT, TARGET)           # 对方不在用户表里

    assert auto_tasks.sync_points_documents() == 1
    assert written[0][1][FIELD_WISH_USER_NAME] == "小北"
    assert written[0][1][FIELD_WISH_TARGET_NAME] == ""     # 查不到就留空，不阻断建单


def test_a_matchmaker_order_reaches_the_table(points_db, monkeypatch):
    users = {USER: "小北"}
    written = _doc_env(monkeypatch, users=users)
    _fund()
    points_redeem.redeem_matchmaker(USER, "爱笑、能一起爬山")

    assert auto_tasks.sync_points_documents() == 1
    tid, fields = written[0]
    assert tid == "tbl_mm"
    assert fields[FIELD_MM_USER_OPENID] == USER
    assert fields[FIELD_MM_USER_NAME] == "小北"
    assert fields[FIELD_MM_CONDITION] == "爱笑、能一起爬山"
    assert fields[FIELD_MM_STATUS] == "待处理"
    assert fields[FIELD_MM_REDEMPTION_ID]


def test_each_order_is_written_once(points_db, monkeypatch):
    """循环 30 秒跑一次，不记账就会把同一张单子刷一屏。"""
    written = _doc_env(monkeypatch, users={USER: "小北"})
    _fund()
    points_redeem.redeem_wish(USER, ACT, TARGET)

    assert auto_tasks.sync_points_documents() == 1
    assert auto_tasks.sync_points_documents() == 0
    assert len(written) == 1


def test_a_refunded_order_is_not_written(points_db, monkeypatch):
    """退过穗的单子不该再出现在管理员的待办里。"""
    written = _doc_env(monkeypatch, users={USER: "小北"})
    _fund()
    order = points_redeem.redeem_wish(USER, ACT, TARGET)
    points_redeem.refund(order["id"], reason="测试")

    assert auto_tasks.sync_points_documents() == 0
    assert written == []


def test_an_unconfigured_table_is_skipped_quietly(points_db, monkeypatch):
    """表 ID 没填（生产还没建表）时不能炸循环，也不该硬写一个空表 ID。"""
    written = _doc_env(monkeypatch, users={USER: "小北"})
    monkeypatch.setattr(auto_tasks, "WISH_TABLE_ID", "")
    monkeypatch.setattr(auto_tasks, "MATCHMAKER_ORDER_TABLE_ID", "")
    _fund()
    points_redeem.redeem_wish(USER, ACT, TARGET)

    assert auto_tasks.sync_points_documents() == 0
    assert written == []


def test_a_failed_write_is_retried_next_round(points_db, monkeypatch):
    """飞书写入失败不能记成「建过了」——那这张单子就永远建不出来。"""
    written = _doc_env(monkeypatch, users={USER: "小北"})
    monkeypatch.setattr(auto_tasks, "create_record", lambda tid, fields: None)
    _fund()
    points_redeem.redeem_wish(USER, ACT, TARGET)

    assert auto_tasks.sync_points_documents() == 0

    monkeypatch.setattr(auto_tasks, "create_record",
                        lambda tid, fields: written.append((tid, fields)) or {"record_id": "r1"})
    assert auto_tasks.sync_points_documents() == 1


def test_the_wish_deadline_comes_from_config(points_db):
    """红娘单 14 天没处理就退穗，天数走配置（管理员能改）。"""
    assert int(points_config.get("matchmaker_deadline_days")) == 14
