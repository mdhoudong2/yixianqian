#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试环境「Phase 2 验收演示」数据种子：A-0016 分组 / A-0017 一对一（只允许在 /opt/yixianqian-test 执行）。

给使用者亲手走完整流程的两个新活动，**从「未设定模式」开始**：

- 活动状态=报名中；分组状态/一对一状态与两个模式 flag 全空
  （管理台显示「未设定模式」引导卡，模式由使用者自己选、自己开）；
- 签到功能开启=是（使用者一进活动即可玩现场签到）；
- 报名 12 人 = 猴哥 U-0001（男，管理员）+ 一二三。 U-0006（女，管理员）
  + 10 个假号（5 男 5 女，ou_fake_load_*，状态=单身；按 uid 升序取
  第 9~13 男 / 第 8~12 女，与 A-0011 种子已用的前 8 男/前 7 女错开）；
- 签到三态：6 人已到（按时）、2 人未到、4 人未标记（含两个真号——使用者可以
  真号实测「未签到被挡」，再由管理员标记已到解锁）；
- A-0016 预填 10 个假号分组志愿（轮转法照 seed_test_grouping.py：按 uid 升序
  的第 k 位跳过同位异性、选其余 N-1 位，保证每人被多人选中），两个真号留白；
- A-0017 预填 10 个假号一对一志愿（每人填满全部异性，6 栏），两个真号留白。

不预执行分组/一对一（让使用者自己按）。不触碰 A-0011/A-0012/A-0013 的任何数据。

用法（在 /opt/yixianqian-test 仓库根，必须显式 YIXIANQIAN_ENV=dev）：
  YIXIANQIAN_ENV=dev web/backend/venv/bin/python scripts/dev/seed_acceptance.py plan    # 只打印计划
  YIXIANQIAN_ENV=dev web/backend/venv/bin/python scripts/dev/seed_acceptance.py run     # 执行写入并读回验证
  YIXIANQIAN_ENV=dev web/backend/venv/bin/python scripts/dev/seed_acceptance.py verify  # 只读复核

自建守卫（任一不满足即退出，绝不写入）：
  1) 仓库根 realpath 必须等于 /opt/yixianqian-test；
  2) 解析出的 BASE_TOKEN 必须≠生产 /opt/yixianqian/bot/local_config.py 的值，
     且等于本仓库 bot/local_config.py 文本解析值；
  3) 用户/活动/报名/分组选择/分组结果/一对一选择/一对一结果 7 个表 ID
     必须逐一等于测试 base 已知值（防 bot/constants.py 的生产 fallback 生效；
     参考脚本各查 5 张，本脚本同时涉及分组与一对一两条链，两套 4+4 张全查）；
  4) 必须显式 YIXIANQIAN_ENV=dev（不认 YX_DEV_ALLOW 兜底）。
  反向测试钩子：SEED_FORCE_BAD=base|table 模拟「base 相等 / 表 ID 不符」。

模式 flag 红线：本脚本绝不设置/清除 A-0016/A-0017 的分组/一对一 flag 与状态；
发现它们非空时报错停手（flag 一旦被使用者选过，种子不越权改动）。

自动编号锚点：审计过程用临时活动烧掉了 A-0014/A-0015（飞书自动编号删除不复用），
所以「max(现存)+1」会低估。plan 先把活动表现存最大编号与锚点核对（见
_ANCHOR_EXISTING_MAX / _ANCHOR_ISSUED_MAX），完全一致才信任「下一编号」；
不一致时停在 plan 阶段并报告，绝不盲建。创建后仍逐条核对真实 ID，不符即停手。
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

