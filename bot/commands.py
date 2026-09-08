"""用户指令与管理员指令处理（纯逻辑，发送与查询走 clients/queries/store）。"""
import datetime
import re
import time

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
    """邀请好友：分两条发送——说明 + 一条可整条复制转发的话术（邀请码内联）。"""
    user_records = find_user_by_openid(sender_id)
    if not user_records:
        return "你还没有注册，无法邀请好友。\n\n发送「注册」先完成注册吧~"
    user_fields = user_records[0].get("fields", {})
    user_id = user_fields.get("用户ID", "")
    if not user_id:
        return "系统未找到你的用户ID，请联系管理员。"
    hearts = get_field_number(user_fields, FIELD_HEART_REMAIN, INITIAL_HEARTS)

    # 统计已邀请人数
    rewarded = load_invite_rewarded()
    invite_count = sum(1 for v in rewarded.values() if v == sender_id)

    # 第一条：规则说明 + 操作提示
    tip = (
        f"💕 邀请好友注册，双方都受益！\n\n"
        f"每成功邀请1位好友注册并审核通过，你将获得 1颗爱心（上限{MAX_HEARTS}颗）。\n"
        f"你当前有 {int(hearts)} 颗爱心，已成功邀请 {invite_count} 人。\n\n"
        f"👇 长按下面这条消息 → 复制，直接发给好友即可："
    )
    # 第二条：整条就是可转发话术，自包含、无需选取
    forward = f"我在一线牵等你～注册时，在“邀请人ID”里填 {user_id} 就可以啦"

    send_text_message(sender_id, tip)
    send_text_message(sender_id, forward)
    return None




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
    if len(users) > 1:
        return "找到多个匹配用户，请使用用户ID操作，如：通知 U-0003 内容"

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


# ========== 活动群发（活动通知 活动ID → 预览 → 确认发送）==========
# 活动表字段名（与 web/backend/config.py 的 F_ACTIVITY_* 保持一致）
_A_NAME = "活动名称"
_A_DESC = "活动描述"
_A_LOCATION = "活动地点"
_A_CONDITION = "参与条件"
_A_FEE = "费用"
_A_FOOD = "食宿"
_A_MAX = "报名人数上限"
_A_CUR = "当前报名人数"
_A_START = "开始时间"
_A_END = "结束时间"
_A_POSTER = "活动海报"

_BROADCAST_PENDING = {}   # activity_id -> 待发送快照（收件人+卡片+时间戳）
_BROADCAST_TTL = 900      # 预览有效期 15 分钟
_POSTER_IMG_CACHE = {}    # 海报 file_token -> 飞书 img_key，避免重复上传


def _norm_activity_id(text):
    """从一段文本里提取活动ID并归一化为 A-0005 形态；提取不到返回 ''。"""
    m = re.search(r"[Aa]-?\s*(\d+)", str(text)) or re.search(r"(\d+)", str(text))
    return f"A-{int(m.group(1)):04d}" if m else ""


def _field_epoch_ms(fields, key):
    """兼容多种日期返回形态，统一转成毫秒时间戳。"""
    v = fields.get(key)
    if isinstance(v, list) and v:
        v = v[0]
    if isinstance(v, dict):
        v = v.get("timestamp", v.get("value"))
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n * 1000 if n < 10 ** 11 else n


def _fmt_activity_time(ms):
    if not ms:
        return ""
    try:
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _collect_broadcast_recipients():
    """收集活动通知收件人：账号状态=单身的在档用户 + 村情六处观察员。
    按 open_id 去重（同一人既是单身又是观察员只发一次），跳过未绑定飞书/测试假号。
    返回 [(open_id, 昵称, 角色)]，保持用户表顺序。"""
    chosen = {}
    order = []

    def _add(rec, role):
        fields = rec.get("fields", {})
        oid = get_field_text(fields, FIELD_FEISHU_ID)
        if not oid or is_test_fake_openid(oid) or oid in chosen:
            return
        chosen[oid] = (get_field_text(fields, FIELD_NICKNAME) or "用户", role)
        order.append(oid)

    for rec in search_records(USER_TABLE_ID):
        if get_field_text(rec.get("fields", {}), FIELD_ACCOUNT_STATUS) == "单身":
            _add(rec, "单身")
    if OBSERVER_TABLE_ID:
        for rec in search_records(OBSERVER_TABLE_ID):
            _add(rec, "观察员")
    return [(oid, chosen[oid][0], chosen[oid][1]) for oid in order]


def _poster_img_key(fields):
    """把多维表格里的活动海报附件换成飞书卡片可用的 img_key（带缓存）。无海报/失败返回 ''。"""
    tokens = get_attachment_tokens(fields, _A_POSTER)
    if not tokens:
        return ""
    token = tokens[0]
    if token in _POSTER_IMG_CACHE:
        return _POSTER_IMG_CACHE[token]
    img_key = ""
    try:
        data = feishu.download_media(token)
        if data:
            img_key = feishu.upload_message_image(data, "activity_poster.jpg") or ""
    except Exception as e:
        log(f"活动海报转img_key失败: {e}")
    _POSTER_IMG_CACHE[token] = img_key
    return img_key


def _num_text(fields, key):
    """数字字段转简洁字符串（整数不带小数点），空返回 ''。"""
    n = get_field_number(fields, key)
    if n is None:
        return ""
    try:
        return str(int(n)) if float(n) == int(n) else str(n)
    except (TypeError, ValueError):
        return ""


