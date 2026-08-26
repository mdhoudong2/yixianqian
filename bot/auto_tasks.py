"""后台轮询任务：自动绑定 / 审核通知 / 喜欢处理 / 报名处理 / 数字红娘推荐。"""
import os
import time

import requests
from cards import generate_h5_url, send_main_menu_card
from clients import *
from constants import *
from queries import (
    find_user_by_id_or_name,
    find_user_by_nickname,
    find_user_by_openid,
    get_creator_openid,
    pick_primary_record,
    update_user_feishu_id,
)
from store import (
    add_notification,
    consume_observer_code,
    load_invite_rewarded,
    load_observer_codes,
    reserve_notified,
    save_invite_rewarded,
    unreserve_notified,
    update_bindings,
)

from lib import storage


def auto_bind_from_creator():
    """查找飞书用户ID为空但创建人不为空的记录，自动绑定；含防重复注册"""
    items = search_records(USER_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_FEISHU_ID, "operator": "isEmpty", "value": []}]
    })
    if not items:
        auto_fill_like_links()
        return

    # 查询所有已绑定飞书用户ID的记录，用于防重复
    existing = search_records(USER_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_FEISHU_ID, "operator": "isNotEmpty", "value": []}]
    })
    # 按「类型」拆两组：只拦同类型重复，跨类型（普通↔观察员）放行
    existing_observer_oids = set()  # 已注册过观察员的 open_id
    existing_user_oids = set()      # 已注册过普通用户的 open_id
    existing_nick = {}              # open_id -> 昵称（提示语用）
    for e in existing:
        ef = e.get("fields", {})
        eoid = get_field_text(ef, FIELD_FEISHU_ID)
        if not eoid:
            continue
        existing_nick.setdefault(eoid, get_field_text(ef, FIELD_NICKNAME))
        if get_field_text(ef, FIELD_ACCOUNT_STATUS) == STATUS_OBSERVER:
            existing_observer_oids.add(eoid)
        else:
            existing_user_oids.add(eoid)

    bound_count = 0
    duplicate_count = 0
    for item in items:
        record_id = item.get("record_id")
        fields = item.get("fields", {})
        open_id = get_creator_openid(fields)
        nickname = get_field_text(fields, FIELD_NICKNAME)

        if not open_id:
            continue

        # 先判目标类型（邀请码）：有效未用码→观察员，错误/已用→审核不通过，空→普通注册
        invite_code = (get_field_text(fields, FIELD_INVITE_CODE) or "").strip()
        is_observer = False
        is_wrong_code = False
        codes = {}
        if invite_code:
            codes = load_observer_codes()
            code_valid = invite_code in codes and not codes.get(invite_code, {}).get("used")
            is_observer = code_valid
            is_wrong_code = not code_valid

        # 防重复：只拦「同类型」重复，跨类型（普通↔观察员）放行
        if is_observer and open_id in existing_observer_oids:
            old_nick = existing_nick.get(open_id, "")
            update_record(USER_TABLE_ID, record_id, {
                FIELD_ACCOUNT_STATUS: "审核不通过",
                FIELD_FEISHU_ID: open_id,
            })
            duplicate_count += 1
            log(f"重复注册观察员已拦截: {nickname} (open_id={open_id}), 已注册为 {old_nick}")
            send_text_message(open_id,
                f"你已经注册过村情六处啦！姓名：{old_nick}\n\n"
                f"快去「一线牵 App」中浏览资料。"
            )
            send_main_menu_card(open_id)
            continue

        if not is_observer and not is_wrong_code and open_id in existing_user_oids:
            old_nick = existing_nick.get(open_id, "")
            # 重复记录标记「审核不通过」（门禁全拦、不可被普通激活），并补写飞书ID：
            # 否则飞书ID仍为空，下一轮会被当成"未绑定"重复轮询、反复发"已注册过"
            update_record(USER_TABLE_ID, record_id, {
                FIELD_ACCOUNT_STATUS: "审核不通过",
                FIELD_FEISHU_ID: open_id,
            })
            duplicate_count += 1
            log(f"重复注册已拦截: {nickname} (open_id={open_id}), 已注册为 {old_nick}")
            send_text_message(open_id,
                f"你已经注册过啦！姓名：{old_nick}\n\n"
                f"快去「一线牵 App」中浏览异性资料。"
            )
            send_main_menu_card(open_id)
            continue

        # 观察员注册：有效码走限权分支（跳过人工审核），爱心置 0（观察员无喜欢/报名权限，不参与爱心账）
        if is_observer:
            # 原子消耗邀请码（未用→已用）；已被并发占用则拒绝，杜绝一码多用
            if not consume_observer_code(invite_code, nickname):
                update_record(USER_TABLE_ID, record_id, {
                    FIELD_ACCOUNT_STATUS: "审核不通过",
                    FIELD_FEISHU_ID: open_id,
                })
                duplicate_count += 1
                log(f"观察员邀请码已被使用: {nickname} (open_id={open_id})")
                send_text_message(open_id, "邀请码已被使用，注册未通过。如有疑问请联系管理员。")
                continue
            update_record(USER_TABLE_ID, record_id, {
                FIELD_ACCOUNT_STATUS: STATUS_OBSERVER,
                FIELD_HEART_REMAIN: 0,
            })
            if update_user_feishu_id(record_id, open_id):
                bound_count += 1
                log(f"观察员注册成功: {nickname} -> {open_id}")
                existing_observer_oids.add(open_id)
                existing_nick[open_id] = nickname

                def _bind_observer(_data):
                    _data[open_id] = {
                        "open_id": open_id, "nickname": nickname, "record_id": record_id,
                        "bind_time": time.strftime("%Y-%m-%d %H:%M:%S"), "bind_type": "observer"
                    }
                    return _data

                update_bindings(_bind_observer)
                send_text_message(
                    open_id,
                    "欢迎你成为一线牵「村情六处」！\U0001f389\n\n"
                    "作为村情六处，你可以：\n"
                    "• 浏览男生/女生资料\n"
                    "• 留言、反馈\n"
                    "• 查看活动\n\n"
                    "（不含点喜欢、报名活动等交友功能）\n\n"
                    "点下方按钮进入一线牵App看看吧："
                )
                send_main_menu_card(open_id)
            continue

        if is_wrong_code:
            # 邀请码错误或已被使用：审核不通过（门禁全拦），补写飞书ID避免下一轮被当成「未绑定」重复轮询
            msg = "邀请码已被使用" if invite_code in codes else "邀请码错误"
            update_record(USER_TABLE_ID, record_id, {
                FIELD_ACCOUNT_STATUS: "审核不通过",
                FIELD_FEISHU_ID: open_id,
            })
            log(f"观察员注册邀请码无效: {nickname} (open_id={open_id})")
            send_text_message(open_id, f"{msg}，注册未通过。如有疑问请联系管理员。")
            continue

        # 新注册用户强制设为待审核（防止表单默认值或用户自选导致直接单身）
        current_status = get_field_text(fields, FIELD_ACCOUNT_STATUS)
        update_fields_bind = {}
        if current_status != "已脱单":
            update_fields_bind[FIELD_ACCOUNT_STATUS] = "待审核"
        # 设置初始爱心（仅字段为空时兜底写3；表格默认值已设为3，此处不覆盖）
        existing_hearts = get_field_number(fields, FIELD_HEART_REMAIN, -1)
        if existing_hearts < 0:
            update_fields_bind[FIELD_HEART_REMAIN] = INITIAL_HEARTS
        if update_fields_bind:
            update_record(USER_TABLE_ID, record_id, update_fields_bind)

        if update_user_feishu_id(record_id, open_id):
            bound_count += 1
            log(f"自动绑定成功: {nickname} -> {open_id} (状态: 待审核)")
            existing_user_oids.add(open_id)
            existing_nick[open_id] = nickname

            def _bind(_data):
                _data[open_id] = {
                    "open_id": open_id, "nickname": nickname, "record_id": record_id,
                    "bind_time": time.strftime("%Y-%m-%d %H:%M:%S"), "bind_type": "auto"
                }
                return _data

            update_bindings(_bind)

    if bound_count > 0:
        log(f"自动绑定轮询完成，本次绑定 {bound_count} 个用户")
    if duplicate_count > 0:
        log(f"重复注册拦截完成，本次拦截 {duplicate_count} 个")
    # 同时填充缺少喜欢链接的用户
    auto_fill_like_links()




