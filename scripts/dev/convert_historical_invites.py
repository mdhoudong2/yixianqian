#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""历史邀请奖励一次性换算成麦穗 + 清零旧的「管理员加赠」额度字段。

背景：v7（2026-10-01）上线后麦穗账本是空的——上线前邀请好友拿到的
「爱心 / 实名名额」没有换算成麦穗。本脚本把历史邀请按麦穗规则
（女 20 / 男 15）一次性补发，并把用户表里旧的「管理员加赠」字段清零
（实名总量由 reconcile_hearts 在下一轮对账自动回到基础 1）。

换算口径（2026-10-02 与运营确认）：
  · 以旧计数 balances.json「invites」全量 46 人份为准；
  · 邀请对象性别能查到的按实际算，查不到的一律按男 15（与
    lib/points_invite.reward_amount 里「性别未知发低档」一致）；
  · 啊星(U-0022) 管理员指定固定 300 穗。

用法（在生产服务器 /opt/yixianqian 下）：
    cd /opt/yixianqian && YX_DEV_ALLOW=1 /opt/yixianqian/bot/venv/bin/python \
        scripts/dev/convert_historical_invites.py             # 干跑，只打印
    cd /opt/yixianqian && YX_DEV_ALLOW=1 /opt/yixianqian/bot/venv/bin/python \
        scripts/dev/convert_historical_invites.py --apply     # 真写（发穗 + 清零）

幂等：发穗用 idempotency_key=hist_invite_convert:<用户ID>，重跑不会重复发；
清零写 0，本就为 0 的记录跳过。

