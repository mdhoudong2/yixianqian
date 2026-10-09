"""活动一对一：各自独立 top-N 必聊名单匹配、选择卡片、指令处理。

与 grouping.py 同构，差异只在匹配算法：分组是「卫星滚动分组」（把人塞进小组），
一对一则是「每人独立算一份最多 10 人的必聊名单」，按 `0.7*志愿优先级 + 0.3*匹配度`
排序取前 N。匹配度复用 lib.recommend.match_score。
"""
import threading
from concurrent.futures import ThreadPoolExecutor

from clients import *
from constants import *
from grouping import get_activity_signups
from queries import find_activity_by_id, find_user_by_id_or_name, find_user_by_openid

from lib import recommend

# 一对一执行互斥锁：防止管理员并发两次「执行一对一」互相删除/覆盖结果
_onetoone_lock = threading.Lock()


def run_onetoone_matching(participants, selections, profiles, top_n=10):
    """各自独立：给每人算出 up to top_n 个异性必聊。

    participants: [{"id": openid, "gender": "male"/"female"}, ...]
    selections: {selector_oid: [{"id": target_oid, "priority": 1..7}, ...]}
    profiles: {oid: 打分资料dict}（键与 lib.recommend.match_score 对齐，已 prepare_profile）
    返回: {oid: [(target_oid, rank), ...]}，rank 从 1 开始。
    """
    males = [p["id"] for p in participants if p.get("gender") == "male"]
    females = [p["id"] for p in participants if p.get("gender") == "female"]

    result = {}
    for p in participants:
        pid = p["id"]
        g = p.get("gender")
        if g == "male":
            opp = females
        elif g == "female":
            opp = males
        else:
            # 性别缺失/未知：不进任何人的候选，也不给名单
            result[pid] = []
            continue
        candidates = [q for q in opp if q != pid]

        # 志愿映射：target -> priority（同人多次填取最小优先级，即最高意愿）
        choice_priority = {}
        for s in selections.get(pid, []):
            if s["id"] not in choice_priority or s["priority"] < choice_priority[s["id"]]:
                choice_priority[s["id"]] = s["priority"]

        scored = []
        prof_a = profiles.get(pid, {})
        for qid in candidates:
            priority = PRIORITY_SCORES.get(choice_priority.get(qid), 0)
            try:
                match = recommend.match_score(prof_a, profiles.get(qid, {}))[0]
            except Exception:
                match = 0
            final = WEIGHT_ONETOONE_PRIORITY * priority + WEIGHT_ONETOONE_MATCH * match
            scored.append((final, qid))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:min(top_n, len(scored))]
        result[pid] = [(qid, rank) for rank, (_, qid) in enumerate(top, 1)]
    return result


def get_user_onetoone_selection(activity_id, open_id):
    """获取用户在某活动的一对一选择"""
    records = search_records(ONETOONE_SELECT_TABLE, {
        "conjunction": "and",
        "conditions": [
            {"field_name": FIELD_OTO_ACTIVITY_ID, "operator": "is", "value": [activity_id]},
            {"field_name": FIELD_OTO_SELECTOR_OID, "operator": "is", "value": [open_id]}
        ]
    })
    if not records:
        return None
    fields = records[0].get("fields", {})
    choices = []
    for i, cf in enumerate(FIELD_OTO_CHOICES):
        val = get_field_text(fields, cf)
        if val:
            choices.append(val)
    return {"record_id": records[0]["record_id"], "choices": choices}


def _dup_nicknames(participants):
    names = [p.get("nickname", "") for p in participants]
    return len(names) != len(set(names))


def _option_label(nickname, user_id, with_uid):
    if with_uid and user_id:
        return f"{nickname}（{user_id}）"
    return nickname


