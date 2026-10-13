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

噪音说明与自愈：测试服常驻 bot 会把「飞书用户ID为空且创建人非空」的预填记录
自动绑定到创建人（=应用身份），判定重复注册时还会直接删除记录；其抢绑用的是
查询时点的陈旧空ID列表、盲写更新，因此 claim 成功后刚绑定的记录也可能被覆写回
应用身份或整条删除。本脚本已内置自愈与容错：S1/S2 claim 404 时删除（容错）重建
同参数记录并重试一次；认领后绑定被覆写则就地恢复、记录被删则重建重认领一次；
S4b 未得 409 时重建两条重试一次；清理删除遇 RecordIdNotFound / 读回确认已不
存在视为「已被 bot 清理」不计失败；并按本脚本创建的 record_id 与生成的三个
姓名自清理绑定残留（data/yixianqian_bindings.json）与报名重试队列残留
（data/yixianqian_notified.json 的 signup_retry）。仍建议不要与其它写测试并发
运行。不触碰 A-0011 活动本体与任何其它既有数据。

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
_BINDING_FILE = os.path.join(_ROOT, "data", "yixianqian_bindings.json")
_NOTIFIED_FILE = os.path.join(_ROOT, "data", "yixianqian_notified.json")

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
from lib import storage  # noqa: E402
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


def _record_confirmed_gone(table_id, record_id):
    """复查记录是否确已不存在：get_record 取不到 + 全表查询成功且不含该 id。

    get_record 返回 None 无法区分「已删」与「瞬时查询失败」，所以再用一次成功的
    全表查询做阳性确认；全表查询也失败则返回 False（无法确认，不当作已删）。"""
    if bitable.get_record(table_id, record_id) is not None:
        return False
    rows = bitable.raw_search_records(table_id)
    if rows is None:
        return False
    return all(r.get("record_id") != record_id for r in rows)


def _delete_tolerant(table_id, record_id, desc):
    """删除记录，容忍「已被 bot 抢先清理」：删除失败但失败信息含 RecordIdNotFound，
    或读回确认记录已不存在 → 视为成功（打印说明，不计失败）。

    返回 True = 确认已删除 / 本就不存在；False = 删除失败且无法确认已不存在。"""
    captured = []
    client = getattr(bitable, "_client", None)
    old_logger = getattr(client, "_logger", None)
    if client is not None and old_logger is not None:
        client._logger = captured.append  # 暂时捕获 delete_record 记下的失败信息
    try:
        ok = bitable.delete_record(table_id, record_id)
    finally:
        if client is not None and old_logger is not None:
            client._logger = old_logger
    if ok:
        return True
    if any("RecordIdNotFound" in str(m) for m in captured):
        print(f"      ℹ️ {desc} {record_id} 已被 bot 清理（RecordIdNotFound），不计失败")
        return True
    if _record_confirmed_gone(table_id, record_id):
        print(f"      ℹ️ {desc} {record_id} 复查已不存在（bot 已清理），不计失败")
        return True
    return False


def _heal_stolen_prefill(label, rid, phone, name, status):
    """claim 返回 404 后的自愈：预填记录被 bot 抢绑（飞书用户ID 已非空）或已被
    删除时，删除残留（容错）并重建同参数预填记录。返回新 record_id；无法或无需
    重建时返回 None。"""
    live = bitable.get_record(CFG.USER_TABLE_ID, rid)
    if live is None:
        if not _record_confirmed_gone(CFG.USER_TABLE_ID, rid):
            print(f"    ⚠️ {label} 预填记录 {rid} 读回失败且无法确认已删，放弃自愈")
            return None
        print(f"    ⚠️ {label} 预填记录 {rid} 已被 bot 删除，重建后重试")
    else:
        bound = get_field_text(live.get("fields", {}), CFG.F_FEISHU_ID).strip()
        if not bound:
            print(f"    ⚠️ {label} 预填记录 {rid} 仍在且飞书用户ID为空，404 原因不明，放弃自愈")
            return None
        print(f"    ⚠️ {label} 预填记录 {rid} 已被 bot 抢绑"
              f"（飞书用户ID={bound[:16]}…），删除后重建重试")
        _delete_tolerant(CFG.USER_TABLE_ID, rid, f"{label} 被抢绑记录")
    new_rid = _create_prefill(name, phone, status, paid=True)
    if not new_rid:
        return None
    _created_users.append(new_rid)
    return new_rid


