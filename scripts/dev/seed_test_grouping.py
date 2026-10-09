#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试环境 A-0011「关系自洽」分组数据种子（只允许在 /opt/yixianqian-test 执行）。

目标：让真号 U-0006（女性）能走完「提交志愿 → 分组」全流程。
数据设计（16 人 = U-0006 + 8 假男 + 7 假女，凑成 8 男 8 女）：

- 报名 16 条：字段集与值照抄 H5（web/backend/app.py 的 signup）与 bot
  auto_signup_new_user——活动ID/报名人open_id/报名人昵称/状态=已报名。
  「报名时间」是报名表的自动创建时间字段，写入方不写，创建时由表自动落。

- 志愿 15 条（U-0006 留白，走 H5）：每条填满 7 个志愿位，存被选人 open_id。
  轮转法：异性 8 人按 uid 升序成 list，第 k 位跳过「同序位 k」的那位，
  选其余 7 位（list[(k+1..k+7) mod 8]），保证每人都被 7 位异性选中。

- 活动设置：handle_admin_start_group("A-0011 4 4")（分组状态=收集中/
  4男4女/清历史结果）+ handle_admin_toggle_group_flag("A-0011 开")
  （分组功能开启=是）。不执行分组，不动活动其它状态。

用法（在 /opt/yixianqian-test 仓库根，必须显式 YIXIANQIAN_ENV=dev）：
  YIXIANQIAN_ENV=dev python3 scripts/dev/seed_test_grouping.py plan    # 只打印计划，不写
  YIXIANQIAN_ENV=dev python3 scripts/dev/seed_test_grouping.py run     # 执行写入并读回验证
  YIXIANQIAN_ENV=dev python3 scripts/dev/seed_test_grouping.py verify  # 只读复核（U-0006 留白）

自建守卫（任一不满足即退出，绝不写入）：
  1) 仓库根 realpath 必须等于 /opt/yixianqian-test；
  2) 解析出的 BASE_TOKEN 必须≠生产 /opt/yixianqian/bot/local_config.py 的值，
     且等于本仓库 bot/local_config.py 文本解析值；
  3) 用户/活动/报名/分组选择/分组结果 5 个表 ID 必须逐一等于测试 base 已知值
     （防 bot/constants.py 的生产 fallback 生效）；
  4) 必须显式 YIXIANQIAN_ENV=dev（不认 YX_DEV_ALLOW 兜底）。
  反向测试钩子：SEED_FORCE_BAD=base|table 模拟「base 相等 / 表 ID 不符」。
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
_GROUP_MALES = 4
_GROUP_FEMALES = 4
_EXPECTED_TABLES = {
    "USER_TABLE_ID": "tblMFjHwrTlTqrKZ",
    "ACTIVITY_TABLE_ID": "tblFg5jAXaHzUweF",
    "SIGNUP_TABLE_ID": "tblSUxXKbG7gdqVU",
    "GROUP_SELECT_TABLE": "tblykkDzC0LaJxi8",
    "GROUP_RESULT_TABLE": "tblRg2riWkq88iJP",
}
# 报名状态/选项：代码中各处均为字面量（H5、bot、auto_tasks），此处跟随。
_SIGNUP_SIGNED = "已报名"
_SIGNUP_CANCELLED = "已取消"
# 用户表「用户ID」没有对应常量，bot/queries.py 同样用字面量。
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
    """守衛 1/2/3/4 + 导入 bot 包（导入无网络副作用，写操作仅发生在 run）。"""
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
    force = os.environ.get("SEED_FORCE_BAD", "")
    eff_base = prod_base if force == "base" else _constants.BASE_TOKEN
    if eff_base == prod_base:
        _fail("守卫2 失败：解析出的 BASE_TOKEN 与生产 base 相同（拒绝连生产）")
    if _constants.BASE_TOKEN != repo_base:
        _fail("守卫2 失败：constants.BASE_TOKEN 与本仓库 bot/local_config.py 文本解析值不一致")
    print(f"✅ 守卫2 BASE_TOKEN≠生产 且=本仓库 local_config"
          f"（test={_mask(_constants.BASE_TOKEN)} prod={_mask(prod_base)}）")

    got = {
        "USER_TABLE_ID": _constants.USER_TABLE_ID,
        "ACTIVITY_TABLE_ID": _constants.ACTIVITY_TABLE_ID,
        "SIGNUP_TABLE_ID": _constants.SIGNUP_TABLE_ID,
        "GROUP_SELECT_TABLE": _constants.GROUP_SELECT_TABLE,
        "GROUP_RESULT_TABLE": _constants.GROUP_RESULT_TABLE,
    }
    if force == "table":  # 反向测试钩子：模拟 constants 生产 fallback 生效
        got["USER_TABLE_ID"] = "tblsecbZZv0thaPe"
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
            "status": get_field_text(f, C.FIELD_ACCOUNT_STATUS),
        })
    return users


