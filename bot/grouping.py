# -*- coding: utf-8 -*-
"""活动分组：卫星滚动分组算法、分组卡片、分组指令处理。"""
from concurrent.futures import ThreadPoolExecutor

from clients import *
from constants import *
from queries import find_activity_by_id, find_user_by_id_or_name, find_user_by_openid

def _user_selection_score(priority):
    return PRIORITY_SCORES.get(priority, 0)




def _mutual_affinity_score(p1_id, p2_id, selections, matchmaker_picks=None):
    """计算互有好感分数（双向分数相加，含权重）—— 严格对照JS版"""
    if matchmaker_picks is None:
        matchmaker_picks = []

    # p1 -> p2
    us1 = 0
    if p1_id in selections:
        for s in selections[p1_id]:
            if s["id"] == p2_id:
                us1 = _user_selection_score(s["priority"])
                break
    # p2 -> p1
    us2 = 0
    if p2_id in selections:
        for s in selections[p2_id]:
            if s["id"] == p1_id:
                us2 = _user_selection_score(s["priority"])
                break

    # 红娘推荐分数
    ms1 = ms2 = 0
    for pick in matchmaker_picks:
        if pick.get("person1_id") == p1_id and pick.get("person2_id") == p2_id:
            ms1 = max(ms1, MATCHMAKER_STAR_SCORES.get(pick.get("stars", 0), 0))
        if pick.get("person1_id") == p2_id and pick.get("person2_id") == p1_id:
            ms2 = max(ms2, MATCHMAKER_STAR_SCORES.get(pick.get("stars", 0), 0))

    final1 = us1 * WEIGHT_USER_SELECTION + ms1 * WEIGHT_MATCHMAKER_PICK
    final2 = us2 * WEIGHT_USER_SELECTION + ms2 * WEIGHT_MATCHMAKER_PICK
    return final1 + final2




def _group_affinity_score(candidate_id, group_member_ids, selections, matchmaker_picks=None):
    """候选人与小组所有成员的亲和力总分"""
    total = 0
    for mid in group_member_ids:
        total += _mutual_affinity_score(candidate_id, mid, selections, matchmaker_picks)
    return total




def _create_pair_scores_table(males, females, selections, matchmaker_picks=None):
    """创建所有男女配对的分数表，按分数降序"""
    table = []
    for m in males:
        for f in females:
            table.append({
                "maleId": m, "femaleId": f,
                "score": _mutual_affinity_score(m, f, selections, matchmaker_picks)
            })
    table.sort(key=lambda x: x["score"], reverse=True)
    return table




def _find_core_pair(pair_scores, assigned):
    """找第一个双方都未分配的配对"""
    for pair in pair_scores:
        if pair["maleId"] not in assigned and pair["femaleId"] not in assigned:
            return pair
    return None




def _add_unassigned_to_group(group, males, females, assigned):
    """核心配对找不到时，各加一个未分配的男女"""
    um = [m for m in males if m not in assigned]
    uf = [f for f in females if f not in assigned]
    if um:
        group["male_ids"].append(um[0])
        assigned.add(um[0])
    if uf:
        group["female_ids"].append(uf[0])
        assigned.add(uf[0])




def _determine_target_gender(group, target_male, target_female):
    """确定需要添加的性别"""
    if len(group["male_ids"]) < target_male and len(group["female_ids"]) < target_female:
        return "male" if len(group["male_ids"]) <= len(group["female_ids"]) else "female"
    elif len(group["male_ids"]) < target_male:
        return "male"
    elif len(group["female_ids"]) < target_female:
        return "female"
    return None




def _expand_group_to_full(group, males, females, assigned, target_male, target_female,
                          selections, matchmaker_picks=None):
    """扩充小组到满编"""
    max_iter = max(target_male, target_female) * 2
    for _ in range(max_iter):
        if len(group["male_ids"]) >= target_male and len(group["female_ids"]) >= target_female:
            break
        gender = _determine_target_gender(group, target_male, target_female)
        if not gender:
            break
        pool = [x for x in (males if gender == "male" else females) if x not in assigned]
        if not pool:
            break
        members = group["male_ids"] + group["female_ids"]
        scored = [{"id": c, "score": _group_affinity_score(c, members, selections, matchmaker_picks)}
                  for c in pool]
        scored.sort(key=lambda x: x["score"], reverse=True)
        best = scored[0]["id"]
        if gender == "male":
            group["male_ids"].append(best)
        else:
            group["female_ids"].append(best)
        assigned.add(best)




