"""活动分组（bot 侧）：**渲染层 + re-export**，业务逻辑在 lib/grouping.py。

管理员那五条指令的算法、读写、幂等判断都搬进了 `lib.grouping`，这里只做两件事：
把 lib 回传的结构化结果拼成给管理员看的中文，以及把用户侧的分组卡片流程原样留着
（用户侧 H5 已经完整实现，飞书卡片是旧通道，本次不迁移）。

回传的每一句中文字符串与迁移前逐字相同，并由 tests/test_grouping_parity.py 对着
从 base commit 冻结的旧实作逐案比对钉死——改文案必红灯。

保留的 re-export 是为了让旧入口继续可用：bot/onetoone.py 与
scripts/dev/seed_test_grouping.py 直接 `from grouping import ...`。
"""

from clients import *
from constants import *
from queries import find_activity_by_id, find_user_by_id_or_name, find_user_by_openid

from lib import grouping as lib_grouping
from lib.grouping import (  # noqa: F401 — re-export 兼容旧入口，勿删
    PRIORITY_SCORES,
    GroupingBusy,
    _dup_nicknames,
    _mutual_affinity_score,
    _option_label,
    collect_participants,
    grouping_lock,
    run_grouping_algorithm,
)

# 分组指令互斥锁：防止并发「执行分组」「开始填志愿」互相删除/覆盖结果。
# bot 与 H5 后端是两个进程、共用同一个数据目录，单纯的 threading.Lock 挡不住对面，
# 所以走 lib.grouping 的 flock 版本。
GROUPING_LOCK_TIMEOUT = 180
# 抢锁逾时的提示。整条路径是新增的（原先是无限等待，压根等不到这个分支）。
GROUPING_BUSY_REPLY = "当前已有分组任务在执行中，请稍候再试。"


def configure_grouping_lib(lock_dir):
    """把本进程用的表 ID / 栏位名注入 lib.grouping。

    放在这里而不是 bot/yixianqian_bot_ws.py，是为了让「lib 需要哪些栏位」只有一处
    定义——main() 和 tests/test_grouping_parity.py 调的是同一个函数，两边不会各配
    各的、各配错的。H5 后端另有一份自己那份注入。
    """
    lib_grouping.configure(
        activity_table_id=ACTIVITY_TABLE_ID,
        group_select_table=GROUP_SELECT_TABLE,
        group_result_table=GROUP_RESULT_TABLE,
        signup_table_id=SIGNUP_TABLE_ID,
        fields={
            "activity_id": FIELD_ACTIVITY_ID,
            "activity_name": FIELD_ACTIVITY_NAME,
            "group_status": FIELD_ACT_GROUP_STATUS,
            "male_per_group": FIELD_ACT_MALE_PER_GROUP,
            "female_per_group": FIELD_ACT_FEMALE_PER_GROUP,
            "group_flag": FIELD_ACT_GROUP_FLAG,
            "onetoone_flag": FIELD_ACT_ONETOONE_FLAG,
            "onetoone_status": FIELD_ACT_ONETOONE_STATUS,
            "gs_activity_id": FIELD_GS_ACTIVITY_ID,
            "gs_selector_oid": FIELD_GS_SELECTOR_OID,
            "gs_selector_gender": FIELD_GS_SELECTOR_GENDER,
            "gs_choices": FIELD_GS_CHOICES,
            "gr_activity_id": FIELD_GR_ACTIVITY_ID,
            "gr_group_no": FIELD_GR_GROUP_NO,
            "gr_user_oid": FIELD_GR_USER_OID,
            "gr_user_name": FIELD_GR_USER_NAME,
            "gr_user_gender": FIELD_GR_USER_GENDER,
            "gr_round": FIELD_GR_ROUND,
            "signup_activity_id": FIELD_SIGNUP_ACTIVITY_ID,
            "signup_status": FIELD_SIGNUP_STATUS,
            "signup_openid": FIELD_SIGNUP_OPENID,
            "signup_nickname": FIELD_SIGNUP_NICKNAME,
            "gender": FIELD_GENDER,
            "user_id": "用户ID",
        },
        lock_dir=lock_dir,
    )