def auto_fill_like_links():
    """为缺少「喜欢（可点击）」链接的用户补齐超链接（已填充相同链接的不重复更新）"""
    items = search_records(USER_TABLE_ID)
    if not items:
        return
    filled = 0
    for item in items:
        fields = item.get("fields", {})
        user_id = fields.get("用户ID")
        if not user_id:
            continue
        link = f"{LIKE_FORM_URL}?prefill_目标用户ID={requests.utils.quote(str(user_id))}"
        # 已填充过相同链接则跳过，避免每轮对全部用户重复写相同内容消耗API配额
        existing = fields.get("喜欢（可点击）")
        existing_link = ""
        if isinstance(existing, dict):
            existing_link = str(existing.get("link", "") or "")
        elif isinstance(existing, list) and existing:
            e0 = existing[0]
            if isinstance(e0, dict):
                existing_link = str(e0.get("link", "") or "")
        if existing_link == link:
            continue
        if update_record(USER_TABLE_ID, item.get("record_id"),
                         {"喜欢（可点击）": {"text": "❤️  喜 欢 TA 就 点 这 里  ❤️", "link": link}}):
            filled += 1
    if filled > 0:
        log(f"喜欢链接填充完成，本次填充 {filled} 个用户")




def auto_bind_loop(interval=30):
    log(f"自动绑定服务已启动，轮询间隔 {interval} 秒")
    while True:
        try:
            auto_bind_from_creator()
        except Exception as e:
            log(f"自动绑定循环异常: {e}")
        time.sleep(interval)




def auto_send_view_after_approval():
    """检测账号状态从待审核变为单身，发送H5链接并处理邀请奖励"""
    items = search_records(USER_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": FIELD_ACCOUNT_STATUS, "operator": "is", "value": ["单身"]},
            {"field_name": FIELD_FEISHU_ID, "operator": "isNotEmpty", "value": []}
        ]
    })
    if not items:
        return
    sent_count = 0
    for item in items:
        record_id = item.get("record_id")
        if not reserve_notified("approval_sent", record_id):
            continue
        fields = item.get("fields", {})
        nickname = get_field_text(fields, FIELD_NICKNAME)
        gender = get_field_text(fields, FIELD_GENDER)
        open_id = get_field_text(fields, FIELD_FEISHU_ID)
        if not open_id or not nickname:
            unreserve_notified("approval_sent", record_id)
            continue

        message_head = (
            "恭喜你，资料审核已通过！\U0001f389\n\n"
            "去一线牵App，开始牵线吧："
        )
        message_tail = (
            f"初始有 {INITIAL_HEARTS} 颗爱心，邀请好友注册可获得更多爱心（上限{MAX_HEARTS}颗）。\n\n"
            f"祝你早日找到天主给你准备的另一半！\U0001f495"
        )
        if send_text_message(open_id, message_head):
            send_main_menu_card(open_id)
            send_text_message(open_id, message_tail)
            sent_count += 1
            log(f"审核通过通知已发送: {nickname} ({gender})")

            inviter_id = get_field_text(fields, FIELD_INVITER_ID)
            if inviter_id:
                reward_inviter(open_id, nickname, inviter_id)
        else:
            unreserve_notified("approval_sent", record_id)

    if sent_count > 0:
        log(f"审核通过通知轮询完成，本次发送 {sent_count} 条")