def _distribute_remaining(groups, all_remaining, males_set, females_set, full_count):
    """剩余人员平均分配到各满编组"""
    if not all_remaining or full_count == 0:
        return
    rem_males = [x for x in all_remaining if x in males_set]
    rem_females = [x for x in all_remaining if x in females_set]
    m_per = len(rem_males) // full_count
    m_extra = len(rem_males) % full_count
    f_per = len(rem_females) // full_count
    f_extra = len(rem_females) % full_count
    mi = fi = 0
    for gi in range(full_count):
        g = groups[gi]
        mc = m_per + (1 if gi < m_extra else 0)
        fc = f_per + (1 if gi < f_extra else 0)
        for _ in range(mc):
            if mi < len(rem_males):
                g["male_ids"].append(rem_males[mi])
                mi += 1
        for _ in range(fc):
            if fi < len(rem_females):
                g["female_ids"].append(rem_females[fi])
                fi += 1




def run_grouping_algorithm(participants, selections, males_per_group, females_per_group,
                           matchmaker_picks=None):
    """
    卫星滚动分组法 —— 严格对照JS版generateGroups
    participants: [{"id": "xxx", "gender": "male"/"female"}, ...]
    selections: {user_id: [{"id": target_id, "priority": 1-7}, ...], ...}
    matchmaker_picks: [] (红娘推荐，暂不使用)
    返回: [{"group_id": 1, "male_ids": [...], "female_ids": [...]}, ...]
    """
    if matchmaker_picks is None:
        matchmaker_picks = []

    # 参数校验
    if males_per_group < 1 or females_per_group < 1:
        raise ValueError("每组男女人数必须大于0")

    males = [p["id"] for p in participants if p.get("gender") == "male"]
    females = [p["id"] for p in participants if p.get("gender") == "female"]
    males_set = set(males)
    females_set = set(females)

    max_by_males = len(males) // males_per_group
    max_by_females = len(females) // females_per_group
    full_count = min(max_by_males, max_by_females)

    if full_count == 0:
        return []

    pair_scores = _create_pair_scores_table(males, females, selections, matchmaker_picks)
    assigned = set()
    groups = []

    for gi in range(full_count):
        group = {"group_id": gi + 1, "male_ids": [], "female_ids": []}
        core = _find_core_pair(pair_scores, assigned)
        if not core:
            _add_unassigned_to_group(group, males, females, assigned)
        else:
            group["male_ids"].append(core["maleId"])
            group["female_ids"].append(core["femaleId"])
            assigned.add(core["maleId"])
            assigned.add(core["femaleId"])

        _expand_group_to_full(group, males, females, assigned, males_per_group,
                              females_per_group, selections, matchmaker_picks)

        if len(group["male_ids"]) >= males_per_group and len(group["female_ids"]) >= females_per_group:
            groups.append(group)
        else:
            # 不满编，退回人员，停止创建
            for mid in group["male_ids"] + group["female_ids"]:
                assigned.discard(mid)
            break

    # 剩余人员
    remaining = [x for x in males if x not in assigned] + [x for x in females if x not in assigned]
    if remaining and len(groups) > 0:
        _distribute_remaining(groups, remaining, males_set, females_set, len(groups))

    return groups



def get_activity_signups(activity_id):
    """获取活动的已报名用户列表"""
    signups = search_records(SIGNUP_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": FIELD_SIGNUP_ACTIVITY_ID, "operator": "is", "value": [activity_id]},
            {"field_name": FIELD_SIGNUP_STATUS, "operator": "is", "value": ["已报名"]}
        ]
    })

    # 逐人查用户表取性别是串行 API 的耗时瓶颈（大活动尤其明显）。
    # 用线程池并发查，显著降低延迟。
    signup_rows = []
    for s in signups:
        sf = s.get("fields", {})
        oid = get_field_text(sf, FIELD_SIGNUP_OPENID)
        nickname = get_field_text(sf, FIELD_SIGNUP_NICKNAME)
        if oid:
            signup_rows.append((oid, nickname))

    def _fetch(row):
        oid, nickname = row
        gender = ""
        user_recs = find_user_by_openid(oid)
        if user_recs:
            gender = get_field_text(user_recs[0].get("fields", {}), FIELD_GENDER)
        return {"open_id": oid, "nickname": nickname, "gender": gender}

    users = []
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="signup-gender") as pool:
        for u in pool.map(_fetch, signup_rows):
            users.append(u)
    return users