def _primary_by_oid(users):
    """同 oid 多档时取主档案：单身优先、uid 最小（与 bot/queries.pick_primary_record 同口径）。"""
    out = {}
    for u in sorted(users, key=lambda u: (0 if u["status"] == "单身" else 1, _uid_num(u["uid"]))):
        out.setdefault(u["oid"], u)
    return out


def _read_activity():
    for r in _must(bitable.search_records(C.ACTIVITY_TABLE_ID), "活动表"):
        f = r.get("fields", {})
        if get_field_text(f, C.FIELD_ACTIVITY_ID) == _ACTIVITY_ID:
            return r
    _fail(f"活动表中找不到 {_ACTIVITY_ID}")


def _read_activity_signups():
    return _must(bitable.search_records(C.SIGNUP_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": C.FIELD_SIGNUP_ACTIVITY_ID,
                        "operator": "is", "value": [_ACTIVITY_ID]}],
    }), "报名表(A-0011)")


def _read_activity_selections():
    return _must(bitable.search_records(C.GROUP_SELECT_TABLE, {
        "conjunction": "and",
        "conditions": [{"field_name": C.FIELD_GS_ACTIVITY_ID,
                        "operator": "is", "value": [_ACTIVITY_ID]}],
    }), "分组选择表(A-0011)")


def _read_activity_results():
    return _must(bitable.search_records(C.GROUP_RESULT_TABLE, {
        "conjunction": "and",
        "conditions": [{"field_name": C.FIELD_GR_ACTIVITY_ID,
                        "operator": "is", "value": [_ACTIVITY_ID]}],
    }), "分组结果表(A-0011)")


def _pick_participants(users):
    u6_matches = [u for u in users if u["uid"] == "U-0006"]
    if len(u6_matches) != 1:
        _fail(f"U-0006 记录数异常：{len(u6_matches)}（期望 1）")
    u6 = u6_matches[0]
    if (u6["gender"] != "女性" or not u6["oid"].startswith("ou_08e")
            or u6["status"] not in ("活跃", "单身")):
        _fail(f"U-0006 资料不符预期：gender={u6['gender']!r} oid={_mask(u6['oid'])} "
              f"status={u6['status']!r}")
    fakes = [u for u in users
             if u["oid"].startswith("ou_fake_load_")
             and u["gender"] in ("男性", "女性")
             and u["status"] in ("活跃", "单身")
             and re.match(r"U-\d+$", u["uid"] or "")]
    males = sorted([u for u in fakes if u["gender"] == "男性"], key=lambda u: _uid_num(u["uid"]))[:8]
    females = sorted([u for u in fakes if u["gender"] == "女性"], key=lambda u: _uid_num(u["uid"]))[:7]
    if len(males) < 8 or len(females) < 7:
        _fail(f"可用压测假号不足：男 {len(males)}/8，女 {len(females)}/7")
    return u6, males, females