_EXPECTED_ROOT = "/opt/yixianqian-test"
_PROD_CFG_PATH = "/opt/yixianqian/bot/local_config.py"
_EXPECTED_TABLES = {
    "USER_TABLE_ID": "tblMFjHwrTlTqrKZ",
    "ACTIVITY_TABLE_ID": "tblFg5jAXaHzUweF",
    "SIGNUP_TABLE_ID": "tblSUxXKbG7gdqVU",
    "GROUP_SELECT_TABLE": "tblykkDzC0LaJxi8",
    "GROUP_RESULT_TABLE": "tblRg2riWkq88iJP",
    "ONETOONE_SELECT_TABLE": "tblTRXtuLhODm8Q0",
    "ONETOONE_RESULT_TABLE": "tbllYrqHD81gHyI2",
}
_GROUP_ACT_NAME = "验收演示·分组"
_OTO_ACT_NAME = "验收演示·一对一"
_GROUP_EXPECTED_ID = "A-0016"
_OTO_EXPECTED_ID = "A-0017"
# 自动编号锚点（2026-10-12 实测）：审计临时活动烧掉 A-0014/A-0015 后，
# 活动表现存最大编号=A-0013，已发编号上限=A-0015（删除不复用）。
# 新建前必须核对「现存最大编号」与 _ANCHOR_EXISTING_MAX 完全一致（期间无人新建/删除），
# 此时下一编号 = _ANCHOR_ISSUED_MAX + 1；不一致则停手，人工核对后更新锚点。
_ANCHOR_EXISTING_MAX = 13
_ANCHOR_ISSUED_MAX = 15
_REAL_MALE_UID = "U-0001"
_REAL_FEMALE_UID = "U-0006"
_FAKE_MALE_OFFSET = 8    # A-0011 种子已用前 8 假男（uid 升序），演示人群与其错开
_FAKE_FEMALE_OFFSET = 7  # A-0011 种子已用前 7 假女
_N_PER_GENDER = 5
# 假号签到三态：按池内序号（uid 升序）前 3 已到、第 4 未到、第 5 未标记。
_FAKE_STATES = ("按时", "按时", "按时", "未到", None)
_SIGNUP_SIGNED = "已报名"
_SIGNUP_CANCELLED = "已取消"
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

    got = {k: getattr(_constants, k) for k in _EXPECTED_TABLES}
    if force == "table":  # 反向测试钩子：模拟 constants 生产 fallback 生效
        got["USER_TABLE_ID"] = "tblsecbZZv0thaPe"
    bad = [f"{k}={got[k]}≠{expected}" for k, expected in _EXPECTED_TABLES.items()
           if got[k] != expected]
    if bad:
        _fail("守卫3 失败：表 ID 不符（防 constants 生产 fallback 生效）：" + "；".join(bad))
    print(f"✅ 守卫3 {len(_EXPECTED_TABLES)} 个测试表 ID 全部匹配")

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


def _pick_cast(users):
    """真号 2 名 + 假号 10 名（5 男 5 女，ou_fake_load_*）。"""
    def _by_uid(uid):
        matches = [u for u in users if u["uid"] == uid]
        if len(matches) != 1:
            _fail(f"{uid} 记录数异常：{len(matches)}（期望 1）")
        return matches[0]

    u1 = _by_uid(_REAL_MALE_UID)
    if u1["gender"] != "男性" or not u1["oid"] or u1["status"] not in ("活跃", "单身"):
        _fail(f"{_REAL_MALE_UID} 资料不符预期：gender={u1['gender']!r} "
              f"oid={_mask(u1['oid'])} status={u1['status']!r}")
    u6 = _by_uid(_REAL_FEMALE_UID)
    if (u6["gender"] != "女性" or not u6["oid"].startswith("ou_08e")
            or u6["status"] not in ("活跃", "单身")):
        _fail(f"{_REAL_FEMALE_UID} 资料不符预期：gender={u6['gender']!r} "
              f"oid={_mask(u6['oid'])} status={u6['status']!r}")

    primary = _primary_by_oid(users)
    fakes = [u for u in primary.values()
             if u["oid"].startswith("ou_fake_load_")
             and u["gender"] in ("男性", "女性")
             and u["status"] == "单身"
             and re.match(r"U-\d+$", u["uid"] or "")]
    males = sorted([u for u in fakes if u["gender"] == "男性"],
                   key=lambda u: _uid_num(u["uid"]))
    females = sorted([u for u in fakes if u["gender"] == "女性"],
                     key=lambda u: _uid_num(u["uid"]))
    males = males[_FAKE_MALE_OFFSET:_FAKE_MALE_OFFSET + _N_PER_GENDER]
    females = females[_FAKE_FEMALE_OFFSET:_FAKE_FEMALE_OFFSET + _N_PER_GENDER]
    if len(males) < _N_PER_GENDER or len(females) < _N_PER_GENDER:
        _fail(f"可用压测假号不足：男 {len(males)}/{_N_PER_GENDER}，女 "
              f"{len(females)}/{_N_PER_GENDER}（ou_fake_load_*，状态=单身）")

    males_all = sorted([u1] + males, key=lambda u: _uid_num(u["uid"]))
    females_all = sorted([u6] + females, key=lambda u: _uid_num(u["uid"]))
    states = {}
    for i, u in enumerate(males):
        states[u["oid"]] = _FAKE_STATES[i]
    for i, u in enumerate(females):
        states[u["oid"]] = _FAKE_STATES[i]
    states[u1["oid"]] = None  # 真号：未标记（使用者实测「未签到被挡」）
    states[u6["oid"]] = None
    return {"u1": u1, "u6": u6, "males": males, "females": females,
            "males_all": males_all, "females_all": females_all,
            "participants": males_all + females_all, "states": states,
            "p_by_oid": {u["oid"]: u for u in males_all + females_all}}


