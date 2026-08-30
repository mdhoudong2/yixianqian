#!/usr/bin/env python3
"""
一线牵机器人 - 长连接事件接收服务（纯多维表格方案V3）

模块结构：
- constants.py  应用配置与多维表格字段常量
- clients.py    飞书消息/多维表格共享客户端单例（lib/ 的进程内别名）
- store.py      运行时 JSON 读写（加锁 + 原子写，lib/storage.py）
- queries.py    用户/活动查询辅助
- cards.py      主菜单卡片（卡片1）
- auto_tasks.py 后台轮询任务（绑定/审核/喜欢/报名/推荐）
- grouping.py   活动分组算法与分组指令
- commands.py   用户与管理员指令
本文件：事件处理器、消息分发、工作线程启动与 WebSocket 主循环。

架构：
- 浏览器端：公开卡片视图浏览，点喜欢引导下载飞书
- 飞书APP端：机器人发注册表单→审核通过→发专属异性视图→点喜欢表单→通知
- 所有数据存在多维表格，机器人长连接服务处理业务逻辑和通知
"""

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import lark_oapi as lark
from lark_oapi.api.application.v6 import P2ApplicationBotMenuV6
from lark_oapi.api.im.v1 import *
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from auto_tasks import (
    auto_anonymous_like_loop,
    auto_bind_loop,
    auto_detect_mutual_like_loop,
    auto_fill_like_loop,
    auto_fill_signup_loop,
    auto_generate_match_loop,
    auto_notify_signup_loop,
    auto_send_view_loop,
    auto_update_activity_signup_loop,
    reconcile_hearts_loop,
)
from cards import WELCOME_TEXT, send_main_menu_card
from clients import *
from commands import (
    handle_admin_approve,
    handle_admin_generate_observer_codes,
    handle_admin_help,
    handle_admin_list_observer_codes,
    handle_admin_notify,
    handle_admin_pending,
    handle_admin_reject,
    handle_admin_stats,
    handle_admin_toggle_group_flag,
    handle_group_help,
    handle_h5_command,
    handle_help_command,
    handle_invite_command,
    handle_observer_command,
    handle_register_command,
    handle_status_command,
    handle_welcome,
)
from constants import *
from grouping import (
    handle_admin_group_status,
    handle_admin_start_group,
    handle_admin_stop_group,
    handle_admin_unsubmitted,
    handle_group_command,
    handle_group_submit,
)
from queries import find_user_by_openid
from store import load_bindings, load_welcomed

# WebSocket健康检查
_last_ws_event_time = time.time()
_ws_client_ref = None

# 管理后台重任务线程池：把「开始填志愿/执行分组/分组状态」等耗时操作放到后台线程，
# 让单线程消息处理器不被阻塞，管理员指令能秒回、期间其它消息也能正常响应。
ADMIN_TASK_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="admin-task")

def _run_admin_task_async(sender_id, task_name, handler, *args):
    """把管理员重任务放入后台线程执行，完成后直接回复管理员。

    线程内 send_text_message 已有 is_test_fake_openid 守卫，安全。
    处理器立即返回（不阻塞主处理器），保证 Bot 秒回其它消息。
    """
    try:
        ADMIN_TASK_EXECUTOR.submit(_admin_task_worker, sender_id, task_name, handler, args)
    except Exception as e:
        log(f"提交管理员任务失败 {task_name}: {e}")
        return "后台任务提交失败，请稍后重试。"




def _admin_task_worker(sender_id, task_name, handler, args):
    """后台线程执行体：跑 handler，把结果回复给管理员。"""
    try:
        reply = handler(*args)
    except Exception as e:
        log(f"管理员任务执行异常 {task_name}: {e}")
        reply = f"任务处理失败：{e}"
    if reply and send_text_message(sender_id, reply):
        log(f"管理员任务已回复: {task_name} -> {sender_id}")
    else:
        log(f"管理员任务回复失败或无内容: {task_name}")




