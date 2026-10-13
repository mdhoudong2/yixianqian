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


# ========== 自动报名重试队列 ==========
# 审核通过通知发送前就占下了 approval_sent 去重位，报名若因飞书瞬时错误失败
# 占位也不会回滚——没有这个队列，那条记录会被整条去重、永远不再尝试报名。
# 存在 NOTIFIED_FILE 的 "signup_retry" 键：{user表record_id: 已重试次数}。
SIGNUP_RETRY_MAX = 240  # ≈2 小时（30s 轮询一轮补试一次）


def add_signup_retry(record_id):
    """加入自动报名重试队列；已在队列则不重置已有计数（避免反复入队把计数清零）。"""
    if not record_id:
        return

    def _m(data):
        q = data.setdefault("signup_retry", {})
        if record_id in q:
            return None
        q[record_id] = 0
        return data

    storage.update_json(NOTIFIED_FILE, {"likes": [], "mutual": []}, _m)


def get_signup_retry(record_id):
    """该记录当前已重试次数；0 = 不在队列。"""
    q = storage.load_json(NOTIFIED_FILE, {}) or {}
    try:
        return int((q.get("signup_retry", {}) or {}).get(record_id, 0) or 0)
    except (TypeError, ValueError):
        return 0


def bump_signup_retry(record_id):
    """重试次数 +1 并返回新值（跨进程原子，每次 retry_* 失败调用一次）。"""
    new_value = [0]

    def _m(data):
        q = data.setdefault("signup_retry", {})
        try:
            n = int(q.get(record_id, 0) or 0) + 1
        except (TypeError, ValueError):
            n = 1
        q[record_id] = n
        new_value[0] = n
        return data

    storage.update_json(NOTIFIED_FILE, {"likes": [], "mutual": []}, _m)
    return new_value[0]


def clear_signup_retry(record_id):
    """出队（报名成功 / 业务性跳过 / 用户记录已不存在）。不在队列则零写入。"""
    def _m(data):
        q = data.get("signup_retry", {})
        if record_id not in q:
            return None
        q.pop(record_id, None)
        return data

    storage.update_json(NOTIFIED_FILE, {"likes": [], "mutual": []}, _m)


def list_signup_retries(limit):
    """按已重试次数升序返回 [(record_id, attempts), ...]，最多 limit 条。

    次数少的先补试：新入队（0 次）的重试成功率最高，不会排在一群老失败后面饿死。
    attempts 相同时按 record_id 排序，保证跨进程输出稳定可比。
    """
    q = storage.load_json(NOTIFIED_FILE, {}) or {}
    items = []
    for k, v in (q.get("signup_retry", {}) or {}).items():
        try:
            items.append((k, int(v or 0)))
        except (TypeError, ValueError):
            items.append((k, 0))
    items.sort(key=lambda kv: (kv[1], kv[0]))
    return items[:limit]


def reserve_praise_week(week, open_id):
    """占一个「这个周已经给这个人发过点赞汇总」的位。True = 这次该我发。

    不复用 reserve_notified：那份文件的键是「事件 + 对象」，点赞周汇总却是
    「每个收件人每周一条」——用它的格式，文件会以 人数×周数 的速度长下去，
    而每占一次位都要整份重写。这里只留最近一个周，换周时整份清掉，见
    PRAISE_WEEK_FILE 的注释。
    """
    acquired = [False]

    def _m(data):
        if data.get("week") != week:
            data.clear()
            data.update({"week": week, "sent": []})
        sent = data.setdefault("sent", [])
        if open_id in sent:
            return None                     # 已发过，放弃写入
        sent.append(open_id)
        acquired[0] = True
        return data

    storage.update_json(PRAISE_WEEK_FILE, {}, _m)
    return acquired[0]


def unreserve_praise_week(week, open_id):
    """发送失败时退回占位，让下一轮重试。周对不上就不动——那是上一周的事。"""
    def _m(data):
        if data.get("week") != week:
            return None
        sent = data.get("sent", [])
        if open_id in sent:
            sent.remove(open_id)
            return data
        return None

    storage.update_json(PRAISE_WEEK_FILE, {}, _m)


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


# load_heart_bonus / add_heart_bonus 已删除：管理员加赠先改成直接填用户表字段，
# 2026-10 起那个「管理员加赠」字段也整个下线删除了（永久名额只剩麦穗兑换）。


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