def _group_rotation(cast):
    """轮转法：第 k 位跳过同序位 k，选其余 N-1 位异性（照 seed_test_grouping.py）。"""
    males, females = cast["males_all"], cast["females_all"]
    plan = []
    for k, sel in enumerate(males):
        plan.append({"selector": sel,
                     "targets": [females[(k + 1 + i) % len(females)] for i in range(len(females) - 1)]})
    for k, sel in enumerate(females):
        plan.append({"selector": sel,
                     "targets": [males[(k + 1 + i) % len(males)] for i in range(len(males) - 1)]})
    for item in plan:
        targets = item["targets"]
        assert len(targets) == len(males) - 1
        assert len({t["oid"] for t in targets}) == len(targets)
        assert all(t["gender"] != item["selector"]["gender"] for t in targets)
    return plan


def _oto_all_opposite(cast):
    """一对一：每位假号选全部异性（6 位），真号留白。"""
    plan = []
    for sel in cast["males_all"] + cast["females_all"]:
        opp = cast["females_all"] if sel["gender"] == "男性" else cast["males_all"]
        plan.append({"selector": sel, "targets": list(opp)})
    for item in plan:
        assert all(t["gender"] != item["selector"]["gender"] for t in item["targets"])
    return plan


def _writable(plan):
    """两真号留白（走 H5 提交）：只写假号的志愿。"""
    return [i for i in plan
            if i["selector"]["uid"] not in (_REAL_MALE_UID, _REAL_FEMALE_UID)]


def _read_activities():
    acts = []
    for r in _must(bitable.search_records(C.ACTIVITY_TABLE_ID), "活动表"):
        f = r.get("fields", {})
        acts.append({"record": r, "record_id": r.get("record_id"),
                     "id": get_field_text(f, C.FIELD_ACTIVITY_ID),
                     "name": get_field_text(f, C.FIELD_ACTIVITY_NAME)})
    return acts


def _find_by_name(acts, name):
    return [a for a in acts if a["name"] == name]


def _read_signups(act_id):
    return _must(bitable.search_records(C.SIGNUP_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": C.FIELD_SIGNUP_ACTIVITY_ID,
                        "operator": "is", "value": [act_id]}],
    }), f"报名表({act_id})")


def _read_group_selections(act_id):
    return _must(bitable.search_records(C.GROUP_SELECT_TABLE, {
        "conjunction": "and",
        "conditions": [{"field_name": C.FIELD_GS_ACTIVITY_ID,
                        "operator": "is", "value": [act_id]}],
    }), f"分组选择表({act_id})")


def _read_oto_selections(act_id):
    return _must(bitable.search_records(C.ONETOONE_SELECT_TABLE, {
        "conjunction": "and",
        "conditions": [{"field_name": C.FIELD_OTO_ACTIVITY_ID,
                        "operator": "is", "value": [act_id]}],
    }), f"一对一选择表({act_id})")


def _id_num(activity_id):
    m = re.match(r"A-(\d+)$", activity_id or "")
    return int(m.group(1)) if m else 0


def _next_activity_num(acts):
    return max([_id_num(a["id"]) for a in acts], default=0)


def _compute_plan():
    users = _read_users()
    cast = _pick_cast(users)
    acts = _read_activities()

    found_group = _find_by_name(acts, _GROUP_ACT_NAME)
    found_oto = _find_by_name(acts, _OTO_ACT_NAME)
    for label, found, expected in (("分组", found_group, _GROUP_EXPECTED_ID),
                                   ("一对一", found_oto, _OTO_EXPECTED_ID)):
        if len(found) > 1:
            _fail(f"活动表里名为「{found[0]['name']}」的活动有 {len(found)} 个，请人工核对")
        if found and found[0]["id"] != expected:
            _fail(f"{label}演示活动已存在但活动ID={found[0]['id']}≠预期 {expected}（自动编号被什么改过？停手）")

    # 下一编号：飞书自动编号删除不复用，只能靠「现存编号与锚点完全吻合」来信任。
    # 任何不一致（新建/删除/又烧号）都停手回報，由人工核对后更新锚点常量。
    observed_max = _next_activity_num(acts)
    expected_new = []
    if found_group and found_oto:
        pass  # 两个都已建：幂等，无需预测
    elif not found_group and not found_oto:
        if observed_max != _ANCHOR_EXISTING_MAX:
            _fail(f"自动编号锚点核对失败：活动表现存最大编号 A-{observed_max:04d}≠锚点 "
                  f"A-{_ANCHOR_EXISTING_MAX:04d}（已发上限锚点 A-{_ANCHOR_ISSUED_MAX:04d} 依据："
                  f"A-0014/A-0015 被审计临时活动烧掉、删除不复用）。无法可靠推断真实下一编号，"
                  f"请人工核对活动表后更新脚本锚点常量；不创建，停手回報")
        next_num = _ANCHOR_ISSUED_MAX + 1
        expected_new = [("group", f"A-{next_num:04d}"), ("oto", f"A-{next_num + 1:04d}")]
    else:
        # run 中断重跑：已建的那个必须是最高编号，才能确定下一个编号；否则停手。
        existing = (found_group or found_oto)[0]
        existing_num = _id_num(existing["id"])
        if observed_max != existing_num:
            _fail(f"自动编号核对失败：已存在演示活动 {existing['id']}，但活动表现存最大编号 "
                  f"A-{observed_max:04d}≠A-{existing_num:04d}（期间有新建/删除，删除不复用）。"
                  f"无法可靠推断下一个编号，请人工核对；不创建，停手回報")
        next_num = existing_num + 1
        expected_new.append(("group" if not found_group else "oto", f"A-{next_num:04d}"))
    for kind, eid in expected_new:
        want = _GROUP_EXPECTED_ID if kind == "group" else _OTO_EXPECTED_ID
        if eid != want:
            _fail(f"活动表下一自动编号推算为 {eid}，与预期 {want} 不符；不创建，停手回報")

    return {"cast": cast, "acts": acts,
            "group": found_group[0] if found_group else None,
            "oto": found_oto[0] if found_oto else None,
            "observed_max": observed_max,
            "expected_new": expected_new}


