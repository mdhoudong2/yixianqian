#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试服「100 人一对一」压测种子 + 执行 + 读回验证（只允许在 /opt/yixianqian-test 执行）。

造 100 个假号（50 男 + 50 女，open_id=ou_fake_oto_{male|female}_NNN），新建一个专用活动
（一对一功能开启=是），全部报名 + 轮转法预填 7 志愿，走真实 bot 命令链
handle_admin_stop_onetoone("<活动ID> <女生场次>")——「女生坐镇、男生上门」均衡分配
（轮次缺省=最新+1，新活动即第 1 轮），最后读回结果表逐条验证均衡不变量。

- 结果行数 = 2 × 女生数 × K（50 女 × 10 = 500 女名单 + 500 男名单 = 1000 条）；
- 每个女生恰好 K 位不同男性来访、排名 1..K 无重复；
- 每个男生 base~base+1 位、排名 1..base(+1) 无重复（50/50 时恰好 10）；
- 双向一致：女 f 名单含男 m ⇔ 男 m 名单含女 f；无自己、无同性。

「用户ID」是自动编号(type=1005)不可写；「活动ID」同样是自动编号，创建后读回。所以
本脚本不写这两个字段，只写 飞书用户ID/open_id 等可写字段。

用法（在 /opt/yixianqian-test 仓库根，必须显式 YIXIANQIAN_ENV=dev）：
  YIXIANQIAN_ENV=dev bot/venv/bin/python scripts/dev/seed_onetoone_100.py plan   # 只打印计划
  YIXIANQIAN_ENV=dev bot/venv/bin/python scripts/dev/seed_onetoone_100.py run    # 种子+执行+验证
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
    "ONETOONE_SELECT_TABLE": "tblykkDzC0LaJxi8",  # 志愿表已统一：= 分组选择表（2026-10-13）
    "ONETOONE_RESULT_TABLE": "tbllYrqHD81gHyI2",
}
_N_MALE = 50
_N_FEMALE = 50
_FEMALE_CHATS = 10  # 女生场次 K：每个女生坐镇、由 K 位男生上门（50 男时 ≤ 50）
_PREFIX_MALE = "ou_fake_oto_male_"
_PREFIX_FEMALE = "ou_fake_oto_female_"
_FIELD_USER_ID = "用户ID"  # 自动编号，只读
_ACTIVITY_NAME = "百人对一对一演示"

# 资料相似度维度取值（与 web/backend/config.py 的选项池子集一致，保证可写入）
_EDUCATIONS = ["大专", "本科", "硕士", "博士"]
_CITIES = ["深圳", "广州", "北京", "上海", "杭州"]
_CHURCHES = ["深圳南头堂", "广州石室圣心堂", "北京西什库堂", "上海徐家汇堂", "杭州天主堂"]
_HOBBIES = ["听音乐", "看电影", "看书", "旅游爱好者", "做饭", "寻觅美食", "养宠物", "看小说"]
_SPORTS = ["游泳", "跑步", "爬山", "羽毛球", "篮球", "瑜伽", "骑行", "跳舞"]
_TRAITS = ["比较主动", "性格沉稳", "积极乐观", "有始有终", "理性待事", "善于交际", "容易相处", "有点小幽默"]
_MBTI = ["E", "I", "S", "N", "T", "F", "J", "P"]

C = None
bitable = None
get_field_text = get_select_value = get_field_number = None


def _fail(msg):
    print(f"⛔ {msg}")
    sys.exit(1)