def build_onetoone_select_card(activity_id, activity_name, participants, user_gender, existing_choices=None):
    """构建一对一选择卡片（表单容器，一次性提交）"""
    opp_gender = "女性" if user_gender == "男性" else "男性"
    opp_participants = [p for p in participants if p["gender"] == opp_gender]
    use_select = len(opp_participants) <= 50
    dup_nick = use_select and _dup_nicknames(opp_participants)

    hint = "如遇同名请以编号为准。" if dup_nick else ""
    form_elements = [
        {
            "tag": "markdown",
            "content": f"**活动：{activity_name}**\n请从{len(opp_participants)}位{opp_gender}中选择7位想认识的人，按意愿从高到低排序。{hint}"
        },
        {"tag": "hr"}
    ]

    if use_select:
        options = [{"text": {"tag": "plain_text", "content": _option_label(p["nickname"], p.get("user_id", ""), dup_nick)},
                    "value": p["open_id"]} for p in opp_participants]
        for i in range(7):
            sel = {
                "tag": "select_static",
                "name": f"choice_{i}",
                "placeholder": {"tag": "plain_text",
                                "content": f"第{i+1}志愿（最想认识）" if i == 0 else f"第{i+1}志愿"},
                "options": options
            }
            if existing_choices and i < len(existing_choices):
                sel["initial_option"] = existing_choices[i]
            form_elements.append({
                "tag": "div",
                "fields": [{"is_short": False, "text": {"tag": "plain_text", "content": f"第{i+1}志愿："}}]
            })
            form_elements.append(sel)
    else:
        form_elements.append({
            "tag": "markdown",
            "content": "参与者较多，请输入对方编号（如U-0003）。"
        })
        for i in range(7):
            inp = {
                "tag": "input",
                "name": f"choice_{i}",
                "placeholder": {"tag": "plain_text", "content": f"第{i+1}志愿（输入编号如U-0003）"}
            }
            if existing_choices and i < len(existing_choices):
                inp["default_value"] = existing_choices[i]
            form_elements.append(inp)

    form_elements.append({"tag": "hr"})

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "一对一选择"},
            "template": "blue"
        },
        "elements": [
            {
                "tag": "form",
                "name": "onetoone_select_form",
                "elements": form_elements,
                "submit": {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "提交选择"},
                    "type": "primary",
                    "name": "submit_onetoone",
                    "action_type": "form_submit",
                    "value": {"action": "submit_onetoone", "activity_id": activity_id}
                }
            }
        ]
    }
    return card


def handle_onetoone_command(sender_id):
    """用户发「一对一」指令"""
    user_recs = find_user_by_openid(sender_id)
    if not user_recs:
        return "你还没有注册，请先发送「注册」完成注册。"
    user_fields = user_recs[0].get("fields", {})
    user_gender = get_field_text(user_fields, FIELD_GENDER)

    signups = search_records(SIGNUP_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": FIELD_SIGNUP_OPENID, "operator": "is", "value": [sender_id]},
            {"field_name": FIELD_SIGNUP_STATUS, "operator": "is", "value": ["已报名"]}
        ]
    })
    if not signups:
        return "你还没有报名任何活动。\n发送「活动」查看当前活动并报名。"

    collecting_activities = []
    for s in signups:
        aid = get_field_text(s.get("fields", {}), FIELD_SIGNUP_ACTIVITY_ID)
        activity = find_activity_by_id(aid)
        if activity:
            af = activity.get("fields", {})
            if get_field_text(af, FIELD_ACT_ONETOONE_STATUS) == "收集中":
                collecting_activities.append(activity)

    if not collecting_activities:
        return "当前没有正在进行一对一选择的活动。\n一对一选择由管理员在活动前开启，请关注通知。"

    activity = collecting_activities[0]
    af = activity.get("fields", {})
    activity_id = get_field_text(af, "活动ID")
    activity_name = get_field_text(af, FIELD_ACTIVITY_NAME)

    participants = get_activity_signups(activity_id)
    existing = get_user_onetoone_selection(activity_id, sender_id)

    card = build_onetoone_select_card(
        activity_id, activity_name, participants, user_gender,
        existing_choices=existing.get("choices") if existing else None
    )
    send_card_message(sender_id, card)

    if len(collecting_activities) > 1:
        return f"你报名了多个正在进行一对一的活动，当前显示「{activity_name}」。如需其他活动请联系管理员。"
    return ""


