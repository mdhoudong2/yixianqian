"""麦穗的管理员指令：加穗/扣穗、查穗、签到、心愿、推荐单、配置。

和 commands.py 分开，是因为这一整块只服务麦穗：规则在 lib/points*.py，单据在
多维表格，指令层只做「解析参数 → 调 lib → 把结果写成人话」。混进 commands.py
会让那边更难找东西，而这边单文件能看完全貌。

一条贯穿全篇的约定：**能改到穗的指令都要说清楚改了多少、改完剩多少**。管理员
手一滑打错一位数，回复里看不出来的话，账就悄悄错了。
"""
from clients import get_field_text, log, search_records, update_record
from constants import (
    ATTENDANCE_OPTIONS,
    FIELD_ACTIVITY_NAME,
    FIELD_MM_CONDITION,
    FIELD_MM_HANDLED_AT,
    FIELD_MM_OPERATOR,
    FIELD_MM_REDEMPTION_ID,
    FIELD_MM_STATUS,
    FIELD_MM_USER_NAME,
    FIELD_NICKNAME,
    FIELD_SIGNUP_ACTIVITY_ID,
    FIELD_SIGNUP_ATTENDANCE,
    FIELD_SIGNUP_OPENID,
    FIELD_SIGNUP_STATUS,
    FIELD_WISH_ACTIVITY_ID,
    FIELD_WISH_HANDLED_AT,
    FIELD_WISH_OPERATOR,
    FIELD_WISH_REDEMPTION_ID,
    FIELD_WISH_STATUS,
    FIELD_WISH_TARGET_NAME,
    FIELD_WISH_USER_NAME,
    MATCHMAKER_ORDER_TABLE_ID,
    SIGNUP_TABLE_ID,
    WISH_STATUS_ARRANGED,
    WISH_STATUS_FAILED,
    WISH_STATUS_PENDING,
    WISH_TABLE_ID,
)
from queries import find_activity_by_id, find_user_by_id_or_name

from lib import points, points_award, points_config, points_redeem

# 心愿/推荐单在表格里的状态 → lib 的兑换单状态。
# 「已安排」不退穗，另外两个都退——这是需求里唯一一处「什么算办成了」的定义。
_WISH_TO_REDEMPTION = {
    WISH_STATUS_ARRANGED: points_redeem.ST_ARRANGED,
    WISH_STATUS_FAILED: points_redeem.ST_REFUNDED,
}
_MM_TO_REDEMPTION = {
    "已推荐": points_redeem.ST_RECOMMENDED,
    "已完成": points_redeem.ST_DONE,
    "无法推荐": points_redeem.ST_REFUNDED,
}
MM_STATUSES = ("已推荐", "已完成", "无法推荐")


def _now():
    from lib import points_db
    return points_db.now_str()


def _one_user(keyword):
    """按 U-xxxx 或昵称找人，返回 (主档记录, 错误信息)。多人同名时拒绝，
    不能让管理员在「张三」「张三」之间凭运气选一个扣穗。"""
    items = find_user_by_id_or_name(keyword) or []
    if not items:
        return None, f"没找到用户：{keyword}"
    if len(items) > 1:
        names = "、".join(get_field_text(i.get("fields", {}), "用户ID") for i in items[:5])
        return None, f"「{keyword}」匹配到 {len(items)} 个人（{names}），请用用户ID指定"
    return items[0], ""


def _open_id_of(record):
    from constants import FIELD_FEISHU_ID
    return get_field_text(record.get("fields", {}), FIELD_FEISHU_ID)


def _nick_of(record):
    return get_field_text(record.get("fields", {}), FIELD_NICKNAME)


def _balance_line(open_id):
    s = points.summary(open_id)
    return f"当前余额：{s['balance']} 穗（累计获得 {s['earned']}，累计使用 {s['spent']}）"


# ---------------------------------------------------------------- 加穗 / 扣穗