def _print_cast(cast):
    print(f"\n【参与者 12 人】真号 2 + 假号 10（ou_fake_load_*，uid 升序取第 "
          f"{_FAKE_MALE_OFFSET + 1}~{_FAKE_MALE_OFFSET + _N_PER_GENDER} 男 / 第 "
          f"{_FAKE_FEMALE_OFFSET + 1}~{_FAKE_FEMALE_OFFSET + _N_PER_GENDER} 女，与 A-0011 错开）：")
    for p in cast["participants"]:
        tag = "真号" if p["uid"] in (_REAL_MALE_UID, _REAL_FEMALE_UID) else "假号"
        print(f"  {p['uid']}  {p['gender']}  {p['nick']}  oid={_mask(p['oid'])}  [{tag}]")


def _print_plan(plan):
    cast = plan["cast"]
    print("\n========== PLAN（只读，不写任何数据） ==========")
    _print_cast(cast)

    print("\n【活动】")
    for label, key, name, eid in (("分组", "group", _GROUP_ACT_NAME, _GROUP_EXPECTED_ID),
                                  ("一对一", "oto", _OTO_ACT_NAME, _OTO_EXPECTED_ID)):
        cur = plan[key]
        if cur:
            print(f"  = {label}演示：已存在 {cur['id']}「{name}」（不重复创建）")
        else:
            exp = dict(plan["expected_new"]).get(key, "?")
            print(f"  + {label}演示：不存在 → 将创建「{name}」（活动ID 自动编号，预期 {exp}）")
    if plan["expected_new"]:
        exp_ids = "、".join(eid for _, eid in plan["expected_new"])
        print(f"    自动编号锚点核对通过：现存最大 A-{plan['observed_max']:04d}，"
              f"下一编号 {exp_ids}（删除不复用；锚点=现存 {_ANCHOR_EXISTING_MAX}/已发 {_ANCHOR_ISSUED_MAX}）")
    print(f"    创建字段：活动状态=报名中、当前报名人数=12、报名人数上限=无、时间三栏；"
          f"签到功能开启随后单独置「是」；分组/一对一状态与 flag 保持空。")

    print(f"\n【报名】每个活动 12 条（状态=已报名；查重：同活动同人已有有效报名则只对齐签到状态）：")
    counts = {"按时": 0, "未到": 0, None: 0}
    for p in cast["participants"]:
        counts[cast["states"][p["oid"]]] += 1

    def _who(state):
        return "、".join(f"{p['uid']}" for p in cast["participants"]
                         if cast["states"][p["oid"]] == state)
    print(f"  已到（按时）{counts['按时']}：{_who('按时')}")
    print(f"  未到（未到）{counts['未到']}：{_who('未到')}")
    print(f"  未标记（空）{counts[None]}：{_who(None)}  ← 含猴哥/一二三。，可实测「未签到被挡」")

    gplan = _writable(_group_rotation(cast))
    oplan = _writable(_oto_all_opposite(cast))
    gid = plan["group"]["id"] if plan["group"] else _GROUP_EXPECTED_ID
    oid = plan["oto"]["id"] if plan["oto"] else _OTO_EXPECTED_ID
    print(f"\n【{gid} 分组志愿】将创建/对齐 {len(gplan)} 条（两真号留白走 H5）：")
    for item in gplan:
        sel = item["selector"]
        print(f"  + {sel['uid']}({sel['gender']}) {len(item['targets'])} 个志愿 -> "
              + ",".join(t["uid"] for t in item["targets"]))
    print(f"【{oid} 一对一志愿】将创建/对齐 {len(oplan)} 条（两真号留白走 H5）：")
    for item in oplan:
        sel = item["selector"]
        print(f"  + {sel['uid']}({sel['gender']}) {len(item['targets'])} 个志愿 -> "
              + ",".join(t["uid"] for t in item["targets"]))

    print("\n【不触碰】A-0011（当前为分组模式）/ A-0012 / A-0013 的任何数据。")
    print("（plan 模式：未执行任何写入。确认无误后用 run 执行。）")


