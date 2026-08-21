#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一线牵机器人 - 长连接事件接收服务（纯多维表格方案V3）

架构：
- 浏览器端：公开卡片视图浏览，点喜欢引导下载飞书
- 飞书APP端：机器人发注册表单→审核通过→发专属异性视图→点喜欢表单→通知
- 所有数据存在多维表格，机器人长连接服务处理业务逻辑和通知

功能：
1. 用户添加机器人自动发送注册表单链接
2. 自动绑定（通过创建人字段获取open_id）
3. 审核通过后自动发送对应性别的活跃异性视图链接
4. 自动填充喜欢记录的发起用户昵称和open_id
5. 匿名喜欢通知
6. 相互喜欢检测与通知
7. 自动扣减爱心
8. 活动报名人数更新
9. 数字红娘推荐
"""

import json
import time
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
import lark_oapi as lark
from lark_oapi.api.im.v1 import *
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger, P2CardActionTriggerResponse
from lark_oapi.api.application.v6 import P2ApplicationBotMenuV6

# ==================== 应用配置 ====================
import os

# 通过环境变量区分环境：YIXIANQIAN_ENV=dev 为开发版，默认生产版
IS_DEV = os.environ.get("YIXIANQIAN_ENV", "prod") == "dev"

import local_config as _cfg

if IS_DEV:
    APP_ID = _cfg.ASSISTANT_APP_ID
    APP_SECRET = _cfg.ASSISTANT_APP_SECRET
else:
    APP_ID = _cfg.APP_ID
    APP_SECRET = _cfg.APP_SECRET

# 管理员open_id列表（可添加多个）
ADMIN_OPEN_IDS = _cfg.ADMIN_OPEN_IDS

# ==================== 多维表格链接配置（需在飞书UI中获取后替换） ====================
REGISTER_FORM_URL = "https://lcnz8zx7fjk4.feishu.cn/share/base/form/shrcn04AWZCwqilzelLqT5CJsNd"
GIRL_VIEW_URL = "https://lcnz8zx7fjk4.feishu.cn/share/base/view/shrcnrFgDNL3nLMkaB68HwU9iCd"
BOY_VIEW_URL = "https://lcnz8zx7fjk4.feishu.cn/share/base/view/shrcnVmfOJq5Vmx83aBL7jdU13g"
ACTIVITY_VIEW_URL = "https://lcnz8zx7fjk4.feishu.cn/share/base/view/shrcnKs9V7lRWTKq51Lpg9tOfff"
LIKE_FORM_URL = "https://lcnz8zx7fjk4.feishu.cn/share/base/form/shrcnNhTZVdSTcmVRzRTwZg0E0b"

# ==================== 多维表格配置 ====================
BASE_TOKEN = _cfg.BASE_TOKEN
USER_TABLE_ID = "tblsecbZZv0thaPe"
LIKE_TABLE_ID = "tblaciMZHRQH7QBA"
ACTIVITY_TABLE_ID = "tblHLltReY8xHTfu"
SIGNUP_TABLE_ID = "tblNVJCnohVaWf8t"
MATCH_TABLE_ID = "tbl8eu9Y85tQZCu7"

FIELD_NICKNAME = "昵称"
FIELD_FEISHU_ID = "飞书用户ID"
FIELD_HEART_REMAIN = "爱心剩余"
FIELD_ACCOUNT_STATUS = "账号状态"
FIELD_GENDER = "性别"
FIELD_EDUCATION = "学历"
FIELD_SELF_HOBBIES = "我是一个怎样的人-爱好"
FIELD_CREATOR = "创建人"

FIELD_LIKE_INITIATOR = "发起用户昵称"
FIELD_LIKE_TARGET = "目标用户昵称"
FIELD_LIKE_STATUS = "状态"
FIELD_LIKE_HEART_DEDUCTED = "爱心已扣减"
FIELD_LIKE_MESSAGE = "附言"
FIELD_LIKE_INITIATOR_OPENID = "发起用户open_id"
FIELD_LIKE_TARGET_OPENID = "目标用户open_id"
FIELD_LIKE_NOTIFIED = "已通知目标"
FIELD_LIKE_INITIATOR_ID = "发起用户ID"
FIELD_LIKE_TARGET_ID = "目标用户ID"  # 新增：是否已发送匿名通知
FIELD_LIKE_TYPE = "喜欢类型"  # 匿名/实名

FIELD_ACTIVITY_NAME = "活动名称"
FIELD_ACTIVITY_CURRENT_SIGNUP = "当前报名人数"
FIELD_ACTIVITY_STATUS = "活动状态"

FIELD_SIGNUP_ACTIVITY_ID = "活动ID"
FIELD_SIGNUP_CREATOR = "创建人"
FIELD_SIGNUP_OPENID = "报名人open_id"
FIELD_SIGNUP_NICKNAME = "报名人昵称"
FIELD_SIGNUP_STATUS = "状态"
FIELD_SIGNUP_NOTIFIED = "已通知喜欢者"

FIELD_MATCH_FOR_USER = "推荐给用户"
FIELD_MATCH_TARGET_USER = "被推荐用户"
FIELD_MATCH_REASON = "推荐理由"
FIELD_MATCH_STATUS = "推荐状态"

# 分组功能
GROUP_SELECT_TABLE = "tblYo86Vd7dmzRQJ"
GROUP_RESULT_TABLE = "tbl3xxAYhyTDGWAB"
FIELD_GS_ACTIVITY_ID = "活动ID"
FIELD_GS_SELECTOR_OID = "选择人open_id"
FIELD_GS_SELECTOR_NAME = "选择人昵称"
FIELD_GS_SELECTOR_GENDER = "选择人性别"
FIELD_GS_CHOICES = ["第1志愿", "第2志愿", "第3志愿", "第4志愿", "第5志愿", "第6志愿", "第7志愿"]
FIELD_GR_ACTIVITY_ID = "活动ID"
FIELD_GR_GROUP_NO = "组号"
FIELD_GR_USER_OID = "用户open_id"
FIELD_GR_USER_NAME = "用户昵称"
FIELD_GR_USER_GENDER = "用户性别"
FIELD_ACT_GROUP_STATUS = "分组状态"
FIELD_ACT_MALE_PER_GROUP = "每组男生数"
FIELD_ACT_FEMALE_PER_GROUP = "每组女生数"
FIELD_ACT_GROUP_FLAG = "分组功能开启"  # 单选(是/否)：控制 H5 我的页是否显示「我的分组」入口
FIELD_GR_ROUND = "轮次"  # 分组结果轮次，单选(1/2/3...)，支持同活动多次分组

# 邀请功能
FIELD_INVITER_ID = "邀请人ID"  # 邀请人的用户ID（如U-0003）
INITIAL_HEARTS = 3
MAX_HEARTS = 30
H5_BACKEND_URL = "http://127.0.0.1:8091"

# ==================== 本地记录文件 ====================
BINDING_FILE = "yixianqian_bindings.json"
NOTIFIED_FILE = "yixianqian_notified.json"  # 记录已发送通知的记录ID，避免重复
WELCOMED_FILE = "yixianqian_welcomed.json"  # 记录已发送进入欢迎消息的用户open_id，避免重复
MENU_CARD_FILE = "yixianqian_menu_card.json"  # 记录上次发送菜单卡片的时间，用于节流
INVITE_REWARDED_FILE = "yixianqian_invites.json"  # 记录已奖励的邀请关系
NOTIFICATIONS_FILE = "/opt/yixianqian/yixianqian_notifications.json"  # 共享通知（机器人写，H5读）

# WebSocket健康检查
_last_ws_event_time = time.time()
_ws_client_ref = None
WS_HEALTH_CHECK_INTERVAL = 60      # 每60秒检查一次
WS_HEALTH_CHECK_TIMEOUT = 600      # 10分钟无任何事件则强制重连

_token_cache = {"token": None, "expire_time": 0}

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


# ==================== 工具函数 ====================
def get_tenant_access_token():
    now = time.time()
    if _token_cache["token"] and _token_cache["expire_time"] > now + 60:
        return _token_cache["token"]
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    data = {"app_id": APP_ID, "app_secret": APP_SECRET}
    for attempt in range(3):
        try:
            resp = requests.post(url, json=data, timeout=15)
            result = resp.json()
            if result.get("code") == 0:
                _token_cache["token"] = result["tenant_access_token"]
                _token_cache["expire_time"] = now + result.get("expire", 7200)
                return _token_cache["token"]
            else:
                log(f"获取token失败(第{attempt+1}次): {result}")
        except Exception as e:
            log(f"获取token异常(第{attempt+1}次): {e}")
        if attempt < 2:
            time.sleep(2 ** attempt)
    return None


def is_test_fake_openid(open_id):
    """判断是否为测试用假 open_id（ou_fake_*）。这些不是真实飞书用户，无法收到消息，
    直接跳过以免拖慢/阻塞向全体参与者的群发通知。"""
    return isinstance(open_id, str) and open_id.startswith("ou_fake_")


def send_text_message(receive_id, text):
    # 假测试账号不发起真实发送，避免 99992351 报错刷屏并阻塞后续消息处理
    if is_test_fake_openid(receive_id):
        return False
    token = get_tenant_access_token()
    token = get_tenant_access_token()
    if not token:
        return False
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    content = json.dumps({"text": text}, ensure_ascii=False)
    data = {"receive_id": receive_id, "msg_type": "text", "content": content}
    for attempt in range(3):
        try:
            resp = requests.post(url, headers=headers, json=data, timeout=15)
            result = resp.json()
            if result.get("code") == 0:
                return True
            else:
                log(f"发送消息失败(第{attempt+1}次): {result}")
        except Exception as e:
            log(f"发送消息异常(第{attempt+1}次): {e}")
        if attempt < 2:
            time.sleep(2 ** attempt)
    return False



def send_card_message(receive_id, card_content):
    """发送交互卡片消息"""
    token = get_tenant_access_token()
    if not token:
        return False
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    content_str = json.dumps(card_content, ensure_ascii=False)
    data = {"receive_id": receive_id, "msg_type": "interactive", "content": content_str}
    for attempt in range(3):
        try:
            resp = requests.post(url, headers=headers, json=data, timeout=15)
            result = resp.json()
            if result.get("code") == 0:
                return result.get("data", {}).get("message_id", True)
            else:
                log(f"发送卡片失败(第{attempt+1}次): {result}")
        except Exception as e:
            log(f"发送卡片异常(第{attempt+1}次): {e}")
        if attempt < 2:
            time.sleep(2 ** attempt)
    return False

def send_user_card(receive_id, share_open_id):
    """发送个人名片消息"""
    token = get_tenant_access_token()
    if not token:
        return False
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {
        "receive_id": receive_id,
        "msg_type": "share_user",
        "content": json.dumps({"user_id": share_open_id})
    }
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=15)
        result = resp.json()
        if result.get("code") == 0:
            return True
        log(f"发送名片失败: {result}")
    except Exception as e:
        log(f"发送名片异常: {e}")
    return False


def create_mutual_chat(openid_a, openid_b, name_a, name_b):
    """相互喜欢后创建群聊，把双方拉进去"""
    token = get_tenant_access_token()
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # 创建群聊（不传owner_id，机器人自动成为群主）
    chat_name = f"💕 {name_a} & {name_b}"
    create_url = "https://open.feishu.cn/open-apis/im/v1/chats?uuid=" + str(int(time.time())) + "&user_id_type=open_id"
    create_data = {
        "name": chat_name,
        "description": "一线牵-相互喜欢聊天通道",
        "chat_mode": "group",
        "chat_type": "private",
        "external": True,
        "owner_id": ADMIN_OPEN_IDS[0] if ADMIN_OPEN_IDS else BOT_OPEN_ID
    }
    try:
        resp = requests.post(create_url, headers=headers, json=create_data, timeout=15)
        result = resp.json()
        if result.get("code") != 0:
            log(f"创建群聊失败: {result}")
            return None
        chat_id = result.get("data", {}).get("chat_id")

        # 添加成员
        add_url = f"https://open.feishu.cn/open-apis/im/v1/chats/{chat_id}/members?member_id_type=open_id&succeed_type=0"
        add_resp = requests.post(add_url, headers=headers,
                                 json={"id_list": [openid_a, openid_b]}, timeout=15)
        add_result = add_resp.json()
        if add_result.get("code") == 0:
            log(f"创建相互喜欢群聊成功: {chat_name} ({chat_id})")
            return chat_id
        else:
            log(f"添加群成员失败: {add_result}")
            # 群创建成功但拉人失败，仍返回chat_id
            return chat_id
    except Exception as e:
        log(f"创建群聊异常: {e}")
    return None


def load_bindings():
    try:
        with open(BINDING_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}


def save_bindings(bindings):
    with open(BINDING_FILE, 'w', encoding='utf-8') as f:
        json.dump(bindings, f, ensure_ascii=False, indent=2)


def load_notified():
    try:
        with open(NOTIFIED_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {"likes": [], "mutual": []}


_notified_lock = threading.Lock()


def save_notified(data):
    """合并写回（加锁）：只更新 data 中的 key，保留文件中其他 key。
    避免多个后台通知线程并发读-改-写时互相覆盖去重记录，导致消息重复发送。"""
    with _notified_lock:
        try:
            with open(NOTIFIED_FILE, 'r', encoding='utf-8') as f:
                current = json.load(f)
        except Exception:
            current = {}
        for k, v in data.items():
            if isinstance(v, list) and isinstance(current.get(k), list):
                merged = list(current[k])
                for item in v:
                    if item not in merged:
                        merged.append(item)
                current[k] = merged
            else:
                current[k] = v
        with open(NOTIFIED_FILE, 'w', encoding='utf-8') as f:
            json.dump(current, f, ensure_ascii=False, indent=2)


def load_welcomed():
    """加载已发送进入欢迎消息的用户列表"""
    try:
        with open(WELCOMED_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return []


def save_welcomed(data):
    with open(WELCOMED_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_menu_card_time():
    try:
        with open(MENU_CARD_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}


def save_menu_card_time(data):
    with open(MENU_CARD_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)


def load_invite_rewarded():
    """加载已奖励的邀请记录 {invitee_openid: inviter_openid}"""
    try:
        with open(INVITE_REWARDED_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

def save_invite_rewarded(data):
    with open(INVITE_REWARDED_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

_notif_lock = threading.Lock()
def load_notifications():
    """加载共享通知文件"""
    try:
        with open(NOTIFICATIONS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {"items": []}
def save_notifications(data):
    with open(NOTIFICATIONS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
def add_notification(recipient, ntype, text, key=None, extra=None):
    """写入一条通知（按 key 去重），供 H5 消息页『动态』分区读取"""
    with _notif_lock:
        data = load_notifications()
        items = data.get("items", [])
        if key and any(it.get("key") == key for it in items):
            return
        item = {
            "recipient": recipient, "type": ntype, "text": text,
            "key": key, "time": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        if extra:
            item.update(extra)
        items.append(item)
        data["items"] = items
        save_notifications(data)

def generate_h5_url(open_id):
    """生成 H5 入口链接。身份由飞书「网页免登」确定，URL 不再携带登录 token（防止链接被转发冒用身份）"""
    return "https://app.nantou.love/"


def find_user_by_nickname(nickname):
    token = get_tenant_access_token()
    if not token:
        return []
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{USER_TABLE_ID}/records/search"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"filter": {"conjunction": "and", "conditions": [{"field_name": FIELD_NICKNAME, "operator": "is", "value": [nickname]}]}}
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=10)
        result = resp.json()
        if result.get("code") == 0:
            return result.get("data", {}).get("items", [])
        else:
            log(f"搜索用户失败: {result}")
            return []
    except Exception as e:
        log(f"搜索用户异常: {e}")
        return []


def find_user_by_openid(open_id):
    token = get_tenant_access_token()
    if not token:
        return []
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{USER_TABLE_ID}/records/search"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"filter": {"conjunction": "and", "conditions": [{"field_name": FIELD_FEISHU_ID, "operator": "is", "value": [open_id]}]}}
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=10)
        result = resp.json()
        if result.get("code") == 0:
            return result.get("data", {}).get("items", [])
        else:
            return []
    except Exception as e:
        log(f"搜索用户异常: {e}")
        return []


def update_user_feishu_id(record_id, open_id):
    return update_record(USER_TABLE_ID, record_id, {FIELD_FEISHU_ID: open_id})


def search_records(table_id, filter_conditions=None, page_size=100):
    token = get_tenant_access_token()
    if not token:
        return []
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/search"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"page_size": page_size}
    if filter_conditions:
        data["filter"] = filter_conditions
    all_items = []
    page_token = None
    while True:
        params = {"page_token": page_token} if page_token else {}
        try:
            resp = requests.post(url, headers=headers, json=data, params=params, timeout=10)
            result = resp.json()
            if result.get("code") != 0:
                log(f"搜索记录失败(table={table_id}): {result}")
                break
            d = result.get("data", {})
            all_items.extend(d.get("items", []))
            if not d.get("has_more"):
                break
            page_token = d.get("page_token")
            if not page_token:
                break
        except Exception as e:
            log(f"搜索记录异常(table={table_id}): {e}")
            break
    return all_items


def update_record(table_id, record_id, fields):
    token = get_tenant_access_token()
    if not token:
        return False
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/{record_id}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"fields": fields}
    try:
        resp = requests.put(url, headers=headers, json=data, timeout=10)
        result = resp.json()
        if result.get("code") == 0:
            return True
        else:
            log(f"更新记录失败(table={table_id}): {result}")
            return False
    except Exception as e:
        log(f"更新记录异常(table={table_id}): {e}")
        return False


def create_record(table_id, fields):
    token = get_tenant_access_token()
    if not token:
        return None
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"fields": fields}
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=10)
        result = resp.json()
        if result.get("code") == 0:
            return result.get("data", {}).get("record", {})
        else:
            log(f"创建记录失败(table={table_id}): {result}")
            return None
    except Exception as e:
        log(f"创建记录异常(table={table_id}): {e}")
        return None


def batch_create_records(table_id, records_fields, batch_size=200):
    """批量创建记录（Feishu Bitable batch_create）。

    records_fields: [{"fields": {...}}, ...] 或 [fields_dict, ...]（自动包装）。
    每批最多 batch_size 条（默认 200，接口上限 1000）。返回成功创建的条数。
    相比逐条 create_record，能显著降低大活动（数百人分组结果）的写入耗时。
    """
    if not records_fields:
        return 0
    token = get_tenant_access_token()
    if not token:
        return 0
    # 统一成 {"fields": {...}} 形式
    records = []
    for rf in records_fields:
        if isinstance(rf, dict) and "fields" in rf:
            records.append(rf)
        else:
            records.append({"fields": rf})

    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/batch_create"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    created = 0
    for i in range(0, len(records), batch_size):
        chunk = records[i:i + batch_size]
        data = {"records": chunk}
        try:
            resp = requests.post(url, headers=headers, json=data, timeout=30)
            result = resp.json()
            if result.get("code") == 0:
                created += len(result.get("data", {}).get("records", []) or chunk)
            else:
                log(f"批量创建记录失败(table={table_id},批{i//batch_size+1}): code={result.get('code')} msg={result.get('msg')}")
        except Exception as e:
            log(f"批量创建记录异常(table={table_id},批{i//batch_size+1}): {e}")
    return created


def delete_record(table_id, record_id):
    token = get_tenant_access_token()
    if not token:
        return False
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/{record_id}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        resp = requests.delete(url, headers=headers, timeout=10)
        result = resp.json()
        if result.get("code") == 0:
            return True
        else:
            log(f"删除记录失败(table={table_id}): {result}")
            return False
    except Exception as e:
        log(f"删除记录异常(table={table_id}): {e}")
        return False


def batch_delete_records(table_id, record_ids, batch_size=500):
    """批量删除记录（Feishu Bitable batch_delete）。record_ids 逐条 delete_record 太慢，
    大活动旧分组结果较多时用批量接口一次性删。返回成功删除条数。
    """
    ids = [rid for rid in record_ids if rid]
    if not ids:
        return 0
    token = get_tenant_access_token()
    if not token:
        return 0
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/batch_delete"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    deleted = 0
    for i in range(0, len(ids), batch_size):
        chunk = ids[i:i + batch_size]
        data = {"records": chunk}
        try:
            resp = requests.post(url, headers=headers, json=data, timeout=30)
            result = resp.json()
            if result.get("code") == 0:
                deleted += len(chunk)
            else:
                log(f"批量删除记录失败(table={table_id},批{i//batch_size+1}): code={result.get('code')} msg={result.get('msg')}")
        except Exception as e:
            log(f"批量删除记录异常(table={table_id},批{i//batch_size+1}): {e}")
    return deleted


def get_field_text(fields, field_name):
    val = fields.get(field_name, "")
    # 公式字段 {'type': N, 'value': [...]}
    if isinstance(val, dict) and "value" in val and isinstance(val["value"], list):
        val = val["value"]
    if isinstance(val, list) and val:
        first = val[0]
        if isinstance(first, dict):
            return str(first.get("text", "") or first.get("name", "") or "")
        return str(first)
    if isinstance(val, dict):
        return str(val.get("text", "") or val.get("name", "") or "")
    return val if isinstance(val, str) else ""


def get_field_number(fields, field_name, default=0):
    """获取数字字段值，兼容多种返回格式与公式字段"""
    val = fields.get(field_name, default)
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, dict) and "value" in val and isinstance(val["value"], list):
        val = val["value"]
    if isinstance(val, list) and val:
        v = val[0]
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
        if isinstance(v, dict):
            val = v.get("value", v.get("text", default))
        else:
            val = v
    if isinstance(val, dict):
        val = val.get("value", val.get("text", default))
    try:
        f = float(val)
        # 整数则返回 int，避免爱心等数字字段被写成浮点
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return default


def get_multi_select_value(fields, field_name):
    """获取多选字段值，返回选项名列表"""
    val = fields.get(field_name)
    if val is None:
        return []
    if isinstance(val, dict) and "value" in val and isinstance(val["value"], list):
        val = val["value"]
    if isinstance(val, list):
        out = []
        for item in val:
            if isinstance(item, dict):
                out.append(str(item.get("text", "") or item.get("name", "") or ""))
            else:
                out.append(str(item))
        return [x for x in out if x]
    if isinstance(val, str) and val:
        return [val]
    return []


def get_creator_openid(fields):
    """从创建人字段获取open_id"""
    creators = fields.get(FIELD_CREATOR, [])
    if not creators:
        return None
    creator = creators[0] if isinstance(creators, list) else creators
    return creator.get("id") if isinstance(creator, dict) else None


def log(msg):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")


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


# ==================== 功能1：自动绑定 ====================
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
    existing_openids = {}
    for e in existing:
        ef = e.get("fields", {})
        eoid = get_field_text(ef, FIELD_FEISHU_ID)
        if eoid:
            existing_openids[eoid] = get_field_text(ef, FIELD_NICKNAME)

    # 也检查bindings缓存
    bindings = load_bindings()
    for oid, info in bindings.items():
        if oid not in existing_openids:
            existing_openids[oid] = info.get("nickname", "")

    bound_count = 0
    duplicate_count = 0
    for item in items:
        record_id = item.get("record_id")
        fields = item.get("fields", {})
        open_id = get_creator_openid(fields)
        nickname = get_field_text(fields, FIELD_NICKNAME)

        if not open_id:
            continue

        # 防重复：该飞书账号已注册过
        if open_id in existing_openids:
            old_nick = existing_openids[open_id]
            # 将重复记录标记为已隐藏，不绑定
            update_record(USER_TABLE_ID, record_id, {FIELD_ACCOUNT_STATUS: "已隐藏"})
            duplicate_count += 1
            log(f"重复注册已拦截: {nickname} (open_id={open_id}), 已注册为 {old_nick}")
            send_text_message(open_id,
                f"你已经注册过啦！姓名：{old_nick}\n\n"
                f"快去「一线牵 App」中浏览异性资料。"
            )
            send_main_menu_card(open_id)
            continue

        # 新注册用户强制设为待审核（防止表单默认值或用户自选导致直接活跃）
        current_status = get_field_text(fields, FIELD_ACCOUNT_STATUS)
        update_fields_bind = {}
        if current_status != "已隐藏":
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
            bindings[open_id] = {
                "open_id": open_id, "nickname": nickname, "record_id": record_id,
                "bind_time": time.strftime("%Y-%m-%d %H:%M:%S"), "bind_type": "auto"
            }
            existing_openids[open_id] = nickname
            save_bindings(bindings)

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


# ==================== 功能2：审核通过后发送专属视图链接 ====================
def auto_send_view_after_approval():
    """检测账号状态从待审核变为活跃，发送H5链接并处理邀请奖励"""
    items = search_records(USER_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": FIELD_ACCOUNT_STATUS, "operator": "is", "value": ["活跃"]},
            {"field_name": FIELD_FEISHU_ID, "operator": "isNotEmpty", "value": []}
        ]
    })
    if not items:
        return
    notified = load_notified()
    sent_count = 0
    for item in items:
        record_id = item.get("record_id")
        if record_id in notified.get("approval_sent", []):
            continue
        fields = item.get("fields", {})
        nickname = get_field_text(fields, FIELD_NICKNAME)
        gender = get_field_text(fields, FIELD_GENDER)
        open_id = get_field_text(fields, FIELD_FEISHU_ID)
        if not open_id or not nickname:
            continue

        message_head = (
            f"恭喜你，资料审核已通过！\U0001f389\n\n"
            f"去一线牵App，开始牵线吧："
        )
        message_tail = (
            f"初始有 {INITIAL_HEARTS} 颗爱心，邀请好友注册可获得更多爱心（上限{MAX_HEARTS}颗）。\n\n"
            f"祝你早日找到天主给你准备的另一半！\U0001f495"
        )
        if send_text_message(open_id, message_head):
            send_main_menu_card(open_id)
            send_text_message(open_id, message_tail)
            sent_count += 1
            notified.setdefault("approval_sent", []).append(record_id)
            log(f"审核通过通知已发送: {nickname} ({gender})")

            inviter_id = get_field_text(fields, FIELD_INVITER_ID)
            if inviter_id:
                reward_inviter(open_id, nickname, inviter_id)

    save_notified(notified)
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


# ==================== 功能3：自动填充喜欢记录的发起用户信息 ====================
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


# ==================== 功能4：匿名喜欢通知 ====================
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

    notified = load_notified()
    sent_count = 0

    for item in items:
        record_id = item.get("record_id")
        if record_id in notified.get("like_notified", []):
            continue

        fields = item.get("fields", {})
        target_openid = get_field_text(fields, FIELD_LIKE_TARGET_OPENID)
        target_nickname = get_field_text(fields, FIELD_LIKE_TARGET)
        initiator_nickname = get_field_text(fields, FIELD_LIKE_INITIATOR)
        initiator_id = get_field_text(fields, FIELD_LIKE_INITIATOR_ID)
        like_type = get_field_text(fields, FIELD_LIKE_TYPE)

        if not target_openid or not initiator_nickname:
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
            notified.setdefault("like_notified", []).append(record_id)
            log(f"匿名喜欢通知已发送: -> {target_nickname} (当前{like_count}人喜欢)")

    save_notified(notified)
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


# ==================== 功能5：相互喜欢检测与通知 ====================
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

    notified = load_notified()
    mutual_count = 0
    processed_pairs = set()

    for (initiator_oid, target_oid), info in like_map.items():
        pair_key = tuple(sorted([initiator_oid, target_oid]))
        if pair_key in processed_pairs:
            continue

        reverse_info = like_map.get((target_oid, initiator_oid))
        if reverse_info:
            # 更新两条记录状态
            update_record(LIKE_TABLE_ID, info["record_id"], {FIELD_LIKE_STATUS: "相互喜欢"})
            update_record(LIKE_TABLE_ID, reverse_info["record_id"], {FIELD_LIKE_STATUS: "相互喜欢"})
            processed_pairs.add(pair_key)
            mutual_count += 1
            log(f"检测到相互喜欢: {info['initiator_name']} <-> {info['target_name']}")

            # 发送通知（避免重复）
            pair_notify_key = f"{initiator_oid}_{target_oid}"
            if pair_notify_key not in notified.get("mutual_notified", []):
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
                    send_text_message(info["initiator_openid"], msg_a)
                    send_user_card(info["initiator_openid"], reverse_info["initiator_openid"])

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
                    send_text_message(reverse_info["initiator_openid"], msg_b)
                    send_user_card(reverse_info["initiator_openid"], info["initiator_openid"])

                notified.setdefault("mutual_notified", []).append(pair_notify_key)
                log(f"相互喜欢通知已发送: {info['initiator_name']} <-> {info['target_name']}")

    save_notified(notified)
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


# ==================== 功能6：自动扣减爱心 ====================
def auto_deduct_hearts():
    # 扣减：非已取消且未扣减（单向喜欢或相互喜欢都扣，避免竞态）
    pending_deduct = search_records(LIKE_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": FIELD_LIKE_HEART_DEDUCTED, "operator": "is", "value": ["false"]},
            {"field_name": FIELD_LIKE_INITIATOR_OPENID, "operator": "isNotEmpty", "value": []}
        ]
    })
    for item in pending_deduct:
        record_id = item.get("record_id")
        fields = item.get("fields", {})
        status = get_field_text(fields, FIELD_LIKE_STATUS)
        if status == "已取消":
            continue
        initiator_oid = get_field_text(fields, FIELD_LIKE_INITIATOR_OPENID)
        initiator_name = get_field_text(fields, FIELD_LIKE_INITIATOR)
        if not initiator_oid:
            continue
        user_records = find_user_by_openid(initiator_oid)
        if not user_records:
            continue
        user = user_records[0]
        user_record_id = user.get("record_id")
        current_hearts = get_field_number(user.get("fields", {}), FIELD_HEART_REMAIN, 30)
        if current_hearts <= 0:
            log(f"扣减爱心失败: {initiator_name} 爱心不足")
            update_record(LIKE_TABLE_ID, record_id, {
                FIELD_LIKE_HEART_DEDUCTED: True,
                FIELD_LIKE_STATUS: "已取消"
            })
            send_text_message(
                initiator_oid,
                "你的爱心已用完，本次喜欢未生效。\n\n"
                "取消已有喜欢可返还爱心，或邀请好友获得更多爱心。"
            )
            send_main_menu_card(initiator_oid)
            continue
        new_hearts = current_hearts - 1
        if update_record(USER_TABLE_ID, user_record_id, {FIELD_HEART_REMAIN: new_hearts}):
            update_record(LIKE_TABLE_ID, record_id, {FIELD_LIKE_HEART_DEDUCTED: True})
            log(f"扣减爱心成功: {initiator_name} 剩余 {new_hearts}")

    # 返还：已取消且已扣减
    pending_refund = search_records(LIKE_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": FIELD_LIKE_STATUS, "operator": "is", "value": ["已取消"]},
            {"field_name": FIELD_LIKE_HEART_DEDUCTED, "operator": "is", "value": ["true"]}
        ]
    })
    for item in pending_refund:
        record_id = item.get("record_id")
        fields = item.get("fields", {})
        initiator_oid = get_field_text(fields, FIELD_LIKE_INITIATOR_OPENID)
        initiator_name = get_field_text(fields, FIELD_LIKE_INITIATOR)
        if not initiator_oid:
            continue
        user_records = find_user_by_openid(initiator_oid)
        if not user_records:
            continue
        user = user_records[0]
        user_record_id = user.get("record_id")
        current_hearts = get_field_number(user.get("fields", {}), FIELD_HEART_REMAIN, 30)
        new_hearts = current_hearts + 1
        if update_record(USER_TABLE_ID, user_record_id, {FIELD_HEART_REMAIN: new_hearts}):
            update_record(LIKE_TABLE_ID, record_id, {FIELD_LIKE_HEART_DEDUCTED: False})
            log(f"返还爱心成功: {initiator_name} 剩余 {new_hearts}")


def auto_deduct_hearts_loop(interval=25):
    log(f"自动扣减爱心服务已启动，轮询间隔 {interval} 秒")
    while True:
        try:
            auto_deduct_hearts()
        except Exception as e:
            log(f"自动扣减爱心循环异常: {e}")
        time.sleep(interval)


# ==================== 功能7：活动报名处理 ====================
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

    notified = load_notified()
    notified_set = set(notified.get("signup_notified", []))
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
            if key in notified_set or liker_oid in notified_this_round:
                continue
            msg = (
                f"🔔 你喜欢的「{signup_name}」报名了「{activity_name}」活动！\n\n"
                f"你也去看看吧，说不定能线下偶遇哦~"
            )
            send_text_message(liker_oid, msg)
            send_main_menu_card(liker_oid)
            add_notification(liker_oid, "signup", f"你喜欢的 {signup_name} 报名了 {activity_name} 活动", key,
                             extra={"activity_id": act_record_by_id.get(activity_id, "")})
            notified.setdefault("signup_notified", []).append(key)
            notified_this_round.add(liker_oid)
            log(f"报名通知(实名): {liker_oid} <- {signup_name} 报名了 {activity_name}")

        # 2. 报名者喜欢了谁 → 匿名通知（排除相互喜欢的，已发过实名）
        for target_oid, _status in liked_by_initiator.get(signup_oid, []):
            if target_oid == signup_oid:
                continue
            if target_oid in mutual_oids:
                continue  # 相互喜欢已发实名
            key = f"signup_{signup_id}_{target_oid}_anon"
            if key in notified_set or target_oid in notified_this_round:
                continue
            msg = (
                f"💌 喜欢你的人报名了「{activity_name}」活动！\n\n"
                f"你也去看看吧，万一你也喜欢TA呢~"
            )
            send_text_message(target_oid, msg)
            send_main_menu_card(target_oid)
            add_notification(target_oid, "signup", f"喜欢你的人报名了 {activity_name} 活动", key,
                             extra={"activity_id": act_record_by_id.get(activity_id, "")})
            notified.setdefault("signup_notified", []).append(key)
            notified_this_round.add(target_oid)
            log(f"报名通知(匿名): {target_oid} <- 有人喜欢TA并报名了 {activity_name}")
    save_notified(notified)


def find_activity_by_id(activity_id):
    """通过活动ID查找活动（自动编号字段需传数字）"""
    if not activity_id:
        return None
    # A-0002 -> 2
    import re
    m = re.search(r'(\d+)', str(activity_id))
    if not m:
        return None
    num = int(m.group(1))
    items = search_records(ACTIVITY_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": "活动ID", "operator": "is", "value": [str(num)]}]
    })
    return items[0] if items else None


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


def auto_update_activity_signup_loop(interval=30):
    log(f"活动报名人数更新服务已启动，轮询间隔 {interval} 秒")
    while True:
        try:
            auto_update_activity_signup_count()
        except Exception as e:
            log(f"活动报名人数更新循环异常: {e}")
        time.sleep(interval)


# ==================== 功能8：数字红娘推荐 ====================
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
    match_log_file = "yixianqian_match_log.json"
    try:
        with open(match_log_file, 'r', encoding='utf-8') as f:
            match_log = json.load(f)
    except:
        match_log = {}
    if match_log.get("last_generate_date") == today:
        return
    active_users = search_records(USER_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_ACCOUNT_STATUS, "operator": "is", "value": ["活跃"]}]
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
    with open(match_log_file, 'w', encoding='utf-8') as f:
        json.dump(match_log, f, ensure_ascii=False, indent=2)
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



# ==================== 功能9：活动分组 ====================

# 用户选择优先级分数（与JS版一致，最多10个）
PRIORITY_SCORES = {1: 100, 2: 90, 3: 80, 4: 70, 5: 65, 6: 60, 7: 55, 8: 50, 9: 45, 10: 40}
# 红娘星级分数
MATCHMAKER_STAR_SCORES = {5: 100, 4: 90, 3: 80, 2: 75, 1: 70}
# 权重
WEIGHT_USER_SELECTION = 0.7
WEIGHT_MATCHMAKER_PICK = 0.3


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


# ==================== 消息处理 ====================
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
        f"3. 审核通过后，我会自动发送H5使用链接给你\n\n"
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
    if status != "活跃":
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
    hearts = user_fields.get(FIELD_HEART_REMAIN, 30)
    if not isinstance(hearts, (int, float)):
        hearts = 30

    lines = [
        "你的账号状态：\n",
        f"昵称：{nickname}",
        f"状态：{status}",
        f"爱心剩余：{int(hearts)}",
        ""
    ]
    if status == "待审核":
        lines.append("资料正在审核中，请耐心等待，通过后会通知你。")
    elif status == "活跃":
        lines.append("账号已激活，发送「浏览」即可查看异性资料。")
    elif status == "已退出":
        lines.append("你已暂时退出相亲市场，如需恢复请联系管理员。")
    elif status == "已隐藏":
        lines.append("账号已被隐藏，如有疑问请联系管理员。")
    else:
        lines.append("如有疑问请联系管理员。")
    return "\n".join(lines)


def build_main_menu_card(h5_url=None):
    """构建主菜单卡片（卡片1：一线牵 App + 邀请好友 + 帮助）"""
    app_button = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "一线牵 App"},
        "type": "primary",
    }
    if h5_url:
        app_button["url"] = h5_url
    else:
        app_button["value"] = {"action": "menu_h5"}

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "欢迎使用一线牵\U0001f495"},
            "template": "red"
        },
        "elements": [
            {
                "tag": "action",
                "actions": [
                    app_button,
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "邀请好友"},
                        "type": "default",
                        "value": {"action": "menu_invite"}
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "帮助"},
                        "type": "default",
                        "value": {"action": "menu_help"}
                    }
                ]
            }
        ]
    }


def send_main_menu_card(open_id):
    """发送卡片1（主菜单卡片），「一线牵 App」按钮直接跳转H5"""
    return send_card_message(open_id, build_main_menu_card(generate_h5_url(open_id)))


def handle_help_command(sender_id):
    """发送帮助说明（文本）"""
    return WELCOME_TEXT





WELCOME_TEXT = (
    "欢迎欢迎\U0001f44f！\n\n"
    "给机器人发送下列指令：\n\n"
    "【一线牵】\n"
    "获取一线牵App链接（牵线、消息、活动、我的）；\n\n"
    "【邀请】\n"
    "获取专属邀请链接，邀请好友得爱心；\n\n"
    "【注册】\n"
    "获取注册表单链接；\n\n"
    "【状态】\n"
    "查看注册审核进度；\n\n"
    "【帮助】\n"
    "查看所有指令；\n\n"
    "祝你早日找到另一半！\U0001f495"
)


def handle_welcome(sender_id, is_first_time=False):
    if is_first_time:
        return handle_register_command(sender_id)
    return WELCOME_TEXT


def is_admin(open_id):
    return open_id in ADMIN_OPEN_IDS


def find_user_by_id_or_name(keyword):
    """通过用户ID（如U-0003）或昵称查找用户"""
    # 自动编号字段搜索需要数字，先尝试提取数字
    import re
    m = re.match(r"[Uu]-?(\d+)", keyword)
    if m:
        items = search_records(USER_TABLE_ID, {
            "conjunction": "and",
            "conditions": [{"field_name": "用户ID", "operator": "is", "value": [m.group(1)]}]
        })
        if items:
            return items
    # 再按昵称查
    return find_user_by_nickname(keyword)


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
    lines.append("回复「拒绝 用户ID」或「拒绝 昵称」隐藏该用户")
    return "\n".join(lines)


def handle_admin_approve(keyword):
    """管理员审核通过用户"""
    records = find_user_by_id_or_name(keyword)
    if not records:
        return f"未找到用户：{keyword}"
    if len(records) > 1:
        return f"找到多个匹配用户，请使用用户ID操作，如：通过 U-0003"

    record = records[0]
    record_id = record.get("record_id")
    fields = record.get("fields", {})
    nickname = get_field_text(fields, FIELD_NICKNAME)
    uid = fields.get("用户ID", "")
    current_status = get_field_text(fields, FIELD_ACCOUNT_STATUS)
    open_id = get_field_text(fields, FIELD_FEISHU_ID)

    if current_status == "活跃":
        return f"{uid} {nickname} 已经是活跃状态，无需重复操作。"

    if not open_id:
        return f"{uid} {nickname} 尚未绑定飞书账号（open_id为空），无法发送通知。请等待自动绑定后再审核。"

    # 更新状态为活跃
    if update_record(USER_TABLE_ID, record_id, {FIELD_ACCOUNT_STATUS: "活跃"}):
        log(f"管理员审核通过: {uid} {nickname}")
        # 立即发送审核通过通知给用户（审核通过通知线程也会发，但这里立即发一次）
        gender = get_field_text(fields, FIELD_GENDER)
        if gender == "男性":
            view_desc = "活跃女生"
        elif gender == "女性":
            view_desc = "活跃男生"
        else:
            view_desc = "活跃异性"

        h5_url = generate_h5_url(open_id)
        user_msg = (
            f"恭喜你，资料审核已通过！🎉\n\n"
            f"点击下方链接进入一线牵App，浏览「{view_desc}」并点喜欢：\n\n"
            f"{h5_url}\n\n"
            f"【点喜欢说明】\n"
            f"在对方卡片上点击「♥」按钮，填写一句附言，对方会匿名收到「有人喜欢你」的通知；你们相互喜欢后，附言才会发给对方。\n"
            f"如果对方也喜欢你，系统会通知你们相互喜欢，并开通聊天通道。\n\n"
            f"祝你早日脱单！💕"
        )
        send_text_message(open_id, user_msg)

        # 标记已发送，避免审核通过通知线程重复发送
        notified = load_notified()
        notified.setdefault("approval_sent", []).append(record_id)
        save_notified(notified)

        return f"已审核通过：{uid} {nickname}\n已发送审核通过通知和App链接给TA。"
    else:
        return f"审核操作失败，请稍后重试。"


def handle_admin_reject(keyword):
    """管理员拒绝/隐藏用户"""
    records = find_user_by_id_or_name(keyword)
    if not records:
        return f"未找到用户：{keyword}"
    if len(records) > 1:
        return f"找到多个匹配用户，请使用用户ID操作，如：拒绝 U-0003"

    record = records[0]
    record_id = record.get("record_id")
    fields = record.get("fields", {})
    nickname = get_field_text(fields, FIELD_NICKNAME)
    uid = fields.get("用户ID", "")
    current_status = get_field_text(fields, FIELD_ACCOUNT_STATUS)

    if current_status == "已隐藏":
        return f"{uid} {nickname} 已经是隐藏状态。"

    if update_record(USER_TABLE_ID, record_id, {FIELD_ACCOUNT_STATUS: "已隐藏"}):
        log(f"管理员拒绝用户: {uid} {nickname}")
        # 通知用户
        open_id = get_field_text(fields, FIELD_FEISHU_ID)
        if open_id:
            send_text_message(open_id, "很抱歉，你的资料暂未通过审核。如有疑问请联系管理员。")
        return f"已隐藏用户：{uid} {nickname}\n该用户将不会出现在浏览列表中。"
    else:
        return f"操作失败，请稍后重试。"


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
        return f"发送失败，用户可能未与机器人对话过"


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


def handle_admin_help():
    return (
        "管理员指令：\n\n"
        "【待审核】查看待审核用户\n"
        "【通过 U-xxx或姓名】审核通过\n"
        "【拒绝/隐藏 U-xxx或姓名】拒绝用户\n"
        "【通知 U-xxx 内容】给用户发消息\n"
        "【用户统计】查看统计数据\n"
        "【开始填志愿 活动ID 男数 女数】开始填志愿\n"
        "【开启分组功能 活动ID 开/关】控制 H5 我的页分组入口\n"
        "【查看未提交 活动ID】查看未提交志愿的报名者\n"
        "【执行分组 活动ID [轮次]】执行分组算法（默认第1轮）\n"
        "【分组状态 活动ID】查看分组进度\n"
        "【分组帮助】分组指令说明\n"
        "【管理员帮助】查看本帮助"
    )


def handle_admin_stats():
    """用户统计"""
    all_users = search_records(USER_TABLE_ID)
    total = len(all_users)
    pending = active = hidden = exited = unbound = 0
    for item in all_users:
        fields = item.get("fields", {})
        status = get_field_text(fields, FIELD_ACCOUNT_STATUS)
        if status == "待审核":
            pending += 1
        elif status == "活跃":
            active += 1
        elif status == "已隐藏":
            hidden += 1
        elif status == "已退出":
            exited += 1
        if not get_field_text(fields, FIELD_FEISHU_ID):
            unbound += 1

    return (
        f"用户统计：\n\n"
        f"总注册：{total}人\n"
        f"待审核：{pending}人\n"
        f"活跃：{active}人\n"
        f"已隐藏：{hidden}人\n"
        f"已退出：{exited}人\n"
        f"未绑定open_id：{unbound}人"
    )


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
            if status == "活跃":
                # 取消「重新登录就发卡片」：历史消息卡片也能触发进入单聊事件，多发刷屏
                log(f"用户 {user_open_id} 为活跃用户，不再发送菜单卡片")
                return
            else:
                # 非活跃用户，发送卡片1（只发一次）
                welcomed = load_welcomed()
                if user_open_id in welcomed:
                    return
                if send_main_menu_card(user_open_id):
                    welcomed.append(user_open_id)
                    save_welcomed(welcomed)
        else:
            # 新用户，发送注册表单（只发一次）
            welcomed = load_welcomed()
            if user_open_id in welcomed:
                return
            message = handle_register_command(user_open_id)
            if send_text_message(user_open_id, message):
                welcomed.append(user_open_id)
                save_welcomed(welcomed)
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


# 消息去重：记录已处理的message_id，防止飞书重连重复投递
_processed_msg_ids = set()
_MAX_PROCESSED_IDS = 500


def do_p2_im_message_message_read_v1(data: lark.im.v1.P2ImMessageMessageReadV1) -> None:
    """已读回执事件：无业务逻辑，仅注册以消除日志中 "processor not found" 刷屏。"""
    pass


def do_p2_im_message_receive_v1(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    event = data.event
    message = event.message
    sender = event.sender
    sender_id = sender.sender_id.open_id
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
        elif text.startswith("拒绝") or text.startswith("隐藏"):
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
        elif text_lower in ["分组帮助", "group help"]:
            reply = handle_group_help()

    # 普通用户指令
    if not reply:
        if text_lower in ["注册", "register", "我要注册", "报名"]:
            reply = handle_register_command(sender_id)
        elif text_lower in ["邀请", "invite", "邀请好友", "分享"]:
            reply = handle_invite_command(sender_id)
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


# ==================== 主函数 ====================
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
        ("自动扣减爱心", auto_deduct_hearts_loop, 25),
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