def reward_inviter(invitee_openid, invitee_nickname, inviter_user_id):
    """邀请人奖励：被邀请人审核通过后，给邀请人+1爱心（上限30）"""
    rewarded = load_invite_rewarded()
    if invitee_openid in rewarded:
        return

    inviter_records = find_user_by_id_or_name(inviter_user_id)
    if not inviter_records:
        log(f"邀请奖励：未找到邀请人 {inviter_user_id}")
        return
    inviter = inviter_records[0]
    inviter_fields = inviter.get("fields", {})
    inviter_openid = get_field_text(inviter_fields, FIELD_FEISHU_ID)
    inviter_nickname = get_field_text(inviter_fields, FIELD_NICKNAME)
    inviter_record_id = inviter.get("record_id")

    if not inviter_openid:
        log(f"邀请奖励：邀请人 {inviter_nickname} 未绑定飞书")
        return

    current_hearts = get_field_number(inviter_fields, FIELD_HEART_REMAIN, INITIAL_HEARTS)
    if current_hearts >= MAX_HEARTS:
        log(f"邀请奖励：{inviter_nickname} 爱心已达上限 {MAX_HEARTS}")
        rewarded[invitee_openid] = inviter_openid
        save_invite_rewarded(rewarded)
        return

    new_hearts = min(current_hearts + 1, MAX_HEARTS)
    if update_record(USER_TABLE_ID, inviter_record_id, {FIELD_HEART_REMAIN: new_hearts}):
        rewarded[invitee_openid] = inviter_openid
        save_invite_rewarded(rewarded)
        log(f"邀请奖励: {inviter_nickname} +1爱心 (当前{int(new_hearts)}颗), 被邀请人: {invitee_nickname}")
        send_text_message(
            inviter_openid,
            f"\U0001f389 你的好友「{invitee_nickname}」已注册并审核通过！\n\n"
            f"你获得了 1颗爱心奖励，当前共有 {int(new_hearts)} 颗爱心。\n"
            f"继续邀请好友，最多可获得 {MAX_HEARTS} 颗爱心~"
        )
        send_main_menu_card(inviter_openid)




def auto_send_view_loop(interval=30):
    log(f"审核通过通知服务已启动，轮询间隔 {interval} 秒")
    while True:
        try:
            auto_send_view_after_approval()
        except Exception as e:
            log(f"审核通过通知循环异常: {e}")
        time.sleep(interval)




def auto_fill_like_initiator():
    """查找发起用户昵称为空但创建人不为空的喜欢记录，自动填充；含防重复喜欢逻辑"""
    items = search_records(LIKE_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_LIKE_INITIATOR, "operator": "isEmpty", "value": []}]
    })

    if not items:
        return

    filled_count = 0
    for item in items:
        record_id = item.get("record_id")
        fields = item.get("fields", {})
        initiator_openid = get_creator_openid(fields)
        target_user_id = get_field_text(fields, FIELD_LIKE_TARGET_ID)
        target_nickname = get_field_text(fields, FIELD_LIKE_TARGET)

        if not initiator_openid:
            continue

        # 通过open_id查找发起用户
        user_records = find_user_by_openid(initiator_openid)
        if not user_records:
            log(f"未找到open_id对应的用户: {initiator_openid}")
            continue

        user = user_records[0]
        user_fields = user.get("fields", {})
        initiator_nickname = get_field_text(user_fields, FIELD_NICKNAME)
        initiator_user_id = user_fields.get("用户ID", "")

        # 通过用户ID查找目标用户（优先），其次按昵称
        target_openid = ""
        target_records = []
        if target_user_id:
            target_records = find_user_by_id_or_name(target_user_id)
        elif target_nickname:
            target_records = find_user_by_nickname(target_nickname)
        if target_records:
            target_fields = target_records[0].get("fields", {})
            target_openid = get_field_text(target_fields, FIELD_FEISHU_ID)
            target_nickname = get_field_text(target_fields, FIELD_NICKNAME)
            target_user_id = target_fields.get("用户ID", target_user_id)

        # 防重复：用open_id检查（单向喜欢或相互喜欢都算重复）
        is_duplicate = False
        if initiator_openid and target_openid:
            existing = search_records(LIKE_TABLE_ID, {
                "conjunction": "and",
                "conditions": [
                    {"field_name": FIELD_LIKE_INITIATOR_OPENID, "operator": "is", "value": [initiator_openid]},
                    {"field_name": FIELD_LIKE_TARGET_OPENID, "operator": "is", "value": [target_openid]},
                    {"field_name": FIELD_LIKE_STATUS, "operator": "isNot", "value": ["已取消"]}
                ]
            })
            is_duplicate = any(r.get("record_id") != record_id for r in existing)

        # 不能喜欢自己
        is_self_like = initiator_openid and target_openid and initiator_openid == target_openid

        update_fields = {
            FIELD_LIKE_INITIATOR: initiator_nickname,
            FIELD_LIKE_INITIATOR_OPENID: initiator_openid,
            FIELD_LIKE_INITIATOR_ID: str(initiator_user_id) if initiator_user_id else "",
            FIELD_LIKE_TARGET: target_nickname,
            FIELD_LIKE_TARGET_ID: str(target_user_id) if target_user_id else ""
        }
        if target_openid:
            update_fields[FIELD_LIKE_TARGET_OPENID] = target_openid

        if is_duplicate:
            update_fields[FIELD_LIKE_STATUS] = "已取消"
            if update_record(LIKE_TABLE_ID, record_id, update_fields):
                filled_count += 1
                log(f"重复喜欢已拦截: {initiator_nickname} -> {target_nickname}")
                send_text_message(
                    initiator_openid,
                    f"你已经喜欢过「{target_nickname}」了，无需重复操作~"
                )
        elif is_self_like:
            update_fields[FIELD_LIKE_STATUS] = "已取消"
            if update_record(LIKE_TABLE_ID, record_id, update_fields):
                log(f"自喜欢已拦截: {initiator_nickname}")
                send_text_message(initiator_openid, "不能喜欢自己哦~")
        else:
            current_status = get_field_text(fields, FIELD_LIKE_STATUS)
            if not current_status:
                update_fields[FIELD_LIKE_STATUS] = "单向喜欢"
            if update_record(LIKE_TABLE_ID, record_id, update_fields):
                filled_count += 1
                log(f"填充喜欢记录成功: {initiator_nickname}({initiator_user_id}) -> {target_nickname}({target_user_id})")

    if filled_count > 0:
        log(f"填充喜欢记录轮询完成，本次填充 {filled_count} 条")




