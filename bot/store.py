"""共享运行时 JSON 的读写封装（加锁 + 原子写，见 lib/storage.py）。"""
import time

from constants import *

from lib import storage


def load_bindings():
    return storage.load_json(BINDING_FILE, {})




def save_bindings(bindings):
    storage.save_json(BINDING_FILE, bindings)


def update_bindings(mutator):
    """加锁读-改-写绑定表（跨进程 flock + 原子写），避免并发丢失绑定"""
    return storage.update_json(BINDING_FILE, {}, mutator)




def load_notified():
    return storage.load_json(NOTIFIED_FILE, {"likes": [], "mutual": []})




def save_notified(data):
    """合并写回：只更新 data 中的 key，保留文件中其他 key。
    加锁（线程锁 + 跨进程 flock）+ 原子写，避免多个后台通知线程/进程并发读-改-写
    互相覆盖去重记录，导致消息重复发送。"""

    def _merge(current):
        for k, v in data.items():
            if isinstance(v, list) and isinstance(current.get(k), list):
                merged = list(current[k])
                for item in v:
                    if item not in merged:
                        merged.append(item)
                current[k] = merged
            else:
                current[k] = v
        return current

    storage.update_json(NOTIFIED_FILE, {"likes": [], "mutual": []}, _merge)


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




def save_welcomed(data):
    storage.save_json(WELCOMED_FILE, data)


def update_welcomed(mutator):
    """加锁读-改-写已欢迎列表（跨进程 flock + 原子写）"""
    return storage.update_json(WELCOMED_FILE, [], mutator)




def load_menu_card_time():
    return storage.load_json(MENU_CARD_FILE, {})




def save_menu_card_time(data):
    storage.save_json(MENU_CARD_FILE, data)


def update_menu_card_time(mutator):
    """加锁读-改-写菜单卡节流时间（跨进程 flock + 原子写）"""
    return storage.update_json(MENU_CARD_FILE, {}, mutator)




def load_invite_rewarded():
    """加载已奖励的邀请记录 {invitee_openid: inviter_openid}"""
    return storage.load_json(INVITE_REWARDED_FILE, {})



def save_invite_rewarded(data):
    storage.save_json(INVITE_REWARDED_FILE, data)


def update_invite_rewarded(mutator):
    """加锁读-改-写邀请奖励记录（跨进程 flock + 原子写）"""
    return storage.update_json(INVITE_REWARDED_FILE, {}, mutator)



def load_notifications():
    """加载共享通知文件"""
    return storage.load_json(NOTIFICATIONS_FILE, {"items": []})



def save_notifications(data):
    storage.save_json(NOTIFICATIONS_FILE, data)



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