def _mask(oid, keep=10):
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
        _fail(f"守卫2 失败：无法从生产配置 {_PROD_CFG_PATH} 解析 BASE_TOKEN，拒绝执行")
    if _constants.BASE_TOKEN == prod_base:
        _fail("守卫2 失败：解析出的 BASE_TOKEN 与生产 base 相同（拒绝连生产）")
    if _constants.BASE_TOKEN != repo_base:
        _fail("守卫2 失败：constants.BASE_TOKEN 与本仓库 bot/local_config.py 文本解析值不一致")
    print(f"✅ 守卫2 BASE_TOKEN≠生产 且=本仓库 local_config"
          f"（test={_mask(_constants.BASE_TOKEN)} prod={_mask(prod_base)}）")

    got = {k: getattr(_constants, k) for k in _EXPECTED_TABLES}
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

    # handler 内部走 grouping_lock 与业务入口的 _require_configured 闸门：必须在使用前
    # 注入（口径同 bot/yixianqian_bot_ws.py 的 main()）。本脚本的执行链只用 lib.onetoone
    # 自己的配置 + 锁，但注入分组一项与 main() 对齐，避免日后接线到 lib.grouping 业务函数时复发。
    from constants import SHARED_DATA_DIR
    from grouping import configure_grouping_lib
    configure_grouping_lib(SHARED_DATA_DIR)
    from onetoone import configure_onetoone_lib
    configure_onetoone_lib(SHARED_DATA_DIR)


_guard_and_import()


def _must(records, what):
    if records is None:
        _fail(f"读取失败：{what}（飞书查询返回错误，已中止，不做任何写入）")
    return records


def _pick(pool, n, offset):
    return [pool[(offset + j) % len(pool)] for j in range(n)]


def _build_users():
    """100 个假号（50 男 + 50 女）。"""
    males, females = [], []
    for i in range(_N_MALE):
        males.append({
            "oid": f"{_PREFIX_MALE}{i + 1:03d}",
            "gender": "男性",
            "nickname": f"百人男{i + 1:03d}",
            "name": f"百人男{i + 1:03d}",
            "education": _EDUCATIONS[i % len(_EDUCATIONS)],
            "city": _CITIES[i % len(_CITIES)],
            "church": _CHURCHES[i % len(_CHURCHES)],
            "hobbies": _pick(_HOBBIES, 3, i),
            "sports": _pick(_SPORTS, 2, i),
            "traits": _pick(_TRAITS, 2, i),
            "mbti": _pick(_MBTI, 4, i),
        })
    for i in range(_N_FEMALE):
        females.append({
            "oid": f"{_PREFIX_FEMALE}{i + 1:03d}",
            "gender": "女性",
            "nickname": f"百人女{i + 1:03d}",
            "name": f"百人女{i + 1:03d}",
            "education": _EDUCATIONS[(i + 2) % len(_EDUCATIONS)],
            "city": _CITIES[(i + 2) % len(_CITIES)],
            "church": _CHURCHES[(i + 2) % len(_CHURCHES)],
            "hobbies": _pick(_HOBBIES, 3, i + 5),
            "sports": _pick(_SPORTS, 2, i + 5),
            "traits": _pick(_TRAITS, 2, i + 5),
            "mbti": _pick(_MBTI, 4, i + 5),
        })
    return males, females


def _user_fields(u):
    return {
        C.FIELD_FEISHU_ID: u["oid"],
        C.FIELD_NICKNAME: u["nickname"],
        "姓名": u["name"],
        C.FIELD_GENDER: u["gender"],
        C.FIELD_ACCOUNT_STATUS: "单身",
        C.FIELD_EDUCATION: u["education"],
        C.FIELD_CITY: u["city"],
        C.FIELD_CHURCH: u["church"],
        C.FIELD_SELF_HOBBIES: u["hobbies"],
        C.FIELD_SELF_SPORTS: u["sports"],
        C.FIELD_SELF_TRAITS: u["traits"],
        C.FIELD_MBTI: u["mbti"],
    }