def handle_admin_grant(text, operator_oid):
    """加穗 / 扣穗 <用户ID|昵称> <数量> [活动ID] <原因>

    需求原文：协助组织活动 10–50 穗；同一人同一活动最多 50 穗。带活动ID 就是
    「协助组织」，走 lib.points_award.grant_assist 的上下限；不带就是普通手动
    加减，只要求有原因。两条路径都不允许空原因——没有原因的手动加分，事后
    谁也说不清那笔钱是怎么回事。
    """
    verb = "加穗" if text.startswith("加穗") else "扣穗"
    parts = text[len(verb):].strip().split()
    if len(parts) < 3:
        return (f"格式：{verb} 用户ID 数量 原因\n"
                f"　　　{verb} 用户ID 数量 活动ID 原因（协助组织，单次 "
                f"{points_config.get('assist_min')}–{points_config.get('assist_max')} 穗）")

    keyword, amount_text = parts[0], parts[1]
    rest = parts[2:]
    try:
        amount = int(amount_text)
    except ValueError:
        return f"数量要写数字，比如 {verb} U-0003 20 帮忙布置场地"

    record, err = _one_user(keyword)
    if err:
        return err
    open_id = _open_id_of(record)
    if not open_id:
        return f"「{keyword}」还没绑定飞书账号，先让他给机器人发条消息"
    nick = _nick_of(record)

    # 第三段是不是活动ID：能查到活动就当活动。查不到也不算错——原因开头正好
    # 长得像 A-0001 是可能的，宁可把它当原因，也不能凭空拒绝一条合法的手动加减。
    activity = find_activity_by_id(rest[0]) if rest else None
    if activity:
        activity_id = get_field_text(activity.get("fields", {}), "活动ID")
        reason = " ".join(rest[1:]).strip() or "协助组织活动"
    else:
        activity_id = ""
        reason = " ".join(rest).strip()
    if not reason:
        return "原因不能为空——请写一句这笔穗是因为什么。"

    signed = amount if verb == "加穗" else -amount
    if signed == 0:
        return "数量不能是 0。"
    try:
        if activity_id:
            if signed <= 0:
                return "扣穗不关联活动，请去掉活动ID。"
            points_award.grant_assist(open_id, activity_id, signed, reason,
                                      operator_oid=operator_oid)
            used = points_award.activity_assist_total(open_id, activity_id)
            detail = (f"（活动 {activity_id}，该活动累计 {used}/"
                      f"{points_config.get('assist_activity_cap')} 穗）")
        else:
            points_award.adjust(open_id, signed, reason, operator_oid=operator_oid)
            detail = ""
    except points.DuplicateEntry as e:
        return f"这笔已经记过账了：{e}"
    except points_award.AwardError as e:
        return f"没加成：{e}"
    except points.PointsError as e:
        return f"没加成：{e}"

    log(f"管理员{verb} {nick}({open_id}) {signed} 穗：{reason} by {operator_oid}")
    return (f"已给「{nick}」{verb} {abs(signed)} 穗{detail}\n"
            f"原因：{reason}\n{_balance_line(open_id)}")


# ---------------------------------------------------------------- 查穗

def handle_admin_query_points(text):
    """查穗 <用户ID|昵称>"""
    keyword = text[len("查穗"):].strip()
    if not keyword:
        return "格式：查穗 U-0003 或 查穗 张三"
    record, err = _one_user(keyword)
    if err:
        return err
    open_id = _open_id_of(record)
    if not open_id:
        return f"「{keyword}」还没绑定飞书账号，查不到账"

    s = points.summary(open_id)
    lines = [f"🌾 {_nick_of(record)}", _balance_line(open_id)]

    prog = points_invite_progress(open_id)
    if prog["total"]:
        lines.append(
            f"邀请 {prog['total']} 人：已到账 {prog['finalized']}、"
            f"待确认 {prog['confirmed']}、待注册 {prog['pending']}、"
            f"已收回 {prog['clawed_back']}（待确认的 {prog['pending_points']} 穗不算余额）")

    orders = points_redeem.list_for(open_id, limit=5)
    if orders:
        lines.append("最近兑换：")
        for o in orders:
            lines.append(f"　#{o['id']} {o['item_label']} -{o['cost']}穗 {o['status_label']} {o['created_at']}")

    entries = points.entries(open_id, limit=10)
    if entries:
        lines.append("最近流水：")
        for e in entries:
            sign = "+" if e["delta"] > 0 else ""
            lines.append(f"　{sign}{e['delta']}穗 {e['kind_label']} {e['reason']} {e['created_at']}")
    return "\n".join(lines)


def points_invite_progress(open_id):
    from lib import points_invite
    try:
        return points_invite.progress(open_id)
    except Exception:
        return {"total": 0, "pending": 0, "confirmed": 0, "finalized": 0,
                "clawed_back": 0, "rejected": 0, "pending_points": 0,
                "finalized_points": 0}


# ---------------------------------------------------------------- 签到

