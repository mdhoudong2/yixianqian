"""用户指令与管理员指令处理（纯逻辑，发送与查询走 clients/queries/store）。"""
from urllib.parse import quote

from cards import WELCOME_TEXT, generate_h5_url, send_main_menu_card
from clients import *
from constants import *
from queries import find_activity_by_id, find_user_by_id_or_name, find_user_by_openid
from store import (
    generate_observer_codes,
    load_invite_rewarded,
    load_observer_codes,
    reserve_notified,
    unreserve_notified,
)


def handle_register_command(sender_id):
    """发送注册表单链接"""
    if "待替换" in REGISTER_FORM_URL:
        return "注册表单链接尚未配置，请联系管理员。"
    message = (
        f"欢迎加入一线牵！💕\n\n"
        f"请点击下方链接填写注册表单（请在飞书APP内打开）：\n\n"
        f"{REGISTER_FORM_URL}\n\n"
        f"填写说明：\n"
        f"1. 请填写真实资料，照片清晰可见\n"
        f"2. 提交后等待人工审核（通常1-24小时）\n"
        f"3. 审核通过后，我会自动发送H5使用链接给你\n"
        f"4. 已注册过的用户请勿重复填写，直接发送「一线牵」进入\n\n"
        f"有任何问题随时问我~"
    )
    return message




def handle_invite_command(sender_id):
    """邀请好友：生成带邀请人ID的注册链接"""
    user_records = find_user_by_openid(sender_id)
    if not user_records:
        return "你还没有注册，无法邀请好友。\n\n发送「注册」先完成注册吧~"
    user_fields = user_records[0].get("fields", {})
    nickname = get_field_text(user_fields, FIELD_NICKNAME)
    user_id = user_fields.get("用户ID", "")
    if not user_id:
        return "系统未找到你的用户ID，请联系管理员。"
    hearts = get_field_number(user_fields, FIELD_HEART_REMAIN, INITIAL_HEARTS)

    # 生成带邀请人ID预填的注册表单链接
    invite_link = f"{REGISTER_FORM_URL}?prefill_{quote(FIELD_INVITER_ID)}={quote(str(user_id))}"

    # 统计已邀请人数
    rewarded = load_invite_rewarded()
    invite_count = sum(1 for v in rewarded.values() if v == sender_id)

    return (
        f"💕 邀请好友注册，双方都受益！\n\n"
        f"每成功邀请1位好友注册并审核通过，你将获得 1颗爱心（上限{MAX_HEARTS}颗）。\n"
        f"你当前有 {int(hearts)} 颗爱心，已成功邀请 {invite_count} 人。\n\n"
        f"👇 将下面的链接发给好友，TA通过链接注册即可：\n\n"
        f"{invite_link}\n\n"
        f"好友注册审核通过后，爱心会自动到账~"
    )




def handle_h5_command(sender_id):
    """发送卡片1（主菜单卡片）"""
    user_records = find_user_by_openid(sender_id)
    if not user_records:
        return "你还没有注册哦~\n发送「注册」先填写资料，审核通过后即可使用一线牵App。"
    user_fields = user_records[0].get("fields", {})
    status = get_field_text(user_fields, FIELD_ACCOUNT_STATUS)
    if status not in ("单身", STATUS_OBSERVER):
        return f"你的资料当前状态：{status}\n审核通过后即可使用一线牵App，请耐心等待~"
    if send_main_menu_card(sender_id):
        log(f"已发送卡片1(主菜单): {sender_id}")
        return None
    h5_url = generate_h5_url(sender_id)
    return f"点击进入一线牵App：\n{h5_url}"




def handle_status_command(sender_id):
    user_records = find_user_by_openid(sender_id)
    if not user_records:
        return "你还未注册。\n\n发送「注册」获取注册表单链接。"
    user_fields = user_records[0].get("fields", {})
    nickname = get_field_text(user_fields, FIELD_NICKNAME)
    status = get_field_text(user_fields, FIELD_ACCOUNT_STATUS)
    hearts = get_field_number(user_fields, FIELD_HEART_REMAIN, INITIAL_HEARTS)

    lines = [
        "你的账号状态：\n",
        f"昵称：{nickname}",
        f"状态：{status}",
        f"爱心剩余：{int(hearts)}",
        ""
    ]
    if status == "待审核":
        lines.append("资料正在审核中，请耐心等待，通过后会通知你。")
    elif status == "单身":
        lines.append("账号已激活，发送「一线牵」即可进入App查看异性资料。")
    elif status == "已退出":
        lines.append("你已暂时退出相亲市场，如需恢复请联系管理员。")
    elif status == "审核不通过":
        lines.append("很抱歉，你的资料未通过审核，如有疑问请联系管理员。")
    elif status == "已脱单":
        lines.append("账号已脱单（不出现在他人牵线中），可随时在App「我的」页恢复单身。")
    elif status == STATUS_OBSERVER:
        lines.append("你是村情六处账号：可浏览男生/女生资料、留言、反馈、查看活动。")
    else:
        lines.append("如有疑问请联系管理员。")
    return "\n".join(lines)