def _create_users(males, females):
    """创建 100 个假号，已存在（按 open_id）则跳过。返回带 uid 的列表。"""
    created = skipped = 0
    out = []
    for u in males + females:
        exist = bitable.search_records(C.USER_TABLE_ID, [
            {"field_name": C.FIELD_FEISHU_ID, "operator": "is", "value": [u["oid"]]}])
        if exist:
            rec = exist[0]
            u["uid"] = get_field_text(rec.get("fields", {}), _FIELD_USER_ID)
            u["record_id"] = rec.get("record_id")
            skipped += 1
            out.append(u)
            continue
        rec = bitable.create_record(C.USER_TABLE_ID, _user_fields(u))
        if not rec:
            _fail(f"创建用户失败：{u['oid']}（中止）")
        u["uid"] = get_field_text(rec.get("fields", {}), _FIELD_USER_ID)
        u["record_id"] = rec.get("record_id")
        created += 1
        out.append(u)
    print(f"  用户：新建 {created}，已存在跳过 {skipped}")
    return out


def _create_activity():
    """新建一个专用活动，读回自动编号活动ID。"""
    now_ms = int(time.time() * 1000)
    fields = {
        C.FIELD_ACTIVITY_NAME: _ACTIVITY_NAME,
        C.FIELD_ACTIVITY_STATUS: "报名中",
        C.FIELD_ACT_ONETOONE_FLAG: "是",
        "报名人数上限": "无",
        "当前报名人数": _N_MALE + _N_FEMALE,
        "发布时间": now_ms,
        "开始时间": now_ms + 86400000,
        "结束时间": now_ms + 172800000,
    }
    rec = bitable.create_record(C.ACTIVITY_TABLE_ID, fields)
    if not rec:
        _fail("创建活动失败（中止）")
    act_id = get_field_text(rec.get("fields", {}), C.FIELD_ACTIVITY_ID)
    if not act_id:
        # 自动编号字段创建响应偶发不返回，兜底再读一次
        rec2 = bitable.get_record(C.ACTIVITY_TABLE_ID, rec.get("record_id"))
        act_id = get_field_text(rec2.get("fields", {}), C.FIELD_ACTIVITY_ID)
    if not act_id:
        _fail("创建活动成功但读不到活动ID（中止）")
    return act_id, rec.get("record_id")


def _enroll(act_id, users):
    """100 人报名（按 open_id 防重），同笔写入「签到状态=按时」。

    模拟全员现场到场（签到默认开启后的执行池口径）：签到功能默认打开后，
    执行池按「已到」（按时/迟到）过滤，测试服报名签到状态为空会把执行池
    清空；本 seed 不测签到闸门本身，故统一置「按时」。
    """
    ok = 0
    for u in users:
        exist = bitable.search_records(C.SIGNUP_TABLE_ID, [
            {"field_name": C.FIELD_SIGNUP_ACTIVITY_ID, "operator": "is", "value": [act_id]},
            {"field_name": C.FIELD_SIGNUP_OPENID, "operator": "is", "value": [u["oid"]]}])
        if exist:
            continue
        rec = bitable.create_record(C.SIGNUP_TABLE_ID, {
            C.FIELD_SIGNUP_ACTIVITY_ID: act_id,
            C.FIELD_SIGNUP_OPENID: u["oid"],
            C.FIELD_SIGNUP_NICKNAME: u["nickname"],
            C.FIELD_SIGNUP_STATUS: "已报名",
            # 模拟全员现场到场（签到默认开启后的执行池口径）
            C.FIELD_SIGNUP_ATTENDANCE: "按时",
        })
        if not rec:
            _fail(f"报名失败：{u['oid']}（中止）")
        ok += 1
    print(f"  报名：新增 {ok} 条（已报名跳过 {len(users) - ok}）")
    return ok