def handle_onetoone_submit(operator_open_id, action_value, form_value):
    """处理一对一选择提交"""
    activity_id = action_value.get("activity_id", "")

    activity = find_activity_by_id(activity_id)
    if not activity:
        return {"toast": {"type": "error", "content": "活动不存在"}}
    af = activity.get("fields", {})
    if get_field_text(af, FIELD_ACT_ONETOONE_STATUS) != "收集中":
        return {"toast": {"type": "warning", "content": "一对一选择已截止"}}

    signups = search_records(SIGNUP_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": FIELD_SIGNUP_ACTIVITY_ID, "operator": "is", "value": [activity_id]},
            {"field_name": FIELD_SIGNUP_OPENID, "operator": "is", "value": [operator_open_id]},
            {"field_name": FIELD_SIGNUP_STATUS, "operator": "is", "value": ["已报名"]}
        ]
    })
    if not signups:
        return {"toast": {"type": "error", "content": "你未报名此活动"}}

    user_recs = find_user_by_openid(operator_open_id)
    if not user_recs:
        return {"toast": {"type": "error", "content": "用户信息异常"}}
    uf = user_recs[0].get("fields", {})
    user_nickname = get_field_text(uf, FIELD_NICKNAME)
    user_gender = get_field_text(uf, FIELD_GENDER)

    participants = get_activity_signups(activity_id)
    opp_gender = "女性" if user_gender == "男性" else "男性"
    valid_oids = {p["open_id"] for p in participants if p["gender"] == opp_gender}

    choices = []
    if form_value:
        for i in range(7):
            val = ""
            if hasattr(form_value, 'get'):
                val = form_value.get(f"choice_{i}", "")
            if val:
                val = val.strip()
                if val.startswith("U-") or val.startswith("u-"):
                    target = find_user_by_id_or_name(val)
                    if target:
                        val = get_field_text(target[0].get("fields", {}), FIELD_FEISHU_ID)
                else:
                    target = find_user_by_id_or_name(val)
                    if len(target) > 1:
                        return {"toast": {"type": "error",
                                          "content": f"「{val}」有同名多人，第{i+1}志愿请改用对方编号（如U-0003）"}}
                    if len(target) == 1:
                        val = get_field_text(target[0].get("fields", {}), FIELD_FEISHU_ID)
                choices.append(val)
    elif "selections" in action_value:
        choices = action_value["selections"]

    if len(choices) != 7:
        return {"toast": {"type": "error", "content": f"请选择7位（当前{len(choices)}位）"}}
    if len(set(choices)) != 7:
        return {"toast": {"type": "error", "content": "不能重复选择同一人"}}
    for c in choices:
        if c not in valid_oids:
            return {"toast": {"type": "error", "content": "选择包含无效参与者"}}

    fields = {
        FIELD_OTO_ACTIVITY_ID: activity_id,
        FIELD_OTO_SELECTOR_OID: operator_open_id,
        FIELD_OTO_SELECTOR_NAME: user_nickname,
        FIELD_OTO_SELECTOR_GENDER: user_gender,
    }
    for i, cf in enumerate(FIELD_OTO_CHOICES):
        fields[cf] = choices[i]

    existing = get_user_onetoone_selection(activity_id, operator_open_id)
    if existing:
        update_record(ONETOONE_SELECT_TABLE, existing["record_id"], fields)
        msg = "选择已更新"
    else:
        create_record(ONETOONE_SELECT_TABLE, fields)
        msg = "选择已提交"

    log(f"一对一选择: {user_nickname} 活动{activity_id} {msg}")
    return {"toast": {"type": "success", "content": msg}}


def handle_admin_start_onetoone(keyword):
    """管理员：开始一对一 格式: 开始一对一 A-0002"""
    activity_id = keyword.strip()
    if not activity_id:
        return "格式：开始一对一 活动ID\n例如：开始一对一 A-0002"
    activity = find_activity_by_id(activity_id)
    if not activity:
        return f"未找到活动：{activity_id}"

    af = activity.get("fields", {})
    activity_name = get_field_text(af, FIELD_ACTIVITY_NAME)
    record_id = activity.get("record_id")

    update_record(ACTIVITY_TABLE_ID, record_id, {FIELD_ACT_ONETOONE_STATUS: "收集中"})

    # 重新开放：清除该活动历史一对一结果，否则「执行一对一」的幂等保护会拦截重跑
    old_results = search_records(ONETOONE_RESULT_TABLE, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_OTO_ACTIVITY_ID, "operator": "is", "value": [activity_id]}]
    })
    if old_results:
        batch_delete_records(ONETOONE_RESULT_TABLE, [r["record_id"] for r in old_results])
        log(f"重新开放一对一，已清除 {len(old_results)} 条历史结果: {activity_name}")

    log(f"管理员开始一对一: {activity_name}")
    return (f"一对一志愿填写已开始：{activity_name}\n"
            f"已开放，未发送群发通知。\n"
            f"请让用户在 H5 页面自查并提交志愿（现场活动）。")