def get_activity_signups(activity_id):
    """获取活动的已报名用户列表（bot/onetoone.py 仍在用这个入口）。"""
    return lib_grouping.collect_participants(
        activity_id, client=bitable, find_user_by_openid=find_user_by_openid)




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
    dup_nick = use_select and _dup_nicknames(opp_participants)

    hint = "如遇同名请以编号为准。" if dup_nick else ""
    form_elements = [
        {
            "tag": "markdown",
            "content": f"**活动：{activity_name}**\n请从{len(opp_participants)}位{opp_gender}中选择7位想同组的人，按意愿从高到低排序。{hint}"
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
                # 输入编号（如U-0003）直接按ID转换；输名字则精确匹配，重名时报错引导改用编号
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

    activity_name = get_field_text(activity.get("fields", {}), FIELD_ACTIVITY_NAME)

    # 改状态 + 清历史结果必须整段持锁。原先这段完全不持锁，而管理指令跑在
    # ADMIN_TASK_EXECUTOR（4 条线程）上，同时来「开始填志愿」和「执行分组」
    # 就会删掉一半旧结果、写进去一半新结果。
    try:
        with grouping_lock(activity_id, timeout=GROUPING_LOCK_TIMEOUT):
            res = lib_grouping.start_group(activity_id, m_per, f_per,
                                           client=bitable, activity=activity)
    except GroupingBusy:
        return GROUPING_BUSY_REPLY

    # start_group 的回传码不能丢。lib 里 SEARCH_FAILED 只有在
    # treat_search_failure_as_empty=False 且「读历史分组结果失败」时才可能返回；
    # bot 走默认 True，所以今天这行恒为 OK（parity 42 案例为证）——但正因如此，
    # 一旦日后有人照 H5 的接法改成传 False，「清历史失败」就会被静默吞掉，管理员
    # 仍收到「志愿填写已开始」。这里钉一道断言，让那种改动在这里立刻炸掉，
    # 而不是悄悄发一条与事实相反的回执。抛异常由 _admin_task_worker 统一兜成
    # 「任务处理失败：…」，不会把错误伪装成成功。
    if res.get("code") == "EXCLUSIVE_MODE":
        # 动作层互斥（设计 §2.3）：对面一对一开着或没收尾，明确让管理员先关对面。
        # 新分支只在活动表有一对一栏位且命中时出现，parity 桩表不触发。
        return ("无法开始填志愿：该活动的一对一模式正在使用中。\n"
                "为避免两边混着跑，请先在 H5「活动管理」把模式切换为「分组」"
                "（或等一对一收尾）后再试。")
    if res.get("code") != "OK":
        raise RuntimeError(
            f"start_group 未成功（code={res.get('code')}），不发送成功回执。"
            "bot 应保持默认 treat_search_failure_as_empty=True；若确需改为严格模式，"
            "请一并为该分支补上面向管理员的失败文案。")

    # 不再向报名者群发通知（现场活动，用户自查 H5）。管理员收到开始指令回执即可。
    log(f"管理员开始填志愿: {activity_name} {m_per}男{f_per}女")
    return (f"志愿填写已开始：{activity_name}（每组{m_per}男{f_per}女）\n"
            f"已开放，未发送群发通知。\n"
            f"请让用户在 H5 页面自查并提交志愿（现场活动）。")




def _render_grouping_result(res, round_no):
    """把 execute_round 的结构化结果拼成给管理员看的中文。

    文案归渲染层：lib 只给数据。改这里的字必须同步改 tests/_golden_grouping_impl.py
    之外的那份断言——等价性测试会直接红。
    """
    code = res["code"]
    if code == "EXCLUSIVE_MODE":
        return (f"无法执行分组：活动「{res['activity_name']}」的一对一模式已开启。\n"
                f"请先在 H5「活动管理」把模式切换为「分组」再执行。")
    if code == "NOT_ENOUGH_PEOPLE":
        return (f"人数不足，无法分组：{res['males']}男{res['females']}女，"
                f"每组需要{res['males_per_group']}男{res['females_per_group']}女。\n"
                f"状态已恢复为「收集中」，等人够了再截止。"
                + (f"\n（{res['skipped']}人因性别信息缺失被跳过）" if res["skipped"] else ""))
    if code == "ALGORITHM_ERROR":
        return f"分组算法出错：{res['error']}，状态已恢复为「收集中」"
    if code == "EMPTY_GROUPS":
        return "分组失败，人数不足以组成完整小组，状态已恢复为「收集中」"
    if code == "PARTIAL_WRITE":
        return (f"分组结果写入失败（成功 {res['created']}/{res['total']} 条），已回滚，"
                f"状态恢复为「收集中」，请稍后重试。")

    # 走到这里就是 OK。bot 侧永远不会拿到 SEARCH_FAILED——execute_round 默认
    # treat_search_failure_as_empty=True，那条是留给 H5 用的。
    lines = [f"🎉 活动「{res['activity_name']}」第{round_no}轮 分组结果",
             f"共{res['group_count']}组，参与{res['participant_count']}人，"
             f"每组{res['males_per_group']}男{res['females_per_group']}女。", ""]
    for g in res["groups"]:
        names = "、".join(m["display_name"] for m in g["members"])
        lines.append(f"第{g['group_id']}组：{names}")
    lines.append("")
    lines.append("用户可自查 H5 查看自己的分组结果。")
    return "\n".join(lines)




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
    m_per = int(get_field_number(af, FIELD_ACT_MALE_PER_GROUP, 3))
    f_per = int(get_field_number(af, FIELD_ACT_FEMALE_PER_GROUP, 3))

    if m_per < 1 or f_per < 1:
        return f"每组男女人数必须大于0（当前：{m_per}男{f_per}女），请先设置再截止"

    try:
        with grouping_lock(activity_id, timeout=GROUPING_LOCK_TIMEOUT):
            res = lib_grouping.execute_round(
                activity_id, round_no, client=bitable, activity=activity,
                find_user_by_openid=find_user_by_openid)
            if res["code"] == "ALREADY_DONE":
                return (f"活动「{res['activity_name']}」第{round_no}轮已生成过分组结果，请勿重复执行。"
                        f"如需重新分组，请先发送「开始填志愿 {activity_id} {m_per} {f_per}」重新开放，再执行分组。")
            return _render_grouping_result(res, round_no)
    except GroupingBusy:
        return GROUPING_BUSY_REPLY


def handle_admin_group_status(keyword):
    """管理员：查看分组状态 格式: 分组状态 A-0002"""
    activity_id = keyword.strip()
    if not activity_id:
        return "格式：分组状态 活动ID"

    activity = find_activity_by_id(activity_id)
    if not activity:
        return f"未找到活动：{activity_id}"

    res = lib_grouping.fetch_status(activity_id, client=bitable, activity=activity,
                                    find_user_by_openid=find_user_by_openid)
    return (f"活动「{res['name']}」分组状态：\n"
            f"状态：{res['group_status']}\n"
            f"每组：{int(res['males_per_group'])}男{int(res['females_per_group'])}女\n"
            f"报名人数：{res['signup_count']}\n"
            f"已提交选择：{res['submitted_count']}人")




def handle_admin_unsubmitted(keyword):
    """管理员：查看未提交志愿的报名者 格式: 查看未提交 A-0002"""
    activity_id = keyword.strip()
    if not activity_id:
        return "格式：查看未提交 活动ID"
    activity = find_activity_by_id(activity_id)
    if not activity:
        return f"未找到活动：{activity_id}"

    res = lib_grouping.fetch_unsubmitted(activity_id, client=bitable, activity=activity,
                                         find_user_by_openid=find_user_by_openid)
    act_name = res["name"]
    unsubmitted = res["unsubmitted"]

    if not unsubmitted:
        return f"活动「{act_name}」所有报名者均已提交志愿。"
    lines = [f"「{act_name}」未提交志愿（{len(unsubmitted)}人）："]
    for u in unsubmitted:
        lines.append(f"{u['user_id'] or '-'} · {u['gender'] or '-'} · {u['nickname']}")
    return "\n".join(lines)