def handle_help_command(sender_id):
    """发送帮助说明（文本）"""
    return WELCOME_TEXT


def handle_observer_command(sender_id):
    """观察员注册：返回独立表单链接 + 提示填管理员发放的邀请码（仅非单身看热闹用）"""
    if not OBSERVER_FORM_URL:
        return "村情六处注册尚未开放，如有需要请联系管理员。"
    return (
        "村情六处（非单身看热闹）注册说明：\n\n"
        f"请在飞书APP内打开下方链接填写村情六处注册表单：\n\n"
        f"{OBSERVER_FORM_URL}\n\n"
        f"填写时需填入管理员发放的「邀请码」（每个邀请码仅可用一次）。\n"
        f"提交后可浏览男生/女生资料、留言、反馈、查看活动（不含点喜欢、报名等交友功能）。"
    )







def handle_welcome(sender_id, is_first_time=False):
    if is_first_time:
        return handle_register_command(sender_id)
    return WELCOME_TEXT




def handle_admin_pending():
    """查看待审核用户列表"""
    items = search_records(USER_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_ACCOUNT_STATUS, "operator": "is", "value": ["待审核"]}]
    })
    if not items:
        return "当前没有待审核的用户。"

    lines = [f"待审核用户（共{len(items)}人）：\n"]
    for i, item in enumerate(items, 1):
        fields = item.get("fields", {})
        uid = fields.get("用户ID", "")
        nickname = get_field_text(fields, FIELD_NICKNAME)
        gender = get_field_text(fields, FIELD_GENDER)
        education = get_field_text(fields, FIELD_EDUCATION)
        name_val = fields.get("姓名", "")
        name = name_val[0].get("text", "") if isinstance(name_val, list) and name_val else str(name_val)
        phone = fields.get("手机号", "")
        feishu_id = get_field_text(fields, FIELD_FEISHU_ID)
        lines.append(f"{i}. {uid} {nickname}（{name}）")
        lines.append(f"   {gender} {education}")
        if phone:
            lines.append(f"   手机：{phone}")
        lines.append(f"   open_id：{'已绑定' if feishu_id else '未绑定'}")
        lines.append("")

    lines.append("回复「通过 用户ID」或「通过 昵称」审核通过")
    lines.append("回复「拒绝 用户ID」或「拒绝 昵称」审核不通过")
    return "\n".join(lines)




def handle_admin_approve(keyword):
    """管理员审核通过用户"""
    records = find_user_by_id_or_name(keyword)
    if not records:
        return f"未找到用户：{keyword}"
    if len(records) > 1:
        return "找到多个匹配用户，请使用用户ID操作，如：通过 U-0003"

    record = records[0]
    record_id = record.get("record_id")
    fields = record.get("fields", {})
    nickname = get_field_text(fields, FIELD_NICKNAME)
    uid = fields.get("用户ID", "")
    current_status = get_field_text(fields, FIELD_ACCOUNT_STATUS)
    open_id = get_field_text(fields, FIELD_FEISHU_ID)

    if current_status == "单身":
        return f"{uid} {nickname} 已经是单身状态，无需重复操作。"

    if not open_id:
        return f"{uid} {nickname} 尚未绑定飞书账号（open_id为空），无法发送通知。请等待自动绑定后再审核。"

    # 同号重复档案守卫：该飞书账号名下已有其他单身档案时拒绝激活，
    # 防止同一人多个档案并存导致头像/资料/爱心归属错乱
    others = [r for r in search_records(USER_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_FEISHU_ID, "operator": "is", "value": [open_id]}]
    }) if r.get("record_id") != record_id
        and get_field_text(r.get("fields", {}), FIELD_ACCOUNT_STATUS) == "单身"]
    if others:
        other = others[0]
        other_uid = get_field_text(other.get("fields", {}), FIELD_NICKNAME)
        other_id = get_field_text(other.get("fields", {}), "用户ID") or "无ID"
        return (f"⚠️ 激活被拦截：该飞书账号已绑定单身档案 {other_id} {other_uid}，"
                f"疑似重复资料。\n如确需替换，请先将旧档案「拒绝」后再操作本条。")

    # 更新状态为单身
    if update_record(USER_TABLE_ID, record_id, {FIELD_ACCOUNT_STATUS: "单身"}):
        log(f"管理员审核通过: {uid} {nickname}")
        # 立即发送审核通过通知（原子预约去重，避免与 30s 轮询线程重复发送）
        if reserve_notified("approval_sent", record_id):
            gender = get_field_text(fields, FIELD_GENDER)
            if gender == "男性":
                view_desc = "单身女生"
            elif gender == "女性":
                view_desc = "单身男生"
            else:
                view_desc = "单身异性"

            h5_url = generate_h5_url(open_id)
            user_msg = (
                f"恭喜你，资料审核已通过！🎉\n\n"
                f"点击下方链接进入一线牵App，浏览「{view_desc}」并点喜欢：\n\n"
                f"{h5_url}\n\n"
                f"【点喜欢说明】\n"
                f"在对方卡片上点击「♥」按钮，填写一句附言，对方会匿名收到「有人喜欢你」的通知；你们相互喜欢后，附言才会发给对方。\n"
                f"如果对方也喜欢你，系统会通知你们相互喜欢，并开通聊天通道。\n\n"
                f"祝你早日找到另一半！💕"
            )
            if not send_text_message(open_id, user_msg):
                unreserve_notified("approval_sent", record_id)

        return f"已审核通过：{uid} {nickname}\n已发送审核通过通知和App链接给TA。"
    else:
        return "审核操作失败，请稍后重试。"