def _wait_snap_users_clear(expect_oid, timeout=8.0):
    """自愈重认领前，等应用内「用户快照」不再映射到 expect_oid。

    claim 的幂等分支先查内存快照（残留已删/被覆写的旧记录会让重认领误判
    「已有档案」返回 claimed=false）。触发一次真抓（coalesce=0）并轮询等待交换；
    快照本就为空时实时兜底立即返回。返回 True=已清；超时返回 False。"""
    try:
        h5_mod.refresh_snapshot_table_async("users")
    except Exception as e:                                    # noqa: BLE001
        print(f"    ⚠️ 触发用户快照刷新失败：{e}")
        return False
    end = time.time() + timeout
    while time.time() < end:
        if h5_mod.snap_find_user_by_openid(expect_oid) is None:
            return True
        time.sleep(0.5)
    print(f"    ⚠️ 用户快照 {timeout:.0f}s 内仍映射到 {expect_oid[:16]}…，仍尝试重认领")
    return False


def _claim_healed(claim, code, phone, name, rid, status, label):
    """发起 claim；404 时自愈（记录被 bot 抢绑/删除 → 重建后重试一次）。

    身份级限流 8/600：每次自愈只增加一次 claim 调用；极端情况（404 自愈与认领后
    被删自愈各触发一次）OID1 全程 ≤4 次、OID2 ≤3 次，OID4B ≤2 次，均 ≤6。返回
    (resp, data, rid, reported)；reported=True 表示已在内部按「被 bot 抢绑/干扰」
    报错，调用方勿重复报。"""
    r = claim(code, phone, name)
    data = r.get_json() or {}
    if r.status_code != 404:
        return r, data, rid, False
    print(f"    ⚠️ {label} claim 返回 404，检查预填记录是否被常驻 bot 抢绑/删除")
    new_rid = _heal_stolen_prefill(label, rid, phone, name, status)
    if not new_rid:
        return r, data, rid, False
    r = claim(code, phone, name)
    data = r.get_json() or {}
    if r.status_code != 404:
        return r, data, new_rid, False
    _fail(f"{label} 重建预填记录后 claim 仍 404：被 bot 抢绑/干扰")
    return r, data, new_rid, True


def _repair_clobbered_binding(rid, expect_oid, label):
    """认领后绑定被 bot 覆写回应用身份 → 就地恢复为 expect_oid（记录/报名不动）。

    只在读回确认「记录存在且飞书用户ID非空且 ≠ 本人」时修复；返回 True=已恢复。"""
    live = bitable.get_record(CFG.USER_TABLE_ID, rid)
    if live is None:
        return False
    bound = get_field_text(live.get("fields", {}), CFG.F_FEISHU_ID).strip()
    if not bound or bound == expect_oid:
        return False
    print(f"    ⚠️ {label} 认领后绑定被 bot 覆写（{bound[:16]}…），就地恢复为本人")
    fixed = bitable.update_record(CFG.USER_TABLE_ID, rid, {CFG.F_FEISHU_ID: expect_oid})
    after = get_field_text((bitable.get_record(CFG.USER_TABLE_ID, rid) or {}).get("fields", {}),
                           CFG.F_FEISHU_ID).strip()
    if fixed and after == expect_oid:
        print(f"    （{label} 绑定已恢复；记录与报名未重建）")
        return True
    print(f"    ⚠️ {label} 就地恢复失败（读回 {after[:16] or '记录不存在'}）")
    return False