def handle_admin_stop_onetoone(keyword):
    """管理员：执行一对一匹配 格式: 执行一对一 A-0002"""
    activity_id = keyword.strip()
    if not activity_id:
        return "格式：执行一对一 活动ID\n例如：执行一对一 A-0002"

    activity = find_activity_by_id(activity_id)
    if not activity:
        return f"未找到活动：{activity_id}"

    with _onetoone_lock:
        # 幂等保护：该活动已生成过结果则拒绝重跑
        existing_results = search_records(ONETOONE_RESULT_TABLE, {
            "conjunction": "and",
            "conditions": [{"field_name": FIELD_OTO_ACTIVITY_ID, "operator": "is", "value": [activity_id]}]
        })
        if existing_results:
            return (f"活动「{get_field_text(activity.get('fields', {}), FIELD_ACTIVITY_NAME)}」"
                    f"已生成过一对一结果，请勿重复执行。如需重新匹配，请先发送"
                    f"「开始一对一 {activity_id}」重新开放，再执行。")
        return _do_onetoone(activity)


def _build_profiles(open_ids):
    """为每个 open_id 构建打分资料（键与 lib.recommend.match_score 对齐）。"""
    from auto_tasks import _profile_of

    def _fetch(oid):
        recs = find_user_by_openid(oid)
        if not recs:
            return oid, {}
        return oid, _profile_of(recs[0].get("fields", {}))

    profiles = {}
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="onetoone-profile") as pool:
        for oid, prof in pool.map(_fetch, open_ids):
            profiles[oid] = prof
    return profiles


def _do_onetoone(activity):
    """执行一对一匹配（调用方需已持有 _onetoone_lock）"""
    af = activity.get("fields", {})
    activity_id = get_field_text(af, FIELD_ACTIVITY_ID) or ""
    activity_name = get_field_text(af, FIELD_ACTIVITY_NAME)
    record_id = activity.get("record_id")

    update_record(ACTIVITY_TABLE_ID, record_id, {FIELD_ACT_ONETOONE_STATUS: "已截止"})

    selections_records = search_records(ONETOONE_SELECT_TABLE, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_OTO_ACTIVITY_ID, "operator": "is", "value": [activity_id]}]
    })

    participants = []
    seen_oids = set()
    selections = {}
    skipped = 0
    for sr in selections_records:
        sf = sr.get("fields", {})
        oid = get_field_text(sf, FIELD_OTO_SELECTOR_OID)
        gender = get_field_text(sf, FIELD_OTO_SELECTOR_GENDER)
        if not oid or oid in seen_oids:
            continue
        if gender not in ("男性", "女性"):
            skipped += 1
            continue
        seen_oids.add(oid)
        participants.append({"id": oid, "gender": "male" if gender == "男性" else "female"})
        choices = []
        for i, cf in enumerate(FIELD_OTO_CHOICES):
            val = get_field_text(sf, cf)
            if val:
                choices.append({"id": val, "priority": i + 1})
        if choices:
            selections[oid] = choices

    if not participants:
        update_record(ACTIVITY_TABLE_ID, record_id, {FIELD_ACT_ONETOONE_STATUS: "收集中"})
        return "没有已提交志愿的参与者，状态已恢复为「收集中」。"

    profiles = _build_profiles([p["id"] for p in participants])

    try:
        matches = run_onetoone_matching(participants, selections, profiles, top_n=10)
    except Exception as e:
        update_record(ACTIVITY_TABLE_ID, record_id, {FIELD_ACT_ONETOONE_STATUS: "收集中"})
        return f"一对一匹配出错：{e}，状态已恢复为「收集中」"

    # 报名信息：open_id -> 昵称
    oid_to_nickname = {p["open_id"]: p["nickname"] for p in get_activity_signups(activity_id)}
    result_records = []
    for p in participants:
        pid = p["id"]
        for target_oid, rank in matches.get(pid, []):
            result_records.append({
                FIELD_OTO_ACTIVITY_ID: activity_id,
                FIELD_OTO_RANK: rank,
                FIELD_OTO_USER_OID: pid,
                FIELD_OTO_USER_NAME: oid_to_nickname.get(pid, ""),
                FIELD_OTO_USER_GENDER: "男性" if p["gender"] == "male" else "女性",
                FIELD_OTO_TARGET_OID: target_oid,
                FIELD_OTO_TARGET_NAME: oid_to_nickname.get(target_oid, ""),
            })

    if result_records:
        created = batch_create_records(ONETOONE_RESULT_TABLE, result_records)
        if created < len(result_records):
            partial = search_records(ONETOONE_RESULT_TABLE, {
                "conjunction": "and",
                "conditions": [{"field_name": FIELD_OTO_ACTIVITY_ID, "operator": "is", "value": [activity_id]}]
            })
            if partial:
                batch_delete_records(ONETOONE_RESULT_TABLE, [p["record_id"] for p in partial])
            update_record(ACTIVITY_TABLE_ID, record_id, {FIELD_ACT_ONETOONE_STATUS: "收集中"})
            return (f"一对一结果写入失败（成功 {created}/{len(result_records)} 条），已回滚，"
                    f"状态恢复为「收集中」，请稍后重试。")

    update_record(ACTIVITY_TABLE_ID, record_id, {FIELD_ACT_ONETOONE_STATUS: "已完成"})
    total_pairs = sum(len(v) for v in matches.values())
    log(f"一对一匹配完成: {activity_name}, {len(participants)}人, {total_pairs}条必聊")
    return (f"🎉 活动「{activity_name}」一对一匹配完成：\n"
            f"参与 {len(participants)} 人，共生成 {total_pairs} 条必聊关系。\n"
            f"用户可自查 H5「我的一对一」查看名单。")