def _build_selection_plan(u6, males, females7):
    """轮转法：异性 8 人按 uid 升序，第 k 位跳过同序位 k，选 list[(k+1..k+7) mod 8]。"""
    males_sorted = sorted(males, key=lambda u: _uid_num(u["uid"]))
    females_sorted = sorted([u6] + females7, key=lambda u: _uid_num(u["uid"]))
    plan = []
    for k, sel in enumerate(males_sorted):
        plan.append({"selector": sel,
                     "targets": [females_sorted[(k + 1 + i) % 8] for i in range(7)]})
    for k, sel in enumerate(females_sorted):
        if sel["uid"] == "U-0006":  # 留白给使用者走 H5
            continue
        plan.append({"selector": sel,
                     "targets": [males_sorted[(k + 1 + i) % 8] for i in range(7)]})
    for item in plan:
        targets = item["targets"]
        assert len(targets) == 7 and len({t["oid"] for t in targets}) == 7
        assert all(t["gender"] != item["selector"]["gender"] for t in targets)
    assert len(plan) == 15, f"志愿条数异常：{len(plan)}"
    return plan


def _compute_plan():
    users = _read_users()
    primary = _primary_by_oid(users)
    u6, males, females7 = _pick_participants(users)
    participants = [u6] + males + females7
    p_by_oid = {p["oid"]: p for p in participants}
    if len(p_by_oid) != 16:
        _fail("参与者 oid 去重后不足 16 人")

    signups = _read_activity_signups()
    occupied = set()
    for s in signups:
        sf = s.get("fields", {})
        oid = get_field_text(sf, C.FIELD_SIGNUP_OPENID)
        status = get_select_value(sf, C.FIELD_SIGNUP_STATUS)
        if oid and status != _SIGNUP_CANCELLED:
            occupied.add(oid)
    signups_to_create = [p for p in participants if p["oid"] not in occupied]
    signups_skipped = [p for p in participants if p["oid"] in occupied]

    sel_records = _read_activity_selections()
    sel_existing = {}
    for r in sel_records:
        oid = get_field_text(r.get("fields", {}), C.FIELD_GS_SELECTOR_OID)
        if oid:
            sel_existing[oid] = r
    selection_plan = _build_selection_plan(u6, males, females7)
    for item in selection_plan:
        item["existing"] = sel_existing.get(item["selector"]["oid"])

    activity = _read_activity()
    results = _read_activity_results()
    return {
        "users": users,
        "primary": primary,
        "u6": u6,
        "males": males,
        "females7": females7,
        "participants": participants,
        "p_by_oid": p_by_oid,
        "signups": signups,
        "signups_to_create": signups_to_create,
        "signups_skipped": signups_skipped,
        "selection_plan": selection_plan,
        "activity": activity,
        "results": results,
    }


