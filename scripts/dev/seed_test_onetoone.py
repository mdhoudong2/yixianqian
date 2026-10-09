#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试环境「一对一」端到端种子 + 执行 + 读回验证（只允许在 /opt/yixianqian-test 执行）。

复用 A-0011 的报名（U-0006 真号 + 8 假男 + 7 假女 = 16 人，8 男 8 女，与
seed_test_grouping.py 同一批人），给每人预填 7 个异性志愿（轮转法），然后走真实
bot 命令链：handle_admin_start_onetoone → handle_admin_stop_onetoone（内部
_do_onetoone 做匹配并写结果），最后读回结果表逐条验证。

端到端要覆盖的核心断言：
- 16 人 × 每人 8 个异性 = 128 条结果行（异性 8 < top_n 10，有多少算多少）；
- 每人名单里 7 个志愿必居前 7 且顺序=志愿优先级；
- 无自己、无同性、排名 1..8 无重复。

用法（在 /opt/yixianqian-test 仓库根，必须显式 YIXIANQIAN_ENV=dev）：
  YIXIANQIAN_ENV=dev python3 scripts/dev/seed_test_onetoone.py plan    # 只打印计划，不写
  YIXIANQIAN_ENV=dev python3 scripts/dev/seed_test_onetoone.py run     # 种子+执行+读回验证
  YIXIANQIAN_ENV=dev python3 scripts/dev/seed_test_onetoone.py verify  # 只读复核结果表