def get_user_group_selection(activity_id, open_id):
    """获取用户在某活动的分组选择"""
    records = search_records(GROUP_SELECT_TABLE, {
        "conjunction": "and",
        "conditions": [
            {"field_name": FIELD_GS_ACTIVITY_ID, "operator": "is", "value": [activity_id]},
            {"field_name": FIELD_GS_SELECTOR_OID, "operator": "is", "value": [open_id]}
        ]
    })
    if not records:
        return None
    fields = records[0].get("fields", {})
    choices = []
    for i, cf in enumerate(FIELD_GS_CHOICES):
        val = get_field_text(fields, cf)
        if val:
            choices.append(val)
    return {"record_id": records[0]["record_id"], "choices": choices}




def build_group_select_card(activity_id, activity_name, participants, user_gender, existing_choices=None):
    """构建分组选择卡片（表单容器，一次性提交）"""
    opp_gender = "女性" if user_gender == "男性" else "男性"
    opp_participants = [p for p in participants if p["gender"] == opp_gender]
    use_select = len(opp_participants) <= 50

    form_elements = [
        {
            "tag": "markdown",
            "content": f"**活动：{activity_name}**\n请从{len(opp_participants)}位{opp_gender}性中选择7位想同组的人，按意愿从高到低排序。"
        },
        {"tag": "hr"}
    ]

    if use_select:
        options = [{"text": {"tag": "plain_text", "content": p["nickname"]},
                    "value": p["open_id"]} for p in opp_participants]
        for i in range(7):
            sel = {
                "tag": "select_static",
                "name": f"choice_{i}",
                "placeholder": {"tag": "plain_text",
                                "content": f"第{i+1}志愿（最想同组）" if i == 0 else f"第{i+1}志愿"},
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
            "content": f"参与者较多，请输入对方编号（如U-0003）。"
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
            "title": {"tag": "plain_text", "content": "分组选择"},
            "template": "blue"
        },
        "elements": [
            {
                "tag": "form",
                "name": "group_select_form",
                "elements": form_elements,
                "submit": {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "提交选择"},
                    "type": "primary",
                    "name": "submit_group",
                    "action_type": "form_submit",
                    "value": {"action": "submit_group", "activity_id": activity_id}
                }
            }
        ]
    }
    return card




def handle_group_command(sender_id):
    """用户发"分组"指令"""
    # 查用户
    user_recs = find_user_by_openid(sender_id)
    if not user_recs:
        return "你还没有注册，请先发送「注册」完成注册。"
    user_fields = user_recs[0].get("fields", {})
    user_gender = get_field_text(user_fields, FIELD_GENDER)
    user_nickname = get_field_text(user_fields, FIELD_NICKNAME)

    # 查找用户报名了哪些"收集中"的活动
    signups = search_records(SIGNUP_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": FIELD_SIGNUP_OPENID, "operator": "is", "value": [sender_id]},
            {"field_name": FIELD_SIGNUP_STATUS, "operator": "is", "value": ["已报名"]}
        ]
    })
    if not signups:
        return "你还没有报名任何活动。\n发送「活动」查看当前活动并报名。"

    # 找收集中的活动
    collecting_activities = []
    for s in signups:
        aid = get_field_text(s.get("fields", {}), FIELD_SIGNUP_ACTIVITY_ID)
        activity = find_activity_by_id(aid)
        if activity:
            af = activity.get("fields", {})
            group_status = get_field_text(af, FIELD_ACT_GROUP_STATUS)
            if group_status == "收集中":
                collecting_activities.append(activity)

    if not collecting_activities:
        return "当前没有正在进行分组选择的活动。\n分组选择由管理员在活动前开启，请关注通知。"

    # 如果只有一个活动，直接发卡片
    activity = collecting_activities[0]
    af = activity.get("fields", {})
    activity_id = get_field_text(af, "活动ID")
    activity_name = get_field_text(af, FIELD_ACTIVITY_NAME)

    participants = get_activity_signups(activity_id)
    existing = get_user_group_selection(activity_id, sender_id)

    card = build_group_select_card(
        activity_id, activity_name, participants, user_gender,
        existing_choices=existing.get("choices") if existing else None
    )
    send_card_message(sender_id, card)

    if len(collecting_activities) > 1:
        return f"你报名了多个正在分组的活动，当前显示「{activity_name}」。如需其他活动请联系管理员。"
    return ""




