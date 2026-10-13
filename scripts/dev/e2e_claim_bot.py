#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试服「注册→审核→机器人认领→自动报名」bot 侧端到端测试（只允许在 /opt/yixianqian-test 执行）。

用法（仓库根，必须显式 YIXIANQIAN_ENV=dev；bot venv 无 pytest，本脚本只依赖运行时）：
  YIXIANQIAN_ENV=dev bot/venv/bin/python scripts/dev/e2e_claim_bot.py

覆盖 FIX-A 的真实链路（真实测试 base，全部读写后清理）：
  S1 auto_signup_new_user 真集成：造用户（假 open_id、单身、微信缴费=我已缴费）→
     真活动 AUTO_SIGNUP_ACTIVITY_ID（测试值 A-0011「报名中」）→ 报名成功且报名表
     出现记录 → 再调一次 False（幂等）；
  S2 _auto_signup_new_user_ex：同记录缴费清空 → 返回 skip_not_paid；
  S3 重试队列真集成：add_signup_retry(record_id) → process_signup_retries()
     → 真正建出报名记录且队列条目被清除；
  S4 清理：删本脚本创建的全部报名/用户记录，读回核对无残留。

守卫（仿 scripts/dev/seed_acceptance.py + e2e_onetoone_h5.py）：
  1) 仓库根 realpath 必须 = /opt/yixianqian-test；
  2) 必须显式 YIXIANQIAN_ENV=dev；
  3) BASE_TOKEN 必须来自本仓库 bot/local_config.py 且 ≠ 生产 /opt/yixianqian 的值。