def auto_fill_like_loop(interval=20):
    log(f"喜欢记录填充服务已启动，轮询间隔 {interval} 秒")
    while True:
        try:
            auto_fill_like_initiator()
        except Exception as e:
            log(f"喜欢记录填充循环异常: {e}")
        time.sleep(interval)




def auto_send_anonymous_like_notification():
    """检测新的单向喜欢记录，给目标用户发送匿名通知（不含附言，附言仅相互喜欢后发送）"""
    # 一次查询所有有效喜欢（单向+相互），在内存中分别得到「待通知项」与「喜欢者统计」
    all_valid_likes = search_records(LIKE_TABLE_ID, {
        "conjunction": "or",
        "conditions": [
            {"field_name": FIELD_LIKE_STATUS, "operator": "is", "value": ["单向喜欢"]},
            {"field_name": FIELD_LIKE_STATUS, "operator": "is", "value": ["相互喜欢"]}
        ]
    })

    items = []
    target_likers = {}
    for like in all_valid_likes:
        like_fields = like.get("fields", {})
        status = get_field_text(like_fields, FIELD_LIKE_STATUS)
        tgt_oid = get_field_text(like_fields, FIELD_LIKE_TARGET_OPENID)
        init_oid = get_field_text(like_fields, FIELD_LIKE_INITIATOR_OPENID)
        # 单向喜欢且目标openid不为空 → 待通知
        if status == "单向喜欢" and tgt_oid:
            items.append(like)
        # 统计每位用户被多少人喜欢（发起者open_id去重）
        if tgt_oid and init_oid:
            target_likers.setdefault(tgt_oid, set()).add(init_oid)

    if not items:
        return

    # 批量查询用户性别，用于给不同性别的用户发对应视图链接
    all_users = search_records(USER_TABLE_ID)
    user_gender_map = {}
    for u in all_users:
        uf = u.get("fields", {})
        oid = get_field_text(uf, FIELD_FEISHU_ID)
        if oid:
            user_gender_map[oid] = get_field_text(uf, FIELD_GENDER)

    sent_count = 0

    for item in items:
        record_id = item.get("record_id")
        if not reserve_notified("like_notified", record_id):
            continue

        fields = item.get("fields", {})
        target_openid = get_field_text(fields, FIELD_LIKE_TARGET_OPENID)
        target_nickname = get_field_text(fields, FIELD_LIKE_TARGET)
        initiator_nickname = get_field_text(fields, FIELD_LIKE_INITIATOR)
        initiator_id = get_field_text(fields, FIELD_LIKE_INITIATOR_ID)
        like_type = get_field_text(fields, FIELD_LIKE_TYPE)

        if not target_openid or not initiator_nickname:
            unreserve_notified("like_notified", record_id)
            continue

        # 发送前复核：若刚被判定为相互喜欢（相互消息已发/将发），
        # 撤销本次匿名通知，避免「相互喜欢」后紧跟一条多余的「有人喜欢了你」
        live = bitable.get_record(LIKE_TABLE_ID, record_id)
        if not live or get_field_text(live.get("fields", {}), FIELD_LIKE_STATUS) != "单向喜欢":
            unreserve_notified("like_notified", record_id)
            continue

        like_count = len(target_likers.get(target_openid, set()))

        # 改为 H5 入口链接（在 App 内交互更友好）
        view_url = generate_h5_url(target_openid)

        if like_type == "实名":
            identity = f"{initiator_nickname}（用户ID {initiator_id}）" if initiator_id else initiator_nickname
            message = (
                f"\U0001f48c {identity} 实名喜欢了你！\n\n"
                f"截至目前，有 {like_count} 位异性喜欢你！\n\n"
                f"到下面找找看👇，说不定就是你心动的那个人~\n"
            )
            if view_url:
                message += f"{view_url}"
        else:
            message = (
                f"\U0001f48c 有人喜欢了你！\n"
                f"（为保护隐私，暂不透露对方身份，相互喜欢后才会揭晓哦！）\n\n"
                f"截至目前，有 {like_count} 位异性喜欢你！\n\n"
                f"到下面找找看👇，说不定就是你心动的那个人~"
            )

        if send_text_message(target_openid, message):
            if like_type != "实名":
                send_main_menu_card(target_openid)
            sent_count += 1
            log(f"匿名喜欢通知已发送: -> {target_nickname} (当前{like_count}人喜欢)")
        else:
            unreserve_notified("like_notified", record_id)

    if sent_count > 0:
        log(f"匿名喜欢通知轮询完成，本次发送 {sent_count} 条")