def _create_activity(name):
    now_ms = int(time.time() * 1000)
    fields = {
        C.FIELD_ACTIVITY_NAME: name,
        C.FIELD_ACTIVITY_STATUS: "报名中",
        C.FIELD_ACTIVITY_CURRENT_SIGNUP: 12,
        "报名人数上限": "无",
        "发布时间": now_ms,
        "开始时间": now_ms + 86400000,
        "结束时间": now_ms + 172800000,
    }
    rec = bitable.create_record(C.ACTIVITY_TABLE_ID, fields)
    if not rec:
        _fail(f"创建活动失败：「{name}」（中止，未做任何删除）")
    act_id = get_field_text(rec.get("fields", {}), C.FIELD_ACTIVITY_ID)
    if not act_id:
        # 自动编号字段创建响应偶发不返回，兜底再读一次
        rec2 = bitable.get_record(C.ACTIVITY_TABLE_ID, rec.get("record_id"))
        act_id = get_field_text((rec2 or {}).get("fields", {}), C.FIELD_ACTIVITY_ID)
    if not act_id:
        _fail(f"活动「{name}」创建成功但读不到活动ID（中止）")
    return {"record_id": rec.get("record_id"), "id": act_id, "name": name}


def _ensure_signups(act_id, cast):
    exist = _read_signups(act_id)
    by_oid = {}
    for r in exist:
        oid = get_field_text(r.get("fields", {}), C.FIELD_SIGNUP_OPENID)
        if oid:
            by_oid.setdefault(oid, []).append(r)
    created = updated = 0
    for p in cast["participants"]:
        state = cast["states"][p["oid"]]
        live = [r for r in by_oid.get(p["oid"], [])
                if get_select_value(r.get("fields", {}), C.FIELD_SIGNUP_STATUS) != _SIGNUP_CANCELLED]
        if len(live) > 1:
            _fail(f"{p['uid']} 在 {act_id} 有多条有效报名（{len(live)}），请人工核对后再跑")
        if not live:
            fields = {
                C.FIELD_SIGNUP_ACTIVITY_ID: act_id,
                C.FIELD_SIGNUP_OPENID: p["oid"],
                C.FIELD_SIGNUP_NICKNAME: p["nick"],
                C.FIELD_SIGNUP_STATUS: _SIGNUP_SIGNED,
            }
            if state:
                fields[C.FIELD_SIGNUP_ATTENDANCE] = state
            if not bitable.create_record(C.SIGNUP_TABLE_ID, fields):
                _fail(f"创建报名失败：{p['uid']}（中止，未做任何删除）")
            created += 1
        else:
            cur = get_field_text(live[0].get("fields", {}), C.FIELD_SIGNUP_ATTENDANCE)
            want = state or ""
            if cur != want:
                if not bitable.update_record(C.SIGNUP_TABLE_ID, live[0].get("record_id"),
                                             {C.FIELD_SIGNUP_ATTENDANCE: want}):
                    _fail(f"更新签到状态失败：{p['uid']}（中止）")
                updated += 1
    return created, updated


def _write_choices(table_id, fields, plan_items, act_id):
    """fields: {act_field, sel_field, name_field, gender_field, choices}。"""
    exist = {}
    records = _read_group_selections(act_id) if fields["table_key"] == "group" \
        else _read_oto_selections(act_id)
    for r in records:
        oid = get_field_text(r.get("fields", {}), fields["sel_field"])
        if oid:
            exist[oid] = r
    created = updated = 0
    for item in plan_items:
        sel = item["selector"]
        row = {
            fields["act_field"]: act_id,
            fields["sel_field"]: sel["oid"],
            fields["name_field"]: sel["nick"],
            fields["gender_field"]: sel["gender"],
        }
        targets = [t["oid"] for t in item["targets"]]
        # 写满所有志愿位：未用的置空（H5 同款语义，保证可重跑收敛）
        for i, cf in enumerate(fields["choices"]):
            row[cf] = targets[i] if i < len(targets) else ""
        if sel["oid"] in exist:
            if not bitable.update_record(table_id, exist[sel["oid"]].get("record_id"), row):
                _fail(f"更新志愿失败：{sel['uid']}（中止，未做任何删除）")
            updated += 1
        else:
            if not bitable.create_record(table_id, row):
                _fail(f"创建志愿失败：{sel['uid']}（中止，未做任何删除）")
            created += 1
    return created, updated


