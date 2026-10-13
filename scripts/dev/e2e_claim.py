#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试服「注册→人工审核→机器人认领→自动报名」认领全链路 E2E（只允许在 /opt/yixianqian-test 执行）。

只依赖 H5 侧模块（app / config / bitable），用 Flask test_client 直接打
POST /api/auth/claim，monkeypatch 掉飞书 code 换 open_id（造不出真实飞书账号），
其余逻辑（预填记录匹配、锁定、自动报名、限流）全部走真实实现、真实测试 base。

场景：
  S1 已审核（单身）+ 已缴费：认领成功 → 自动报名 PROMO_ACTIVITY_ID，读回核对
     档案飞书ID 与报名表「已报名」；
  S2 待审核：认领成功但 auto_signed_up=false 且无 note、状态保持待审核、无报名；
  S3 幂等：S1 用户再次认领 → claimed=false、档案数不变、报名数不变；
  S4 错误路径：不存在手机号 404 / 同名同号两条 409 / 缺 code 400 / 手机号非法 400；
  S5 认领页可达：GET /?claim=1 → 200 且含认领面板关键串。

守卫（任一不满足即退出，绝不写入）：
  1) 仓库根 realpath 必须 = /opt/yixianqian-test；
  2) 必须显式 YIXIANQIAN_ENV=dev；
  3) BASE_TOKEN = 本仓库 bot/local_config.py 文本解析值，且 ≠ 生产
     /opt/yixianqian/bot/local_config.py 的值；
  4) USER_TABLE_ID / SIGNUP_TABLE_ID / ACTIVITY_TABLE_ID 必须等于测试 base
     已知值（与 seed_acceptance.py 的 _EXPECTED_TABLES 同源）。

噪音说明：测试服常驻机器人的轮询可能对造出的单身记录发通知（发给假 open_id 会
失败），属预期噪音；脚本收尾删除 S1/S2 造出的用户与全部报名记录并读回核对，
清理失败即非 0 退出。不触碰 A-0011 活动本体与任何其它既有数据。

用法（在 /opt/yixianqian-test 仓库根）：
  YIXIANQIAN_ENV=dev web/backend/venv/bin/python scripts/dev/e2e_claim.py