def auto_anonymous_like_loop(interval=25):
    log(f"匿名喜欢通知服务已启动，轮询间隔 {interval} 秒")
    while True:
        try:
            auto_send_anonymous_like_notification()
        except Exception as e:
            log(f"匿名喜欢通知循环异常: {e}")
        time.sleep(interval)




def auto_detect_mutual_like():
    """检测相互喜欢，更新状态并发送通知"""
    one_way_likes = search_records(LIKE_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_LIKE_STATUS, "operator": "is", "value": ["单向喜欢"]}]
    })
    if not one_way_likes:
        return

    like_map = {}
    for item in one_way_likes:
        fields = item.get("fields", {})
        initiator = get_field_text(fields, FIELD_LIKE_INITIATOR)
        target = get_field_text(fields, FIELD_LIKE_TARGET)
        initiator_oid = get_field_text(fields, FIELD_LIKE_INITIATOR_OPENID)
        target_oid = get_field_text(fields, FIELD_LIKE_TARGET_OPENID)
        if initiator_oid and target_oid:
            like_map[(initiator_oid, target_oid)] = {
                "record_id": item.get("record_id"),
                "initiator_name": initiator,
                "target_name": target,
                "initiator_openid": initiator_oid,
                "target_openid": target_oid,
                "message": get_field_text(fields, FIELD_LIKE_MESSAGE)
            }

    mutual_count = 0
    processed_pairs = set()

    for (initiator_oid, target_oid), info in like_map.items():
        pair_key = tuple(sorted([initiator_oid, target_oid]))
        if pair_key in processed_pairs:
            continue

        reverse_info = like_map.get((target_oid, initiator_oid))
        if reverse_info:
            # 立即标记该配对已处理，避免同轮内 (B,A) 方向重复处理/重复发送
            processed_pairs.add(pair_key)
            log(f"检测到相互喜欢: {info['initiator_name']} <-> {info['target_name']}")

            # 发送通知（原子预约去重，杜绝重复发送）
            pair_notify_key = f"{initiator_oid}_{target_oid}"
            if reserve_notified("mutual_notified", pair_notify_key):
                all_ok = True
                # 通知A：文字+对方名片
                if info["initiator_openid"]:
                    msg_a = (
                        f"🎉 好消息！你们相互喜欢了！\n\n"
                        f"{target} 也喜欢你~\n\n"
                        f"TA当初点爱心时说：\n"
                        f"「{reverse_info['message']}」\n\n"
                        f"你点爱心时说：\n"
                        f"「{info['message']}」\n\n"
                        f"点击下方名片，添加TA为好友开始聊天吧！\n\n"
                        f"⚠️ 温馨提示：交友需谨慎，注意保护个人隐私和财产安全，警惕诈骗。"
                    )
                    if send_text_message(info["initiator_openid"], msg_a):
                        send_user_card(info["initiator_openid"], reverse_info["initiator_openid"])
                    else:
                        all_ok = False

                # 通知B：文字+对方名片
                if reverse_info["initiator_openid"]:
                    msg_b = (
                        f"🎉 好消息！你们相互喜欢了！\n\n"
                        f"{initiator} 也喜欢你~\n\n"
                        f"TA当初点爱心时说：\n"
                        f"「{info['message']}」\n\n"
                        f"你点爱心时说：\n"
                        f"「{reverse_info['message']}」\n\n"
                        f"点击下方名片，添加TA为好友开始聊天吧！\n\n"
                        f"⚠️ 温馨提示：交友需谨慎，注意保护个人隐私和财产安全，警惕诈骗。"
                    )
                    if send_text_message(reverse_info["initiator_openid"], msg_b):
                        send_user_card(reverse_info["initiator_openid"], info["initiator_openid"])
                    else:
                        all_ok = False

                if not all_ok:
                    # 通知失败：回滚去重位，状态保持「单向喜欢」，下轮重新检测并重试，
                    # 避免「状态先落为相互喜欢 → 下轮查不到 → 通知永久丢失」。
                    unreserve_notified("mutual_notified", pair_notify_key)
                else:
                    # 通知全部成功后才落状态
                    update_record(LIKE_TABLE_ID, info["record_id"], {FIELD_LIKE_STATUS: "相互喜欢"})
                    update_record(LIKE_TABLE_ID, reverse_info["record_id"], {FIELD_LIKE_STATUS: "相互喜欢"})
                    # 撤销两条记录的匿名通知预约位：杜绝翻转窗口内匿名循环补发「有人喜欢了你」
                    unreserve_notified("like_notified", info["record_id"])
                    unreserve_notified("like_notified", reverse_info["record_id"])
                    mutual_count += 1
                    log(f"相互喜欢通知已发送: {info['initiator_name']} <-> {info['target_name']}")

    if mutual_count > 0:
        log(f"相互喜欢检测完成，本次发现 {mutual_count} 对")




def auto_detect_mutual_like_loop(interval=30):
    log(f"相互喜欢检测服务已启动，轮询间隔 {interval} 秒")
    while True:
        try:
            auto_detect_mutual_like()
        except Exception as e:
            log(f"相互喜欢检测循环异常: {e}")
        time.sleep(interval)