def _print_plan(plan):
    print("\n========== PLAN（只读，不写任何数据） ==========")
    print(f"【参与者 16 人】U-0006（真·女）+ 8 假男 + 7 假女（均来自 ou_fake_load_*，按 uid 升序）")
    for p in plan["participants"]:
        tag = "真号" if p["uid"] == "U-0006" else "假号"
        print(f"  {p['uid']}  {p['gender']}  {p['nick']}  oid={_mask(p['oid'])}  [{tag}]")

    print(f"\n【报名】将创建 {len(plan['signups_to_create'])} 条"
          f"（查重跳过 {len(plan['signups_skipped'])} 条）：")
    for p in plan["signups_to_create"]:
        print(f"  + 活动ID={_ACTIVITY_ID} 报名人open_id={_mask(p['oid'])} "
              f"报名人昵称={p['nick']!r} 状态={_SIGNUP_SIGNED}")
    for p in plan["signups_skipped"]:
        print(f"  = 跳过（已有有效报名）：{p['uid']} oid={_mask(p['oid'])}")
    print("  注：「报名时间」为报名表自动创建时间字段，H5/bot 均不写，创建时自动落。")

    creates = [i for i in plan["selection_plan"] if not i.get("existing")]
    updates = [i for i in plan["selection_plan"] if i.get("existing")]
    print(f"\n【志愿】将创建 {len(creates)} 条、覆盖更新 {len(updates)} 条；"
          f"U-0006 留白（走 H5 提交）：")
    for item in plan["selection_plan"]:
        sel = item["selector"]
        verb = "~" if item.get("existing") else "+"
        targets = ",".join(t["uid"] for t in item["targets"])
        print(f"  {verb} {sel['uid']}({sel['gender']}) 7 个志愿 -> {targets}")

    af = plan["activity"].get("fields", {})
    print("\n【活动更新 2 项】")
    print(f"  1) handle_admin_start_group(\"{_ACTIVITY_ID} {_GROUP_MALES} {_GROUP_FEMALES}\")："
          f"分组状态 {get_select_value(af, C.FIELD_ACT_GROUP_STATUS) or '空'}→收集中、"
          f"每组男生数 {get_field_number(af, C.FIELD_ACT_MALE_PER_GROUP)}→{_GROUP_MALES}、"
          f"每组女生数 {get_field_number(af, C.FIELD_ACT_FEMALE_PER_GROUP)}→{_GROUP_FEMALES}"
          f"（历史分组结果 {len(plan['results'])} 条，"
          f"{'将拒绝执行：与“不删现有记录”红线冲突' if plan['results'] else '无删除'}）")
    print(f"  2) handle_admin_toggle_group_flag(\"{_ACTIVITY_ID} 开\")："
          f"分组功能开启 {get_select_value(af, C.FIELD_ACT_GROUP_FLAG) or '空'}→是")
    print("\n【预期】分组池=15 条志愿+U-0006 H5 提交=8男8女 → 每组4男4女，恰好 2 组。")
    print("【保留】既有 D（U-0004，女，已报名）与 4 条已取消报名原样不动；"
          "bot 对账后「当前报名人数」预计 17（16 新 + 已有 D）。")


def _create_signups(plan):
    created, skipped = [], list(plan["signups_skipped"])
    for p in plan["signups_to_create"]:
        rec = bitable.create_record(C.SIGNUP_TABLE_ID, {
            C.FIELD_SIGNUP_ACTIVITY_ID: _ACTIVITY_ID,
            C.FIELD_SIGNUP_OPENID: p["oid"],
            C.FIELD_SIGNUP_NICKNAME: p["nick"],
            C.FIELD_SIGNUP_STATUS: _SIGNUP_SIGNED,
        })
        if not rec:
            _fail(f"创建报名失败：{p['uid']} oid={_mask(p['oid'])}"
                  f"（写入被拒；已创建 {len(created)} 条，未做任何删除）")
        created.append(p)
    return created, skipped


def _write_selections(plan):
    created, updated = [], []
    for item in plan["selection_plan"]:
        sel = item["selector"]
        fields = {
            C.FIELD_GS_ACTIVITY_ID: _ACTIVITY_ID,
            C.FIELD_GS_SELECTOR_OID: sel["oid"],
            C.FIELD_GS_SELECTOR_NAME: sel["nick"],
            C.FIELD_GS_SELECTOR_GENDER: sel["gender"],
        }
        for i, tgt in enumerate(item["targets"]):
            fields[C.FIELD_GS_CHOICES[i]] = tgt["oid"]
        if item.get("existing"):
            rec = bitable.update_record(C.GROUP_SELECT_TABLE, item["existing"]["record_id"], fields)
            if not rec:
                _fail(f"更新志愿失败：{sel['uid']}（中止，未做任何删除）")
            updated.append(sel)
        else:
            rec = bitable.create_record(C.GROUP_SELECT_TABLE, fields)
            if not rec:
                _fail(f"创建志愿失败：{sel['uid']}（中止，未做任何删除）")
            created.append(sel)
    return created, updated


def _apply_activity_settings():
    from commands import handle_admin_toggle_group_flag
    from grouping import handle_admin_start_group

    reply1 = handle_admin_start_group(f"{_ACTIVITY_ID} {_GROUP_MALES} {_GROUP_FEMALES}")
    print(f"  handle_admin_start_group -> {reply1.splitlines()[0]}")
    if "志愿填写已开始" not in reply1:
        _fail(f"开始填志愿失败：{reply1}（写入可能未生效，已读回验证会如实报告）")
    reply2 = handle_admin_toggle_group_flag(f"{_ACTIVITY_ID} 开")
    print(f"  handle_admin_toggle_group_flag -> {reply2.splitlines()[0]}")
    if "已开启" not in reply2:
        _fail(f"开启分组功能失败：{reply2}")