def _mark_signups_attendance_present(act_id):
    """该活动全部「已报名」报名记录 → 签到状态「按时」（模拟全员现场到场）。

    与 seed_test_onetoone/seed_test_grouping 的 helper 同款语义：空或旧值一律
    盲覆盖为「按时」；查询失败/部分失败 fail-closed 中止。`_enroll` 对已存在
    报名直接跳过、不补签到，重跑/中断遗留会把执行池清空，本步兜底拉回。
    """
    signups = _must(bitable.search_records(C.SIGNUP_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": C.FIELD_SIGNUP_ACTIVITY_ID, "operator": "is",
             "value": [act_id]},
            {"field_name": C.FIELD_SIGNUP_STATUS, "operator": "is",
             "value": ["已报名"]},
        ],
    }), f"报名表({act_id} 的已报名记录)")
    updates = [{"record_id": r.get("record_id"),
                "fields": {C.FIELD_SIGNUP_ATTENDANCE: "按时"}}
               for r in signups if r.get("record_id")]
    if not updates:
        _fail(f"{act_id} 没有「已报名」报名记录，无法置「按时」（中止）")
    updated = bitable.batch_update_records(C.SIGNUP_TABLE_ID, updates)
    if updated != len(updates):
        _fail(f"签到状态批量更新失败：{updated}/{len(updates)} 条（中止）")
    print(f"  签到状态：{updated} 条「已报名」→ 按时（模拟全员现场到场）")


def _seed_choices(act_id, males, females):
    """轮转法：每人从对侧选 7 位异性（跳过同序位）。"""
    plan = []
    for k, m in enumerate(males):
        plan.append({"selector": m, "targets": [females[(k + 1 + i) % len(females)] for i in range(7)]})
    for k, f in enumerate(females):
        plan.append({"selector": f, "targets": [males[(k + 1 + i) % len(males)] for i in range(7)]})
    for item in plan:
        assert len(item["targets"]) == 7 and len({t["oid"] for t in item["targets"]}) == 7
        assert all(t["gender"] != item["selector"]["gender"] for t in item["targets"])

    written = 0
    for item in plan:
        sel = item["selector"]
        fields = {
            C.FIELD_OTO_ACTIVITY_ID: act_id,
            C.FIELD_OTO_SELECTOR_OID: sel["oid"],
            C.FIELD_OTO_SELECTOR_NAME: sel["nickname"],
            C.FIELD_OTO_SELECTOR_GENDER: sel["gender"],
        }
        for i, tgt in enumerate(item["targets"]):
            fields[C.FIELD_OTO_CHOICES[i]] = tgt["oid"]
        # 防重：同活动同人已选则覆盖更新
        exist = bitable.search_records(C.ONETOONE_SELECT_TABLE, [
            {"field_name": C.FIELD_OTO_ACTIVITY_ID, "operator": "is", "value": [act_id]},
            {"field_name": C.FIELD_OTO_SELECTOR_OID, "operator": "is", "value": [sel["oid"]]}])
        if exist:
            rec = bitable.update_record(C.ONETOONE_SELECT_TABLE, exist[0]["record_id"], fields)
        else:
            rec = bitable.create_record(C.ONETOONE_SELECT_TABLE, fields)
        if not rec:
            _fail(f"写志愿失败：{sel['oid']}（中止）")
        written += 1
    print(f"  志愿：写入 {written} 条（每人 7 异性）")
    return plan


def _run_matching(act_id):
    from onetoone import handle_admin_stop_onetoone
    reply = handle_admin_stop_onetoone(f"{act_id} {_FEMALE_CHATS}")
    print(f"  handle_admin_stop_onetoone({act_id} {_FEMALE_CHATS}) -> {reply.splitlines()[0]}")
    if "匹配完成" not in reply:
        _fail(f"执行一对一失败：{reply}")
    return reply