def _heal_post_claim_interference(claim, code, phone, name, status, rid, expect_oid, label):
    """claim 成功后的兜底：bot 用抢绑前查到的陈旧空ID列表盲写，可能把刚认领成功的
    记录覆写回应用身份，或按重复注册把整条记录删掉。

    - 仅绑定被覆写（记录还在）→ 就地恢复绑定，记录/报名不动（确定性，无重建窗口）；
    - 记录被删 → 删旧（容错）重建同参数记录并重认领一次（同 _claim_healed）。

    返回可能更新后的 record_id；无法确认/修复时不硬来，交由调用方断言暴露。"""
    live = bitable.get_record(CFG.USER_TABLE_ID, rid)
    bound = get_field_text((live or {}).get("fields", {}), CFG.F_FEISHU_ID).strip()
    if live is not None and bound == expect_oid:
        return rid
    if live is not None and bound:
        if not _repair_clobbered_binding(rid, expect_oid, label):
            _fail(f"{label} 认领后绑定被 bot 覆写且就地恢复失败")
        return rid
    if live is not None and not bound:
        print(f"    ⚠️ {label} 认领后记录仍在但飞书用户ID为空（异常），跳过自愈")
        return rid
    if not _record_confirmed_gone(CFG.USER_TABLE_ID, rid):
        print(f"    ⚠️ {label} 认领后读回失败且无法确认已删，跳过自愈")
        return rid
    print(f"    ⚠️ {label} 认领后记录被 bot 删除，重建后重认领一次")
    new_rid = _heal_stolen_prefill(label, rid, phone, name, status)
    if not new_rid:
        _fail(f"{label} 认领后被 bot 删除且自愈重建失败")
        return rid
    _wait_snap_users_clear(expect_oid)
    r = claim(code, phone, name)
    data = r.get_json() or {}
    if r.status_code != 200 or not data.get("claimed"):
        _fail(f"{label} 自愈重认领失败 HTTP {r.status_code}: {data}（被 bot 抢绑/干扰）")
        return new_rid
    live2 = bitable.get_record(CFG.USER_TABLE_ID, new_rid)
    bound2 = get_field_text((live2 or {}).get("fields", {}), CFG.F_FEISHU_ID).strip()
    if bound2 != expect_oid:
        _fail(f"{label} 自愈重认领后绑定仍不符（{bound2[:16] or '记录不存在'}）：被 bot 抢绑/干扰")
    else:
        print(f"    （{label} 已重建记录并重新认领成功，报名/状态由后续读回核对）")
    return new_rid


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
        r, data, rid1, reported = _claim_healed(claim, "s1", PHONE1, NAME1, rid1, "单身", "S1")
        if reported:
            pass  # 已按「被 bot 抢绑/干扰」报错，不再重复报
        elif r.status_code != 200 or not data.get("claimed"):
            _fail(f"S1 认领失败 HTTP {r.status_code}: {data}")
        elif not data.get("auto_signed_up"):
            _fail(f"S1 未自动报名：{data}")
        else:
            print(f"  claimed=true / auto_signed_up=true（{data.get('user', {}).get('nickname')}）")

        rid1 = _heal_post_claim_interference(
            claim, "s1", PHONE1, NAME1, "单身", rid1, OID1, "S1")

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
        r, data, rid2, reported = _claim_healed(claim, "s2", PHONE2, NAME2, rid2, "待审核", "S2")
        if reported:
            pass  # 已按「被 bot 抢绑/干扰」报错，不再重复报
        elif r.status_code != 200 or not data.get("claimed"):
            _fail(f"S2 认领失败 HTTP {r.status_code}: {data}")
        else:
            if data.get("auto_signed_up") is not False:
                _fail(f"S2 不应自动报名：{data}")
            elif data.get("auto_signed_note"):
                _fail(f"S2 待审核属正常跳过，不应带 note：{data.get('auto_signed_note')}")
            else:
                print("  claimed=true / auto_signed_up=false / 无 note")
        rid2 = _heal_post_claim_interference(
            claim, "s2", PHONE2, NAME2, "待审核", rid2, OID2, "S2")
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
        if r.status_code == 409:
            print("  同名同号两条 → 409")
        else:
            print(f"    ⚠️ S4b 未得到 409（HTTP {r.status_code}），"
                  f"dup 记录疑被 bot 删除/抢绑，重建两条后重试一次")
            for d in (dup1, dup2):
                if bitable.get_record(CFG.USER_TABLE_ID, d) is not None:
                    _delete_tolerant(CFG.USER_TABLE_ID, d, "S4b 旧重复记录")  # 真失败由清理阶段兜底
            # 若 claim 曾成功绑过 OID4B，先等用户快照清掉该记录，防幂等分支误判（同自愈重认领）
            _wait_snap_users_clear(OID4B)
            dup1 = _create_prefill(NAME4, PHONE4, "待审核")
            dup2 = _create_prefill(NAME4, PHONE4, "待审核")
            if not dup1 or not dup2:
                _fail("S4b 重建两条重复预填记录失败")
            else:
                _created_dups.extend([dup1, dup2])
                r = claim("s4b", PHONE4, NAME4)
                if r.status_code != 409:
                    _fail(f"S4b 重建两条后仍非 409，实际 {r.status_code}（防重复校验被 bot 干扰）")
                else:
                    print("  同名同号两条 → 409（重建后重试）")
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