def _read_choices(fields):
    return [get_field_text(fields, name) for name in C.FIELD_GS_CHOICES]


def _verify_state(plan):
    """读回验证（C/D/E/H 项）。返回 (ok, lines)。"""
    lines = []
    issues = []
    wrong_gender = self_ref = 0

    users = _read_users()
    primary = _primary_by_oid(users)

    # C1 报名
    signups = _read_activity_signups()
    by_oid = {}
    for s in signups:
        sf = s.get("fields", {})
        by_oid.setdefault(get_field_text(sf, C.FIELD_SIGNUP_OPENID), []).append(s)
    signed_ok = 0
    u6_signed = False
    for p in plan["participants"]:
        recs = [s for s in by_oid.get(p["oid"], [])
                if get_select_value(s.get("fields", {}), C.FIELD_SIGNUP_STATUS) == _SIGNUP_SIGNED]
        if len(recs) == 1:
            signed_ok += 1
        elif len(recs) == 0:
            issues.append(f"报名缺失：{p['uid']}")
        else:
            issues.append(f"报名重复：{p['uid']} x{len(recs)}")
        for r in recs:
            rf = r.get("fields", {})
            if get_field_text(rf, C.FIELD_SIGNUP_NICKNAME) != p["nick"]:
                issues.append(f"报名昵称不一致：{p['uid']}")
        if p["uid"] == "U-0006" and len(recs) == 1:
            u6_signed = True
    lines.append(f"[报名] {signed_ok}/16 条状态=已报名；昵称/oid 与用户表一致；"
                 f"U-0006 已报名={'是' if u6_signed else '否'}")

    # C2 志愿
    sels = _read_activity_selections()
    sel_by_oid = {}
    for r in sels:
        oid = get_field_text(r.get("fields", {}), C.FIELD_GS_SELECTOR_OID)
        sel_by_oid.setdefault(oid, []).append(r)
    sel_ok = 0
    for item in plan["selection_plan"]:
        sel = item["selector"]
        recs = sel_by_oid.get(sel["oid"], [])
        if len(recs) != 1:
            issues.append(f"志愿记录数异常：{sel['uid']} x{len(recs)}")
            continue
        choices = _read_choices(recs[0].get("fields", {}))
        if len(choices) != 7 or any(not c for c in choices):
            issues.append(f"志愿有空栏：{sel['uid']}")
            continue
        if len(set(choices)) != 7:
            issues.append(f"志愿内有重复：{sel['uid']}")
        ok = True
        for c in choices:
            tgt = plan["p_by_oid"].get(c)
            if not tgt:
                issues.append(f"志愿指向非本活动16人：{sel['uid']} -> {_mask(c)}")
                ok = False
            elif tgt["gender"] == sel["gender"]:
                issues.append(f"志愿非异性：{sel['uid']} -> {tgt['uid']}")
                wrong_gender += 1
                ok = False
            elif c == sel["oid"]:
                issues.append(f"志愿选择自己：{sel['uid']}")
                self_ref += 1
                ok = False
        if ok:
            sel_ok += 1
    lines.append(f"[志愿] {sel_ok}/15 条：每条 7 栏非空、全为本活动 16 人内异性、无自己、无重复")

    # E 候选人（模拟 H5 逻辑：get_signups=状态 isNot 已取消 → 按异性过滤）
    cands = []
    for s in signups:
        sf = s.get("fields", {})
        if get_select_value(sf, C.FIELD_SIGNUP_STATUS) == _SIGNUP_CANCELLED:
            continue
        oid = get_field_text(sf, C.FIELD_SIGNUP_OPENID)
        if oid == plan["u6"]["oid"]:
            continue
        u = primary.get(oid)
        if u and u["gender"] == "男性":
            cands.append(u)
    lines.append(f"[候选人] U-0006(女) 视角男性候选 {len(cands)} 人："
                 + " ".join(_mask(c["oid"]) for c in cands))
    if len(cands) != 8:
        issues.append(f"候选人数量异常：{len(cands)}（期望 8）")

    # H U-0006 志愿留白
    if sel_by_oid.get(plan["u6"]["oid"]):
        issues.append("U-0006 已存在志愿记录（应留白给 H5）")
    lines.append(f"[U-0006 志愿] "
                 f"{'不存在（留白，待 H5 提交）' if not sel_by_oid.get(plan['u6']['oid']) else '存在!'}"
                 f"；U-0006 已报名={'是' if u6_signed else '否'}")

    # 关系完整性（D）：报名 oid 全部存在于用户表、我们 16 人无重复报名
    orphan = dup = 0
    for s in signups:
        oid = get_field_text(s.get("fields", {}), C.FIELD_SIGNUP_OPENID)
        if oid and oid not in primary:
            orphan += 1
    for oid in plan["p_by_oid"]:
        signed = [r for r in by_oid.get(oid, [])
                  if get_select_value(r.get("fields", {}), C.FIELD_SIGNUP_STATUS) == _SIGNUP_SIGNED]
        if len(signed) > 1:
            dup += 1
    lines.append(f"[关系] orphans={orphan}, dup={dup}, wrong_gender={wrong_gender}, "
                 f"self_ref={self_ref}")

    # C3 活动字段
    af = _read_activity().get("fields", {})
    gs = get_select_value(af, C.FIELD_ACT_GROUP_STATUS)
    mp = get_field_number(af, C.FIELD_ACT_MALE_PER_GROUP)
    fp = get_field_number(af, C.FIELD_ACT_FEMALE_PER_GROUP)
    flag = get_select_value(af, C.FIELD_ACT_GROUP_FLAG)
    lines.append(f"[活动] 分组状态={gs} 每组男生数={mp} 每组女生数={fp} 分组功能开启={flag}")
    if (gs, mp, fp, flag) != ("收集中", _GROUP_MALES, _GROUP_FEMALES, "是"):
        issues.append(f"活动四栏不符：{gs}/{mp}/{fp}/{flag}")
    cur = get_field_number(af, C.FIELD_ACTIVITY_CURRENT_SIGNUP)
    lines.append(f"[当前报名人数] {cur}（16 新 + 既有 D；由 bot 每 30s 对账，运行后可能短暂滞后）")

    for line in lines:
        print("  " + line)
    for issue in issues:
        print("  ⛔ " + issue)
    return (not issues), lines


