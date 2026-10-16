"""活动一对一（bot 侧）：**渲染层 + re-export**，业务逻辑在 lib/onetoone.py。

与 bot/grouping.py 同构：匹配算法、志愿读取/写入、执行状态机都搬进 lib.onetoone，
这里只做两件事：把 lib 回传的结构化结果拼成给用户/管理员看的中文，以及保留旧入口
（get_activity_signups / get_user_onetoone_selection / _build_profiles 等）的
re-export，外部 seed 脚本与旧代码继续可用。

回传的每一句中文字符串与迁移前逐字相同，并由 tests/test_onetoone_parity.py 对着
tests/_golden_onetoone_impl.py（自 HEAD 冻结）逐案比对钉死——改文案必红灯。
"""
from clients import *
from constants import *
from queries import find_activity_by_id, find_user_by_id_or_name, find_user_by_openid

from lib import grouping as lib_grouping
from lib import onetoone as lib_onetoone
from lib.onetoone import (  # noqa: F401 — re-export 兼容旧入口，勿删
    ONETOONE_UNSELECTED_SCORE,
    WEIGHT_OFFLINE_PRIORITY,
    WEIGHT_ONETOONE_MATCH,
    WEIGHT_ONETOONE_MY_PRIORITY,
    WEIGHT_ONETOONE_THEIR_PRIORITY,
    profile_of,
    run_offline_onetoone_matching,
    run_onetoone_matching,
)


def configure_onetoone_lib(lock_dir):
    """把本进程用的表 ID / 栏位名注入 lib.onetoone。

    放在这里而不是 bot/yixianqian_bot_ws.py，是为了让「lib 需要哪些栏位」只有一处
    定义——main() 和 tests/test_onetoone_parity.py 调的是同一个函数，两边不会各配
    各的、各配错的。lock_dir 会一并转注入 lib.grouping（提交/开始收集的跨进程互斥
    复用分组那批 yixianqian_grouping_<活动>.lock）。
    """
    lib_onetoone.configure(
        onetoone_select_table=ONETOONE_SELECT_TABLE,
        onetoone_result_table=ONETOONE_RESULT_TABLE,
        activity_table_id=ACTIVITY_TABLE_ID,
        signup_table_id=SIGNUP_TABLE_ID,
        fields={
            "activity_id": FIELD_ACTIVITY_ID,
            "activity_name": FIELD_ACTIVITY_NAME,
            "onetoone_status": FIELD_ACT_ONETOONE_STATUS,
            "onetoone_flag": FIELD_ACT_ONETOONE_FLAG,
            "group_flag": FIELD_ACT_GROUP_FLAG,
            "group_status": FIELD_ACT_GROUP_STATUS,
            "oto_activity_id": FIELD_OTO_ACTIVITY_ID,
            "oto_selector_oid": FIELD_OTO_SELECTOR_OID,
            "oto_selector_name": FIELD_OTO_SELECTOR_NAME,
            "oto_selector_gender": FIELD_OTO_SELECTOR_GENDER,
            "oto_choices": FIELD_OTO_CHOICES,
            "oto_rank": FIELD_OTO_RANK,
            "oto_user_oid": FIELD_OTO_USER_OID,
            "oto_user_name": FIELD_OTO_USER_NAME,
            "oto_user_gender": FIELD_OTO_USER_GENDER,
            "oto_target_oid": FIELD_OTO_TARGET_OID,
            "oto_target_name": FIELD_OTO_TARGET_NAME,
            "oto_round": FIELD_OTO_ROUND,
            "signup_activity_id": FIELD_SIGNUP_ACTIVITY_ID,
            "signup_openid": FIELD_SIGNUP_OPENID,
            "signup_nickname": FIELD_SIGNUP_NICKNAME,
            "signup_status": FIELD_SIGNUP_STATUS,
            "gender": FIELD_GENDER,
            "user_id": FIELD_USER_ID,
            "real_name": FIELD_REAL_NAME,
            "self_hobbies": FIELD_SELF_HOBBIES,
            "self_sports": FIELD_SELF_SPORTS,
            "self_traits": FIELD_SELF_TRAITS,
            "education": FIELD_EDUCATION,
            "mbti": FIELD_MBTI,
            "city": FIELD_CITY,
            "church": FIELD_CHURCH,
        },
        lock_dir=lock_dir,
    )