def _verify(act_id):
    """读回结果表，逐条验证「女生坐镇」均衡不变量（纯读，自包含，性别取结果行本身）。"""
    results = bitable.search_records(C.ONETOONE_RESULT_TABLE, [
        {"field_name": C.FIELD_OTO_ACTIVITY_ID, "operator": "is", "value": [act_id]}])
    results = _must(results, "一对一结果表")

    owners = {}  # oid -> {"gender": "男性"/"女性", "rows": [(rank, target_oid)]}
    for r in results:
        f = r.get("fields", {})
        uoid = get_field_text(f, C.FIELD_OTO_USER_OID)
        gender = get_field_text(f, C.FIELD_OTO_USER_GENDER)
        rank = get_field_number(f, C.FIELD_OTO_RANK, 0)
        target = get_field_text(f, C.FIELD_OTO_TARGET_OID)
        owners.setdefault(uoid, {"gender": gender, "rows": []})["rows"].append((rank, target))

    male_oids = {o for o, v in owners.items() if v["gender"] == "男性"}
    female_oids = {o for o, v in owners.items() if v["gender"] == "女性"}
    m, f = len(male_oids), len(female_oids)
    issues = []
    lines = []

    if f == 0 or m == 0:
        issues.append(f"性别侧缺失：男 {m} / 女 {f}")

    # 每个女生 K 场（K 从任意女生名单长度推出，要求全部一致）
    female_counts = {len(v["rows"]) for v in owners.values() if v["gender"] == "女性"}
    k = next(iter(female_counts)) if len(female_counts) == 1 else None
    if k is None or len(female_counts) != 1:
        issues.append(f"女生名单长度不一致：{sorted(female_counts)}")
        k = None
    base = (f * k) // m if (k is not None and m) else None
    total_expected = 2 * f * k if (k is not None) else None

    lines.append(f"[结果] 总行数 {len(results)}（男 {m} / 女 {f}，女生场次 K={k}，"
                 f"期望 {total_expected}）")
    if total_expected is not None and len(results) != total_expected:
        issues.append(f"结果行数不符：{len(results)}≠{total_expected}")

    bidir_mismatch = 0
    for oid, v in owners.items():
        rows = sorted(v["rows"], key=lambda x: x[0])
        ranks = [x[0] for x in rows]
        targets = [x[1] for x in rows]
        bad = []
        if v["gender"] == "女性":
            if k is not None and len(rows) != k:
                bad.append(f"名单人数 {len(rows)}≠K {k}")
        else:
            if base is not None and len(rows) not in (base, base + 1):
                bad.append(f"名单人数 {len(rows)}∉{{{base},{base + 1}}}")
        if ranks != list(range(1, len(rows) + 1)):
            bad.append(f"排名非 1..{len(rows)}：{ranks}")
        if len(set(targets)) != len(targets):
            bad.append("名单含重复")
        opp = female_oids if v["gender"] == "男性" else male_oids
        if any(t not in opp for t in targets):
            bad.append("含非异性/自己")
        if bad:
            issues.append(f"{oid}({v['gender']})：{'; '.join(bad)}")

    # 双向一致：男 m 名单含女 f ⇔ 女 f 名单含男 m
    for mo in male_oids:
        for to in [t for _, t in sorted(owners[mo]["rows"])]:
            if to not in owners or owners[to]["gender"] != "女性":
                continue
            if mo not in {t for _, t in owners[to]["rows"]}:
                bidir_mismatch += 1
    if bidir_mismatch:
        issues.append(f"双向不一致：{bidir_mismatch} 条（男名单含女但女名单缺男）")

    ok = not issues
    male_desc = f"{base}~{base + 1}" if base is not None else "?"
    lines.append(f"[均衡不变量] {'✅ 通过' if ok else '未通过'}："
                 f"女生 {f} 人×K={k}、男生 {m} 人×{male_desc}、总行数=2×F×K、双向一致、无自己无同性")
    for line in lines:
        print("  " + line)
    for issue in issues:
        print("  ⛔ " + issue)
    return ok, lines


def _find_activity(act_id):
    """活动ID 是自动编号，不支持 filter，全表扫 + 文本匹配（活动表很小）。"""
    for r in _must(bitable.search_records(C.ACTIVITY_TABLE_ID), "活动表"):
        if get_field_text(r.get("fields", {}), C.FIELD_ACTIVITY_ID) == act_id:
            return r
    return None


