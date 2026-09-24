"""共享运行时 JSON 的读写封装（加锁 + 原子写，见 lib/storage.py）。"""
import secrets
import time

from constants import *

from lib import storage


def load_bindings():
    return storage.load_json(BINDING_FILE, {})


def update_bindings(mutator):
    """加锁读-改-写绑定表（跨进程 flock + 原子写），避免并发丢失绑定"""
    return storage.update_json(BINDING_FILE, {}, mutator)


def reserve_notified(key, item_key):
    """加锁原子预约去重位：返回 True 表示本进程获得发送权（首次），False 表示已被处理。
    在发送消息之前调用，杜绝“先发后记”的重复发送窗口。"""
    acquired = [False]

    def _m(data):
        lst = data.setdefault(key, [])
        if item_key in lst:
            return None  # 已存在，放弃写入
        lst.append(item_key)
        data[key] = lst
        acquired[0] = True
        return data

    storage.update_json(NOTIFIED_FILE, {"likes": [], "mutual": []}, _m)
    return acquired[0]


def unreserve_notified(key, item_key):
    """发送失败时回滚去重位，允许下轮重试"""
    def _m(data):
        lst = data.get(key, [])
        if item_key in lst:
            lst.remove(item_key)
            data[key] = lst
            return data
        return None

    storage.update_json(NOTIFIED_FILE, {"likes": [], "mutual": []}, _m)


def load_welcomed():
    """加载已发送进入欢迎消息的用户列表"""
    return storage.load_json(WELCOMED_FILE, [])


def update_welcomed(mutator):
    """加锁读-改-写已欢迎列表（跨进程 flock + 原子写）"""
    return storage.update_json(WELCOMED_FILE, [], mutator)


def load_invite_rewarded():
    """加载已奖励的邀请记录 {invitee_openid: inviter_openid}"""
    return storage.load_json(INVITE_REWARDED_FILE, {})


def save_invite_rewarded(data):
    storage.save_json(INVITE_REWARDED_FILE, data)


# 邀请人解析失败后的重试节流（秒）：6 小时才重试一次
INVITE_RETRY_INTERVAL = 6 * 3600
# 节流记录保留天数（超过就修剪，避免文件无限增长）
INVITE_RETRY_KEEP_DAYS = 30


def invite_retry_due(invitee_openid, now=None):
    """邀请人解析失败后的负缓存节流：未满 INVITE_RETRY_INTERVAL 秒返回 False。

    被邀请人填的邀请人ID 可能是「没有/不知道」这类永远解析不到用户的垃圾值。
    没有节流时补扫每 30 秒重查一次——生产实测每 45 秒一整轮、每天 2000+ 行
    日志、白烧飞书查询配额（2026-09-24 审查）。节流只影响重试频率，不影响
    正确性：管理员事后把邀请人ID 改对，最多等一个节流窗口就会补发。
    """
    if not invitee_openid:
        return False
    now = time.time() if now is None else now
    allowed = [False]

    def _m(data):
        try:
            last = float(data.get(invitee_openid, 0) or 0)
        except (TypeError, ValueError):
            last = 0.0
        if now - last < INVITE_RETRY_INTERVAL:
            return None  # 还在节流窗口内，放弃写入
        data[invitee_openid] = now
        cutoff = now - INVITE_RETRY_KEEP_DAYS * 86400
        for k in [k for k, v in list(data.items())
                  if not isinstance(v, (int, float)) or v < cutoff]:
            data.pop(k, None)
        allowed[0] = True
        return data

    storage.update_json(INVITE_RETRY_FILE, {}, _m)
    return allowed[0]


def load_p2p_chats():
    """加载 open_id -> p2p单聊chat_id 映射，用于主动推送优先走chat_id规避230101"""
    return storage.load_json(P2P_CHAT_FILE, {})


def update_p2p_chat(open_id, chat_id):
    """加锁原子写入一条 open_id->chat_id 映射（入站消息/进入单聊时调用）"""
    if not open_id or not chat_id:
        return

    def _m(data):
        if data.get(open_id) == chat_id:
            return None  # 无变化，不写盘
        data[open_id] = chat_id
        return data

    storage.update_json(P2P_CHAT_FILE, {}, _m)


# load_heart_bonus / add_heart_bonus 已删除：管理员加赠不再走这个独立账本，
# 而是直接填用户表的「管理员加赠」字段，由 reconcile_hearts 读取叠加。


def add_notification(recipient, ntype, text, key=None, extra=None):
    """写入一条通知（按 key 去重），供 H5 消息页『动态』分区读取"""
    def _add(data):
        items = data.get("items", [])
        if key and any(it.get("key") == key for it in items):
            return None
        item = {
            "recipient": recipient, "type": ntype, "text": text,
            "key": key, "time": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        if extra:
            item.update(extra)
        items.append(item)
        data["items"] = items
        return data
    storage.update_json(NOTIFICATIONS_FILE, {"items": []}, _add)


# ========== 观察员邀请码（管理员批量生成，一次性使用，限制注册人数） ==========

def load_observer_codes():
    """加载观察员邀请码 {code: {"used": bool, "used_by": str, "used_at": str}}"""
    return storage.load_json(OBSERVER_CODES_FILE, {})


def generate_observer_codes(n):
    """批量生成 n 个唯一邀请码（8位大写字母+数字，去除易混淆字符），返回新码列表"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    generated = []

    def _m(data):
        for _ in range(n):
            while True:
                code = "".join(secrets.choice(alphabet) for _ in range(8))
                if code not in data:
                    break
            data[code] = {"used": False, "used_by": "", "used_at": ""}
            generated.append(code)
        return data

    storage.update_json(OBSERVER_CODES_FILE, {}, _m)
    return generated


def consume_observer_code(code, nickname):
    """原子消耗邀请码：未使用→已使用，返回 True；不存在或已使用返回 False"""
    consumed = [False]

    def _m(data):
        entry = data.get(code)
        if not entry or entry.get("used"):
            return None  # 不存在或已被使用，放弃写入
        data[code] = {"used": True, "used_by": nickname,
                      "used_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        consumed[0] = True
        return data

    storage.update_json(OBSERVER_CODES_FILE, {}, _m)
    return consumed[0]


def release_observer_code(code):
    """回滚邀请码：已使用→未使用（搬移失败时调用，避免邀请码被浪费）。返回 True/False。"""
    released = [False]

    def _m(data):
        entry = data.get(code)
        if not entry or not entry.get("used"):
            return None  # 不存在或未使用，无需回滚
        data[code] = {"used": False, "used_by": "", "used_at": ""}
        released[0] = True
        return data

    storage.update_json(OBSERVER_CODES_FILE, {}, _m)
    return released[0]