def _cleanup_bindings():
    """自清理 bot 抢绑留下的绑定残留（data/yixianqian_bindings.json，lib.storage 原子读改写）。

    删除满足任一条件的条目：① value["record_id"] ∈ 本脚本创建过的用户记录 id 集合；
    ② value["nickname"] 恰等于本脚本生成的三个姓名之一（NAME1/NAME2/NAME4）。
    无匹配条目时零写入。"""
    ours_ids = {rid for rid in _created_users + _created_dups if rid}
    ours_names = {NAME1, NAME2, NAME4}
    seen = {}

    def _m(data):
        if not isinstance(data, dict):
            data = {}
        seen["before"] = len(data)
        stale = [k for k, v in data.items()
                 if isinstance(v, dict)
                 and (v.get("record_id") in ours_ids or v.get("nickname") in ours_names)]
        if not stale:
            seen["after"] = len(data)
            return None  # 零写入
        for k in stale:
            data.pop(k, None)
        seen["after"] = len(data)
        seen["removed"] = len(stale)
        return data

    storage.update_json(_BINDING_FILE, {}, _m)
    print(f"  绑定残留：清理前 {seen.get('before', '?')} 条 / 清理后 "
          f"{seen.get('after', '?')} 条（删除本脚本条目 {seen.get('removed', 0)} 条）")


def _cleanup_signup_retry():
    """自清理报名重试队列残留（data/yixianqian_notified.json 的 signup_retry）。

    bot 可能把被抢绑记录加进队列；用 lib.storage 原子删除 record_id ∈ 本脚本创建
    集合的条目；无匹配条目时零写入。"""
    ours_ids = {rid for rid in _created_users + _created_dups if rid}
    seen = {}

    def _m(data):
        if not isinstance(data, dict):
            data = {}
        q = data.get("signup_retry") or {}
        seen["before"] = len(q)
        stale = [rid for rid in list(q) if rid in ours_ids]
        if not stale:
            seen["after"] = len(q)
            return None  # 零写入
        for rid in stale:
            q.pop(rid, None)
        data["signup_retry"] = q
        seen["after"] = len(q)
        seen["removed"] = len(stale)
        return data

    storage.update_json(_NOTIFIED_FILE, {}, _m)
    print(f"  报名重试队列残留：清理前 {seen.get('before', '?')} 条 / 清理后 "
          f"{seen.get('after', '?')} 条（删除本脚本条目 {seen.get('removed', 0)} 条）")


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
        if not _delete_tolerant(CFG.SIGNUP_TABLE_ID, rid, "报名"):
            failed.append(f"报名 {rid}")
    for rid in _created_users + _created_dups:
        if not _delete_tolerant(CFG.USER_TABLE_ID, rid, "用户"):
            failed.append(f"用户 {rid}")

    # 文件残留自清理（bot 抢绑/删除的副产物，失败不应掩盖记录清理结果但需报出）
    try:
        _cleanup_bindings()
    except Exception as e:                                   # noqa: BLE001
        _fail(f"绑定残留清理异常：{e}")
    try:
        _cleanup_signup_retry()
    except Exception as e:                                   # noqa: BLE001
        _fail(f"报名重试队列残留清理异常：{e}")

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