def handle_group_submit(operator_open_id, action_value, form_value):
    """处理分组选择提交"""
    activity_id = action_value.get("activity_id", "")

    # 验证活动状态
    activity = find_activity_by_id(activity_id)
    if not activity:
        return {"toast": {"type": "error", "content": "活动不存在"}}
    af = activity.get("fields", {})
    if get_field_text(af, FIELD_ACT_GROUP_STATUS) != "收集中":
        return {"toast": {"type": "warning", "content": "分组选择已截止"}}

    # 验证用户报名
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

    # 获取用户信息
    user_recs = find_user_by_openid(operator_open_id)
    if not user_recs:
        return {"toast": {"type": "error", "content": "用户信息异常"}}
    uf = user_recs[0].get("fields", {})
    user_nickname = get_field_text(uf, FIELD_NICKNAME)
    user_gender = get_field_text(uf, FIELD_GENDER)

    # 获取活动参与者
    participants = get_activity_signups(activity_id)
    opp_gender = "女性" if user_gender == "男性" else "男性"
    valid_oids = {p["open_id"] for p in participants if p["gender"] == opp_gender}

    # 收集选择
    choices = []
    # 从表单值获取（input方式）
    if form_value:
        for i in range(7):
            val = ""
            if hasattr(form_value, 'get'):
                val = form_value.get(f"choice_{i}", "")
            if val:
                # 如果输入的是用户ID（如U-0003），转换为open_id
                val = val.strip()
                if val.startswith("U-") or val.startswith("u-"):
                    target = find_user_by_id_or_name(val)
                    if target:
                        val = get_field_text(target[0].get("fields", {}), FIELD_FEISHU_ID)
                choices.append(val)
    # 从action_value获取（select_static方式，在卡片回调中逐个收集）
    elif "selections" in action_value:
        choices = action_value["selections"]

    # 验证
    if len(choices) != 7:
        return {"toast": {"type": "error", "content": f"请选择7位（当前{len(choices)}位）"}}
    if len(set(choices)) != 7:
        return {"toast": {"type": "error", "content": "不能重复选择同一人"}}
    for c in choices:
        if c not in valid_oids:
            return {"toast": {"type": "error", "content": "选择包含无效参与者"}}

    # 保存或更新
    fields = {
        FIELD_GS_ACTIVITY_ID: activity_id,
        FIELD_GS_SELECTOR_OID: operator_open_id,
        FIELD_GS_SELECTOR_NAME: user_nickname,
        FIELD_GS_SELECTOR_GENDER: user_gender,
    }
    for i, cf in enumerate(FIELD_GS_CHOICES):
        fields[cf] = choices[i]

    existing = get_user_group_selection(activity_id, operator_open_id)
    if existing:
        update_record(GROUP_SELECT_TABLE, existing["record_id"], fields)
        msg = "选择已更新"
    else:
        create_record(GROUP_SELECT_TABLE, fields)
        msg = "选择已提交"

    log(f"分组选择: {user_nickname} 活动{activity_id} {msg}")
    return {"toast": {"type": "success", "content": msg}}




def handle_admin_start_group(keyword):
    """管理员：开始填志愿 格式: 开始填志愿 A-0002 3 3"""
    parts = keyword.split()
    if len(parts) < 3:
        return "格式：开始填志愿 活动ID 每组男生数 每组女生数\n例如：开始填志愿 A-0002 3 3"
    activity_id = parts[0]
    try:
        m_per = int(parts[1])
        f_per = int(parts[2])
    except ValueError:
        return "每组人数必须是数字"

    if m_per < 1 or f_per < 1:
        return "每组男女人数必须大于0"

    activity = find_activity_by_id(activity_id)
    if not activity:
        return f"未找到活动：{activity_id}"

    af = activity.get("fields", {})
    activity_name = get_field_text(af, FIELD_ACTIVITY_NAME)
    record_id = activity.get("record_id")

    update_record(ACTIVITY_TABLE_ID, record_id, {
        FIELD_ACT_GROUP_STATUS: "收集中",
        FIELD_ACT_MALE_PER_GROUP: m_per,
        FIELD_ACT_FEMALE_PER_GROUP: f_per
    })

    # 不再向报名者群发通知（现场活动，用户自查 H5）。管理员收到开始指令回执即可。
    log(f"管理员开始填志愿: {activity_name} {m_per}男{f_per}女")
    return (f"志愿填写已开始：{activity_name}（每组{m_per}男{f_per}女）\n"
            f"已开放，未发送群发通知。\n"
            f"请让用户在 H5 页面自查并提交志愿（现场活动）。")