def reconcile_hearts():
    """爱心对账（v6 事件溯源，余额唯一写者）：
    期望值 = 初始 + 邀请奖励 − 有效喜欢数（状态≠已取消）。
    同 open_id 多档案：只对主档案记账（单身优先/用户ID最小），副本强制 0。
    同时把邀请奖励汇总发布到共享文件，供 H5 端计算显示。
    """
    likes = search_records(LIKE_TABLE_ID)
    active_by_oid = {}
    for l in likes:
        f = l.get("fields", {})
        if get_field_text(f, FIELD_LIKE_STATUS) == "已取消":
            continue
        oid = get_field_text(f, FIELD_LIKE_INITIATOR_OPENID)
        if oid:
            active_by_oid[oid] = active_by_oid.get(oid, 0) + 1

    invited_map = {}
    for inviter_oid in load_invite_rewarded().values():
        invited_map[inviter_oid] = invited_map.get(inviter_oid, 0) + 1

    # 发布邀请汇总（供 H5 computed_hearts 使用）
    try:
        storage.save_json(os.path.join(SHARED_DATA_DIR, "yixianqian_balances.json"),
                          {"invites": invited_map,
                           "updated": time.strftime("%Y-%m-%d %H:%M:%S")})
    except Exception as e:
        log(f"发布邀请汇总失败: {e}")

    groups = {}
    for u in search_records(USER_TABLE_ID):
        uf = u.get("fields", {})
        if get_field_text(uf, FIELD_ACCOUNT_STATUS) == STATUS_OBSERVER:
            continue  # 观察员不参与爱心账，跳过（否则对账会把观察员爱心写回初始值）
        oid = get_field_text(uf, FIELD_FEISHU_ID)
        if not oid:
            continue
        groups.setdefault(oid, []).append(u)

    fixed = 0

    def _write(rec, expected, tag):
        nonlocal fixed
        cur = get_field_number(rec.get("fields", {}), FIELD_HEART_REMAIN, None)
        if cur != expected:
            update_record(USER_TABLE_ID, rec.get("record_id"), {FIELD_HEART_REMAIN: expected})
            fixed += 1
            log(f"爱心对账[{tag}]: {get_field_text(rec.get('fields', {}), FIELD_NICKNAME)} {cur} → {expected}")

    for oid, recs in groups.items():
        primary = pick_primary_record(recs)[0]
        expected = max(0, min(INITIAL_HEARTS + invited_map.get(oid, 0) - active_by_oid.get(oid, 0), MAX_HEARTS))
        _write(primary, expected, "主档")
        for r in recs:
            if r.get("record_id") != primary.get("record_id"):
                _write(r, 0, "副本")

    if fixed:
        log(f"爱心对账完成，本次校正 {fixed} 条")


def reconcile_hearts_loop(interval=25):
    log(f"爱心对账服务已启动，轮询间隔 {interval} 秒")
    while True:
        try:
            reconcile_hearts()
        except Exception as e:
            log(f"爱心对账循环异常: {e}")
        time.sleep(interval)





def auto_fill_signup_info():
    """自动填充报名记录的报名人信息（通过创建人字段）"""
    pending = search_records(SIGNUP_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": FIELD_SIGNUP_OPENID, "operator": "isEmpty", "value": []},
            {"field_name": FIELD_SIGNUP_ACTIVITY_ID, "operator": "isNotEmpty", "value": []}
        ]
    })
    for item in pending:
        record_id = item.get("record_id")
        fields = item.get("fields", {})
        creator_oid = get_creator_openid(fields)
        if not creator_oid:
            continue
        user_records = find_user_by_openid(creator_oid)
        if not user_records:
            log(f"报名记录找不到用户: {creator_oid}")
            continue
        user = user_records[0]
        nickname = get_field_text(user.get("fields", {}), FIELD_NICKNAME)
        activity_id = get_field_text(fields, FIELD_SIGNUP_ACTIVITY_ID)

        # 防重复报名：同一用户同一活动只能报名一次
        existing = search_records(SIGNUP_TABLE_ID, {
            "conjunction": "and",
            "conditions": [
                {"field_name": FIELD_SIGNUP_OPENID, "operator": "is", "value": [creator_oid]},
                {"field_name": FIELD_SIGNUP_ACTIVITY_ID, "operator": "is", "value": [activity_id]},
                {"field_name": FIELD_SIGNUP_STATUS, "operator": "is", "value": ["已报名"]}
            ]
        })
        is_duplicate = any(r.get("record_id") != record_id for r in existing)
        if is_duplicate:
            update_record(SIGNUP_TABLE_ID, record_id, {
                FIELD_SIGNUP_OPENID: creator_oid,
                FIELD_SIGNUP_NICKNAME: nickname,
                FIELD_SIGNUP_STATUS: "已取消"
            })
            log(f"重复报名已拦截: {nickname} 活动 {activity_id}")
            send_text_message(creator_oid, f"你已经报名过「{activity_id}」活动了，无需重复报名~")
            continue

        update_record(SIGNUP_TABLE_ID, record_id, {
            FIELD_SIGNUP_OPENID: creator_oid,
            FIELD_SIGNUP_NICKNAME: nickname,
            FIELD_SIGNUP_STATUS: "已报名"
        })
        log(f"报名信息已填充: {nickname} 报名了活动 {activity_id}")