def ws_watchdog_loop():
    """WebSocket看门狗：检测僵死连接并强制重连"""
    while True:
        time.sleep(WS_HEALTH_CHECK_INTERVAL)
        try:
            elapsed = time.time() - _last_ws_event_time
            if elapsed > WS_HEALTH_CHECK_TIMEOUT:
                log(f"⚠️ WebSocket健康检查：{int(elapsed)}秒未收到任何事件，强制断开重连...")
                if _ws_client_ref:
                    try:
                        _ws_client_ref._disconnect()
                    except Exception as e:
                        log(f"强制断开失败: {e}，退出进程让systemd重启")
                        os._exit(1)
                else:
                    log("WebSocket客户端引用为空，退出进程让systemd重启")
                    os._exit(1)
            elif elapsed > WS_HEALTH_CHECK_TIMEOUT / 2:
                log(f"WebSocket健康检查：{int(elapsed)}秒未收到事件，继续观察")
        except Exception as e:
            log(f"看门狗异常: {e}")




def do_p2_im_chat_access_event_bot_p2p_chat_entered_v1(data: lark.im.v1.P2ImChatAccessEventBotP2pChatEnteredV1) -> None:
    """用户进入机器人单聊时自动发送菜单卡片或注册表单"""
    try:
        event = data.event
        operator_id = event.operator_id
        if not operator_id or not operator_id.open_id:
            return
        user_open_id = operator_id.open_id
        log(f"用户进入单聊: {user_open_id}")

        # 检查用户是否已注册
        user_records = find_user_by_openid(user_open_id)
        if user_records:
            user_fields = user_records[0].get("fields", {})
            status = get_field_text(user_fields, FIELD_ACCOUNT_STATUS)
            if status == "单身":
                # 取消「重新登录就发卡片」：历史消息卡片也能触发进入单聊事件，多发刷屏
                log(f"用户 {user_open_id} 为单身用户，不再发送菜单卡片")
                return
            else:
                # 非单身用户，发送卡片1（只发一次）
                def _mark(_lst):
                    if user_open_id in _lst:
                        return None
                    _lst.append(user_open_id)
                    return _lst

                if user_open_id not in load_welcomed():
                    from store import update_welcomed
                    update_welcomed(_mark)
                    send_main_menu_card(user_open_id)
        else:
            # 新用户，发送注册表单（只发一次）
            def _mark_new(_lst):
                if user_open_id in _lst:
                    return None
                _lst.append(user_open_id)
                return _lst

            if user_open_id not in load_welcomed():
                from store import update_welcomed
                update_welcomed(_mark_new)
                message = handle_register_command(user_open_id)
                send_text_message(user_open_id, message)
                log(f"注册引导已发送: {user_open_id}")
    except Exception as e:
        log(f"处理进入单聊事件异常: {e}")




def do_p2_application_bot_menu_v6(data: P2ApplicationBotMenuV6) -> None:
    """处理机器人菜单点击事件"""
    try:
        event = data.event
        operator = event.operator
        if not operator or not operator.operator_id or not operator.operator_id.open_id:
            return
        open_id = operator.operator_id.open_id
        event_key = event.event_key or ""
        log(f"菜单点击: user={open_id}, key={event_key}")

        if event_key == "invite":
            reply = handle_invite_command(open_id)
        elif event_key == "help":
            reply = handle_help_command(open_id)
        elif event_key == "h5":
            reply = handle_h5_command(open_id)
        else:
            reply = WELCOME_TEXT

        if reply:
            send_text_message(open_id, reply)
    except Exception as e:
        log(f"处理菜单点击异常: {e}")