def handle_admin_attendance(text):
    """签到 <活动ID> <用户ID|昵称> 按时|迟到|未到"""
    parts = text[len("签到"):].strip().split()
    if len(parts) < 3:
        return "格式：签到 A-0001 U-0003 按时\n（考勤只影响退穗判定，不发穗；发穗用「加穗」）"
    activity_id, keyword, state = parts[0], parts[1], parts[2]
    if state not in ATTENDANCE_OPTIONS:
        return f"考勤只能是：{'/'.join(ATTENDANCE_OPTIONS)}"

    activity = find_activity_by_id(activity_id)
    if not activity:
        return f"未找到活动：{activity_id}"
    record, err = _one_user(keyword)
    if err:
        return err
    open_id = _open_id_of(record)
    if not open_id:
        return f"「{keyword}」还没绑定飞书账号"
    nick = _nick_of(record)

    signups = search_records(SIGNUP_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": FIELD_SIGNUP_ACTIVITY_ID, "operator": "is",
             "value": [activity_id]},
            {"field_name": FIELD_SIGNUP_OPENID, "operator": "is", "value": [open_id]},
        ]})
    if not signups:
        return f"「{nick}」没有报名 {activity_id}，先确认活动ID和人。"
    if len(signups) > 1:
        return f"「{nick}」在 {activity_id} 有 {len(signups)} 条报名记录，请先在表格里核对。"

    rec = signups[0]
    signup_status = get_field_text(rec.get("fields", {}), FIELD_SIGNUP_STATUS)
    if not update_record(SIGNUP_TABLE_ID, rec.get("record_id"),
                         {FIELD_SIGNUP_ATTENDANCE: state}):
        return "写入失败，请稍后重试。"

    name = get_field_text(activity.get("fields", {}), FIELD_ACTIVITY_NAME)
    log(f"管理员签到: {nick} {activity_id} -> {state} by 管理员")
    warning = ""
    if state == "未到" and signup_status == "已报名":
        warning = "\n⚠️ 该用户未到场，其优先报名名额不退穗（需求：未到场不退）。"
    return f"已记录「{nick}」在「{name}」的考勤：{state}{warning}"


# ---------------------------------------------------------------- 心愿

def handle_admin_wish_list(text):
    """心愿列表 [活动ID]"""
    if not WISH_TABLE_ID:
        return "心愿单表还没配置（WISH_TABLE_ID 为空）。先跑 scripts/dev/create_points_tables.py。"
    activity_id = text[len("心愿列表"):].strip()
    conds = []
    if activity_id:
        conds.append({"field_name": FIELD_WISH_ACTIVITY_ID, "operator": "is",
                      "value": [activity_id]})
    items = search_records(WISH_TABLE_ID, {"conjunction": "and", "conditions": conds} if conds else None)
    if not items:
        return f"{activity_id or '全部活动'}：没有心愿单。"

    pending = [i for i in items
               if get_field_text(i.get("fields", {}), FIELD_WISH_STATUS) == WISH_STATUS_PENDING]
    lines = [f"心愿单 {len(items)} 张，待安排 {len(pending)} 张："]
    for i in items:
        f = i.get("fields", {})
        lines.append(
            f"　#{i.get('record_id', '')[-6:]} {get_field_text(f, FIELD_WISH_ACTIVITY_ID)} "
            f"{get_field_text(f, FIELD_WISH_USER_NAME)} → "
            f"{get_field_text(f, FIELD_WISH_TARGET_NAME) or '（未指定）'} "
            f"[{get_field_text(f, FIELD_WISH_STATUS)}]")
    lines.append("\n处理：心愿处理 <记录号后6位> 已安排|无法安排")
    lines.append("（「无法安排」会退穗；到活动开始时对方还没报名也是自动退穗）")
    return "\n".join(lines)


def handle_admin_wish_handle(text, operator_oid):
    """心愿处理 <记录号后6位|完整record_id> 已安排|无法安排"""
    parts = text[len("心愿处理"):].strip().split()
    if len(parts) < 2:
        return "格式：心愿处理 <记录号后6位> 已安排|无法安排"
    if not WISH_TABLE_ID:
        return "心愿单表还没配置（WISH_TABLE_ID 为空）。"
    key, state = parts[0], parts[1]
    if state not in (WISH_STATUS_ARRANGED, WISH_STATUS_FAILED):
        return f"状态只能是：{WISH_STATUS_ARRANGED} / {WISH_STATUS_FAILED}"

    rec = _find_by_tail(WISH_TABLE_ID, key)
    if not rec:
        return f"没找到心愿单：{key}"
    fields = rec.get("fields", {})
    cur = get_field_text(fields, FIELD_WISH_STATUS)
    if cur != WISH_STATUS_PENDING:
        return f"这张心愿单已经是「{cur}」了，不能重复处理。"

    if not update_record(WISH_TABLE_ID, rec.get("record_id"), {
        FIELD_WISH_STATUS: state,
        FIELD_WISH_OPERATOR: operator_oid,
        FIELD_WISH_HANDLED_AT: _now(),
    }):
        return "写入失败，请稍后重试。"
    return _resolve_redemption(
        fields, _WISH_TO_REDEMPTION[state],
        f"心愿单标记为{state}", operator_oid=operator_oid)