守卫同 seed_test_grouping.py：仓库根必须 /opt/yixianqian-test、显式 YIXIANQIAN_ENV=dev、
BASE_TOKEN≠生产、5 张表 ID 必须=测试 base 已知值（防 constants 生产 fallback 生效）。
"""
import os
import re
import sys

_D = os.path.dirname(os.path.abspath(__file__))
if _D not in sys.path:
    sys.path.insert(0, _D)

from _prod_guard import guard as _legacy_guard  # noqa: E402

_legacy_guard(os.path.basename(__file__))

_EXPECTED_ROOT = "/opt/yixianqian-test"
_PROD_CFG_PATH = "/opt/yixianqian/bot/local_config.py"
_ACTIVITY_ID = "A-0011"
_EXPECTED_TABLES = {
    "USER_TABLE_ID": "tblMFjHwrTlTqrKZ",
    "ACTIVITY_TABLE_ID": "tblFg5jAXaHzUweF",
    "SIGNUP_TABLE_ID": "tblSUxXKbG7gdqVU",
    "ONETOONE_SELECT_TABLE": "tblTRXtuLhODm8Q0",
    "ONETOONE_RESULT_TABLE": "tbllYrqHD81gHyI2",
}
_TOP_N = 10
_FIELD_USER_ID = "用户ID"

C = None
bitable = None
get_field_text = get_select_value = get_field_number = None


def _fail(msg):
    print(f"⛔ {msg}")
    sys.exit(1)


def _mask(oid, keep=8):
    oid = oid or ""
    return oid[:keep] + "…" if len(oid) > keep else oid


def _parse_base(path):
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        _fail(f"无法读取 {path}（{exc}）")
    m = re.search(r'^BASE_TOKEN\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else None


def _guard_and_import():
    global C, bitable, get_field_text, get_select_value, get_field_number

    real_root = os.path.realpath(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    if real_root != _EXPECTED_ROOT:
        _fail(f"守卫1 失败：仓库根必须是 {_EXPECTED_ROOT}，实际 {real_root}")
    print(f"✅ 守卫1 仓库根 {real_root}")

    env = os.environ.get("YIXIANQIAN_ENV", "prod")
    if env != "dev":
        _fail(f"守卫4 失败：必须显式 YIXIANQIAN_ENV=dev（当前 {env!r}）；本脚本只在测试环境执行")
    print("✅ 守卫4 环境 YIXIANQIAN_ENV=dev")

    sys.path.insert(0, os.path.join(real_root, "bot"))
    sys.path.insert(0, real_root)

    import constants as _constants
    import clients as _clients

    c_file = os.path.realpath(_constants.__file__)
    if not c_file.startswith(real_root + os.sep):
        _fail(f"守卫2 失败：bot.constants 解析自非本仓库 {c_file}")

    repo_base = _parse_base(os.path.join(real_root, "bot", "local_config.py"))
    prod_base = _parse_base(_PROD_CFG_PATH)
    if not prod_base:
        _fail(f"守卫2 失败：无法从生产配置 {_PROD_CFG_PATH} 解析 BASE_TOKEN，无法自证非生产，拒绝执行")
    if _constants.BASE_TOKEN == prod_base:
        _fail("守卫2 失败：解析出的 BASE_TOKEN 与生产 base 相同（拒绝连生产）")
    if _constants.BASE_TOKEN != repo_base:
        _fail("守卫2 失败：constants.BASE_TOKEN 与本仓库 bot/local_config.py 文本解析值不一致")
    print(f"✅ 守卫2 BASE_TOKEN≠生产 且=本仓库 local_config"
          f"（test={_mask(_constants.BASE_TOKEN)} prod={_mask(prod_base)}）")

    got = {
        "USER_TABLE_ID": _constants.USER_TABLE_ID,
        "ACTIVITY_TABLE_ID": _constants.ACTIVITY_TABLE_ID,
        "SIGNUP_TABLE_ID": _constants.SIGNUP_TABLE_ID,
        "ONETOONE_SELECT_TABLE": _constants.ONETOONE_SELECT_TABLE,
        "ONETOONE_RESULT_TABLE": _constants.ONETOONE_RESULT_TABLE,
    }
    bad = [f"{k}={got[k]}≠{expected}" for k, expected in _EXPECTED_TABLES.items()
           if got[k] != expected]
    if bad:
        _fail("守卫3 失败：表 ID 不符（防 constants 生产 fallback 生效）：" + "；".join(bad))
    print("✅ 守卫3 5 个测试表 ID 全部匹配")

    C = _constants
    bitable = _clients.bitable
    from lib.bitable_client import get_field_number as _gfn
    from lib.bitable_client import get_field_text as _gft
    from lib.bitable_client import get_select_value as _gsv
    get_field_text, get_select_value, get_field_number = _gft, _gsv, _gfn


_guard_and_import()


def _must(records, what):
    if records is None:
        _fail(f"读取失败：{what}（飞书查询返回错误，已中止，不做任何写入）")
    return records


def _uid_num(uid):
    m = re.match(r"U-?(\d+)", uid or "")
    return int(m.group(1)) if m else 10 ** 9


def _read_activity():
    for r in _must(bitable.search_records(C.ACTIVITY_TABLE_ID), "活动表"):
        f = r.get("fields", {})
        if get_field_text(f, C.FIELD_ACTIVITY_ID) == _ACTIVITY_ID:
            return r
    _fail(f"活动表中找不到 {_ACTIVITY_ID}")


def _read_signups():
    return _must(bitable.search_records(C.SIGNUP_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": C.FIELD_SIGNUP_ACTIVITY_ID,
                        "operator": "is", "value": [_ACTIVITY_ID]}],
    }), "报名表(A-0011)")


def _read_users():
    users = []
    for r in _must(bitable.search_records(C.USER_TABLE_ID), "用户表"):
        f = r.get("fields", {})
        oid = get_field_text(f, C.FIELD_FEISHU_ID)
        if not oid:
            continue
        users.append({
            "record_id": r.get("record_id"),
            "uid": get_field_text(f, _FIELD_USER_ID),
            "oid": oid,
            "nick": get_field_text(f, C.FIELD_NICKNAME),
            "gender": get_select_value(f, C.FIELD_GENDER),
        })
    return users


def _read_selections():
    return _must(bitable.search_records(C.ONETOONE_SELECT_TABLE, {
        "conjunction": "and",
        "conditions": [{"field_name": C.FIELD_OTO_ACTIVITY_ID,
                        "operator": "is", "value": [_ACTIVITY_ID]}],
    }), "一对一选择表(A-0011)")


def _read_results():
    return _must(bitable.search_records(C.ONETOONE_RESULT_TABLE, {
        "conjunction": "and",
        "conditions": [{"field_name": C.FIELD_OTO_ACTIVITY_ID,
                        "operator": "is", "value": [_ACTIVITY_ID]}],
    }), "一对一结果表(A-0011)")


def _build_participants(signups, users):
    """报名(A-0011) → 参与者（去重、过滤已取消、补用户表性别/昵称）。"""
    primary = {}
    for u in users:
        primary.setdefault(u["oid"], u)
    by_oid = {}
    for s in signups:
        sf = s.get("fields", {})
        oid = get_field_text(sf, C.FIELD_SIGNUP_OPENID)
        status = get_select_value(sf, C.FIELD_SIGNUP_STATUS)
        if not oid or status == "已取消":
            continue
        u = primary.get(oid)
        if not u or u["gender"] not in ("男性", "女性"):
            continue
        by_oid[oid] = {
            "oid": oid,
            "uid": u["uid"],
            "nick": u["nick"],
            "gender": u["gender"],
        }
    males = sorted([p for p in by_oid.values() if p["gender"] == "男性"],
                   key=lambda p: _uid_num(p["uid"]))
    females = sorted([p for p in by_oid.values() if p["gender"] == "女性"],
                     key=lambda p: _uid_num(p["uid"]))
    return males, females


def _build_selection_plan(males, females):
    """轮转法：异性按 uid 升序，第 k 位跳过同序位 k，选其余 7 位。"""
    plan = []
    for k, sel in enumerate(males):
        plan.append({"selector": sel,
                     "targets": [females[(k + 1 + i) % len(females)] for i in range(7)]})
    for k, sel in enumerate(females):
        plan.append({"selector": sel,
                     "targets": [males[(k + 1 + i) % len(males)] for i in range(7)]})
    for item in plan:
        targets = item["targets"]
        assert len(targets) == 7 and len({t["oid"] for t in targets}) == 7
        assert all(t["gender"] != item["selector"]["gender"] for t in targets)
    return plan


def _compute_plan():
    users = _read_users()
    signups = _read_signups()
    males, females = _build_participants(signups, users)
    if len(males) < 8 or len(females) < 8:
        _fail(f"A-0011 参与者不足：男 {len(males)}/8，女 {len(females)}/8"
              f"（先跑 seed_test_grouping.py run 补齐报名）")
    males, females = males[:8], females[:8]
    participants = males + females
    plan = _build_selection_plan(males, females)

    sels = _read_selections()
    sel_existing = {}
    for r in sels:
        oid = get_field_text(r.get("fields", {}), C.FIELD_OTO_SELECTOR_OID)
        if oid:
            sel_existing[oid] = r
    for item in plan:
        item["existing"] = sel_existing.get(item["selector"]["oid"])

    activity = _read_activity()
    results = _read_results()
    return {"males": males, "females": females, "participants": participants,
            "plan": plan, "activity": activity, "results": results,
            "p_by_oid": {p["oid"]: p for p in participants}}


def _print_plan(p):
    print("\n========== PLAN（只读，不写任何数据） ==========")
    print(f"【参与者 {len(p['participants'])} 人】{len(p['males'])} 男 + {len(p['females'])} 女（A-0011 报名）")
    for x in p["participants"]:
        print(f"  {x['uid']}  {x['gender']}  {x['nick']}  oid={_mask(x['oid'])}")
    creates = [i for i in p["plan"] if not i.get("existing")]
    updates = [i for i in p["plan"] if i.get("existing")]
    print(f"\n【志愿】将创建 {len(creates)} 条、覆盖更新 {len(updates)} 条（每人 7 个异性志愿）：")
    for item in p["plan"]:
        sel = item["selector"]
        verb = "~" if item.get("existing") else "+"
        print(f"  {verb} {sel['uid']}({sel['gender']}) -> "
              + ",".join(t["uid"] for t in item["targets"]))
    af = p["activity"].get("fields", {})
    print("\n【活动更新 2 项】")
    print(f"  1) handle_admin_start_onetoone(\"{_ACTIVITY_ID}\")：一对一状态 "
          f"{get_select_value(af, C.FIELD_ACT_ONETOONE_STATUS) or '空'}→收集中"
          f"（历史结果 {len(p['results'])} 条，将清除）")
    print(f"  2) handle_admin_toggle_onetoone_flag(\"{_ACTIVITY_ID} 开\")：一对一功能开启 "
          f"{get_select_value(af, C.FIELD_ACT_ONETOONE_FLAG) or '空'}→是")
    print(f"\n【执行】handle_admin_stop_onetoone(\"{_ACTIVITY_ID}\")：匹配并写结果。")
    print(f"【预期】{len(p['participants'])} 人 × 每人 8 个异性 = "
          f"{len(p['participants']) * 8} 条结果；7 志愿必居前 7。")


def _seed_selections(p):
    created, updated = [], []
    for item in p["plan"]:
        sel = item["selector"]
        fields = {
            C.FIELD_OTO_ACTIVITY_ID: _ACTIVITY_ID,
            C.FIELD_OTO_SELECTOR_OID: sel["oid"],
            C.FIELD_OTO_SELECTOR_NAME: sel["nick"],
            C.FIELD_OTO_SELECTOR_GENDER: sel["gender"],
        }
        for i, tgt in enumerate(item["targets"]):
            fields[C.FIELD_OTO_CHOICES[i]] = tgt["oid"]
        if item.get("existing"):
            rec = bitable.update_record(C.ONETOONE_SELECT_TABLE, item["existing"]["record_id"], fields)
            if not rec:
                _fail(f"更新志愿失败：{sel['uid']}（中止，未做任何删除）")
            updated.append(sel)
        else:
            rec = bitable.create_record(C.ONETOONE_SELECT_TABLE, fields)
            if not rec:
                _fail(f"创建志愿失败：{sel['uid']}（中止，未做任何删除）")
            created.append(sel)
    return created, updated


def _apply_activity_settings():
    from commands import handle_admin_toggle_onetoone_flag
    from onetoone import handle_admin_start_onetoone

    reply1 = handle_admin_start_onetoone(_ACTIVITY_ID)
    print(f"  handle_admin_start_onetoone -> {reply1.splitlines()[0]}")
    if "志愿填写已开始" not in reply1:
        _fail(f"开始一对一失败：{reply1}")
    reply2 = handle_admin_toggle_onetoone_flag(f"{_ACTIVITY_ID} 开")
    print(f"  handle_admin_toggle_onetoone_flag -> {reply2.splitlines()[0]}")
    if "已开启" not in reply2:
        _fail(f"开启一对一功能失败：{reply2}")


def _run_matching():
    from onetoone import handle_admin_stop_onetoone

    reply = handle_admin_stop_onetoone(_ACTIVITY_ID)
    print(f"  handle_admin_stop_onetoone -> {reply.splitlines()[0]}")
    if "匹配完成" not in reply:
        _fail(f"执行一对一失败：{reply}")
    return reply


def _verify_state(p):
    lines, issues = [], []
    results = _read_results()
    af = _read_activity().get("fields", {})
    status = get_select_value(af, C.FIELD_ACT_ONETOONE_STATUS)
    flag = get_select_value(af, C.FIELD_ACT_ONETOONE_FLAG)
    lines.append(f"[活动] 一对一状态={status} 一对一功能开启={flag}")
    if (status, flag) != ("已完成", "是"):
        issues.append(f"活动状态不符：{status}/{flag}")

    by_user = {}
    for r in results:
        f = r.get("fields", {})
        uoid = get_field_text(f, C.FIELD_OTO_USER_OID)
        by_user.setdefault(uoid, []).append(r)

    lines.append(f"[结果] 总行数 {len(results)}（期望 {len(p['participants']) * 8}）")
    if len(results) != len(p["participants"]) * 8:
        issues.append(f"结果行数不符：{len(results)}")

    n_ok = 0
    for person in p["participants"]:
        oid = person["oid"]
        rows = by_user.get(oid, [])
        got = []
        for r in rows:
            f = r.get("fields", {})
            got.append({
                "rank": get_field_number(f, C.FIELD_OTO_RANK, 0),
                "target": get_field_text(f, C.FIELD_OTO_TARGET_OID),
                "tgender": get_field_text(f, C.FIELD_OTO_USER_GENDER),
            })
        ranks = sorted([g["rank"] for g in got])
        targets = [g["target"] for g in got]
        opp = p["females"] if person["gender"] == "男性" else p["males"]
        opp_oids = {x["oid"] for x in opp}
        bad = []
        if len(got) != len(opp_oids):
            bad.append(f"名单人数 {len(got)}≠异性 {len(opp_oids)}")
        if ranks != list(range(1, len(got) + 1)):
            bad.append(f"排名非 1..{len(got)}：{ranks}")
        if any(t not in opp_oids for t in targets):
            bad.append("含非异性/自己")
        if oid in targets:
            bad.append("含自己")
        # 7 志愿必居前 7：找该人的选择
        plan_item = next((i for i in p["plan"] if i["selector"]["oid"] == oid), None)
        if plan_item:
            sel_targets = [t["oid"] for t in plan_item["targets"]]
            top7 = [g["target"] for g in sorted(got, key=lambda g: g["rank"])[:7]]
            if top7 != sel_targets:
                bad.append(f"7 志愿未居前 7 且未按优先级：{top7[:3]}…")
        if bad:
            issues.append(f"{person['uid']}({person['gender']})：{'; '.join(bad)}")
        else:
            n_ok += 1
    lines.append(f"[逐人验证] {n_ok}/{len(p['participants'])} 人通过（每人 8 异性、排名 1..8、"
                 f"7 志愿居前 7 且按优先级、无自己无同性）")

    for line in lines:
        print("  " + line)
    for issue in issues:
        print("  ⛔ " + issue)
    return (not issues), lines


def _cmd_plan():
    p = _compute_plan()
    _print_plan(p)
    print("\n（plan 模式：未执行任何写入。确认无误后用 run 执行。）")


def _cmd_run():
    p = _compute_plan()
    print("========== RUN 开始（只写测试 base） ==========")
    created, updated = _seed_selections(p)
    print(f"[1/4] 志愿：创建 {len(created)} 条，覆盖更新 {len(updated)} 条")
    print("[2/4] 活动设置：")
    _apply_activity_settings()
    print("[3/4] 执行匹配：")
    _run_matching()
    print("[4/4] 读回验证：")
    ok, _ = _verify_state(p)
    if not ok:
        _fail("读回验证未全部通过（见上方 ⛔ 项）")
    print("\n✅ run 完成：志愿种子 + 开始 + 执行 + 结果读回验证全部通过。")


def _cmd_verify():
    p = _compute_plan()
    print("========== VERIFY（只读复核） ==========")
    ok, _ = _verify_state(p)
    if not ok:
        _fail("复核未通过（见上方 ⛔ 项）")
    print("✅ 复核通过。")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "plan":
        _cmd_plan()
    elif mode == "run":
        _cmd_run()
    elif mode == "verify":
        _cmd_verify()
    else:
        print("用法：YIXIANQIAN_ENV=dev python3 scripts/dev/seed_test_onetoone.py plan|run|verify")
        sys.exit(2)


if __name__ == "__main__":
    main()