def handle_admin_stop_group(keyword):
    """管理员：执行分组并运行算法 格式: 执行分组 A-0002 [轮次]（轮次可选，默认第1轮）"""
    parts = keyword.split()
    if not parts:
        return "格式：执行分组 活动ID [轮次]\n例如：执行分组 A-0002 或 执行分组 A-0002 2"
    activity_id = parts[0]
    round_no = 1
    if len(parts) >= 2:
        try:
            round_no = int(parts[1])
            if round_no < 1:
                return "轮次必须为大于0的整数"
        except ValueError:
            return f"轮次必须为数字，当前：{parts[1]}"

    activity = find_activity_by_id(activity_id)
    if not activity:
        return f"未找到活动：{activity_id}"

    af = activity.get("fields", {})
    activity_name = get_field_text(af, FIELD_ACTIVITY_NAME)
    record_id = activity.get("record_id")
    m_per = int(get_field_number(af, FIELD_ACT_MALE_PER_GROUP, 3))
    f_per = int(get_field_number(af, FIELD_ACT_FEMALE_PER_GROUP, 3))

    if m_per < 1 or f_per < 1:
        return f"每组男女人数必须大于0（当前：{m_per}男{f_per}女），请先设置再截止"

    # 更新状态为已截止
    update_record(ACTIVITY_TABLE_ID, record_id, {FIELD_ACT_GROUP_STATUS: "已截止"})

    # 收集选择数据
    selections_records = search_records(GROUP_SELECT_TABLE, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_GS_ACTIVITY_ID, "operator": "is", "value": [activity_id]}]
    })

    participants = []
    seen_oids = set()
    selections = {}
    skipped = 0

    for sr in selections_records:
        sf = sr.get("fields", {})
        oid = get_field_text(sf, FIELD_GS_SELECTOR_OID)
        gender = get_field_text(sf, FIELD_GS_SELECTOR_GENDER)
        if not oid or oid in seen_oids:
            continue
        if gender not in ("男性", "女性"):
            skipped += 1
            continue
        seen_oids.add(oid)
        participants.append({"id": oid, "gender": "male" if gender == "男性" else "female"})
        choices = []
        for i, cf in enumerate(FIELD_GS_CHOICES):
            val = get_field_text(sf, cf)
            if val:
                choices.append({"id": val, "priority": i + 1})
        if choices:
            selections[oid] = choices

    n_males = sum(1 for p in participants if p["gender"] == "male")
    n_females = sum(1 for p in participants if p["gender"] == "female")

    if n_males < m_per or n_females < f_per:
        # 人数不足，恢复状态为收集中
        update_record(ACTIVITY_TABLE_ID, record_id, {FIELD_ACT_GROUP_STATUS: "收集中"})
        return (f"人数不足，无法分组：{n_males}男{n_females}女，"
                f"每组需要{m_per}男{f_per}女。\n"
                f"状态已恢复为「收集中」，等人够了再截止。"
                + (f"\n（{skipped}人因性别信息缺失被跳过）" if skipped else ""))

    # 运行算法
    try:
        groups = run_grouping_algorithm(participants, selections, m_per, f_per)
    except Exception as e:
        update_record(ACTIVITY_TABLE_ID, record_id, {FIELD_ACT_GROUP_STATUS: "收集中"})
        return f"分组算法出错：{e}，状态已恢复为「收集中」"

    if not groups:
        update_record(ACTIVITY_TABLE_ID, record_id, {FIELD_ACT_GROUP_STATUS: "收集中"})
        return "分组失败，人数不足以组成完整小组，状态已恢复为「收集中」"

    # 清除旧结果（仅清该活动本轮次，批量删除避免逐条拖慢）
    old_results = search_records(GROUP_RESULT_TABLE, {
        "conjunction": "and",
        "conditions": [
            {"field_name": FIELD_GR_ACTIVITY_ID, "operator": "is", "value": [activity_id]},
            {"field_name": FIELD_GR_ROUND, "operator": "is", "value": [str(round_no)]}
        ]
    })
    if old_results:
        batch_delete_records(GROUP_RESULT_TABLE, [old["record_id"] for old in old_results])

    # 保存结果（用户自查 H5 查询），不再逐人群发通知（现场活动）。
    # 批量写入分组结果，避免逐条 create_record 在大活动下（数百条）拖到好几分钟。
    oid_to_nickname = {p["open_id"]: p["nickname"] for p in get_activity_signups(activity_id)}
    result_records = []
    for g in groups:
        group_no = g["group_id"]
        all_members = [(mid, "男性") for mid in g["male_ids"]] + [(fid, "女性") for fid in g["female_ids"]]
        for oid, gender in all_members:
            result_records.append({
                FIELD_GR_ACTIVITY_ID: activity_id,
                FIELD_GR_GROUP_NO: group_no,
                FIELD_GR_USER_OID: oid,
                FIELD_GR_USER_NAME: oid_to_nickname.get(oid, ""),
                FIELD_GR_USER_GENDER: gender,
                FIELD_GR_ROUND: str(round_no)
            })
    batch_create_records(GROUP_RESULT_TABLE, result_records)

    # 构建给管理员的完整分组结果文案
    lines = [f"🎉 活动「{activity_name}」第{round_no}轮 分组结果", f"共{len(groups)}组，参与{len(participants)}人，每组{m_per}男{f_per}女。", ""]
    for g in groups:
        all_members = [(mid, "男性") for mid in g["male_ids"]] + [(fid, "女性") for fid in g["female_ids"]]
        names = "、".join(oid_to_nickname.get(oid, oid) for oid, _ in all_members)
        lines.append(f"第{g['group_id']}组：{names}")
    lines.append("")
    lines.append("用户可自查 H5 查看自己的分组结果。")

    # 更新状态为已完成
    update_record(ACTIVITY_TABLE_ID, record_id, {FIELD_ACT_GROUP_STATUS: "已完成"})

    log(f"第{round_no}轮分组完成: {activity_name}, {len(groups)}组, {len(participants)}人")
    return "\n".join(lines)