def auto_notify_signup():
    """报名后双向通知：
    1. 通知喜欢报名者的人：你喜欢的「昵称」报名了xx活动
    2. 通知报名者喜欢的人（匿名）：喜欢你的人报名了xx活动
    相互喜欢的只发实名那条，避免重复
    """
    signups = search_records(SIGNUP_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": FIELD_SIGNUP_STATUS, "operator": "is", "value": ["已报名"]},
            {"field_name": FIELD_SIGNUP_OPENID, "operator": "isNotEmpty", "value": []}
        ]
    })
    if not signups:
        return

    # 批量预取：活动名称映射 + 全部有效喜欢，避免循环内 N+1 次 API 调用
    act_name_by_id = {}
    act_record_by_id = {}
    for act in search_records(ACTIVITY_TABLE_ID):
        af = act.get("fields", {})
        aid = get_field_text(af, "活动ID")
        if aid:
            act_name_by_id[aid] = get_field_text(af, FIELD_ACTIVITY_NAME)
            act_record_by_id[aid] = act.get("record_id")

    likers_by_target = {}    # target_oid -> [(liker_oid, status), ...]
    liked_by_initiator = {}  # initiator_oid -> [(target_oid, status), ...]
    for like in search_records(LIKE_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_LIKE_STATUS, "operator": "isNot", "value": ["已取消"]}]
    }):
        lf = like.get("fields", {})
        init_oid = get_field_text(lf, FIELD_LIKE_INITIATOR_OPENID)
        tgt_oid = get_field_text(lf, FIELD_LIKE_TARGET_OPENID)
        status = get_field_text(lf, FIELD_LIKE_STATUS)
        if not init_oid or not tgt_oid:
            continue
        likers_by_target.setdefault(tgt_oid, []).append((init_oid, status))
        liked_by_initiator.setdefault(init_oid, []).append((tgt_oid, status))

    for signup in signups:
        signup_fields = signup.get("fields", {})
        signup_id = signup.get("record_id")
        signup_oid = get_field_text(signup_fields, FIELD_SIGNUP_OPENID)
        signup_name = get_field_text(signup_fields, FIELD_SIGNUP_NICKNAME)
        activity_id = get_field_text(signup_fields, FIELD_SIGNUP_ACTIVITY_ID)
        activity_name = act_name_by_id.get(activity_id, "")
        if not activity_name:
            continue

        # 本次已通知的人，防止多条喜欢记录导致重复
        notified_this_round = set()

        # 1. 谁喜欢了报名者 → 实名通知
        mutual_oids = set()
        for liker_oid, like_status in likers_by_target.get(signup_oid, []):
            if liker_oid == signup_oid:
                continue
            if like_status == "相互喜欢":
                mutual_oids.add(liker_oid)
            key = f"signup_{signup_id}_{liker_oid}"
            if liker_oid in notified_this_round or not reserve_notified("signup_notified", key):
                continue
            msg = (
                f"🔔 你喜欢的「{signup_name}」报名了「{activity_name}」活动！\n\n"
                f"你也去看看吧，说不定能线下偶遇哦~"
            )
            if send_text_message(liker_oid, msg):
                send_main_menu_card(liker_oid)
                add_notification(liker_oid, "signup", f"你喜欢的 {signup_name} 报名了 {activity_name} 活动", key,
                                 extra={"activity_id": act_record_by_id.get(activity_id, "")})
                notified_this_round.add(liker_oid)
                log(f"报名通知(实名): {liker_oid} <- {signup_name} 报名了 {activity_name}")
            else:
                unreserve_notified("signup_notified", key)

        # 2. 报名者喜欢了谁 → 匿名通知（排除相互喜欢的，已发过实名）
        for target_oid, _status in liked_by_initiator.get(signup_oid, []):
            if target_oid == signup_oid:
                continue
            if target_oid in mutual_oids:
                continue  # 相互喜欢已发实名
            key = f"signup_{signup_id}_{target_oid}_anon"
            if target_oid in notified_this_round or not reserve_notified("signup_notified", key):
                continue
            msg = (
                f"💌 喜欢你的人报名了「{activity_name}」活动！\n\n"
                f"你也去看看吧，万一你也喜欢TA呢~"
            )
            if send_text_message(target_oid, msg):
                send_main_menu_card(target_oid)
                add_notification(target_oid, "signup", f"喜欢你的人报名了 {activity_name} 活动", key,
                                 extra={"activity_id": act_record_by_id.get(activity_id, "")})
                notified_this_round.add(target_oid)
                log(f"报名通知(匿名): {target_oid} <- 有人喜欢TA并报名了 {activity_name}")
            else:
                unreserve_notified("signup_notified", key)




def auto_fill_signup_loop(interval=20):
    log(f"报名信息填充服务已启动，轮询间隔 {interval} 秒")
    while True:
        try:
            auto_fill_signup_info()
        except Exception as e:
            log(f"报名信息填充循环异常: {e}")
        time.sleep(interval)




def auto_notify_signup_loop(interval=30):
    log(f"报名通知服务已启动，轮询间隔 {interval} 秒")
    while True:
        try:
            auto_notify_signup()
        except Exception as e:
            log(f"报名通知循环异常: {e}")
        time.sleep(interval)




def auto_update_activity_signup_count():
    """更新活动报名人数"""
    activities = search_records(ACTIVITY_TABLE_ID)
    if not activities:
        return
    # 一次性查所有已报名记录并按活动ID统计，避免按活动数N+1查询
    all_signups = search_records(SIGNUP_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_SIGNUP_STATUS, "operator": "is", "value": ["已报名"]}]
    })
    signup_count_by_act = {}
    for s in all_signups:
        sf = s.get("fields", {})
        aid = get_field_text(sf, FIELD_SIGNUP_ACTIVITY_ID)
        if aid:
            signup_count_by_act[aid] = signup_count_by_act.get(aid, 0) + 1

    updated_count = 0
    for activity in activities:
        activity_record_id = activity.get("record_id")
        activity_fields = activity.get("fields", {})
        activity_id = get_field_text(activity_fields, "活动ID")
        activity_name = get_field_text(activity_fields, FIELD_ACTIVITY_NAME)
        current_count = activity_fields.get(FIELD_ACTIVITY_CURRENT_SIGNUP, 0)
        if not isinstance(current_count, (int, float)):
            current_count = 0
        if not activity_id:
            continue
        actual_count = signup_count_by_act.get(activity_id, 0)
        if actual_count != current_count:
            update_fields = {FIELD_ACTIVITY_CURRENT_SIGNUP: actual_count}
            capacity_raw = activity_fields.get("报名人数上限", 30)
            try:
                capacity = int(float(str(capacity_raw))) if capacity_raw else 9999
            except (ValueError, TypeError):
                capacity = 9999
            if actual_count >= capacity:
                update_fields[FIELD_ACTIVITY_STATUS] = "已满员"
            elif activity_fields.get(FIELD_ACTIVITY_STATUS) == "已满员" and actual_count < capacity:
                update_fields[FIELD_ACTIVITY_STATUS] = "报名中"
            update_record(ACTIVITY_TABLE_ID, activity_record_id, update_fields)
            updated_count += 1
            log(f"更新活动报名人数: {activity_name} {current_count} -> {actual_count}")
    if updated_count > 0:
        log(f"活动报名人数更新完成，本次更新 {updated_count} 个活动")