def _cmd_verify(act_id):
    if not act_id:
        _fail("用法：verify 活动ID，例如 verify A-0012")
    if not _find_activity(act_id):
        _fail(f"活动表中未找到活动ID：{act_id}")
    print(f"========== VERIFY（只读复核） {act_id} ==========")
    ok, _ = _verify(act_id)
    if not ok:
        _fail("复核未通过（见上方 ⛔ 项）")
    print("✅ 复核通过。")


def _cmd_plan():
    males, females = _build_users()
    print("========== PLAN（只读，不写任何数据） ==========")
    print(f"【假号 {len(males) + len(females)} 人】{len(males)} 男 + {len(females)} 女")
    print(f"  open_id 前缀：{_PREFIX_MALE}001~{_N_MALE:03d} / {_PREFIX_FEMALE}001~{_N_FEMALE:03d}")
    print(f"  昵称：百人男001~{_N_MALE:03d} / 百人女001~{_N_FEMALE:03d}；账号状态=单身")
    print(f"  资料相似度：学历 {_EDUCATIONS} / 城市 {_CITIES} / 教堂 {_CHURCHES} + 爱好/运动/性格/MBTI 轮转")
    print(f"【活动】新建「百人对一对一演示」：报名中、一对一功能开启=是")
    print(f"【志愿】轮转法：每人选 7 位异性（跳过同序位），共 {len(males) + len(females)} 条")
    print(f"【执行】handle_admin_stop_onetoone('<活动ID> {_FEMALE_CHATS}')"
          f"（轮次缺省=最新+1，新活动即第 1 轮）")
    print(f"【预期】女 {len(females)} 人 × K {_FEMALE_CHATS} 场 = "
          f"{len(females) * _FEMALE_CHATS} 女名单 + {len(females) * _FEMALE_CHATS} 男名单 = "
          f"{2 * len(females) * _FEMALE_CHATS} 条结果")


def _cmd_run():
    males, females = _build_users()
    print("========== RUN 开始（只写测试 base） ==========")
    print("[1/6] 创建 100 个假号：")
    users = _create_users(males, females)
    males = [u for u in users if u["gender"] == "男性"]
    females = [u for u in users if u["gender"] == "女性"]
    print("[2/6] 新建活动：")
    act_id, act_rec = _create_activity()
    print(f"  活动ID={act_id}")
    print("[3/6] 报名：")
    _enroll(act_id, users)
    print("[3.5/6] 签到状态：")
    _mark_signups_attendance_present(act_id)
    print("[4/6] 预填志愿：")
    _seed_choices(act_id, males, females)
    print("[5/6] 执行匹配：")
    _run_matching(act_id)
    print("[6/6] 读回验证：")
    ok, _ = _verify(act_id)
    if not ok:
        _fail("读回验证未全部通过（见上方 ⛔ 项）")

    male_sample = males[0]
    female_sample = females[0]
    print("\n✅ run 完成：100 人种子 + 活动 + 报名 + 志愿 + 匹配 + 结果验证全部通过。")
    print(f"   活动ID：{act_id}")
    print(f"   登录查看示例（男）：{male_sample['uid']} {male_sample['nickname']} open_id={male_sample['oid']}")
    print(f"   登录查看示例（女）：{female_sample['uid']} {female_sample['nickname']} open_id={female_sample['oid']}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "plan":
        _cmd_plan()
    elif mode == "run":
        _cmd_run()
    elif mode == "verify":
        _cmd_verify(sys.argv[2] if len(sys.argv) > 2 else "")
    else:
        print("用法：YIXIANQIAN_ENV=dev bot/venv/bin/python scripts/dev/seed_onetoone_100.py plan|run|verify [活动ID]")
        sys.exit(2)


if __name__ == "__main__":
    main()