def build_activity_notify_card(activity, img_key):
    """构建发给用户的活动通知卡片。"""
    f = activity.get("fields", {})
    name = get_field_text(f, _A_NAME) or "新活动"
    lines = []
    t1 = _fmt_activity_time(_field_epoch_ms(f, _A_START))
    t2 = _fmt_activity_time(_field_epoch_ms(f, _A_END))
    if t1:
        lines.append(f"**🕐 时间：**{t1}" + (f" ～ {t2}" if t2 and t2 != t1 else ""))
    loc = get_field_text(f, _A_LOCATION)
    if loc:
        lines.append(f"**📍 地点：**{loc}")
    cond = get_field_text(f, _A_CONDITION)
    if cond:
        lines.append(f"**🙋 参与条件：**{cond}")
    fee = _num_text(f, _A_FEE)
    if fee and fee != "0":
        lines.append(f"**💰 费用：**{fee}")
    food = get_field_text(f, _A_FOOD)
    if food:
        lines.append(f"**🍚 食宿：**{food}")
    cur, maxv = _num_text(f, _A_CUR), _num_text(f, _A_MAX)
    if cur or maxv:
        lines.append(f"**👥 报名情况：**{cur or '0'}/{maxv or '不限'}")

    elements = []
    if img_key:
        elements.append({"tag": "img", "img_key": img_key,
                         "alt": {"tag": "plain_text", "content": "活动海报"}})
    if lines:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    desc = get_field_text(f, _A_DESC)
    if desc:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": desc}})
    elements.append({"tag": "hr"})
    elements.append({
        "tag": "action",
        "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "查看活动 / 立即报名"},
            "type": "primary",
            "url": H5_BASE_URL + "/",
        }],
    })
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "🎉 新活动发布｜" + name}, "template": "red"},
        "elements": elements,
    }


def handle_admin_activity_preview(text, admin_id=None):
    """第一步：生成群发预览（同时把实际卡片发给管理员本人确认），暂存待发送快照。"""
    raw = text[len("活动通知"):].strip()
    aid = _norm_activity_id(raw)
    if not aid:
        return "格式：活动通知 活动ID\n例如：活动通知 A-0005"
    activity = find_activity_by_id(aid)
    if not activity:
        return f"未找到活动「{aid}」，请在活动表核对活动ID后重试。"
    recipients = _collect_broadcast_recipients()
    if not recipients:
        return "没有可发送的用户：单身在档用户与村情六处观察员里都没有已绑定飞书的记录。"

    img_key = _poster_img_key(activity.get("fields", {}))
    card = build_activity_notify_card(activity, img_key)
    name = get_field_text(activity.get("fields", {}), _A_NAME) or "新活动"
    n_single = sum(1 for _, _, role in recipients if role == "单身")
    n_obs = len(recipients) - n_single

    _BROADCAST_PENDING[aid] = {
        "ts": time.time(), "activity_id": aid, "name": name,
        "recipients": recipients, "card": card,
    }
    # 给管理员本人发一份真实卡片，所见即所得
    if admin_id:
        send_card_message(admin_id, card)
    return (
        f"已生成「{name}」群发预览，上方卡片就是用户收到的样式。\n"
        f"收件人：单身 {n_single} 人 ＋ 观察员 {n_obs} 人，合计 {len(recipients)} 人。\n\n"
        f"确认无误请回复：确认发送 {aid}\n"
        f"放弃请回复：取消活动通知 {aid}\n"
        f"（预览 15 分钟内有效；待审核/审核不通过/已脱单/已退出不会收到）"
    )


def consume_activity_pending(text):
    """第二步（同步调用）：取出并移除待发送快照，防止重复群发。返回 (aid, snapshot)。"""
    aid = _norm_activity_id(text)
    if not aid:
        return "", None
    snap = _BROADCAST_PENDING.pop(aid, None)
    # 顺手清理过期快照
    now = time.time()
    for k in [k for k, v in _BROADCAST_PENDING.items() if now - v.get("ts", 0) > _BROADCAST_TTL]:
        _BROADCAST_PENDING.pop(k, None)
    return aid, snap


def execute_activity_broadcast(snapshot):
    """后台线程：逐个发送活动卡片，限速，最后汇总成功/失败。"""
    recipients = snapshot["recipients"]
    card = snapshot["card"]
    name = snapshot.get("name", "新活动")
    ok, fails = 0, []
    for oid, nick, role in recipients:
        if send_card_message(oid, card):
            ok += 1
        else:
            fails.append(f"{nick}（{role}）")
        time.sleep(0.12)  # 温和限速，规避飞书发送频控
    report = f"「{name}」活动群发完成：成功 {ok}/{len(recipients)} 人。"
    if fails:
        shown = "、".join(fails[:20])
        more = f" 等 {len(fails)} 人" if len(fails) > 20 else ""
        report += (f"\n失败 {len(fails)} 人：{shown}{more}\n"
                   "（失败多因对方从未与机器人对话或已停用，可用「通知 用户ID 内容」单发补送）")
    log(f"活动群发完成 {name}: 成功{ok} 失败{len(fails)}")
    return report


def handle_admin_activity_cancel(text):
    aid = _norm_activity_id(text[len("取消活动通知"):])
    snap = _BROADCAST_PENDING.pop(aid, None) if aid else None
    if snap:
        return f"已取消「{snap.get('name', aid)}」的待发送预览，不会向任何人发送。"
    return "没有找到该活动待发送的预览（可能已确认发送、已取消或已超过15分钟有效期）。"




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
        "【活动通知 A-xxxx】预览新活动群发（单身+观察员）\n"
        "【确认发送 A-xxxx】按预览向全员群发活动卡片\n"
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
    observers = search_records(OBSERVER_TABLE_ID) if OBSERVER_TABLE_ID else []
    total = len(all_users) + len(observers)
    pending = active = hidden = rejected = exited = unbound = 0
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
        f"村情六处：{len(observers)}人\n"
        f"未绑定open_id：{unbound}人"
    )