def handle_admin_onetoone_status(keyword):
    """管理员：查看一对一状态 格式: 一对一状态 A-0002"""
    activity_id = keyword.strip()
    if not activity_id:
        return "格式：一对一状态 活动ID"

    activity = find_activity_by_id(activity_id)
    if not activity:
        return f"未找到活动：{activity_id}"

    af = activity.get("fields", {})
    activity_name = get_field_text(af, FIELD_ACTIVITY_NAME)
    status = get_field_text(af, FIELD_ACT_ONETOONE_STATUS) or "未开始"

    signups = get_activity_signups(activity_id)
    selections = search_records(ONETOONE_SELECT_TABLE, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_OTO_ACTIVITY_ID, "operator": "is", "value": [activity_id]}]
    })

    return (f"活动「{activity_name}」一对一状态：\n"
            f"状态：{status}\n"
            f"报名人数：{len(signups)}\n"
            f"已提交选择：{len(selections)}人")


def handle_admin_unsubmitted_onetoone(keyword):
    """管理员：查看未提交一对一志愿的报名者 格式: 查看未提交一对一 A-0002"""
    activity_id = keyword.strip()
    if not activity_id:
        return "格式：查看未提交一对一 活动ID"
    activity = find_activity_by_id(activity_id)
    if not activity:
        return f"未找到活动：{activity_id}"
    act_name = get_field_text(activity.get("fields", {}), FIELD_ACTIVITY_NAME)

    signups = get_activity_signups(activity_id)
    selections = search_records(ONETOONE_SELECT_TABLE, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_OTO_ACTIVITY_ID, "operator": "is", "value": [activity_id]}]
    })
    submitted_oids = {
        get_field_text(s.get("fields", {}), FIELD_OTO_SELECTOR_OID)
        for s in selections if get_field_text(s.get("fields", {}), FIELD_OTO_SELECTOR_OID)
    }

    unsubmitted = []
    for s in signups:
        oid = s["open_id"]
        if oid in submitted_oids:
            continue
        user_id = ""
        for rec in find_user_by_openid(oid):
            user_id = rec.get("fields", {}).get("用户ID", "") or ""
        unsubmitted.append((oid, user_id, s["gender"], s["nickname"]))

    if not unsubmitted:
        return f"活动「{act_name}」所有报名者均已提交志愿。"
    lines = [f"「{act_name}」未提交志愿（{len(unsubmitted)}人）："]
    for _, user_id, gender, nick in unsubmitted:
        lines.append(f"{user_id or '-'} · {gender or '-'} · {nick}")
    return "\n".join(lines)