def _resolve_redemption(fields, target_status, note, operator_oid=""):
    """把表格单据的状态同步回 SQLite 的兑换单（退穗就发生在这里）。

    两处状态不一致时以**表格为准**：表格是管理员看得见、改得动的那一面，
    让看不见的那面赢，管理员会以为自己的操作没生效。所以这里不校验「当前
    是不是还能退」，由 lib 的幂等键保证不会退两次。
    """
    redemption_id = _redemption_id_of(fields)
    if not redemption_id:
        return f"已更新表格状态（{note}），但这条没记兑换单号，穗没动——请人工核对该用户余额。"
    try:
        if target_status == points_redeem.ST_REFUNDED:
            order = points_redeem.get(redemption_id)
            if not order:
                return f"兑换单 #{redemption_id} 找不到了，穗没动，请人工核对。"
            points_redeem.refund(redemption_id, reason=note,
                                 operator_oid=operator_oid)
            return f"{note}，已退还 {order['cost']} 穗。"
        points_redeem.mark(redemption_id, target_status, note=note,
                           operator_oid=operator_oid)
        return f"{note}。"
    except Exception as e:
        log(f"同步兑换单 {redemption_id} 失败: {e}")
        return f"{note}，但穗的账没动（{e}）——请人工核对。"


def _redemption_id_of(fields):
    raw = get_field_text(fields, FIELD_WISH_REDEMPTION_ID) or \
        get_field_text(fields, FIELD_MM_REDEMPTION_ID)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


def _find_by_tail(table_id, key):
    """按 record_id 的后 6 位找记录。列表里给管理员看的就是这 6 位——
    完整 record_id 二十多位，没人会去复制。"""
    key = key.strip()
    items = search_records(table_id)
    for i in items:
        rid = str(i.get("record_id") or "")
        if rid == key or rid.endswith(key):
            return i
    return None


# ---------------------------------------------------------------- 推荐单

def handle_admin_mm_list(text):
    """推荐单列表"""
    if not MATCHMAKER_ORDER_TABLE_ID:
        return "推荐单表还没配置（MATCHMAKER_ORDER_TABLE_ID 为空）。先跑 scripts/dev/create_points_tables.py。"
    items = search_records(MATCHMAKER_ORDER_TABLE_ID)
    if not items:
        return "还没有红娘推荐单。"

    lines = [f"红娘推荐单 {len(items)} 张："]
    for i in items:
        f = i.get("fields", {})
        lines.append(
            f"　#{str(i.get('record_id', ''))[-6:]} {get_field_text(f, FIELD_MM_USER_NAME)} "
            f"[{get_field_text(f, FIELD_MM_STATUS)}] {get_field_text(f, FIELD_MM_CONDITION)[:30]}")
    lines.append("\n处理：推荐单处理 <记录号后6位> 已推荐|已完成|无法推荐")
    lines.append("（「无法推荐」会退穗；超过期限没推荐也会自动退穗）")
    return "\n".join(lines)


def handle_admin_mm_handle(text, operator_oid):
    """推荐单处理 <记录号后6位> 已推荐|已完成|无法推荐"""
    parts = text[len("推荐单处理"):].strip().split()
    if len(parts) < 2:
        return "格式：推荐单处理 <记录号后6位> 已推荐|已完成|无法推荐"
    if not MATCHMAKER_ORDER_TABLE_ID:
        return "推荐单表还没配置（MATCHMAKER_ORDER_TABLE_ID 为空）。"
    key, state = parts[0], parts[1]
    if state not in MM_STATUSES:
        return f"状态只能是：{'/'.join(MM_STATUSES)}"

    rec = _find_by_tail(MATCHMAKER_ORDER_TABLE_ID, key)
    if not rec:
        return f"没找到推荐单：{key}"
    fields = rec.get("fields", {})
    cur = get_field_text(fields, FIELD_MM_STATUS)
    if cur == state:
        return f"这张推荐单已经是「{state}」了。"
    if cur == "无法推荐" or cur == "已完成":
        return f"这张推荐单已经结单（{cur}），不能再改。"

    if not update_record(MATCHMAKER_ORDER_TABLE_ID, rec.get("record_id"), {
        FIELD_MM_STATUS: state,
        FIELD_MM_OPERATOR: operator_oid,
        FIELD_MM_HANDLED_AT: _now(),
    }):
        return "写入失败，请稍后重试。"
    return _resolve_redemption(fields, _MM_TO_REDEMPTION[state],
                               f"推荐单{state}", operator_oid=operator_oid)