def get_activity_signups(activity_id):
    """获取活动的已报名用户列表（旧入口，保留名字）。"""
    return lib_onetoone.build_participants(
        activity_id, client=bitable, find_user_by_openid=find_user_by_openid)




def get_user_onetoone_selection(activity_id, open_id):
    """获取用户在某活动的一对一选择（旧入口，保留名字）。"""
    return lib_onetoone.fetch_selection(activity_id, open_id, client=bitable)




def _build_profiles(open_ids):
    """为每个 open_id 构建打分资料（旧入口，保留名字；seed 脚本在用）。"""
    return lib_onetoone.build_profiles(open_ids, find_user_by_openid=find_user_by_openid)




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
        # 现场活动统一显示「真名（缺省昵称）＋编号」：永远带编号，避免选错人
        options = [{"text": {"tag": "plain_text", "content": _option_label((p.get("real_name") or "").strip() or p["nickname"], p.get("user_id", ""), True)},
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
            # 与 H5 活动页/活动卡入口同口径：只看「一对一功能开启」，
            # 志愿收集状态已废除
            if get_field_text(af, FIELD_ACT_ONETOONE_FLAG) == "是":
                collecting_activities.append(activity)

    if not collecting_activities:
        return "当前没有已开启一对一功能的活动。\n一对一功能由管理员开启，请关注通知。"

    activity = collecting_activities[0]
    af = activity.get("fields", {})
    activity_id = get_field_text(af, FIELD_ACTIVITY_ID)
    activity_name = get_field_text(af, FIELD_ACTIVITY_NAME)

    participants = get_activity_signups(activity_id)
    # 现场签到（条件开启）：候选展示池只留已到者；查不到名单时照常发卡（提交路径
    # 会 fail-closed 拦住未到者）。桩表/未迁移环境无该栏位 → 不触发，行为不变。
    if lib_grouping.signin_enabled(af):
        attendance = lib_grouping.attendance_map(activity_id, client=bitable)
        if attendance is not None:
            participants = [p for p in participants
                            if lib_grouping.is_checked_in(attendance.get(p["open_id"]))]
    existing = get_user_onetoone_selection(activity_id, sender_id)

    card = build_onetoone_select_card(
        activity_id, activity_name, participants, user_gender,
        existing_choices=existing.get("choices") if existing else None
    )
    send_card_message(sender_id, card)

    if len(collecting_activities) > 1:
        return f"你报名了多个已开启一对一功能的活动，当前显示「{activity_name}」。如需其他活动请联系管理员。"
    return ""


def handle_onetoone_submit(operator_open_id, action_value, form_value):
    """处理一对一选择提交"""
    activity_id = action_value.get("activity_id", "")

    # 验证活动存在（志愿随时可改：不再按状态拦截提交；签到闸门在下方 fail-closed 保留）
    activity = find_activity_by_id(activity_id)
    if not activity:
        return {"toast": {"type": "error", "content": "活动不存在"}}
    af = activity.get("fields", {})

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

    # 现场签到准入（条件开启）：本人须已到；查询失败 fail-closed（业务准入）。
    # 活动表没有「签到功能开启」栏位（parity 桩表/未迁移环境）→ 不触发，行为不变。
    attendance = None
    if lib_grouping.signin_enabled(af):
        attendance = lib_grouping.attendance_map(activity_id, client=bitable)
        if attendance is None:
            return {"toast": {"type": "error", "content": "签到信息查询失败，请重试"}}
        if not lib_grouping.is_checked_in(attendance.get(operator_open_id)):
            return {"toast": {"type": "error",
                              "content": "本活动需现场签到后才能填志愿，请联系管理员"}}

    user_recs = find_user_by_openid(operator_open_id)
    if not user_recs:
        return {"toast": {"type": "error", "content": "用户信息异常"}}
    uf = user_recs[0].get("fields", {})
    user_nickname = get_field_text(uf, FIELD_NICKNAME)
    user_gender = get_field_text(uf, FIELD_GENDER)

    participants = get_activity_signups(activity_id)
    if attendance is not None:
        # 候选池同样只留已到异性（与 H5 candidates 同口径）
        participants = [p for p in participants
                        if lib_grouping.is_checked_in(attendance.get(p["open_id"]))]

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

    res = lib_onetoone.validate_choices(choices, participants, user_gender)
    if res["code"] == "COUNT":
        return {"toast": {"type": "error", "content": f"请选择1-7位（当前{len(choices)}位）"}}
    if res["code"] == "DUPLICATE":
        return {"toast": {"type": "error", "content": "不能重复选择同一人"}}
    if res["code"] == "INVALID_TARGET":
        return {"toast": {"type": "error", "content": "选择包含无效参与者"}}

    fields = {
        FIELD_OTO_ACTIVITY_ID: activity_id,
        FIELD_OTO_SELECTOR_OID: operator_open_id,
        FIELD_OTO_SELECTOR_NAME: user_nickname,
        FIELD_OTO_SELECTOR_GENDER: user_gender,
    }
    # 志愿位先全清空，再按顺序填（1..7 位都可提交，空位写空串）
    for cf in FIELD_OTO_CHOICES:
        fields[cf] = ""
    for i, cf in enumerate(FIELD_OTO_CHOICES):
        if i < len(choices):
            fields[cf] = choices[i]

    save = lib_onetoone.save_selection(activity_id, operator_open_id, fields, client=bitable)
    if save["code"] == "BUSY":
        return {"toast": {"type": "error", "content": "当前已有一对一操作正在进行，请稍后重试。"}}
    if save["code"] != "OK":
        # 查重读失败或写失败都不得报成功（旧实现不检查返回值，写失败也发「已提交」）
        return {"toast": {"type": "error", "content": "提交失败，请稍后再试"}}
    msg = "选择已更新" if save["updated"] else "选择已提交"

    log(f"一对一选择: {user_nickname} 活动{activity_id} {msg}")
    return {"toast": {"type": "success", "content": msg}}


def handle_admin_reset_onetoone(keyword):
    """管理员：清空一对一结果 格式: 清空一对一结果 A-0002（该活动全部轮次）"""
    activity_id = keyword.strip()
    if not activity_id:
        return "格式：清空一对一结果 活动ID\n例如：清空一对一结果 A-0002"

    activity = find_activity_by_id(activity_id)
    if not activity:
        return f"未找到活动：{activity_id}"

    res = lib_onetoone.reset_onetoone_results(activity_id, client=bitable, activity=activity)
    code = res.get("code")
    if code == "BUSY":
        return "当前已有一对一任务在执行中，请稍候再试。"
    if code == "RESULTS_SEARCH_FAILED":
        return "清空失败：读取历史一对一结果失败，未做任何删除，请稍后重试。"
    if code == "DELETE_INCOMPLETE":
        return (f"清空失败：一对一结果删除不完整（成功删除 {res['deleted']}/{res['total']} 条），"
                f"请稍后重试。")
    if code == "STATUS_FAILED":
        return (f"清空失败：活动「{res.get('activity_name') or activity_id}」状态清空写入失败"
                f"（已删除 {res.get('cleared_results', 0)} 条结果），请稍后重试。")
    if code != "OK":
        return f"清空一对一结果失败（{code}），请稍后重试。"
    log(f"管理员清空一对一结果: {res['activity_name']} {res['cleared_results']} 条")
    return (f"已清空活动「{res['activity_name']}」的历史一对一结果 {res['cleared_results']} 条，"
            f"一对一状态已重置为未开始。")


def _render_execute_result(res, activity_id):
    """把 execute 的结构化结果拼成给管理员看的中文。

    文案归渲染层：lib 只给数据。失败一律零状态变更，文案不再出现「恢复收集中」
    之类的状态话术；清理残留的指引统一为「清空一对一结果」。
    """
    code = res["code"]
    if code == "ROUND_OUT_OF_RANGE":
        return "轮次必须是不小于 1 的整数（本次未写入任何数据）。"
    if code == "EXCLUSIVE_MODE":
        return (f"无法执行一对一：活动「{res['activity_name']}」的分组模式已开启。\n"
                f"请先在 H5「活动管理」把模式切换为「一对一」再执行。")
    if code == "FEMALE_CHATS_REQUIRED":
        return ("请指定女生场次（每位坐镇女生接待几位男生）。\n"
                "格式：执行一对一 活动ID 女生场次 [轮次]\n"
                "例如：执行一对一 A-0002 10")
    if code == "FEMALE_CHATS_INVALID":
        if "error" in res:
            # 锁内算法拒绝（如女生场次 > 男生人数），零写入
            return f"女生场次不合法：{res['error']}（本次未写入任何数据，请调整后重试）"
        # 进门校验（女生场次非正整数），零写入
        return (f"女生场次必须是 ≥1 的整数（每位女生接待的男生数），当前 {res.get('female_chats')}。\n"
                "格式：执行一对一 活动ID 女生场次 [轮次]")
    if code == "ALREADY_DONE":
        return (f"活动「{res['activity_name']}」第{res['round']}轮已生成过一对一结果，请勿重复执行。"
                f"如需重新匹配，请先发送「清空一对一结果 {activity_id}」清除已有结果，再执行。")
    if code == "BUSY":
        return "当前已有一对一任务在执行中，请稍候再试。"
    if code == "SEARCH_FAILED":
        return "查询历史结果失败，无法确认是否已执行过，已中止（本次未写入任何数据），请稍后重试。"
    if code == "SELECTIONS_SEARCH_FAILED":
        return "读取一对一志愿失败，已中止匹配，本次未写入任何数据，请稍后重试。"
    if code == "SIGNIN_QUERY_FAILED":
        return "签到名单读取失败，无法确定现场到场名单，已中止匹配，本次未写入任何数据，请稍后重试。"
    if code == "NO_PARTICIPANTS":
        return "没有已报名且性别有效的参与者，本次未写入任何数据。"
    if code == "ALGORITHM_ERROR":
        return f"一对一匹配出错：{res['error']}，本次未写入任何数据。"
    if code == "PARTIAL_WRITE":
        return (f"一对一结果写入失败（成功 {res['created']}/{res['total']} 条），已回滚本轮已写入部分，"
                f"状态未变，请稍后重试。")
    if code == "PARTIAL_WRITE_VERIFY_FAILED":
        return (f"结果写入不完整（成功 {res['created']}/{res['total']}）且回滚校验失败，"
                f"可能残留结果；请先发送「清空一对一结果 {activity_id}」清理残留后再执行。")
    if code == "PARTIAL_WRITE_INCOMPLETE":
        return (f"一对一结果写入失败（成功 {res['created']}/{res['total']} 条），回滚不完整"
                f"（残留 {res['residual']} 条结果），请先发送「清空一对一结果 {activity_id}」"
                f"清理残留后再执行。")
    if code == "DONE_STATUS_FAILED":
        return "结果已生成，但状态写入失败（未标记为「已完成」），用户可能看不到结果；请稍后重试或让管理员检查。"

    # 走到这里就是 OK
    return (f"🎉 活动「{res['activity_name']}」第{res['round']}轮 一对一匹配完成：\n"
            f"参与 {res['participant_count']} 人，共生成 {res['total_pairs']} 条必聊关系。\n"
            f"用户可在「我的活动」或活动页查看名单。")


def handle_admin_stop_onetoone(keyword):
    """管理员：执行一对一（女生坐镇均衡分配） 格式: 执行一对一 活动ID 女生场次 [轮次]

    轮次缺省 = 最新已有轮次 + 1（由 lib_onetoone.latest_round 推演；无结果则第1轮）。
    """
    parts = keyword.split()
    if len(parts) < 2:
        return ("格式：执行一对一 活动ID 女生场次 [轮次]\n"
                "例如：执行一对一 A-0002 15 或 执行一对一 A-0002 15 2")
    activity_id = parts[0]
    try:
        female_chats = int(parts[1])
        if female_chats < 1:
            return "女生场次必须为大于0的整数（每位女生接待的男生数）"
    except ValueError:
        return f"女生场次必须为数字（每位女生接待的男生数），当前：{parts[1]}"
    round_no = None
    if len(parts) >= 3:
        try:
            round_no = int(parts[2])
            if round_no < 1:
                return "轮次必须为大于0的整数"
        except ValueError:
            return f"轮次必须为数字，当前：{parts[2]}"

    activity = find_activity_by_id(activity_id)
    if not activity:
        return f"未找到活动：{activity_id}"

    if round_no is None:
        latest = lib_onetoone.latest_round(bitable, activity_id)
        if latest is None:
            return "查询历史一对一结果失败，无法确定默认轮次，请稍后重试或显式指定轮次。"
        round_no = latest + 1

    res = lib_onetoone.execute(activity_id, round_no, client=bitable, activity=activity,
                               find_user_by_openid=find_user_by_openid,
                               female_chats=female_chats)
    return _render_execute_result(res, activity_id)


def handle_admin_onetoone_status(keyword):
    """管理员：查看一对一状态 格式: 一对一状态 A-0002"""
    activity_id = keyword.strip()
    if not activity_id:
        return "格式：一对一状态 活动ID"

    activity = find_activity_by_id(activity_id)
    if not activity:
        return f"未找到活动：{activity_id}"

    res = lib_onetoone.fetch_status(activity_id, client=bitable, activity=activity,
                                    find_user_by_openid=find_user_by_openid)
    rounds = res.get("rounds") or []
    rounds_text = "、".join(f"第{r}轮" for r in rounds) if rounds else "无"
    return (f"活动「{res['name']}」一对一状态：\n"
            f"状态：{res['status']}\n"
            f"报名人数：{res['signup_count']}\n"
            f"已提交选择：{res['submitted_count']}人\n"
            f"已生成轮次：{rounds_text}")


def handle_admin_unsubmitted_onetoone(keyword):
    """管理员：查看未提交一对一志愿的报名者 格式: 查看未提交一对一 A-0002"""
    activity_id = keyword.strip()
    if not activity_id:
        return "格式：查看未提交一对一 活动ID"
    activity = find_activity_by_id(activity_id)
    if not activity:
        return f"未找到活动：{activity_id}"

    res = lib_onetoone.fetch_unsubmitted(activity_id, client=bitable, activity=activity,
                                         find_user_by_openid=find_user_by_openid)
    if not res["unsubmitted"]:
        return f"活动「{res['name']}」所有报名者均已提交志愿。"
    lines = [f"「{res['name']}」未提交志愿（{res['total']}人）："]
    for u in res["unsubmitted"]:
        # 名字=真名缺省昵称（现场活动统一口径；uid/性别格式不变）
        lines.append(f"{u['user_id'] or '-'} · {u['gender'] or '-'} · {u.get('real_name') or u['nickname']}")
    return "\n".join(lines)
