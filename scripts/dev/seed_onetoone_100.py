#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试服「100 人一对一」压测种子 + 执行 + 读回验证（只允许在 /opt/yixianqian-test 执行）。

造 100 个假号（50 男 + 50 女，open_id=ou_fake_oto_{male|female}_NNN），新建一个专用活动，
全部报名 + 轮转法预填 7 志愿，走真实 bot 命令链 handle_admin_start_onetoone →
handle_admin_stop_onetoone 做匹配，最后读回结果表逐条验证。

- 100 人 × 每人 10 个必聊（异性 50 > top_n 10）= 1000 条结果；
- 每人名单顺序 = 双向奔赴公式（0.5×我的优先级 + 0.3×对方优先级 + 0.2×资料相似度，
  未选择=30）逐条比对；
- 无自己、无同性、排名 1..10 无重复。

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
    "ONETOONE_SELECT_TABLE": "tblTRXtuLhODm8Q0",
    "ONETOONE_RESULT_TABLE": "tbllYrqHD81gHyI2",
}
_N_MALE = 50
_N_FEMALE = 50
_TOP_N = 10
_PREFIX_MALE = "ou_fake_oto_male_"
_PREFIX_FEMALE = "ou_fake_oto_female_"
_FIELD_USER_ID = "用户ID"  # 自动编号，只读

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
        C.FIELD_ACTIVITY_NAME: "百人对一对一演示",
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
    """100 人报名（按 open_id 防重）。"""
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
        })
        if not rec:
            _fail(f"报名失败：{u['oid']}（中止）")
        ok += 1
    print(f"  报名：新增 {ok} 条（已报名跳过 {len(users) - ok}）")
    return ok


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
    from onetoone import handle_admin_start_onetoone, handle_admin_stop_onetoone
    reply = handle_admin_start_onetoone(act_id)
    print(f"  handle_admin_start_onetoone -> {reply.splitlines()[0]}")
    if "志愿填写已开始" not in reply:
        _fail(f"开始一对一失败：{reply}")
    reply = handle_admin_stop_onetoone(act_id)
    print(f"  handle_admin_stop_onetoone -> {reply.splitlines()[0]}")
    if "匹配完成" not in reply:
        _fail(f"执行一对一失败：{reply}")
    return reply


def _expected_matches(act_id, males, females, plan):
    from onetoone import run_onetoone_matching, _build_profiles
    participants = [{"id": x["oid"], "gender": "male" if x["gender"] == "男性" else "female"}
                    for x in males + females]
    selections = {}
    for item in plan:
        selections[item["selector"]["oid"]] = [
            {"id": t["oid"], "priority": i + 1} for i, t in enumerate(item["targets"])]
    profiles = _build_profiles([x["oid"] for x in participants])
    return run_onetoone_matching(participants, selections, profiles, top_n=_TOP_N)


def _verify(act_id, males, females, plan):
    expected = _expected_matches(act_id, males, females, plan)
    results = bitable.search_records(C.ONETOONE_RESULT_TABLE, [
        {"field_name": C.FIELD_OTO_ACTIVITY_ID, "operator": "is", "value": [act_id]}])
    results = _must(results, "一对一结果表")

    participants = males + females
    p_by_oid = {p["oid"]: p for p in participants}
    issues = []
    n_people = len(participants)
    lines = [f"[结果] 总行数 {len(results)}（期望 {n_people * _TOP_N}）"]
    if len(results) != n_people * _TOP_N:
        issues.append(f"结果行数不符：{len(results)}")

    by_user = {}
    for r in results:
        f = r.get("fields", {})
        uoid = get_field_text(f, C.FIELD_OTO_USER_OID)
        by_user.setdefault(uoid, []).append(r)

    n_ok = 0
    for p in participants:
        rows = by_user.get(p["oid"], [])
        got = []
        for r in rows:
            f = r.get("fields", {})
            got.append((get_field_number(f, C.FIELD_OTO_RANK, 0),
                        get_field_text(f, C.FIELD_OTO_TARGET_OID)))
        got.sort(key=lambda x: x[0])
        ranks = [g[0] for g in got]
        targets = [g[1] for g in got]
        opp = females if p["gender"] == "男性" else males
        opp_oids = {x["oid"] for x in opp}
        bad = []
        if len(got) != _TOP_N:
            bad.append(f"名单人数 {len(got)}≠{_TOP_N}")
        if ranks != list(range(1, len(got) + 1)):
            bad.append(f"排名非 1..{len(got)}：{ranks}")
        if any(t not in opp_oids for t in targets):
            bad.append("含非异性/自己")
        if p["oid"] in targets:
            bad.append("含自己")
        expect_targets = [t for t, _ in expected.get(p["oid"], [])]
        if targets != expect_targets:
            bad.append(f"名单顺序与公式不符：得 {targets[:3]}… 应 {expect_targets[:3]}…")
        if bad:
            issues.append(f"{p['nickname']}({p['gender']})：{'; '.join(bad)}")
        else:
            n_ok += 1
    lines.append(f"[逐人验证] {n_ok}/{n_people} 人通过（每人 {_TOP_N} 异性、排名 1..{_TOP_N}、"
                 f"名单顺序=双向奔赴公式、无自己无同性）")

    for line in lines:
        print("  " + line)
    for issue in issues:
        print("  ⛔ " + issue)
    return (not issues), lines


def _cmd_plan():
    males, females = _build_users()
    print("========== PLAN（只读，不写任何数据） ==========")
    print(f"【假号 {len(males) + len(females)} 人】{len(males)} 男 + {len(females)} 女")
    print(f"  open_id 前缀：{_PREFIX_MALE}001~{_N_MALE:03d} / {_PREFIX_FEMALE}001~{_N_FEMALE:03d}")
    print(f"  昵称：百人男001~{_N_MALE:03d} / 百人女001~{_N_FEMALE:03d}；账号状态=单身")
    print(f"  资料相似度：学历 {_EDUCATIONS} / 城市 {_CITIES} / 教堂 {_CHURCHES} + 爱好/运动/性格/MBTI 轮转")
    print(f"【活动】新建「百人对一对一演示」：报名中、一对一功能开启=是")
    print(f"【志愿】轮转法：每人选 7 位异性（跳过同序位），共 {len(males) + len(females)} 条")
    print(f"【执行】handle_admin_start_onetoone → handle_admin_stop_onetoone")
    print(f"【预期】{len(males) + len(females)} 人 × {_TOP_N} 必聊 = "
          f"{(len(males) + len(females)) * _TOP_N} 条结果")


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
    print("[4/6] 预填志愿：")
    plan = _seed_choices(act_id, males, females)
    print("[5/6] 执行匹配：")
    _run_matching(act_id)
    print("[6/6] 读回验证：")
    ok, _ = _verify(act_id, males, females, plan)
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
    else:
        print("用法：YIXIANQIAN_ENV=dev bot/venv/bin/python scripts/dev/seed_onetoone_100.py plan|run")
        sys.exit(2)


if __name__ == "__main__":
    main()
