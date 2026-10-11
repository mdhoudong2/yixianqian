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

from lib import onetoone as lib_onetoone
from lib.onetoone import (  # noqa: F401 — re-export 兼容旧入口，勿删
    ONETOONE_UNSELECTED_SCORE,
    WEIGHT_ONETOONE_MATCH,
    WEIGHT_ONETOONE_MY_PRIORITY,
    WEIGHT_ONETOONE_THEIR_PRIORITY,
    profile_of,
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
            "signup_activity_id": FIELD_SIGNUP_ACTIVITY_ID,
            "signup_openid": FIELD_SIGNUP_OPENID,
            "signup_nickname": FIELD_SIGNUP_NICKNAME,
            "signup_status": FIELD_SIGNUP_STATUS,
            "gender": FIELD_GENDER,
            "user_id": FIELD_USER_ID,
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
    activity_id = get_field_text(af, FIELD_ACTIVITY_ID)
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


def _render_start_result(res):
    """把 start_collection 的结构化结果拼成给管理员看的中文。"""
    code = res["code"]
    if code == "OK":
        return (f"一对一志愿填写已开始：{res['activity_name']}\n"
                f"已开放，未发送群发通知。\n"
                f"请让用户在 H5 页面自查并提交志愿（现场活动）。")
    if code == "BUSY":
        return "当前已有一对一任务在执行中，请稍候再试。"
    if code == "STATUS_FAILED":
        return f"一对一志愿填写开始失败：活动「{res['activity_name']}」状态写入失败，请稍后重试。"
    if code == "RESULTS_SEARCH_FAILED":
        return (f"一对一志愿填写开始失败：活动「{res['activity_name']}」状态已写入「收集中」，"
                f"但读取历史结果失败，历史结果可能残留；请稍后重试「开始一对一」清理后再执行。")
    if code == "DELETE_INCOMPLETE":
        return (f"一对一志愿填写开始失败：活动「{res['activity_name']}」历史结果清理不完整"
                f"（成功删除 {res['deleted']}/{res['total']} 条），请稍后重试「开始一对一」清理残留。")
    return f"一对一志愿填写开始失败（{code}）"


def handle_admin_start_onetoone(keyword):
    """管理员：开始一对一 格式: 开始一对一 A-0002"""
    activity_id = keyword.strip()
    if not activity_id:
        return "格式：开始一对一 活动ID\n例如：开始一对一 A-0002"
    activity = find_activity_by_id(activity_id)
    if not activity:
        return f"未找到活动：{activity_id}"

    res = lib_onetoone.start_collection(activity_id, client=bitable, activity=activity)
    return _render_start_result(res)


def _render_execute_result(res, activity_id):
    """把 execute 的结构化结果拼成给管理员看的中文。

    文案归渲染层：lib 只给数据。改这里的字必须同步更新 tests/test_onetoone_parity.py
    的「刻意分歧」断言——其余等价性案例会直接红。
    """
    code = res["code"]
    if code == "ALREADY_DONE":
        return (f"活动「{res['activity_name']}」已生成过一对一结果，请勿重复执行。如需重新匹配，请先发送"
                f"「开始一对一 {activity_id}」重新开放，再执行。")
    if code == "BUSY":
        return "当前已有一对一任务在执行中，请稍候再试。"
    if code == "SEARCH_FAILED":
        return "查询历史结果失败，无法确认是否已执行过，已中止（本次未写入任何数据），请稍后重试。"
    if code == "CLOSE_FAILED":
        return "活动状态写入失败（无法切换为「已截止」），已中止匹配，请稍后重试。"
    if code == "SELECTIONS_SEARCH_FAILED":
        if res.get("restored"):
            return "读取一对一志愿失败，已中止匹配，状态已恢复为「收集中」，请稍后重试。"
        return ("读取一对一志愿失败，已中止匹配；且状态恢复为「收集中」失败"
                "（当前可能仍为「已截止」），请让管理员检查。")
    if code == "NO_PARTICIPANTS":
        if res.get("restored", True):
            return "没有已提交志愿的参与者，状态已恢复为「收集中」。"
        return ("没有已提交志愿的参与者；且状态恢复为「收集中」失败"
                "（当前可能仍为「已截止」），请让管理员检查。")
    if code == "ALGORITHM_ERROR":
        if res.get("restored", True):
            return f"一对一匹配出错：{res['error']}，状态已恢复为「收集中」"
        return (f"一对一匹配出错：{res['error']}；且状态恢复为「收集中」失败"
                f"（当前可能仍为「已截止」），请让管理员检查。")
    if code == "PARTIAL_WRITE":
        if res.get("restored", True):
            return (f"一对一结果写入失败（成功 {res['created']}/{res['total']} 条），已回滚，"
                    f"状态恢复为「收集中」，请稍后重试。")
        return (f"一对一结果写入失败（成功 {res['created']}/{res['total']} 条），已回滚；"
                f"但状态恢复为「收集中」失败（当前可能仍为「已截止」），请让管理员检查。")
    if code == "PARTIAL_WRITE_VERIFY_FAILED":
        if res.get("restored", True):
            return (f"结果写入不完整（成功 {res['created']}/{res['total']}）且回滚校验失败，"
                    f"可能残留结果；状态已恢复收集中，请先『开始一对一』清理残留后再执行")
        return (f"结果写入不完整（成功 {res['created']}/{res['total']}）且回滚校验失败，"
                f"可能残留结果；状态恢复「收集中」失败（当前可能仍为「已截止」），请让管理员检查。")
    if code == "PARTIAL_WRITE_INCOMPLETE":
        if res.get("restored", True):
            return (f"一对一结果写入失败（成功 {res['created']}/{res['total']} 条），回滚不完整"
                    f"（残留 {res['residual']} 条结果），状态已恢复为「收集中」，"
                    f"请先发送「开始一对一」清理残留后再执行。")
        return (f"一对一结果写入失败（成功 {res['created']}/{res['total']} 条），回滚不完整"
                f"（残留 {res['residual']} 条结果）；且状态恢复为「收集中」失败"
                f"（当前可能仍为「已截止」），请让管理员检查。")
    if code == "DONE_STATUS_FAILED":
        return "结果已生成，但状态写入失败（当前状态：已截止），请稍后重试或让管理员检查"

    # 走到这里就是 OK
    return (f"🎉 活动「{res['activity_name']}」一对一匹配完成：\n"
            f"参与 {res['participant_count']} 人，共生成 {res['total_pairs']} 条必聊关系。\n"
            f"用户可自查 H5「我的一对一」查看名单。")


def handle_admin_stop_onetoone(keyword):
    """管理员：执行一对一匹配 格式: 执行一对一 A-0002"""
    activity_id = keyword.strip()
    if not activity_id:
        return "格式：执行一对一 活动ID\n例如：执行一对一 A-0002"

    activity = find_activity_by_id(activity_id)
    if not activity:
        return f"未找到活动：{activity_id}"

    res = lib_onetoone.execute(activity_id, client=bitable, activity=activity,
                               find_user_by_openid=find_user_by_openid)
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
    return (f"活动「{res['name']}」一对一状态：\n"
            f"状态：{res['status']}\n"
            f"报名人数：{res['signup_count']}\n"
            f"已提交选择：{res['submitted_count']}人")


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
        lines.append(f"{u['user_id'] or '-'} · {u['gender'] or '-'} · {u['nickname']}")
    return "\n".join(lines)