def handle_admin_group_status(keyword):
    """管理员：查看分组状态 格式: 分组状态 A-0002"""
    activity_id = keyword.strip()
    if not activity_id:
        return "格式：分组状态 活动ID"

    activity = find_activity_by_id(activity_id)
    if not activity:
        return f"未找到活动：{activity_id}"

    af = activity.get("fields", {})
    activity_name = get_field_text(af, FIELD_ACTIVITY_NAME)
    status = get_field_text(af, FIELD_ACT_GROUP_STATUS) or "未开始"
    m_per = get_field_number(af, FIELD_ACT_MALE_PER_GROUP, 0)
    f_per = get_field_number(af, FIELD_ACT_FEMALE_PER_GROUP, 0)

    signups = get_activity_signups(activity_id)
    selections = search_records(GROUP_SELECT_TABLE, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_GS_ACTIVITY_ID, "operator": "is", "value": [activity_id]}]
    })

    return (f"活动「{activity_name}」分组状态：\n"
            f"状态：{status}\n"
            f"每组：{int(m_per)}男{int(f_per)}女\n"
            f"报名人数：{len(signups)}\n"
            f"已提交选择：{len(selections)}人")




def handle_admin_unsubmitted(keyword):
    """管理员：查看未提交志愿的报名者 格式: 查看未提交 A-0002"""
    activity_id = keyword.strip()
    if not activity_id:
        return "格式：查看未提交 活动ID"
    activity = find_activity_by_id(activity_id)
    if not activity:
        return f"未找到活动：{activity_id}"
    act_name = get_field_text(activity.get("fields", {}), FIELD_ACTIVITY_NAME)

    signups = get_activity_signups(activity_id)          # list of {open_id, nickname, gender}
    selections = search_records(GROUP_SELECT_TABLE, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_GS_ACTIVITY_ID, "operator": "is", "value": [activity_id]}]
    })
    submitted_oids = {
        get_field_text(s.get("fields", {}), FIELD_GS_SELECTOR_OID)
        for s in selections if get_field_text(s.get("fields", {}), FIELD_GS_SELECTOR_OID)
    }

    unsubmitted = []
    for s in signups:
        oid = s["open_id"]
        if oid in submitted_oids:
            continue
        user_id = ""
        for rec in find_user_by_openid(oid):            # returns a list
            user_id = rec.get("fields", {}).get("用户ID", "") or ""
        unsubmitted.append((oid, user_id, s["gender"], s["nickname"]))

    if not unsubmitted:
        return f"活动「{act_name}」所有报名者均已提交志愿。"
    lines = [f"「{act_name}」未提交志愿（{len(unsubmitted)}人）："]
    for _, user_id, gender, nick in unsubmitted:
        lines.append(f"{user_id or '-'} · {gender or '-'} · {nick}")
    return "\n".join(lines)