def handle_admin_reject(keyword):
    """管理员拒绝用户（写入「审核不通过」，与用户自隐「已脱单」区分，被拒者不可自助恢复）"""
    records = find_user_by_id_or_name(keyword)
    if not records:
        return f"未找到用户：{keyword}"
    if len(records) > 1:
        return "找到多个匹配用户，请使用用户ID操作，如：拒绝 U-0003"

    record = records[0]
    record_id = record.get("record_id")
    fields = record.get("fields", {})
    nickname = get_field_text(fields, FIELD_NICKNAME)
    uid = fields.get("用户ID", "")
    current_status = get_field_text(fields, FIELD_ACCOUNT_STATUS)

    if current_status == "审核不通过":
        return f"{uid} {nickname} 已经是审核不通过状态。"

    if update_record(USER_TABLE_ID, record_id, {FIELD_ACCOUNT_STATUS: "审核不通过"}):
        log(f"管理员拒绝用户: {uid} {nickname}")
        # 通知用户
        open_id = get_field_text(fields, FIELD_FEISHU_ID)
        if open_id:
            send_text_message(open_id, "很抱歉，你的资料未通过审核，如有疑问请联系管理员。")
        return (f"已拒绝用户：{uid} {nickname}\n"
                f"该用户将不能进入App浏览和使用（区别于自行脱单）。")
    else:
        return "操作失败，请稍后重试。"




def handle_admin_notify(text):
    """管理员通知用户：通知 用户ID 消息内容"""
    parts = text.split(None, 2)
    if len(parts) < 3:
        return "格式：通知 用户ID 消息内容\n例如：通知 U-0003 活动本周六举行，请准时参加"
    target_id = parts[1].strip()
    message = parts[2].strip()
    if not message:
        return "消息内容不能为空"

    # 通过用户ID查找
    users = find_user_by_id_or_name(target_id)
    if not users:
        return f"未找到用户「{target_id}」"

    target_fields = users[0].get("fields", {})
    target_open_id = get_field_text(target_fields, FIELD_FEISHU_ID)
    target_nickname = get_field_text(target_fields, FIELD_NICKNAME)
    target_uid = target_fields.get("用户ID", target_id)
    if not target_open_id:
        return f"用户「{target_nickname}」尚未绑定飞书，无法发送消息"

    if send_text_message(target_open_id, message):
        log(f"管理员通知已发送: {target_uid} {target_nickname} ({target_open_id})")
        return f"已发送给「{target_nickname}」（{target_uid}）：\n{message}"
    else:
        return "发送失败，用户可能未与机器人对话过"




def handle_admin_toggle_group_flag(keyword):
    """管理员：开启/关闭活动的分组功能（控制 H5 我的页「我的分组」入口显示）
    格式: 开启分组功能 A-xxxx 开/关  或  开启分组功能 A-xxxx on/off"""
    parts = keyword.split()
    if len(parts) < 2:
        return "格式：开启分组功能 活动ID 开/关\n例如：开启分组功能 A-0002 开"
    activity_id, state = parts[0], parts[1].lower()
    if state in ("开", "on", "1", "是", "true"):
        flag = "是"
    elif state in ("关", "off", "0", "否", "false"):
        flag = "否"
    else:
        return "第二参数需为 开/关（on/off）"

    activity = find_activity_by_id(activity_id)
    if not activity:
        return f"未找到活动：{activity_id}"

    af = activity.get("fields", {})
    activity_name = get_field_text(af, FIELD_ACTIVITY_NAME)
    record_id = activity.get("record_id")
    update_record(ACTIVITY_TABLE_ID, record_id, {FIELD_ACT_GROUP_FLAG: flag})
    log(f"管理员设置分组功能开关: {activity_name}({activity_id}) -> {flag}")
    return (f"已{'开启' if flag == '是' else '关闭'}活动「{activity_name}」的分组功能。\n"
            f"开启后，报名该活动的用户可在 H5「我的」页看到「我的分组」入口。"
            + ("\n（活动结束后记得关闭，入口即隐藏）" if flag == "是" else ""))