# ---------------------------------------------------------------- 配置

def handle_admin_config(text):
    """配置 ｜ 配置 <key> <值> ｜ 配置 <key> 默认"""
    keyword = text[len("配置"):].strip()
    if not keyword:
        lines = ["麦穗配置（改：配置 <key> <值>；恢复：配置 <key> 默认）", ""]
        for d in points_config.describe():
            mark = " *" if d["overridden"] else ""
            lines.append(f"【{d['key']}】{d['label']} = {d['value']}{mark}"
                         f"（默认 {d['default']}，{d['min']}~{d['max']}）")
        lines.append("\n带 * 的是被改过的。数值改动下一轮对账/下次刷新即生效。")
        return "\n".join(lines)

    parts = keyword.split()
    key = parts[0]
    if key not in points_config.SPECS:
        return f"没有这项配置：{key}。发「配置」看全部。"

    if len(parts) == 1:
        d = next(x for x in points_config.describe() if x["key"] == key)
        return (f"{d['label']}（{key}）= {d['value']}"
                f"{'（已改过）' if d['overridden'] else '（默认值）'}\n"
                f"范围：{d['min']} ~ {d['max']}")

    value = " ".join(parts[1:]).strip()
    try:
        if value in ("默认", "default", "恢复"):
            got = points_config.reset(key)
            return f"已把 {key} 恢复为默认值 {got}。"
        got = points_config.set_value(key, value)
    except ValueError as e:
        return f"没改成：{e}"
    log(f"管理员改配置 {key} = {got}")
    return f"已把 {key} 改成 {got}。"


# ---------------------------------------------------------------- 帮助

def handle_points_help():
    return (
        "【麦穗指令】\n"
        "【查穗 U-xxx或姓名】余额、邀请、兑换、流水\n"
        "【加穗 用户 数量 原因】手动加穗\n"
        "【加穗 用户 数量 活动ID 原因】协助组织活动"
        f"（{points_config.get('assist_min')}–{points_config.get('assist_max')} 穗，"
        f"同一活动累计上限 {points_config.get('assist_activity_cap')}）\n"
        "【扣穗 用户 数量 原因】手动扣穗\n"
        "【签到 活动ID 用户 按时|迟到|未到】记考勤（不发穗）\n"
        "【心愿列表 [活动ID]】查看心愿单\n"
        "【心愿处理 单号 已安排|无法安排】无法安排→退穗\n"
        "【推荐单列表】查看红娘推荐单\n"
        "【推荐单处理 单号 已推荐|已完成|无法推荐】无法推荐→退穗\n"
        "【配置】查看/修改麦穗数值（分值、比例、期限）"
    )


def settle_due_things():
    """周期任务：结算到期的邀请、超期的推荐单。返回一句日志用的摘要。

    这两件事都有「到点了没人管」的性质，所以放在机器人循环里自己做，而不是
    等管理员想起来。心愿到期（对方没报名）在 auto_tasks 里跟活动开始时间一起判。
    """
    from constants import FIELD_ACCOUNT_STATUS, FIELD_FEISHU_ID, USER_TABLE_ID

    from lib import points_invite

    # 邀请满确认期：账号正常 → 发穗；被封禁 → 收回
    banned = set()
    for u in search_records(USER_TABLE_ID) or []:
        f = u.get("fields", {})
        if get_field_text(f, FIELD_ACCOUNT_STATUS) == "封禁":
            oid = get_field_text(f, FIELD_FEISHU_ID)
            if oid:
                banned.add(oid)

    done = points_invite.settle_all(is_banned=lambda oid: oid in banned)
    settled = done["finalized"] + done["clawed_back"]

    # 推荐单超期没处理 → 退穗
    overdue = points_redeem.matchmaker_overdue()
    for order in overdue:
        try:
            points_redeem.refund(order["id"], reason="红娘推荐超期未处理")
        except Exception as e:
            log(f"推荐单超期退穗失败 #{order['id']}: {e}")
    if settled or overdue:
        log(f"麦穗周期结算：邀请 {settled} 条，推荐单超期退穗 {len(overdue)} 条")
    return settled, len(overdue)


def settle_due_things_loop(interval=120):
    while True:
        try:
            settle_due_things()
        except Exception as e:
            log(f"麦穗周期结算异常: {e}")
        import time
        time.sleep(interval)