安全：干跑是默认；真写前若任一换算对象解析失败（找不到人 / 没绑飞书），
直接中止，一个穗都不发。
"""
import argparse
import os
import re
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_REPO, os.path.join(_REPO, "bot"), os.path.join(_REPO, "scripts", "dev")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _prod_guard import guard  # noqa: E402

guard(os.path.basename(__file__))

from clients import search_records, update_record  # noqa: E402
from constants import (  # noqa: E402
    ADMIN_OPEN_IDS,
    FIELD_FEISHU_ID,
    FIELD_NICKNAME,
    POINTS_DB_FILE,
    USER_TABLE_ID,
)
from lib import points, points_db  # noqa: E402
from lib.bitable_client import get_field_number, get_field_text  # noqa: E402

# 「管理员加赠」字段已于 2026-10 下线删除，constants 里不再有。这份一次性脚本
# 已经跑完，本地定义只是为了它还能被 import / 重跑，不依赖 constants。
FIELD_HEART_BONUS = "管理员加赠"

# 用户ID -> (预期昵称, 穗数, 说明)。仅此一份，改这里就是改发多少。
CONVERSION = [
    ("U-0022", "啊星",      300, "管理员指定固定 300"),
    ("U-0151", "Cici",      85,  "敏敏♀20 + 问了没啊♂15 + Kishi♀20 + 2 未知×15"),
    ("U-0164", "Kishi",     45,  "3 人，未知性别按男 15"),
    ("U-0093", "千百度",    30,  "joseph♂15 + 康古♂15"),
    ("U-0123", "Yoki冰淇淋", 40, "H♀20 + 小旋♀20"),
    ("U-0153", "琪琪",      30,  "2 人，未知性别按男 15"),
    ("U-0154", "问了没啊",  30,  "2 人，未知性别按男 15"),
    ("U-0004", "丹尼斯",    15,  "Alfred♂15"),
    ("U-0052", "心心",      15,  "Ignatius♂15"),
    ("U-0147", "敏敏",      20,  "琪琪♀20"),
]

IDEM_PREFIX = "hist_invite_convert:"
OPERATOR = (ADMIN_OPEN_IDS or [""])[0]  # 操作人写第一个管理员 open_id


def _uid_num(raw):
    """把「U-0022」/「0022」/「22」统一成数字 22，抗字段格式漂移。"""
    m = re.search(r"(\d+)", str(raw or ""))
    return int(m.group(1)) if m else None


def _oid_of(rec):
    return get_field_text(rec.get("fields", {}), FIELD_FEISHU_ID)


def _nick_of(rec):
    return get_field_text(rec.get("fields", {}), FIELD_NICKNAME)


def load_users():
    """全表扫一遍用户表，返回 (按 uid_num 建的主档, 管理员加赠非 0 的记录列表)。

    不靠「用户ID」字段做 filter 搜索：自动编号字段（type=1005）filter 不可靠，
    全表扫描 + 本地建索引最稳，反正清零也要扫全表。
    """
    all_users = search_records(USER_TABLE_ID)
    by_uid = {}
    bonus = []
    for u in all_users:
        f = u.get("fields", {})
        bonus_n = get_field_number(f, FIELD_HEART_BONUS, 0) or 0
        if bonus_n:
            bonus.append((u, bonus_n))
        num = _uid_num(get_field_text(f, "用户ID"))
        if num is not None and num not in by_uid:
            by_uid[num] = u  # 同号多档时保留第一条（用户ID本就唯一，这里兜底）
    return by_uid, bonus


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="真写（发穗 + 清零）。不带就是干跑，不落任何数据。")
    args = ap.parse_args()

    points_db.set_db_path(POINTS_DB_FILE)
    points_db.migrate()
    print(f"麦穗账本：{POINTS_DB_FILE}")
    print(f"模式：{'⚡ 真写' if args.apply else '🔍 干跑（不落任何数据）'}\n")

    by_uid, bonus = load_users()

    # ---- 发穗部分：解析 + 打印计划 ----
    print("== 发穗 ==")
    plan, resolve_errors = [], []
    for uid, expect_nick, amount, note in CONVERSION:
        rec = by_uid.get(_uid_num(uid))
        if rec is None:
            resolve_errors.append(f"{uid}：全表扫不到这个用户ID")
            print(f"  ❌ {uid}：扫不到这个用户ID")
            continue
        oid = _oid_of(rec)
        nick = _nick_of(rec)
        if not oid:
            resolve_errors.append(f"{uid}（{nick}）：还没绑定飞书账号")
            print(f"  ❌ {uid}（{nick}）：还没绑定飞书账号，无法发穗")
            continue
        nick_flag = "" if nick == expect_nick else f"  ⚠️ 昵称和预期「{expect_nick}」不一致"
        cur = points.balance(oid)
        print(f"  {uid} {nick:<8} +{amount:>3} 穗（{note}）｜当前余额 {cur}{nick_flag}")
        plan.append((uid, oid, nick, amount, note))

    total = sum(p[3] for p in plan)
    print(f"  小计：{len(plan)}/{len(CONVERSION)} 人，共 {total} 穗\n")

    # ---- 清零部分：管理员加赠非 0 的记录 ----
    print("== 清零「管理员加赠」字段 ==")
    grant_oids = {p[1] for p in plan}
    to_clear, stray = [], []
    for u, bonus_n in bonus:
        oid = _oid_of(u)
        nick = _nick_of(u)
        uid_text = get_field_text(u.get("fields", {}), "用户ID")
        if oid and oid in grant_oids:
            print(f"  {uid_text} {nick:<8} 管理员加赠 {bonus_n} → 0（实名总量下轮对账回到 1）")
            to_clear.append(u)
        else:
            stray.append((uid_text, nick, bonus_n))
            print(f"  ⚠️ {uid_text} {nick} 管理员加赠 {bonus_n}，但不在换算名单里，"
                  f"不会自动清零，请人工确认")
    if not bonus:
        print("  （没有非 0 的管理员加赠记录）")
    print()

    # ---- 安全闸：真写前必须全解析成功 ----
    if resolve_errors:
        print("❌ 有换算对象解析失败，中止（不写任何数据）：")
        for e in resolve_errors:
            print(f"  · {e}")
        return 1

    if not args.apply:
        print("以上是干跑结果。确认无误后加 --apply 真写。")
        return 0

    # ---- 真写：发穗 ----
    print("开始发穗…")
    for uid, oid, nick, amount, note in plan:
        key = f"{IDEM_PREFIX}{uid}"
        r = points.grant(oid, amount, points.KIND_ADMIN,
                         reason=f"历史邀请换算麦穗：{note}",
                         operator_oid=OPERATOR, idempotency_key=key)
        if r is None:
            print(f"  ⏭ {uid} {nick}：已发过（幂等键 {key}），跳过")
        else:
            print(f"  ✅ {uid} {nick} +{amount} 穗 → 余额 {r[1]}")

    # ---- 真写：清零 ----
    print("开始清零「管理员加赠」…")
    for u in to_clear:
        nick = _nick_of(u)
        if update_record(USER_TABLE_ID, u["record_id"], {FIELD_HEART_BONUS: 0}):
            print(f"  ✅ {nick} 管理员加赠 → 0")
        else:
            print(f"  ❌ {nick} 清零写入失败，请手动把「管理员加赠」改成 0")

    print("\n全部完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