def handle_group_help():
    """管理员：分组指令使用说明"""
    return (
        "分组相关指令：\n\n"
        "【开始填志愿 活动ID 男数 女数】\n  开启志愿收集，如：开始填志愿 A-0002 3 3\n"
        "【开启分组功能 活动ID 开/关】\n  控制 H5 我的页是否显示「我的分组」入口\n"
        "【查看未提交 活动ID】\n  查看报名但未提交志愿的人员\n"
        "【执行分组 活动ID [轮次]】\n  执行本轮分组并保存结果（默认第1轮）\n"
        "【分组状态 活动ID】\n  查看分组进度\n"
        "【分组帮助】\n  查看本说明"
    )




def handle_admin_generate_observer_codes(keyword):
    """管理员：批量生成观察员邀请码。格式：生成观察员邀请码 N"""
    parts = keyword.strip().split()
    if len(parts) != 1 or not parts[0].isdigit():
        return "格式：生成村情六处邀请码 数量\n例如：生成村情六处邀请码 10"
    n = int(parts[0])
    if n <= 0 or n > 200:
        return "数量需在 1~200 之间"
    codes = generate_observer_codes(n)
    return ("已生成 %d 个村情六处邀请码（每个仅可用一次）：\n\n%s\n\n"
            "请逐个发给村情六处，注册时填写。") % (len(codes), "\n".join(codes))


def handle_admin_list_observer_codes():
    """管理员：查看观察员邀请码及使用状态"""
    codes = load_observer_codes()
    if not codes:
        return "尚未生成村情六处邀请码。发送「生成村情六处邀请码 N」批量生成。"
    unused = [(c, v) for c, v in codes.items() if not v.get("used")]
    used = [(c, v) for c, v in codes.items() if v.get("used")]
    lines = [f"村情六处邀请码（未用 {len(unused)} / 已用 {len(used)}）：\n"]
    lines.append("【未使用】")
    lines.extend(sorted(c for c, _ in unused))
    if used:
        lines.append("\n【已使用】")
        for c, v in sorted(used):
            lines.append(f"{c} → {v.get('used_by', '')} ({v.get('used_at', '')})")
    return "\n".join(lines)


def handle_admin_help():
    return (
        "管理员指令：\n\n"
        "【审核管理】\n"
        "【待审核】查看待审核用户\n"
        "【通过 U-xxx或姓名】审核通过\n"
        "【拒绝 U-xxx或姓名】审核不通过\n"
        "【通知 U-xxx 内容】给用户发消息\n"
        "【用户统计】查看统计数据\n\n"
        "【村情六处】\n"
        "【生成村情六处邀请码 N】批量生成村情六处邀请码\n"
        "【查看村情六处邀请码】查看邀请码使用状态\n\n"
        "【分组活动】\n"
        "【开始填志愿 活动ID 男数 女数】开始填志愿\n"
        "【开启分组功能 活动ID 开/关】控制 H5 我的页分组入口\n"
        "【查看未提交 活动ID】查看未提交志愿的报名者\n"
        "【执行分组 活动ID [轮次]】执行分组算法（默认第1轮）\n"
        "【分组状态 活动ID】查看分组进度\n"
        "【分组帮助】分组指令说明\n\n"
        "【系统】\n"
        "【重连】手动重连机器人\n"
        "【管理员帮助】查看本帮助"
    )




def handle_admin_stats():
    """用户统计"""
    all_users = search_records(USER_TABLE_ID)
    total = len(all_users)
    pending = active = hidden = rejected = exited = observer = unbound = 0
    for item in all_users:
        fields = item.get("fields", {})
        status = get_field_text(fields, FIELD_ACCOUNT_STATUS)
        if status == "待审核":
            pending += 1
        elif status == "单身":
            active += 1
        elif status == "已脱单":
            hidden += 1
        elif status == "审核不通过":
            rejected += 1
        elif status == "已退出":
            exited += 1
        elif status == STATUS_OBSERVER:
            observer += 1
        if not get_field_text(fields, FIELD_FEISHU_ID):
            unbound += 1

    return (
        f"用户统计：\n\n"
        f"总注册：{total}人\n"
        f"待审核：{pending}人\n"
        f"单身：{active}人\n"
        f"已脱单：{hidden}人\n"
        f"审核不通过：{rejected}人\n"
        f"已退出：{exited}人\n"
        f"村情六处：{observer}人\n"
        f"未绑定open_id：{unbound}人"
    )


