"""共享运行时 JSON 的读写封装（加锁 + 原子写，见 lib/storage.py）。"""
import random
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
                code = "".join(random.choices(alphabet, k=8))
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