def _cmd_plan():
    plan = _compute_plan()
    _print_plan(plan)
    print("\n（plan 模式：未执行任何写入。确认无误后用 run 执行。）")


def _cmd_run():
    plan = _compute_plan()
    if plan["results"]:
        _fail(f"分组结果表现有 A-0011 记录 {len(plan['results'])} 条，"
              "handle_admin_start_group 会清除它们，与「不删现有记录」红线冲突，拒绝执行")
    print("========== RUN 开始（只写测试 base） ==========")
    created, skipped = _create_signups(plan)
    print(f"[1/4] 报名：创建 {len(created)} 条，跳过 {len(skipped)} 条（查重命中）")
    sc, su = _write_selections(plan)
    print(f"[2/4] 志愿：创建 {len(sc)} 条，覆盖更新 {len(su)} 条")
    print("[3/4] 活动设置：")
    _apply_activity_settings()
    print("[4/4] 读回验证：")
    ok, _ = _verify_state(plan)
    if not ok:
        _fail("读回验证未全部通过（见上方 ⛔ 项）")
    print("\n✅ run 完成：16 报名 + 15 志愿 + 活动设置，读回验证全部通过。")


def _cmd_verify():
    plan = _compute_plan()
    print("========== VERIFY（只读复核） ==========")
    ok, _ = _verify_state(plan)
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
        print("用法：YIXIANQIAN_ENV=dev python3 scripts/dev/seed_test_grouping.py "
              "plan|run|verify")
        sys.exit(2)


if __name__ == "__main__":
    main()