"""
import os
import re
import sys
import time

_D = os.path.dirname(os.path.abspath(__file__))
if _D not in sys.path:
    sys.path.insert(0, _D)

from _prod_guard import guard as _legacy_guard  # noqa: E402

_legacy_guard(os.path.basename(__file__))

_ROOT = os.path.dirname(os.path.dirname(_D))
_EXPECTED_ROOT = "/opt/yixianqian-test"
_PROD_CFG_PATH = "/opt/yixianqian/bot/local_config.py"
_EXPECTED_TABLES = {  # 与 scripts/dev/seed_acceptance.py 的 _EXPECTED_TABLES 同源
    "USER_TABLE_ID": "tblMFjHwrTlTqrKZ",
    "SIGNUP_TABLE_ID": "tblSUxXKbG7gdqVU",
    "ACTIVITY_TABLE_ID": "tblFg5jAXaHzUweF",
}

if os.path.realpath(_ROOT) != _EXPECTED_ROOT:
    print(f"⛔ 守卫1 失败：仓库根必须是 {_EXPECTED_ROOT}，实际 {os.path.realpath(_ROOT)}")
    sys.exit(1)
if os.environ.get("YIXIANQIAN_ENV", "prod") != "dev":
    print("⛔ 守卫2 失败：必须 YIXIANQIAN_ENV=dev（本脚本只测测试服）")
    sys.exit(1)

for _p in (os.path.join(_ROOT, "web", "backend"), _ROOT):
    sys.path.insert(0, _p)

import config as CFG  # noqa: E402
import app as h5_mod  # noqa: E402
from lib.bitable_client import get_field_text, get_phone_value, get_select_value  # noqa: E402

bitable = h5_mod.bitable

_issues = []


def _fail(msg):
    _issues.append(msg)
    print(f"  ⛔ {msg}")


def _parse_base(path):
    """从 local_config.py 文本解析 BASE_TOKEN（不 import，避免读到错误环境的配置）。"""
    with open(path, encoding="utf-8") as fh:
        m = re.search(r'^BASE_TOKEN\s*=\s*"([^"]+)"', fh.read(), re.M)
    return m.group(1) if m else None


def _guard_base_and_tables():
    print("✅ 守卫1 仓库根", os.path.realpath(_ROOT))
    print("✅ 守卫2 环境 YIXIANQIAN_ENV=dev")

    prod_base = _parse_base(_PROD_CFG_PATH)
    repo_base = _parse_base(os.path.join(_ROOT, "bot", "local_config.py"))
    if not prod_base:
        print(f"⛔ 守卫3 失败：无法从生产配置 {_PROD_CFG_PATH} 解析 BASE_TOKEN，无法自证非生产")
        sys.exit(1)
    if CFG.BASE_TOKEN == prod_base:
        print("⛔ 守卫3 失败：BASE_TOKEN 与生产 base 相同（拒绝连生产）")
        sys.exit(1)
    if CFG.BASE_TOKEN != repo_base:
        print("⛔ 守卫3 失败：BASE_TOKEN 与本仓库 bot/local_config.py 文本解析值不一致")
        sys.exit(1)
    print("✅ 守卫3 BASE_TOKEN = 本仓库测试值 且 ≠ 生产值")

    bad = [f"{k}={getattr(CFG, k)}≠{v}" for k, v in _EXPECTED_TABLES.items()
           if getattr(CFG, k) != v]
    if bad:
        print("⛔ 守卫4 失败：表 ID 不符（防生产 fallback 生效）：" + "；".join(bad))
        sys.exit(1)
    print(f"✅ 守卫4 {len(_EXPECTED_TABLES)} 个测试表 ID 全部匹配")


_TAG = f"{int(time.time()) % 100000000:08d}"
PHONE1 = f"199{_TAG}"                      # 11 位、全局唯一
NAME1 = f"E2E认领{_TAG[-6:]}"
PHONE2 = f"198{_TAG}"
NAME2 = f"E2E待审{_TAG[-6:]}"
PHONE4 = f"197{_TAG}"
NAME4 = f"E2E重复{_TAG[-6:]}"

OID1 = f"ou_e2e_claim_{_TAG}"
OID2 = f"ou_e2e_claim_pending_{_TAG}"
OID4A = f"ou_e2e_claim_404_{_TAG}"
OID4B = f"ou_e2e_claim_409_{_TAG}"
OID4C = f"ou_e2e_claim_nocode_{_TAG}"
OID4D = f"ou_e2e_claim_badphone_{_TAG}"
# S3 用 S1 的同一飞书身份复认（幂等），故 s3 映射到 OID1
CODE2OID = {"s1": OID1, "s2": OID2, "s3": OID1,
            "s4a": OID4A, "s4b": OID4B, "s4c": OID4C, "s4d": OID4D}

_created_users = []   # record_id
_created_dups = []    # record_id
_found_signups = []   # record_id


def _create_prefill(name, phone, status, paid=True):
    """造一条「飞书用户ID为空」的预填记录，返回 record_id（失败返回 None）。"""
    fields = {
        CFG.F_REAL_NAME: name,
        CFG.F_NICKNAME: name,
        CFG.F_PHONE: phone,  # 电话字段写入要求纯字符串（与 submit_register 同口径）
        CFG.F_ACCOUNT_STATUS: status,
    }
    if paid:
        fields[CFG.F_WECHAT_PAYMENT] = CFG.WECHAT_PAY_PAID
    rec = bitable.create_record(CFG.USER_TABLE_ID, fields)
    if not rec or not rec.get("record_id"):
        _fail(f"创建预填记录失败：{name}/{phone}")
        return None
    return rec["record_id"]


def _find_signups(oid, tries=3):
    """按活动+open_id 查有效报名，允许少量短重试等写入可见。"""
    for i in range(tries):
        rows = bitable.raw_search_records(CFG.SIGNUP_TABLE_ID, [
            {"field_name": CFG.F_SIGNUP_ACTIVITY_ID, "operator": "is",
             "value": [CFG.PROMO_ACTIVITY_ID]},
            {"field_name": CFG.F_SIGNUP_OPENID, "operator": "is", "value": [oid]},
        ])
        if rows is None:
            _fail(f"报名表查询失败（open_id={oid[:16]}…）")
            return []
        live = [r for r in rows
                if get_select_value(r.get("fields", {}), CFG.F_SIGNUP_STATUS) != "已取消"]
        if live:
            return live
        if i < tries - 1:
            time.sleep(1.2)
    return []


def _user_rows_by_oid(oid):
    rows = bitable.raw_search_records(CFG.USER_TABLE_ID, [
        {"field_name": CFG.F_FEISHU_ID, "operator": "is", "value": [oid]}])
    if rows is None:
        _fail(f"用户表按 open_id 查询失败：{oid[:16]}…")
        return None
    return rows


def main():
    _guard_base_and_tables()
    h5_mod._exchange_code_for_openid = lambda code: CODE2OID.get(code)
    h5_mod.touch_last_active = lambda *a, **k: None  # 避免后台写活跃时间与清理竞态
    client = h5_mod.app.test_client()

    def claim(code, phone, name):
        return client.post("/api/auth/claim",
                           json={"code": code, "phone": phone, "name": name},
                           headers={"X-Requested-With": "XMLHttpRequest"})

    try:
        # ---------- S1 已审核 + 已缴费 → 认领 + 自动报名 ----------
        print("\n[S1] 已审核（单身）+ 已缴费：认领并自动报名")
        rid1 = _create_prefill(NAME1, PHONE1, "单身", paid=True)
        if not rid1:
            return
        _created_users.append(rid1)
        r = claim("s1", PHONE1, NAME1)
        data = r.get_json() or {}
        if r.status_code != 200 or not data.get("claimed"):
            _fail(f"S1 认领失败 HTTP {r.status_code}: {data}")
        elif not data.get("auto_signed_up"):
            _fail(f"S1 未自动报名：{data}")
        else:
            print(f"  claimed=true / auto_signed_up=true（{data.get('user', {}).get('nickname')}）")

        live = bitable.get_record(CFG.USER_TABLE_ID, rid1) or {}
        if get_field_text(live.get("fields", {}), CFG.F_FEISHU_ID) != OID1:
            _fail("S1 档案读回：飞书用户ID 未绑定")
        else:
            print(f"  档案读回：飞书用户ID={OID1[:16]}… 已绑定")

        rows = _find_signups(OID1)
        if len(rows) == 1 and get_select_value(rows[0].get("fields", {}), CFG.F_SIGNUP_STATUS) == "已报名":
            _found_signups.append(rows[0]["record_id"])
            print(f"  报名表读回：{CFG.PROMO_ACTIVITY_ID}「已报名」1 条")
        else:
            _fail(f"S1 报名表读回异常：{len(rows)} 条（期望恰 1 条已报名）")

        # ---------- S2 待审核 → 不自动报名 ----------
        print("\n[S2] 待审核：认领成功但不自动报名")
        rid2 = _create_prefill(NAME2, PHONE2, "待审核", paid=True)
        if not rid2:
            return
        _created_users.append(rid2)
        r = claim("s2", PHONE2, NAME2)
        data = r.get_json() or {}
        if r.status_code != 200 or not data.get("claimed"):
            _fail(f"S2 认领失败 HTTP {r.status_code}: {data}")
        else:
            if data.get("auto_signed_up") is not False:
                _fail(f"S2 不应自动报名：{data}")
            elif data.get("auto_signed_note"):
                _fail(f"S2 待审核属正常跳过，不应带 note：{data.get('auto_signed_note')}")
            else:
                print("  claimed=true / auto_signed_up=false / 无 note")
        live = bitable.get_record(CFG.USER_TABLE_ID, rid2) or {}
        st = get_select_value(live.get("fields", {}), CFG.F_ACCOUNT_STATUS)
        if st != "待审核":
            _fail(f"S2 状态应保持待审核，实际 {st!r}")
        else:
            print("  状态保持「待审核」")
        if _find_signups(OID2, tries=1):
            _fail("S2 不应产生报名记录")
        else:
            print("  报名表无该 open_id 记录")

        # ---------- S3 幂等：再次认领 ----------
        print("\n[S3] 幂等：S1 用户再次认领")
        before = len(_find_signups(OID1, tries=1))
        r = claim("s3", PHONE1, NAME1)
        data = r.get_json() or {}
        if r.status_code != 200 or data.get("claimed") is not False:
            _fail(f"S3 应为幂等登录 HTTP {r.status_code}: {data}")
        else:
            print("  claimed=false（直接登录）")
        rows = _user_rows_by_oid(OID1)
        if rows is not None and len(rows) != 1:
            _fail(f"S3 档案数应保持 1，实际 {len(rows)}")
        else:
            print("  档案数不变（1 条）")
        after = len(_find_signups(OID1, tries=1))
        if after != before:
            _fail(f"S3 报名数变化：{before} -> {after}")
        else:
            print(f"  报名数不变（{after} 条）")

        # ---------- S4 错误路径 ----------
        print("\n[S4] 错误路径")
        r = claim("s4a", "19900000000", "E2E不存在用户")
        if r.status_code != 404:
            _fail(f"S4a 不存在手机号应 404，实际 {r.status_code}")
        else:
            print("  不存在手机号 → 404")
        dup1 = _create_prefill(NAME4, PHONE4, "待审核")
        dup2 = _create_prefill(NAME4, PHONE4, "待审核")
        if not dup1 or not dup2:
            return
        _created_dups.extend([dup1, dup2])
        r = claim("s4b", PHONE4, NAME4)
        if r.status_code != 409:
            _fail(f"S4b 同名同号两条应 409，实际 {r.status_code}")
        else:
            print("  同名同号两条 → 409")
        r = client.post("/api/auth/claim", json={"phone": PHONE4, "name": NAME4},
                        headers={"X-Requested-With": "XMLHttpRequest"})
        if r.status_code != 400:
            _fail(f"S4c 缺 code 应 400，实际 {r.status_code}")
        else:
            print("  缺 code → 400")
        r = claim("s4d", "12345", NAME4)
        if r.status_code != 400:
            _fail(f"S4d 手机号非法应 400，实际 {r.status_code}")
        else:
            print("  手机号非法 → 400")

        # ---------- S5 认领页可达 ----------
        print("\n[S5] 认领页 /?claim=1")
        r = client.get("/?claim=1")
        html = r.get_data(as_text=True)
        if r.status_code != 200:
            _fail(f"S5 认领页 HTTP {r.status_code}")
        elif "submitClaim" not in html:
            _fail("S5 认领页缺少 submitClaim 关键串")
        else:
            print("  HTTP 200 且含 submitClaim")
    finally:
        _cleanup(client)


def _cleanup(client):
    """删除 S1/S2 用户与全部测试报名记录，读回核对为空；失败 → 非 0 退出。"""
    print("\n[清理] 删除测试用户与报名记录")
    failed = []
    # 重查一轮 OID1/OID2 的报名（含异常路径多写的），全部删除后再核验
    signup_ids = {rid for rid in _found_signups if rid}
    for oid in (OID1, OID2):
        for r in _find_signups(oid, tries=1):
            signup_ids.add(r.get("record_id"))
    for rid in signup_ids:
        if not bitable.delete_record(CFG.SIGNUP_TABLE_ID, rid):
            failed.append(f"报名 {rid}")
    for rid in _created_users + _created_dups:
        if not bitable.delete_record(CFG.USER_TABLE_ID, rid):
            failed.append(f"用户 {rid}")

    # 读回核对：按四组 open_id / 三个手机号查询均应为空
    leftover = []
    for oid in (OID1, OID2, OID4A, OID4B, OID4C, OID4D):
        for r in (_user_rows_by_oid(oid) or []):
            leftover.append(f"用户 {r.get('record_id')} (open_id={oid[:16]}…)")
    all_users = bitable.raw_search_records(CFG.USER_TABLE_ID)
    if all_users is None:
        failed.append("用户表全量读回失败")
    else:
        phones = {PHONE1, PHONE2, PHONE4}
        for r in all_users:
            ph = re.sub(r"\D", "", get_phone_value(r.get("fields", {}), CFG.F_PHONE) or "")
            if ph and ph[-11:] in phones:
                leftover.append(f"用户 {r.get('record_id')} (phone={ph[-4:]})")
    for oid in (OID1, OID2):
        for r in _find_signups(oid, tries=1):
            leftover.append(f"报名 {r.get('record_id')} (open_id={oid[:16]}…)")

    if failed:
        _fail("清理删除失败：" + "；".join(failed))
    if leftover:
        _fail("清理核对发现残留：" + "；".join(leftover))
    if not failed and not leftover:
        print(f"  ✅ 已删除用户 {len(_created_users) + len(_created_dups)} 条 / "
              f"报名 {len(signup_ids)} 条；按 open_id/手机号读回均为空")


if __name__ == "__main__":
    main()
    print("\n" + ("✅ 认领全链路 E2E 全部通过" if not _issues else "⛔ 存在失败项"))
    if _issues:
        sys.exit(1)