_GROUP_FIELDS = None
_OTO_FIELDS = None


def _init_choice_fields():
    global _GROUP_FIELDS, _OTO_FIELDS
    _GROUP_FIELDS = {"table_key": "group", "act_field": C.FIELD_GS_ACTIVITY_ID,
                     "sel_field": C.FIELD_GS_SELECTOR_OID, "name_field": C.FIELD_GS_SELECTOR_NAME,
                     "gender_field": C.FIELD_GS_SELECTOR_GENDER, "choices": C.FIELD_GS_CHOICES}
    _OTO_FIELDS = {"table_key": "oto", "act_field": C.FIELD_OTO_ACTIVITY_ID,
                   "sel_field": C.FIELD_OTO_SELECTOR_OID, "name_field": C.FIELD_OTO_SELECTOR_NAME,
                   "gender_field": C.FIELD_OTO_SELECTOR_GENDER, "choices": C.FIELD_OTO_CHOICES}


def _apply_signin_flag(act):
    """签到功能开启=是（走 bot 与 H5 管理台同一写入器 lib_grouping.set_signin_flag）。"""
    from grouping import configure_grouping_lib
    from lib import grouping as lib_grouping

    configure_grouping_lib(None)
    if lib_grouping.signin_enabled(act["record"].get("fields", {})):
        return False
    res = lib_grouping.set_signin_flag(act["id"], True, client=bitable, activity=act["record"])
    if res.get("code") != "OK":
        _fail(f"设置签到功能开启失败：{act['id']}（{res.get('code')}，中止）")
    return True


def _assert_mode_fields_empty(act, what):
    f = act["record"].get("fields", {})
    bad = []
    for field, label in ((C.FIELD_ACT_GROUP_STATUS, "分组状态"),
                         (C.FIELD_ACT_ONETOONE_STATUS, "一对一状态"),
                         (C.FIELD_ACT_GROUP_FLAG, "分组功能开启"),
                         (C.FIELD_ACT_ONETOONE_FLAG, "一对一功能开启")):
        v = get_field_text(f, field)
        if v:
            bad.append(f"{label}={v!r}")
    if bad:
        _fail(f"{what} 的模式栏位非空（{'；'.join(bad)}），与「未设定模式」演示前提冲突；"
              f"种子不越权清除，停手回報")


def _cmd_run():
    plan = _compute_plan()
    cast = plan["cast"]
    print("========== RUN 开始（只写测试 base） ==========")

    print("[1/5] 活动：")
    group = plan["group"]
    oto = plan["oto"]
    if not group:
        group = _create_activity(_GROUP_ACT_NAME)
        print(f"  + 创建「{group['name']}」-> {group['id']}（{group['record_id']}）")
        if group["id"] != _GROUP_EXPECTED_ID:
            _fail(f"新建分组演示活动ID={group['id']}≠预期 {_GROUP_EXPECTED_ID}，停手回報")
        group = {"record": bitable.get_record(C.ACTIVITY_TABLE_ID, group["record_id"]),
                 "record_id": group["record_id"], "id": group["id"], "name": group["name"]}
    else:
        print(f"  = 已存在 {group['id']}「{group['name']}」")
    if not oto:
        oto = _create_activity(_OTO_ACT_NAME)
        print(f"  + 创建「{oto['name']}」-> {oto['id']}（{oto['record_id']}）")
        if oto["id"] != _OTO_EXPECTED_ID:
            _fail(f"新建一对一演示活动ID={oto['id']}≠预期 {_OTO_EXPECTED_ID}，停手回報")
        oto = {"record": bitable.get_record(C.ACTIVITY_TABLE_ID, oto["record_id"]),
               "record_id": oto["record_id"], "id": oto["id"], "name": oto["name"]}
    else:
        print(f"  = 已存在 {oto['id']}「{oto['name']}」")
    _assert_mode_fields_empty(group, group["id"])
    _assert_mode_fields_empty(oto, oto["id"])

    print("[2/5] 报名：")
    for act in (group, oto):
        c, u = _ensure_signups(act["id"], cast)
        print(f"  {act['id']}：创建 {c} 条，对齐签到状态 {u} 条")

    _init_choice_fields()
    print(f"[3/5] 分组志愿（{group['id']}）：")
    c, u = _write_choices(C.GROUP_SELECT_TABLE, _GROUP_FIELDS, _writable(_group_rotation(cast)),
                          group["id"])
    print(f"  {group['id']}：创建 {c} 条，覆盖更新 {u} 条（两真号留白）")

    print(f"[4/5] 一对一志愿（{oto['id']}）：")
    c, u = _write_choices(C.ONETOONE_SELECT_TABLE, _OTO_FIELDS, _writable(_oto_all_opposite(cast)),
                          oto["id"])
    print(f"  {oto['id']}：创建 {c} 条，覆盖更新 {u} 条（两真号留白）")

    print("[5/5] 活动设置（签到功能开启=是）：")
    for act in (group, oto):
        changed = _apply_signin_flag(act)
        print(f"  {act['id']}：签到功能开启 {'置为「是」' if changed else '已是「是」（跳过）'}")

    print("读回验证：")
    ok, _ = _verify_state(group["id"], oto["id"], cast)
    if not ok:
        _fail("读回验证未全部通过（见上方 ⛔ 项）")
    print("\n✅ run 完成：2 活动 + 24 报名（含签到三态）+ 20 志愿，读回验证全部通过。")