任何一步失败退出非 0；所有测试数据用 ou_fake_e2e_claim_ 前缀，跑完必清。
"""
import os
import re
import sys
import time

_D = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_D))
_EXPECTED_ROOT = "/opt/yixianqian-test"
_PROD_CFG = "/opt/yixianqian/bot/local_config.py"
_OID_PREFIX = "ou_fake_e2e_claim_"
_NICK = "E2E认领自动报名"

_issues = []


def _fail(msg):
    _issues.append(msg)
    print(f"⛔ {msg}")


def _ok(msg):
    print(f"✅ {msg}")


class _Abort(Exception):
    """读取失败等硬错误：跳到清理与汇总。"""


def _parse_base(path):
    try:
        with open(path, encoding="utf-8") as fh:
            m = re.search(r'^BASE_TOKEN\s*=\s*"([^"]+)"', fh.read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


# ---------------------------------------------------------------- 守卫
if os.path.realpath(_ROOT) != _EXPECTED_ROOT:
    print(f"⛔ 守卫1 失败：仓库根必须是 {_EXPECTED_ROOT}，实际 {os.path.realpath(_ROOT)}")
    sys.exit(1)
print(f"✅ 守卫1 仓库根 {_EXPECTED_ROOT}")

if os.environ.get("YIXIANQIAN_ENV", "prod") != "dev":
    print("⛔ 守卫2 失败：必须显式 YIXIANQIAN_ENV=dev（本脚本只测测试服）")
    sys.exit(1)
print("✅ 守卫2 YIXIANQIAN_ENV=dev")

sys.path.insert(0, os.path.join(_ROOT, "bot"))
sys.path.insert(0, _ROOT)

import auto_tasks  # noqa: E402
import clients as bot_clients  # noqa: E402
import constants as C  # noqa: E402
import store  # noqa: E402
from lib.bitable_client import (  # noqa: E402
    get_field_number, get_field_text, get_select_value)

repo_base = _parse_base(os.path.join(_ROOT, "bot", "local_config.py"))
prod_base = _parse_base(_PROD_CFG)
if not repo_base:
    print("⛔ 守卫3 失败：无法从本仓库 bot/local_config.py 解析 BASE_TOKEN")
    sys.exit(1)
if not prod_base:
    print(f"⛔ 守卫3 失败：无法从生产配置 {_PROD_CFG} 解析 BASE_TOKEN，无法自证非生产，拒绝执行")
    sys.exit(1)
if C.BASE_TOKEN == prod_base:
    print("⛔ 守卫3 失败：BASE_TOKEN 与生产相同（拒绝连生产）")
    sys.exit(1)
if C.BASE_TOKEN != repo_base:
    print("⛔ 守卫3 失败：constants.BASE_TOKEN 与本仓库 bot/local_config.py 文本解析值不一致")
    sys.exit(1)
if not os.path.realpath(C.__file__).startswith(_EXPECTED_ROOT + os.sep):
    print("⛔ 守卫3 失败：constants 解析自非本仓库")
    sys.exit(1)
print(f"✅ 守卫3 BASE_TOKEN 来自本仓库 local_config 且≠生产"
      f"（test={repo_base[:8]}… prod={prod_base[:8]}…）")

bitable = bot_clients.bitable
ACT = C.AUTO_SIGNUP_ACTIVITY_ID
ACT_ID_TEXT = ""  # 活动记录里读到的活动ID（报名表里同值），S0 里填充
_created_users = {}  # user record_id -> open_id


def _must(recs, what):
    if recs is None:
        _fail(f"读取失败：{what}（飞书查询返回错误，中止）")
        raise _Abort()
    return recs


def _signups_of(oid):
    return _must(bitable.search_records(C.SIGNUP_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": C.FIELD_SIGNUP_ACTIVITY_ID, "operator": "is",
             "value": [ACT_ID_TEXT]},
            {"field_name": C.FIELD_SIGNUP_OPENID, "operator": "is", "value": [oid]},
        ]}), f"报名表(o_id={oid[:14]}…)")


def _sweep_prefix():
    """清掉上次异常退出的残留（仅本脚本前缀），返回删除条数。"""
    removed = 0
    for s in _must(bitable.search_records(C.SIGNUP_TABLE_ID), "报名表"):
        oid = get_field_text(s.get("fields", {}), C.FIELD_SIGNUP_OPENID)
        if oid.startswith(_OID_PREFIX):
            if bitable.delete_record(C.SIGNUP_TABLE_ID, s["record_id"]):
                removed += 1
            else:
                _fail(f"预清理删除报名失败: {s['record_id']}")
    for u in _must(bitable.search_records(C.USER_TABLE_ID), "用户表"):
        oid = get_field_text(u.get("fields", {}), C.FIELD_FEISHU_ID)
        if oid.startswith(_OID_PREFIX):
            if bitable.delete_record(C.USER_TABLE_ID, u["record_id"]):
                removed += 1
            else:
                _fail(f"预清理删除用户失败: {u['record_id']}")
    return removed


def _new_user(oid):
    rec = bitable.create_record(C.USER_TABLE_ID, {
        C.FIELD_FEISHU_ID: oid,
        C.FIELD_NICKNAME: _NICK,
        C.FIELD_ACCOUNT_STATUS: "单身",
        C.FIELD_WECHAT_PAYMENT: C.WECHAT_PAY_PAID,
    })
    if not rec:
        _fail(f"创建用户记录失败: {oid}")
        raise _Abort()
    _created_users[rec["record_id"]] = oid
    return rec["record_id"]


def _run_scenarios():
    global ACT_ID_TEXT

    # ---------- S0：前置检查 ----------
    print(f"\n[S0] 前置：活动 {ACT}")
    act, failed = auto_tasks._find_activity_raw(ACT)
    if failed:
        _fail(f"活动 {ACT} 查询失败（瞬时错误）")
        raise _Abort()
    if not act:
        _fail(f"活动 {ACT} 不存在")
        raise _Abort()
    act_fields = act.get("fields", {})
    ACT_ID_TEXT = get_field_text(act_fields, C.FIELD_ACTIVITY_ID)
    status = get_select_value(act_fields, C.FIELD_ACTIVITY_STATUS)
    capacity = get_field_number(act_fields, "报名人数上限", 0)
    print(f"   活动 {ACT_ID_TEXT} 状态「{status}」上限 {capacity or '不限'}")
    if status != C.STATUS_SIGNUP_OPEN:
        _fail(f"活动状态「{status}」非「{C.STATUS_SIGNUP_OPEN}」，无法验证成功路径")
        raise _Abort()
    taken = _must(bitable.search_records(C.SIGNUP_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": C.FIELD_SIGNUP_ACTIVITY_ID, "operator": "is",
             "value": [ACT_ID_TEXT]},
            {"field_name": C.FIELD_SIGNUP_STATUS, "operator": "is", "value": ["已报名"]},
        ]}), "报名表")
    if capacity and len(taken) >= capacity:
        _fail(f"活动已满员 {len(taken)}/{capacity}，无法验证成功路径")
        raise _Abort()
    swept = _sweep_prefix()
    _ok(f"前置通过（已报名 {len(taken)}/{capacity or '不限'}；清理残留 {swept} 条）")

    ts = int(time.time())
    oid1 = f"{_OID_PREFIX}{ts}_a"
    oid2 = f"{_OID_PREFIX}{ts}_b"

    # ---------- S1：真集成 + 幂等 ----------
    print("\n[S1] auto_signup_new_user：创建 → 幂等")
    rid1 = _new_user(oid1)
    first = auto_tasks.auto_signup_new_user(oid1, _NICK, C.WECHAT_PAY_PAID)
    rows1 = _signups_of(oid1)
    if first is True and rows1:
        _ok(f"首次报名 True，报名表出现 {len(rows1)} 条记录（user record_id={rid1}）")
    else:
        _fail(f"首次报名异常：return={first!r} 报名记录={len(rows1)} 条")

    second = auto_tasks.auto_signup_new_user(oid1, _NICK, C.WECHAT_PAY_PAID)
    code = auto_tasks._auto_signup_new_user_ex(oid1, _NICK, C.WECHAT_PAY_PAID)
    if second is False and code == "already":
        _ok("幂等：第二次调用 False（already），未产生重复报名")
    else:
        _fail(f"幂等失败：second={second!r} code={code!r}")

    # ---------- S2：缴费清空 → skip_not_paid ----------
    print("\n[S2] _auto_signup_new_user_ex：缴费清空 → skip_not_paid")
    upd = bitable.update_record(C.USER_TABLE_ID, rid1, {C.FIELD_WECHAT_PAYMENT: None})
    back = bitable.get_record(C.USER_TABLE_ID, rid1)
    pay = get_select_value((back or {}).get("fields", {}), C.FIELD_WECHAT_PAYMENT)
    code = auto_tasks._auto_signup_new_user_ex(oid1, _NICK, pay)
    if upd and not pay and code == "skip_not_paid":
        _ok(f"缴费字段已清空（readback={pay!r}）且返回 skip_not_paid")
    else:
        _fail(f"期望清空后 skip_not_paid，实际 update={bool(upd)} pay={pay!r} code={code!r}")

    # ---------- S3：重试队列真集成 ----------
    print("\n[S3] 重试队列：add_signup_retry → process_signup_retries")
    rid2 = _new_user(oid2)
    store.add_signup_retry(rid2)
    if store.get_signup_retry(rid2) != 0:
        _fail("入队后 attempts 应为 0")
    queue_before = store.list_signup_retries(1000)
    print(f"   处理前队列 {len(queue_before)} 条；本次加入 record_id={rid2}")
    auto_tasks.process_signup_retries(limit=200)
    rows2 = _signups_of(oid2)
    still_queued = any(r == rid2 for r, _ in store.list_signup_retries(1000))
    if rows2 and not still_queued:
        _ok(f"补偿成功：报名表出现 {len(rows2)} 条记录，队列条目已清除")
    else:
        _fail(f"补偿失败：报名记录={len(rows2)} 条，仍排队={still_queued}")


def _cleanup_and_verify():
    print("\n[S4] 清理 + 读回核对")
    # 队列：清掉本脚本记录 id 的残留条目（正常情况下 process 已清）
    queued = {r for r, _ in store.list_signup_retries(1000)}
    for rid in list(_created_users):
        if rid in queued:
            store.clear_signup_retry(rid)
    # 报名：删同前缀全部
    left = _must(bitable.search_records(C.SIGNUP_TABLE_ID), "报名表")
    deleted = 0
    for s in left:
        oid = get_field_text(s.get("fields", {}), C.FIELD_SIGNUP_OPENID)
        if oid.startswith(_OID_PREFIX):
            if bitable.delete_record(C.SIGNUP_TABLE_ID, s["record_id"]):
                deleted += 1
            else:
                _fail(f"清理删除报名失败: {s['record_id']}")
    # 用户：删本次创建的
    for rid in list(_created_users):
        if bitable.get_record(C.USER_TABLE_ID, rid) is None:
            continue
        if not bitable.delete_record(C.USER_TABLE_ID, rid):
            _fail(f"清理删除用户失败: {rid}")

    # 读回核对
    left_signups = [s for s in _must(bitable.search_records(C.SIGNUP_TABLE_ID), "报名表")
                    if get_field_text(s.get("fields", {}), C.FIELD_SIGNUP_OPENID)
                    .startswith(_OID_PREFIX)]
    left_users = [u for u in _must(bitable.search_records(C.USER_TABLE_ID), "用户表")
                  if get_field_text(u.get("fields", {}), C.FIELD_FEISHU_ID)
                  .startswith(_OID_PREFIX)]
    left_queue = [r for r, _ in store.list_signup_retries(1000)
                  if r in _created_users]
    if left_signups or left_users or left_queue:
        _fail(f"清理不干净：报名 {len(left_signups)} / 用户 {len(left_users)} / 队列 {left_queue}")
    else:
        _ok(f"清理核对通过：报名 0 / 用户 0 / 队列 0（本轮删除报名 {deleted} 条）")


def main():
    print("== FIX-A bot 侧端到端（测试 base）==")
    try:
        _run_scenarios()
    except _Abort:
        pass
    finally:
        _cleanup_and_verify()
    if _issues:
        print(f"\n=== 汇总：{len(_issues)} 项失败 ===")
        for m in _issues:
            print(f"⛔ {m}")
        sys.exit(1)
    print("\n=== 汇总：全部通过 ✅ ===")


if __name__ == "__main__":
    main()