def auto_update_activity_signup_loop(interval=30):
    log(f"活动报名人数更新服务已启动，轮询间隔 {interval} 秒")
    while True:
        try:
            auto_update_activity_signup_count()
        except Exception as e:
            log(f"活动报名人数更新循环异常: {e}")
        time.sleep(interval)




def calculate_match_score(user_a, user_b):
    score = 0
    reasons = []
    def _hobby_set(val):
        if isinstance(val, str):
            return set(h.strip() for h in val.replace("，", ",").split(",") if h.strip())
        if isinstance(val, list):
            return set(str(v) for v in val if v)
        return set()
    hobbies_a = _hobby_set(user_a.get("hobbies"))
    hobbies_b = _hobby_set(user_b.get("hobbies"))
    if hobbies_a and hobbies_b:
        common = hobbies_a & hobbies_b
        total = hobbies_a | hobbies_b
        hobby_score = int(len(common) / len(total) * 40) if total else 0
        score += hobby_score
        if common:
            reasons.append(f"共同兴趣：{'、'.join(list(common)[:3])}")
    else:
        score += 10
    # 年龄维度已移除（用户表已删「年龄」字段），不再参与匹配评分
    edu_order = {"高中及以下": 1, "大专": 2, "本科": 3, "硕士": 4, "博士": 5}
    edu_a = edu_order.get(user_a.get(FIELD_EDUCATION, ""), 0)
    edu_b = edu_order.get(user_b.get(FIELD_EDUCATION, ""), 0)
    if edu_a and edu_b:
        edu_diff = abs(edu_a - edu_b)
        if edu_diff == 0:
            score += 20
            reasons.append("学历相当")
        elif edu_diff == 1:
            score += 15
        elif edu_diff == 2:
            score += 8
        else:
            score += 3
    else:
        score += 8
    score += 15
    return min(score, 100), reasons




def auto_generate_match_recommendations():
    today = time.strftime("%Y-%m-%d")
    match_log_file = os.path.join(SHARED_DATA_DIR, "yixianqian_match_log.json")
    match_log = storage.load_json(match_log_file, {})
    if match_log.get("last_generate_date") == today:
        return
    active_users = search_records(USER_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_ACCOUNT_STATUS, "operator": "is", "value": ["单身"]}]
    })
    if len(active_users) < 2:
        return
    users = []
    for item in active_users:
        fields = item.get("fields", {})
        nickname = get_field_text(fields, FIELD_NICKNAME)
        if not nickname:
            continue
        users.append({
            "nickname": nickname, "record_id": item.get("record_id"),
            FIELD_GENDER: get_field_text(fields, FIELD_GENDER),
            FIELD_EDUCATION: get_field_text(fields, FIELD_EDUCATION),
            "hobbies": get_multi_select_value(fields, FIELD_SELF_HOBBIES)
        })
    existing_recommendations = search_records(MATCH_TABLE_ID)
    existing_pairs = set()
    for rec in existing_recommendations:
        rec_fields = rec.get("fields", {})
        for_user = get_field_text(rec_fields, FIELD_MATCH_FOR_USER)
        target_user = get_field_text(rec_fields, FIELD_MATCH_TARGET_USER)
        if for_user and target_user:
            existing_pairs.add((for_user, target_user))
    generated_count = 0
    for user in users:
        candidates = [u for u in users if u[FIELD_GENDER] != user[FIELD_GENDER] and u["nickname"] != user["nickname"]]
        if not candidates:
            continue
        scored_candidates = []
        for candidate in candidates:
            if (user["nickname"], candidate["nickname"]) in existing_pairs:
                continue
            score, reasons = calculate_match_score(user, candidate)
            scored_candidates.append((score, candidate, reasons))
        scored_candidates.sort(key=lambda x: x[0], reverse=True)
        for score, candidate, reasons in scored_candidates[:3]:
            reason_text = f"匹配度{score}分"
            if reasons:
                reason_text += "，" + "；".join(reasons)
            create_record(MATCH_TABLE_ID, {
                FIELD_MATCH_FOR_USER: user["nickname"],
                FIELD_MATCH_TARGET_USER: candidate["nickname"],
                FIELD_MATCH_REASON: reason_text,
                FIELD_MATCH_STATUS: "待查看"
            })
            generated_count += 1
            log(f"数字红娘推荐: {user['nickname']} -> {candidate['nickname']} ({score}分)")
    match_log["last_generate_date"] = today
    match_log["last_generate_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    match_log["generated_count"] = generated_count
    storage.save_json(match_log_file, match_log)
    if generated_count > 0:
        log(f"数字红娘推荐生成完成，本次生成 {generated_count} 条")




def auto_generate_match_loop(interval=3600):
    log(f"数字红娘推荐服务已启动，检查间隔 {interval} 秒")
    while True:
        try:
            auto_generate_match_recommendations()
        except Exception as e:
            log(f"数字红娘推荐循环异常: {e}")
        time.sleep(interval)