def _verify_state(group_id, oto_id, cast):
    lines, issues = [], []
    acts = _read_activities()
    by_id = {a["id"]: a for a in acts}

    # 场景 30 秒对账前「当前报名人数」可能短暂滞后，只做报告不做判据
    for aid, label in ((group_id, f"{group_id} 分组"), (oto_id, f"{oto_id} 一对一")):
        act = by_id.get(aid)
        if not act:
            issues.append(f"{aid} 找不到")
            continue
        f = act["record"].get("fields", {})
        status = get_select_value(f, C.FIELD_ACTIVITY_STATUS)
        gs = get_field_text(f, C.FIELD_ACT_GROUP_STATUS)
        os_ = get_field_text(f, C.FIELD_ACT_ONETOONE_STATUS)
        gf = get_field_text(f, C.FIELD_ACT_GROUP_FLAG)
        of = get_field_text(f, C.FIELD_ACT_ONETOONE_FLAG)
        sf = get_field_text(f, C.FIELD_ACT_SIGNIN_FLAG)
        cur = get_field_number(f, C.FIELD_ACTIVITY_CURRENT_SIGNUP, 0)
        lines.append(f"[{label}] 状态={status} 分组状态={gs or '空'} 一对一状态={os_ or '空'} "
                     f"分组flag={gf or '空'} 一对一flag={of or '空'} 签到功能开启={sf or '空'} "
                     f"当前报名人数={cur}")
        if status != "报名中":
            issues.append(f"{aid} 活动状态≠报名中：{status!r}")
        if (gs, os_, gf, of) != ("", "", "", ""):
            issues.append(f"{aid} 模式栏位非空：{gs!r}/{os_!r}/{gf!r}/{of!r}")
        if sf != "是":
            issues.append(f"{aid} 签到功能开启≠是：{sf!r}")

        # 报名 12 人 + 签到三态
        signups = _read_signups(aid)
        signed, cancelled, att = [], 0, {}
        for s in signups:
            sf2 = s.get("fields", {})
            st = get_select_value(sf2, C.FIELD_SIGNUP_STATUS)
            oid = get_field_text(sf2, C.FIELD_SIGNUP_OPENID)
            if st == _SIGNUP_CANCELLED:
                cancelled += 1
            elif st == _SIGNUP_SIGNED:
                signed.append(oid)
                att[oid] = get_field_text(sf2, C.FIELD_SIGNUP_ATTENDANCE)
        wrong = [p["uid"] for p in cast["participants"]
                 if att.get(p["oid"]) != (cast["states"][p["oid"]] or "")]
        extra = [o for o in signed if o not in cast["p_by_oid"]]
        dup = [o for o in set(signed) if signed.count(o) > 1]
        n_present = sum(1 for p in cast["participants"] if att.get(p["oid"]) == "按时")
        n_noshow = sum(1 for p in cast["participants"] if att.get(p["oid"]) == "未到")
        n_blank = sum(1 for p in cast["participants"] if not att.get(p["oid"]))
        lines.append(f"[{aid} 报名] 有效 {len(signed)}/12（已取消 {cancelled} 条）、"
                     f"全为演示 12 人={not extra}、无重复={not dup}；"
                     f"签到三态：已到 {n_present} / 未到 {n_noshow} / 未标记 {n_blank}")
        if len(signed) != 12:
            issues.append(f"{aid} 有效报名数≠12：{len(signed)}")
        if extra or dup or wrong:
            issues.append(f"{aid} 报名异常：extra={extra} dup={dup} 状态不符={wrong}")

    # 分组志愿
    gplan = _writable(_group_rotation(cast))
    sels = _read_group_selections(group_id)
    sel_by_oid = {}
    for r in sels:
        oid = get_field_text(r.get("fields", {}), C.FIELD_GS_SELECTOR_OID)
        sel_by_oid.setdefault(oid, []).append(r)
    real_oids = {p["oid"] for p in cast["participants"]
                 if p["uid"] in (_REAL_MALE_UID, _REAL_FEMALE_UID)}
    real_has = [oid for oid in real_oids if oid in sel_by_oid]
    n_ok = n_bad = 0
    for item in gplan:
        sel = item["selector"]
        recs = sel_by_oid.get(sel["oid"], [])
        if len(recs) != 1:
            issues.append(f"{group_id} 志愿记录数异常：{sel['uid']} x{len(recs)}")
            n_bad += 1
            continue
        got = [get_field_text(recs[0].get("fields", {}), cf) for cf in C.FIELD_GS_CHOICES]
        want = [t["oid"] for t in item["targets"]]
        if got != want + [""] * (len(C.FIELD_GS_CHOICES) - len(want)):
            issues.append(f"{group_id} 志愿不符：{sel['uid']} 得 {got} 应 {want}+空")
            n_bad += 1
        else:
            n_ok += 1
    extra_sel = [oid for oid in sel_by_oid if oid not in {i['selector']['oid'] for i in gplan}]
    lines.append(f"[{group_id} 志愿] {len(sels)} 条（期望 10）；逐条与轮转计划一致 {n_ok}/{len(gplan)}；"
                 f"两真号留白={'是' if not real_has else '否'}；多余记录={extra_sel}")
    if len(sels) != len(gplan) or n_bad or real_has or extra_sel:
        issues.append(f"{group_id} 志愿状态异常（{n_bad} 条不符）")

    # 一对一志愿
    oplan = _writable(_oto_all_opposite(cast))
    sels2 = _read_oto_selections(oto_id)
    sel_by_oid2 = {}
    for r in sels2:
        oid = get_field_text(r.get("fields", {}), C.FIELD_OTO_SELECTOR_OID)
        sel_by_oid2.setdefault(oid, []).append(r)
    n_ok2 = n_bad2 = 0
    for item in oplan:
        sel = item["selector"]
        recs = sel_by_oid2.get(sel["oid"], [])
        if len(recs) != 1:
            issues.append(f"{oto_id} 志愿记录数异常：{sel['uid']} x{len(recs)}")
            n_bad2 += 1
            continue
        got = [get_field_text(recs[0].get("fields", {}), cf) for cf in C.FIELD_OTO_CHOICES]
        want = [t["oid"] for t in item["targets"]]
        if got != want + [""] * (len(C.FIELD_OTO_CHOICES) - len(want)):
            issues.append(f"{oto_id} 志愿不符：{sel['uid']} 得 {got} 应全部异性")
            n_bad2 += 1
        else:
            n_ok2 += 1
    real_has2 = [oid for oid in real_oids if oid in sel_by_oid2]
    lines.append(f"[{oto_id} 志愿] {len(sels2)} 条（期望 10）；逐条=全部异性 {n_ok2}/{len(oplan)}；"
                 f"两真号留白={'是' if not real_has2 else '否'}")
    if len(sels2) != len(oplan) or n_bad2 or real_has2:
        issues.append(f"{oto_id} 志愿状态异常（{n_bad2} 条不符）")

    # 既有活动：只报告，不动（A-0011 当前为分组模式，保持原样）
    for aid, expect_desc in (("A-0011", "17 已报名+4 已取消（当前为分组模式，保持原样）"),
                             ("A-0012", "100 已报名"), ("A-0013", "1 已报名")):
        su = _read_signups(aid)
        cnt = {}
        for s in su:
            st = get_select_value(s.get("fields", {}), C.FIELD_SIGNUP_STATUS)
            cnt[st] = cnt.get(st, 0) + 1
        lines.append(f"[不动] {aid}：报名 {len(su)} 条 {cnt}（{expect_desc}）")

    for line in lines:
        print("  " + line)
    for issue in issues:
        print("  ⛔ " + issue)
    return (not issues), lines


def _cmd_verify():
    plan = _compute_plan()
    if not plan["group"] or not plan["oto"]:
        _fail("两个演示活动尚未齐全（先跑 run）")
    print("========== VERIFY（只读复核） ==========")
    ok, _ = _verify_state(plan["group"]["id"], plan["oto"]["id"], plan["cast"])
    if not ok:
        _fail("复核未通过（见上方 ⛔ 项）")
    print("✅ 复核通过。")


def _cmd_plan():
    plan = _compute_plan()
    _print_plan(plan)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "plan":
        _cmd_plan()
    elif mode == "run":
        _cmd_run()
    elif mode == "verify":
        _cmd_verify()
    else:
        print("用法：YIXIANQIAN_ENV=dev web/backend/venv/bin/python "
              "scripts/dev/seed_acceptance.py plan|run|verify")
        sys.exit(2)


if __name__ == "__main__":
    main()