def do_p2_card_action_trigger(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    """处理卡片按钮点击（菜单按钮/分组提交）"""
    try:
        event = data.event
        operator_open_id = event.operator.open_id if event.operator else None
        action_value = event.action.value if event.action else {}

        if not operator_open_id or not action_value:
            return P2CardActionTriggerResponse({
                "toast": {"type": "error", "content": "操作失败，请重试"}
            })

        action = action_value.get("action", "")

        if action == "menu_h5":
            reply = handle_h5_command(operator_open_id)
            if reply:
                send_text_message(operator_open_id, reply)
            return P2CardActionTriggerResponse({
                "toast": {"type": "success", "content": "正在为你准备链接..."}
            })
        elif action == "menu_invite":
            reply = handle_invite_command(operator_open_id)
            if reply:
                send_text_message(operator_open_id, reply)
            return P2CardActionTriggerResponse({
                "toast": {"type": "success", "content": "邀请链接已发送"}
            })
        elif action == "menu_help":
            send_text_message(operator_open_id, WELCOME_TEXT)
            return P2CardActionTriggerResponse({
                "toast": {"type": "info", "content": "帮助信息已发送"}
            })

        elif action == "submit_group":
            # 表单容器提交
            form_value = {}
            try:
                if hasattr(event.action, 'form_value') and event.action.form_value:
                    form_value = event.action.form_value
                elif isinstance(event.action, dict):
                    form_value = event.action.get('form_value', {})
            except:
                form_value = {}

            result = handle_group_submit(operator_open_id, action_value, form_value)
            return P2CardActionTriggerResponse(result)

        return P2CardActionTriggerResponse({
            "toast": {"type": "error", "content": "未知操作"}
        })
    except Exception as e:
        log(f"卡片回调异常: {e}")
        import traceback
        log(traceback.format_exc())
        return P2CardActionTriggerResponse({
            "toast": {"type": "error", "content": "操作失败，请稍后重试"}
        })




def do_p2_im_message_message_read_v1(data: lark.im.v1.P2ImMessageMessageReadV1) -> None:
    """已读回执事件：无业务逻辑，仅注册以消除日志中 "processor not found" 刷屏。"""


def do_p2_p2p_chat_create(data) -> None:
    """单聊创建事件：无业务逻辑（进入单聊由 p2p_chat_entered 处理），
    仅注册以消除 "processor not found" 刷屏并 ACK 事件，避免飞书反复重投。"""




# 消息去重：记录已处理的message_id，防止飞书重连重复投递
_processed_msg_ids = set()
_MAX_PROCESSED_IDS = 500

def do_p2_im_message_receive_v1(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    event = data.event
    message = event.message
    sender = event.sender
    # sender / sender_id 可能为空（系统/机器人消息等），先判空再取 open_id，避免 AttributeError 静默丢消息
    if not sender or not sender.sender_id:
        return
    sender_id = sender.sender_id.open_id
    if not sender_id:
        return
    sender_type = sender.sender_type
    chat_type = message.chat_type
    message_type = message.message_type
    content = message.content
    msg_id = message.message_id

    # 消息去重
    if msg_id:
        if msg_id in _processed_msg_ids:
            log(f"重复消息已忽略: {msg_id}")
            return
        _processed_msg_ids.add(msg_id)
        if len(_processed_msg_ids) > _MAX_PROCESSED_IDS:
            _processed_msg_ids.clear()

    log(f"收到消息: sender={sender_id}, type={sender_type}, chat={chat_type}, msg_type={message_type}")
    if chat_type != "p2p":
        return
    if sender_type != "user":
        return
    if message_type != "text":
        send_text_message(sender_id, "暂不支持该类型消息，请发送文字指令。\n发送「帮助」查看使用说明。")
        return
    try:
        content_dict = json.loads(content)
        text = content_dict.get("text", "").strip()
    except:
        text = ""
    log(f"消息内容: {text}")
    reply = ""
    text_lower = text.lower()

    # 管理员指令优先判断
    if is_admin(sender_id):
        if text_lower in ["待审核", "pending", "审核"]:
            reply = handle_admin_pending()
        elif text.startswith("通过"):
            keyword = text[2:].strip()
            if not keyword:
                reply = "请指定用户，如：通过 U-0003 或 通过 姓名"
            else:
                reply = handle_admin_approve(keyword)
        elif text.startswith("拒绝"):
            keyword = text[2:].strip()
            if not keyword:
                reply = "请指定用户，如：拒绝 U-0003"
            else:
                reply = handle_admin_reject(keyword)
        elif text.startswith("通知"):
            reply = handle_admin_notify(text)
        elif text_lower in ["用户统计", "统计", "stats"]:
            reply = handle_admin_stats()
        elif text_lower in ["管理员帮助", "admin help", "管理帮助"]:
            reply = handle_admin_help()
        elif text.startswith("开始填志愿"):
            keyword = text[5:].strip()
            reply = "收到，正在后台开启志愿填写，请稍候..."
            _run_admin_task_async(sender_id, "开始填志愿", handle_admin_start_group, keyword)
        elif text.startswith("查看未提交"):
            keyword = text[len("查看未提交"):].strip()
            reply = "收到，正在后台查询未提交人员，请稍候..."
            _run_admin_task_async(sender_id, "查看未提交", handle_admin_unsubmitted, keyword)
        elif text.strip() == "重连" and sender_id in ADMIN_OPEN_IDS:
            reply = "正在重连WebSocket..."
            log("管理员触发手动重连")
            if _ws_client_ref:
                try:
                    _ws_client_ref._disconnect()
                except Exception as e:
                    log(f"手动重连断开失败: {e}")
        elif text.startswith("执行分组"):
            keyword = text[4:].strip()
            reply = "收到，正在后台执行分组并保存结果，请稍候..."
            _run_admin_task_async(sender_id, "执行分组", handle_admin_stop_group, keyword)
        elif text.startswith("分组状态"):
            keyword = text[4:].strip()
            reply = "收到，正在后台查询分组状态，请稍候..."
            _run_admin_task_async(sender_id, "分组状态", handle_admin_group_status, keyword)
        elif text.startswith("开启分组功能"):
            keyword = text[len("开启分组功能"):].strip()
            reply = handle_admin_toggle_group_flag(keyword)
        elif text.startswith("生成村情六处邀请码") or text.startswith("生成吃瓜群众邀请码"):
            prefix = "生成村情六处邀请码" if text.startswith("生成村情六处邀请码") else "生成吃瓜群众邀请码"
            keyword = text[len(prefix):].strip()
            reply = handle_admin_generate_observer_codes(keyword)
        elif text_lower in ["查看村情六处邀请码", "村情六处邀请码", "查看吃瓜群众邀请码", "吃瓜群众邀请码"]:
            reply = handle_admin_list_observer_codes()
        elif text_lower in ["分组帮助", "group help"]:
            reply = handle_group_help()

    # 普通用户指令
    if not reply:
        if text_lower in ["注册", "register", "我要注册", "报名"]:
            reply = handle_register_command(sender_id)
        elif text_lower in ["邀请", "invite", "邀请好友", "分享"]:
            reply = handle_invite_command(sender_id)
        elif text_lower in ["村情六处", "村情六处注册", "observer"]:
            reply = handle_observer_command(sender_id)
        elif text_lower in ["h5", "一线牵", "app", "进入", "打开", "网页版", "网页", "web"]:
            reply = handle_h5_command(sender_id)
        elif text_lower in ["帮助", "help", "?", "？", "使用帮助"]:
            reply = handle_help_command(sender_id)
        elif text_lower in ["状态", "我的信息", "我的状态", "status"]:
            reply = handle_status_command(sender_id)
        elif text_lower in ["分组", "分组选择", "选组", "group"]:
            reply = handle_group_command(sender_id)
        else:
            bindings = load_bindings()
            is_first_time = sender_id not in bindings
            reply = handle_welcome(sender_id, is_first_time)
    if reply:
        if send_text_message(sender_id, reply):
            log(f"已回复用户: {sender_id}")
        else:
            log(f"回复失败: {sender_id}")




def heartbeat_loop():
    """每10分钟输出一次心跳，方便确认服务存活"""
    while True:
        try:
            active_threads = sum(1 for t in threading.enumerate() if t.is_alive())
            log(f"心跳: 服务运行中，活跃线程数={active_threads}")
        except Exception as e:
            log(f"心跳异常: {e}")
        time.sleep(600)




def start_worker_threads():
    """启动所有业务线程，返回线程列表用于监控"""
    if IS_DEV:
        log("开发模式：跳过业务线程，仅启动心跳")
        t = threading.Thread(target=heartbeat_loop, daemon=True, name="心跳")
        t.start()
        log("已启动线程: 心跳")
        return

    threads_config = [
        ("自动绑定", auto_bind_loop, 30),
        ("审核通过通知", auto_send_view_loop, 30),
        ("喜欢记录填充", auto_fill_like_loop, 20),
        ("匿名喜欢通知", auto_anonymous_like_loop, 25),
        ("相互喜欢检测", auto_detect_mutual_like_loop, 30),
        ("爱心对账", reconcile_hearts_loop, 25),
        ("报名信息填充", auto_fill_signup_loop, 20),
        ("报名通知", auto_notify_signup_loop, 30),
        ("活动报名更新", auto_update_activity_signup_loop, 30),
        ("数字红娘推荐", auto_generate_match_loop, 3600),
    ]
    for name, func, interval in threads_config:
        t = threading.Thread(target=func, args=(interval,), daemon=True, name=name)
        t.start()
        log(f"已启动线程: {name}")
    # 心跳线程
    t = threading.Thread(target=heartbeat_loop, daemon=True, name="心跳")
    t.start()
    log("已启动线程: 心跳")

    # WebSocket健康看门狗
    t = threading.Thread(target=ws_watchdog_loop, daemon=True, name="WS看门狗")
    t.start()
    log("WebSocket健康看门狗已启动（10分钟无事件自动重连）")




def main():
    print("=" * 60)
    env_label = "【开发版】" if IS_DEV else "【生产版】"
    print(f"一线牵机器人 - 纯多维表格方案V3.1（带自动重连）{env_label}")
    print(f"APP_ID: {APP_ID}")
    print(f"多维表格: {BASE_TOKEN}")
    print(f"用户表: {USER_TABLE_ID}")
    print("等待用户消息...")
    print("=" * 60)

    # 启动业务线程（只启动一次，长连接重连时不重复启动）
    start_worker_threads()

    # 长连接自动重连循环
    reconnect_count = 0
    while True:
        try:
            event_handler = lark.EventDispatcherHandler.builder("", "") \
                .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1) \
                .register_p2_im_message_message_read_v1(do_p2_im_message_message_read_v1) \
                .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(do_p2_im_chat_access_event_bot_p2p_chat_entered_v1) \
                .register_p2_card_action_trigger(do_p2_card_action_trigger) \
                .register_p2_application_bot_menu_v6(do_p2_application_bot_menu_v6) \
                .register_p1_customized_event("p2p_chat_create", do_p2_p2p_chat_create) \
                .register_p2_customized_event("p2p_chat_create", do_p2_p2p_chat_create) \
                .build()
            cli = lark.ws.Client(APP_ID, APP_SECRET, event_handler=event_handler, log_level=lark.LogLevel.INFO)

            # 健康检查：monkey-patch _handle_message 追踪最后收到事件的时间
            global _ws_client_ref
            _ws_client_ref = cli
            _original_handle_message = cli._handle_message
            async def _patched_handle_message(msg):
                global _last_ws_event_time
                _last_ws_event_time = time.time()
                return await _original_handle_message(msg)
            cli._handle_message = _patched_handle_message

            if reconnect_count > 0:
                log(f"长连接第 {reconnect_count} 次重连...")

            log("WebSocket连接中...")
            cli.start()  # 阻塞，直到连接断开或异常
            log("WebSocket连接已断开")

            # 如果 cli.start() 正常返回，说明连接断开了
            log("长连接已断开，5秒后重连...")
        except Exception as e:
            log(f"长连接异常: {e}，5秒后重连...")
        reconnect_count += 1
        time.sleep(5)




if __name__ == "__main__":
    main()
