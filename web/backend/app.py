"""一线牵 H5 后端服务 - Flask"""
import io
import json
import logging
import os
import random
import re
import sys
import threading
import time
from datetime import datetime

import requests
from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    make_response,
    request,
    send_from_directory,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from PIL import Image, ImageOps

# 共享库 lib/ 位于仓库根目录（web/backend 的上两级）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
import bitable
from config import *

from lib import storage

app = Flask(__name__, static_folder="../frontend", static_url_path="")

# 签名会话序列化器：open_id 直接存进签名 cookie，服务重启也不丢失，免去重复登录
_session_signer = URLSafeTimedSerializer(FEISHU_APP_SECRET, salt="yxq-session")

# ========== 简单内存缓存 ==========
import time as _time

_cache = {}
def cache_get(key, ttl=60):
    item = _cache.get(key)
    if item and _time.time() - item[0] < ttl:
        return item[1]
    return None
def cache_set(key, val):
    _cache[key] = (_time.time(), val)
def cache_clear(key_prefix=None):
    if key_prefix:
        for k in list(_cache.keys()):
            if k.startswith(key_prefix):
                _cache.pop(k, None)
    else:
        _cache.clear()

def find_activity(act_id):
    """通过 record_id 或 活动ID 查找活动"""
    if act_id.startswith('rec'):
        rec = bitable.get_record(ACTIVITY_TABLE_ID, act_id)
        if rec:
            return rec
    cached = cache_get('act_' + act_id, ttl=120)
    if cached:
        return cached
    all_acts = cache_get('all_activities', ttl=60)
    if not all_acts:
        all_acts = bitable.search_records(ACTIVITY_TABLE_ID)
        cache_set('all_activities', all_acts)
    for a in all_acts:
        if a['record_id'] == act_id:
            cache_set('act_' + act_id, a)
            return a
        aid = bitable.get_field_text(a.get('fields', {}), F_ACTIVITY_ID)
        if aid == act_id:
            cache_set('act_' + act_id, a)
            return a
    return None

def resolve_activity(act_id):
    """解析活动ID参数，返回 (activity_record, text_activity_id) 或 (None, None)"""
    rec = find_activity(act_id)
    if not rec:
        return None, None
    text_id = bitable.get_field_text(rec.get('fields', {}), F_ACTIVITY_ID)
    return rec, text_id


# ========== 本地快照缓存（后台定时把飞书数据刷进内存，读操作走内存，写操作后定向刷新） ==========
SNAPSHOT_REFRESH_INTERVAL = 60  # 秒（曾为15s，高频轮询易触发飞书限流/超时，导致登录卡顿）

_snapshot = {
    "users": [], "activities": [], "signups": [],
    "likes": [], "group_selections": [], "group_results": [],
}
_snapshot_lock = threading.RLock()

_SNAPSHOT_FETCHERS = {
    "users": lambda: bitable.search_records(USER_TABLE_ID),
    "activities": lambda: bitable.search_records(ACTIVITY_TABLE_ID),
    "signups": lambda: bitable.search_records(SIGNUP_TABLE_ID),
    "likes": lambda: bitable.search_records(LIKE_TABLE_ID),
    "group_selections": lambda: bitable.search_records(GROUP_SELECT_TABLE),
    "group_results": lambda: bitable.search_records(GROUP_RESULT_TABLE),
}


def refresh_snapshot_table(key):
    """只刷新快照中的单个表（写操作后调用，保证读到的数据最新）"""
    fetcher = _SNAPSHOT_FETCHERS.get(key)
    if not fetcher:
        return
    try:
        data = fetcher()
        with _snapshot_lock:
            _snapshot[key] = data
    except Exception as e:
        logging.getLogger(__name__).warning(f"刷新快照表 {key} 失败: {e}")


def refresh_snapshot_table_async(key):
    """后台线程异步刷新单个快照表（写接口回包后调用，避免同步刷新拖慢写请求）"""
    try:
        threading.Thread(target=refresh_snapshot_table, args=(key,), daemon=True).start()
    except Exception as e:
        logging.getLogger(__name__).warning(f"异步刷新快照表 {key} 失败: {e}")


def refresh_snapshot():
    """一次性把飞书各表全量刷入内存快照"""
    for key in _SNAPSHOT_FETCHERS:
        refresh_snapshot_table(key)


def _snapshot_loop():
    while True:
        try:
            refresh_snapshot()
            warm_image_cache()
        except Exception as e:
            logging.getLogger(__name__).warning(f"刷新本地快照失败: {e}")
        time.sleep(SNAPSHOT_REFRESH_INTERVAL)


def start_snapshot_loop():
    threading.Thread(target=_snapshot_loop, daemon=True, name="snapshot-refresh").start()


def _snap(key):
    with _snapshot_lock:
        return list(_snapshot.get(key, []))


# ---- 快照读取辅助（镜像 bitable 常用查询；快照为空时回退到飞书，保证启动初期可用） ----

def _pick_primary_user(records):
    """同一 open_id 多条记录时取主档案：活跃优先，其次用户ID最小（与 bot/queries 同规则）"""
    if not records:
        return None

    def rank(u):
        uf = u.get("fields", {})
        st = bitable.get_select_value(uf, F_ACCOUNT_STATUS)
        m = re.match(r"[Uu]-?(\d+)", bitable.get_field_text(uf, F_USER_ID))
        uid_num = int(m.group(1)) if m else 10 ** 9
        return (0 if st == "活跃" else 1, uid_num)

    return sorted(records, key=rank)[0]


# ========== v6：事件溯源爱心态 + 提交管线（spool 幂等异步写入） ==========
import uuid as _uuid

_spool_lock = threading.Lock()
_spool_queue = []
_SPOOL_FILE = os.path.join(SHARED_DATA_DIR, "yixianqian_spool.jsonl")
_SPOOL_DEAD = os.path.join(SHARED_DATA_DIR, "yixianqian_spool_failed.log")
_BALANCE_FILE = os.path.join(SHARED_DATA_DIR, "yixianqian_balances.json")

# 在途意图（进程级，页面重载不丢失）：
#   _intent_likes    oid -> [(temp_key, ts)]  喜欢已受理、尚未在快照可见（TTL 20s，快照15s周期+余量）
#   _intent_cancels  oid -> [(record_id, ts)] 取消已受理、快照可能仍显示活跃（TTL 60s）
_intent_likes = {}    # temp_key -> {"oid":…, "target":…, "ts":…}
_intent_cancels = {}  # oid -> [(target_openid, ts)]


def _intent_prune():
    now = time.time()
    for k in list(_intent_likes):
        if now - _intent_likes[k]["ts"] > 60:
            _intent_likes.pop(k, None)
    for k in list(_intent_cancels):
        _intent_cancels[k] = [(a, b) for a, b in _intent_cancels[k] if now - b < 60]


def _intent_complete(temp_key):
    """worker 落库成功后消费该意图，避免与快照计数双算"""
    _intent_likes.pop(temp_key, None)


def _balances_file():
    """机器人对账循环写入的邀请奖励汇总（事件溯源中邀请是外部事件，由机器人发布）"""
    try:
        return storage.load_json(_BALANCE_FILE, {"invites": {}}) or {"invites": {}}
    except Exception:
        return {"invites": {}}


def computed_hearts(open_id):
    """爱心数 = 初始 + 邀请奖励 − 有效喜欢数（快照 + 在途意图修正）。
    纯计算，零表读、零延迟、零竞争；对自己操作 0 秒精确。"""
    _intent_prune()
    cancel_targets = {t for t, _ in _intent_cancels.get(open_id, [])}
    cnt = 0
    for l in _snap("likes"):
        f = l.get("fields", {})
        if bitable.get_field_text(f, F_LIKE_INITIATOR_OPENID) != open_id:
            continue
        if bitable.get_select_value(f, F_LIKE_STATUS) == "已取消":
            continue
        if bitable.get_field_text(f, F_LIKE_TARGET_OPENID) in cancel_targets:
            continue
        cnt += 1
    snap_rids = set()
    for l in _snap("likes"):
        if l.get("record_id"):
            snap_rids.add(l.get("record_id"))
    extra = 0
    for it in _intent_likes.values():
        if it["oid"] != open_id:
            continue
        rid = it.get("rid")
        if rid is None or rid not in snap_rids:
            extra += 1  # 未落库 / 已落库但快照未见：均需桥接计数
    invites = _balances_file().get("invites", {}).get(open_id, 0)
    return max(0, min(MAX_HEARTS, INITIAL_HEARTS + invites - cnt - extra))


def _pick_primary_user(records):
    """同一 open_id 多条记录时取主档案：活跃优先，其次用户ID最小（与 bot/queries 同规则）"""
    if not records:
        return None

    def rank(u):
        uf = u.get("fields", {})
        st = bitable.get_select_value(uf, F_ACCOUNT_STATUS)
        m = re.match(r"[Uu]-?(\d+)", bitable.get_field_text(uf, F_USER_ID))
        uid_num = int(m.group(1)) if m else 10 ** 9
        return (0 if st == "活跃" else 1, uid_num)

    return sorted(records, key=rank)[0]


def live_primary_hearts(open_id):
    """实时直查主档案余额，并叠加进程内在途口径：
    + 退款信用（已取消、对账尚未写回）
    − 预留占用（已提交、机器人尚未扣减）
    用于首页/我的等低频读点，保证显示即时且与最终真值收敛"""
    recs = bitable.search_records(USER_TABLE_ID, [
        {"field_name": F_FEISHU_ID, "operator": "is", "value": [open_id]}])
    rec = _pick_primary_user(recs)
    if not rec:
        return None
    stored = bitable.get_field_number(rec.get("fields", {}), F_HEART_REMAIN, INITIAL_HEARTS)
    return max(0, min(MAX_HEARTS, stored))


def snap_find_user_by_openid(open_id):
    """快照优先、实时兜底；同号多档统一解析到主档案"""
    users = _snap("users")
    matches = [u for u in users
               if bitable.get_field_text(u.get("fields", {}), F_FEISHU_ID) == open_id]
    if matches:
        return _pick_primary_user(matches)
    # 快照为空或未命中（如快照在用户写入前加载、尚未刷新）：回退到飞书直查，避免误判「用户不存在」
    recs = bitable.search_records(USER_TABLE_ID, [
        {"field_name": F_FEISHU_ID, "operator": "is", "value": [open_id]}])
    return _pick_primary_user(recs)


def snap_active_users():
    users = _snap("users")
    if not users:
        return bitable.get_all_users()
    return [u for u in users
            if bitable.get_select_value(u.get("fields", {}), F_ACCOUNT_STATUS) == "活跃"]


def snap_find_activity(act_id):
    activities = _snap("activities")
    if not activities:
        return find_activity(act_id)
    for a in activities:
        if a.get("record_id") == act_id:
            return a
        if bitable.get_field_text(a.get("fields", {}), F_ACTIVITY_ID) == act_id:
            return a
    return None


def snap_resolve_activity(act_id):
    rec = snap_find_activity(act_id)
    if not rec:
        return None, None
    return rec, bitable.get_field_text(rec.get("fields", {}), F_ACTIVITY_ID)


def snap_all_activities():
    activities = _snap("activities")
    if not activities:
        return bitable.get_activities()
    return activities


def snap_likes_by_target(open_id):
    likes = _snap("likes")
    if not likes:
        return bitable.search_records(LIKE_TABLE_ID, [
            {"field_name": F_LIKE_TARGET_OPENID, "operator": "is", "value": [open_id]}])
    return [l for l in likes
            if bitable.get_field_text(l.get("fields", {}), F_LIKE_TARGET_OPENID) == open_id]


def snap_likes_by_initiator(open_id):
    likes = _snap("likes")
    if not likes:
        return bitable.search_records(LIKE_TABLE_ID, [
            {"field_name": F_LIKE_INITIATOR_OPENID, "operator": "is", "value": [open_id]}])
    return [l for l in likes
            if bitable.get_field_text(l.get("fields", {}), F_LIKE_INITIATOR_OPENID) == open_id]


def snap_signups_by_openid(open_id):
    signups = _snap("signups")
    if not signups:
        return bitable.search_records(SIGNUP_TABLE_ID, [
            {"field_name": F_SIGNUP_OPENID, "operator": "is", "value": [open_id]}])
    return [s for s in signups
            if bitable.get_field_text(s.get("fields", {}), F_SIGNUP_OPENID) == open_id]


def snap_signups_by_activity(act_id):
    signups = _snap("signups")
    if not signups:
        return bitable.get_signups(act_id)
    return [s for s in signups
            if bitable.get_field_text(s.get("fields", {}), F_SIGNUP_ACTIVITY_ID) == act_id]


def snap_signup(act_id, open_id):
    signups = _snap("signups")
    if not signups:
        return bitable.get_user_signup(act_id, open_id)
    for s in signups:
        f = s.get("fields", {})
        if bitable.get_field_text(f, F_SIGNUP_ACTIVITY_ID) == act_id and bitable.get_field_text(f, F_SIGNUP_OPENID) == open_id:
            return s
    return None


def snap_group_selections_by_selector(open_id):
    gs = _snap("group_selections")
    if not gs:
        return bitable.search_records(GROUP_SELECT_TABLE, [
            {"field_name": F_GS_SELECTOR_OID, "operator": "is", "value": [open_id]}])
    return [g for g in gs
            if bitable.get_field_text(g.get("fields", {}), F_GS_SELECTOR_OID) == open_id]


def snap_group_selection(act_id, open_id):
    gs = _snap("group_selections")
    if not gs:
        return bitable.get_user_group_selection(act_id, open_id)
    for g in gs:
        f = g.get("fields", {})
        if bitable.get_field_text(f, F_GS_ACTIVITY_ID) == act_id and bitable.get_field_text(f, F_GS_SELECTOR_OID) == open_id:
            return g
    return None


def snap_group_results(act_id):
    gr = _snap("group_results")
    if not gr:
        return bitable.get_group_results(act_id)
    return [r for r in gr
            if bitable.get_field_text(r.get("fields", {}), F_GR_ACTIVITY_ID) == act_id]


# ========== Session 管理 ==========
# 跨进程文件锁（替代 threading.Lock，gunicorn 多 worker 下有效）
try:
    import fcntl
except ImportError:
    fcntl = None
_file_lock_path = os.path.join(SHARED_DATA_DIR, ".app.lock")
_thread_locks = {}
_locks_guard = threading.Lock()
def _thread_lock(path):
    with _locks_guard:
        if path not in _thread_locks:
            _thread_locks[path] = threading.Lock()
        return _thread_locks[path]
class _AppLock:
    def __enter__(self):
        tl = _thread_lock(_file_lock_path)
        tl.acquire()
        self._tl = tl
        self._f = open(_file_lock_path + ".lock", "a+")
        if fcntl:
            fcntl.flock(self._f.fileno(), fcntl.LOCK_EX)
        return self
    def __exit__(self, *exc):
        try:
            if fcntl:
                fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                self._f.close()
            except Exception:
                pass
            self._tl.release()
file_lock = _AppLock()


def create_session(open_id, role="user"):
    """创建会话：{open_id, role} 签名后写入 cookie（无服务端状态，重启不丢失）"""
    return _session_signer.dumps({"open_id": open_id, "role": role})


def get_session():
    """从cookie读取并校验当前会话。返回 {"open_id","role"} 或 None。
    兼容旧 cookie（旧值为纯 open_id 字符串，补成 user 角色）。"""
    token = request.cookies.get("yxq_session")
    if not token:
        return None
    try:
        data = _session_signer.loads(token, max_age=SESSION_EXPIRE_DAYS * 86400)
    except (BadSignature, SignatureExpired):
        return None
    if isinstance(data, str):
        return {"open_id": data, "role": "user"}
    if isinstance(data, dict):
        return {"open_id": data.get("open_id"), "role": data.get("role", "user")}
    return None


@app.before_request
def _load_session_into_g():
    """每个请求先把会话解到 g，供 require_login/snap_self_user 统一取用"""
    sess = get_session()
    g.yxq_open_id = sess["open_id"] if sess else None
    g.yxq_role = sess["role"] if sess else "user"


def require_login():
    """要求登录，返回open_id或None"""
    return g.yxq_open_id


def roles_of(open_id):
    """返回该 open_id 拥有的角色集合 {"user","observer"}（快照优先、实时兜底）"""
    users = _snap("users")
    matches = [u for u in users
               if bitable.get_field_text(u.get("fields", {}), F_FEISHU_ID) == open_id]
    if not matches:
        matches = bitable.search_records(USER_TABLE_ID, [
            {"field_name": F_FEISHU_ID, "operator": "is", "value": [open_id]}])
    roles = set()
    for u in matches:
        if bitable.get_select_value(u.get("fields", {}), F_ACCOUNT_STATUS) == STATUS_OBSERVER:
            roles.add("observer")
        else:
            roles.add("user")
    return roles


def snap_self_user():
    """按当前会话 role 解析「本人」档案：
    observer → 状态为观察员的记录；user → 非观察员主档（活跃优先）。"""
    open_id = g.yxq_open_id
    if not open_id:
        return None
    users = _snap("users")
    matches = [u for u in users
               if bitable.get_field_text(u.get("fields", {}), F_FEISHU_ID) == open_id]
    if not matches:
        matches = bitable.search_records(USER_TABLE_ID, [
            {"field_name": F_FEISHU_ID, "operator": "is", "value": [open_id]}])
    if not matches:
        return None
    if g.yxq_role == "observer":
        obs = [u for u in matches
               if bitable.get_select_value(u.get("fields", {}), F_ACCOUNT_STATUS) == STATUS_OBSERVER]
        return (obs or matches)[0]
    non_obs = [u for u in matches
               if bitable.get_select_value(u.get("fields", {}), F_ACCOUNT_STATUS) != STATUS_OBSERVER]
    return _pick_primary_user(non_obs or matches)


# ========== 账号状态门禁 ==========
# 三级权限：
#   活跃        → 全功能
#   已隐藏(自隐) → 可浏览、可取消喜欢/取消报名；不可点爱心、不可报名、不可提交志愿
#   待审核/已拒绝/已退出 → 仅可登录与查看「我的资料」，内容与操作全拦
GATE_MESSAGES = {
    "待审核": {"error": "资料审核中，通过后即可使用一线牵App", "gate": "待审核"},
    "已拒绝": {"error": "很抱歉，你的资料未通过审核，如有疑问请联系管理员", "gate": "已拒绝"},
    "已退出": {"error": "你已暂时退出相亲市场，如需恢复请联系管理员", "gate": "已退出"},
}
LIKE_BLOCKED_MESSAGE = {"error": "你当前处于隐藏状态（他人看不到你），请先在「我的」页恢复活跃后再操作", "gate": "已隐藏"}
OBSERVER_BLOCKED_MESSAGE = {"error": "你是观察员账号，无此操作权限", "gate": "观察员"}


def _account_status(open_id):
    user = snap_self_user()
    if not user:
        return None, None
    return bitable.get_select_value(user.get("fields", {}), F_ACCOUNT_STATUS), user


def account_gate(open_id):
    """浏览级门禁：待审核/已拒绝/已退出 全拦。返回 None 放行；否则 (body, status)"""
    status, _ = _account_status(open_id)
    if status is None:
        return {"error": "用户不存在"}, 404
    body = GATE_MESSAGES.get(status)
    if body:
        return body, 403
    return None


def active_gate(open_id):
    """操作级门禁：仅「活跃」可用（点爱心/报名/提交志愿）。"""
    status, _ = _account_status(open_id)
    if status is None:
        return {"error": "用户不存在"}, 404
    if status == "已隐藏":
        return LIKE_BLOCKED_MESSAGE, 403
    if status == STATUS_OBSERVER:
        return OBSERVER_BLOCKED_MESSAGE, 403
    body = GATE_MESSAGES.get(status)
    if body:
        return body, 403
    return None


def _is_observer(user):
    """判断用户是否为观察员（非单身看热闹，限权）"""
    return bitable.get_select_value(user.get("fields", {}), F_ACCOUNT_STATUS) == STATUS_OBSERVER


# ========== 飞书消息 ==========

def send_text_message(receive_id, text):
    """发送飞书文本消息"""
    token = bitable.get_token()
    if not token:
        return False
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"receive_id": receive_id, "msg_type": "text", "content": json.dumps({"text": text})}
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=15)
        return resp.json().get("code") == 0
    except Exception:
        return False


def send_card_message(receive_id, card_content):
    """发送飞书卡片消息"""
    token = bitable.get_token()
    if not token:
        return False
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"receive_id": receive_id, "msg_type": "interactive", "content": json.dumps(card_content, ensure_ascii=False)}
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=15)
        result = resp.json()
        if result.get("code") == 0:
            return result.get("data", {}).get("message_id", True)
    except Exception:
        pass
    return False


def send_user_card(receive_id, share_open_id):
    """发送个人名片"""
    token = bitable.get_token()
    if not token:
        return False
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"receive_id": receive_id, "msg_type": "share_user",
            "content": json.dumps({"user_id": share_open_id})}
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=15)
        return resp.json().get("code") == 0
    except Exception:
        return False


# ========== 用户信息格式化 ==========

def format_birthday(fields):
    """生日字段格式化为「年-月」，如 98-6 / 01-6（年份保留两位，不足补0）。

    生日现在为 DateTime 手动字段（用户自行填写日期）；读取时间戳后
    输出「yy-m」两位年份。
    """
    raw = bitable.get_datetime_value(fields, F_BIRTHDAY)  # "YYYY-MM-DD"
    if not raw:
        return ""
    m = re.match(r"^(\d{4})-(\d{1,2})", raw)
    if not m:
        return raw
    year, month = int(m.group(1)), int(m.group(2))
    return f"{year % 100:02d}-{month}"


def format_user_brief(record, include_openid=False, full=False):
    """格式化用户信息（卡片展示用，不含敏感信息）"""
    fields = record.get("fields", {})
    photos = bitable.get_attachment_tokens(fields, F_PHOTO)
    photo_url = "/api/image/" + photos[0] if photos else ""
    data = {
        "user_id": bitable.get_field_text(fields, F_USER_ID),
        "nickname": bitable.get_field_text(fields, F_NICKNAME),
        "gender": bitable.get_select_value(fields, F_GENDER),
        "height": int(bitable.get_field_number(fields, F_HEIGHT, 0)) or "",
        "education": bitable.get_select_value(fields, F_EDUCATION),
        "hobbies": "、".join(bitable.get_multi_select_value(fields, F_SELF_HOBBIES)),
        "hearts": bitable.get_field_number(fields, F_HEART_REMAIN, INITIAL_HEARTS),
        "account_status": bitable.get_select_value(fields, F_ACCOUNT_STATUS),
        "is_observer": bitable.get_select_value(fields, F_ACCOUNT_STATUS) == STATUS_OBSERVER,
        "photo": photo_url,
        "record_id": record.get("record_id")
    }
    if full:
        data.update({
            "baptismal_name": bitable.get_field_text(fields, F_BAPTISMAL_NAME),
            "church": bitable.get_field_text(fields, F_CHURCH),
            "group": bitable.get_field_text(fields, F_GROUP),
            "church_location": bitable.get_field_text(fields, F_CHURCH_LOCATION),
            "city": bitable.get_field_text(fields, F_CITY),
            "native_place": bitable.get_field_text(fields, F_NATIVE_PLACE),
            "industry": bitable.get_field_text(fields, F_INDUSTRY),
            "position": bitable.get_field_text(fields, F_POSITION),
            "income": bitable.get_select_value(fields, F_INCOME),
            "personality": bitable.get_field_text(fields, F_PERSONALITY),
            "self_traits": "、".join(bitable.get_multi_select_value(fields, F_SELF_TRAITS)),
            "self_sports": "、".join(bitable.get_multi_select_value(fields, F_SELF_SPORTS)),
            "mbti": "、".join(bitable.get_multi_select_value(fields, F_MBTI)),
            "partner_criteria": bitable.get_field_text(fields, F_PARTNER_CRITERIA),
            "partner_traits": "、".join(bitable.get_multi_select_value(fields, F_PARTNER_TRAITS)),
            "partner_hobbies": "、".join(bitable.get_multi_select_value(fields, F_PARTNER_HOBBIES)),
            "partner_sports": "、".join(bitable.get_multi_select_value(fields, F_PARTNER_SPORTS)),
            "marriage": bitable.get_select_value(fields, F_MARRIAGE),
            "house": bitable.get_field_text(fields, F_HOUSE),
            "driving": bitable.get_field_text(fields, F_DRIVING),
            "family": bitable.get_field_text(fields, F_FAMILY),
            "live_with_parents": bitable.get_select_value(fields, F_LIVE_WITH_PARENTS),
            "birthday": format_birthday(fields),
            "photos": ["/api/image/" + t for t in photos],
        })
    if include_openid:
        data["openid"] = bitable.get_field_text(fields, F_FEISHU_ID)
    return data


def format_user_profile(record):
    """格式化用户完整资料（本人查看，含敏感信息）"""
    fields = record.get("fields", {})
    data = format_user_brief(record, include_openid=True, full=True)
    data.update({
        "real_name": bitable.get_field_text(fields, F_REAL_NAME),
        "wechat": bitable.get_field_text(fields, F_WECHAT),
        "phone": bitable.get_phone_value(fields, F_PHONE),
        "register_time": bitable.get_date_value(fields, F_REGISTER_TIME),
    })
    return data


def format_activity(record):
    """格式化活动信息"""
    fields = record.get("fields", {})
    posters = bitable.get_attachment_tokens(fields, F_ACTIVITY_POSTER)
    poster_url = "/api/image/" + posters[0] if posters else ""
    return {
        "activity_id": bitable.get_field_text(fields, F_ACTIVITY_ID),
        "name": bitable.get_field_text(fields, F_ACTIVITY_NAME),
        "description": bitable.get_field_text(fields, F_ACTIVITY_DESC),
        "location": bitable.get_field_text(fields, F_ACTIVITY_LOCATION),
        "condition": bitable.get_field_text(fields, F_ACTIVITY_CONDITION),
        "max_signup": int(bitable.get_field_number(fields, F_ACTIVITY_MAX_SIGNUP, 0)),
        "current_signup": int(bitable.get_field_number(fields, F_ACTIVITY_CURRENT_SIGNUP, 0)),
        "fee": bitable.get_field_number(fields, F_ACTIVITY_FEE, 0),
        "food": bitable.get_field_text(fields, F_ACTIVITY_FOOD),
        "status": bitable.get_select_value(fields, F_ACTIVITY_STATUS),
        "poster": poster_url,
        "group_status": bitable.get_select_value(fields, F_ACTIVITY_GROUP_STATUS),
        "male_per_group": int(bitable.get_field_number(fields, F_ACTIVITY_MALE_PER_GROUP, 0)),
        "female_per_group": int(bitable.get_field_number(fields, F_ACTIVITY_FEMALE_PER_GROUP, 0)),
        "start_time": fields.get(F_ACTIVITY_START_TIME),
        "end_time": fields.get(F_ACTIVITY_END_TIME),
        "record_id": record.get("record_id")
    }


# ========== 卡片动态字段 ==========
def _field_value(fields, fname, ftype):
    """按类型读取字段值，返回字符串（空则返回 ''）"""
    if ftype == "text":
        return bitable.get_field_text(fields, fname)
    if ftype == "select":
        return bitable.get_select_value(fields, fname)
    if ftype == "multi":
        return "、".join(bitable.get_multi_select_value(fields, fname))
    if ftype == "number":
        n = bitable.get_field_number(fields, fname)
        return "" if (n is None or n == 0) else (str(int(n)) if float(n) == int(n) else str(n))
    if ftype == "birthday":
        return format_birthday(fields)
    return ""


def build_display_fields(fields):
    """生成展示字段：简洁行（圣名·生日·身高·学历）+ 分组字段（基本信息/工作与经济/关于我/理想中的TA）"""
    # 简洁行：圣名 · 生日 · 身高 · 学历（不标注字段名）
    simple = ""
    simple_parts = [p for p in (_field_value(fields, fname, ftype) for fname, ftype in SIMPLE_FIELDS) if p]
    if simple_parts:
        simple = " · ".join(simple_parts)
    # 分组字段（标注字段名）
    sections = []
    for title, icon, items in CARD_SECTIONS:
        section_fields = []
        for label, fname, ftype in items:
            val = _field_value(fields, fname, ftype)
            if val:
                section_fields.append({"label": label, "value": str(val)})
        if section_fields:
            sections.append({"title": title, "icon": icon, "fields": section_fields})
    return {"simple": simple, "sections": sections}


def build_subtitle(fields):
    """卡片照片叠层副标题：生日 · 城市"""
    parts = []
    birthday = format_birthday(fields)
    if birthday:
        parts.append(birthday)
    city = bitable.get_field_text(fields, F_CITY)
    if city:
        parts.append(city)
    return " · ".join(parts)


def order_cards_likes_first(cards, liked_me_openids):
    """把「喜欢我的异性」卡片靠前，但前 10 个位置随机混排。

    匿名喜欢不能让用户一翻牌就猜出「第一个/前几个就是喜欢我的人」，否则会暴露是谁喜欢、
    破坏匿名体面。所以把喜欢我的人推进前 10 个位置里，和普通卡片随机打乱。
    """
    if not liked_me_openids:
        return cards
    liked = [c for c in cards if c.get("openid") in liked_me_openids]
    others = [c for c in cards if c.get("openid") not in liked_me_openids]
    random.shuffle(others)
    # 前 10 个位置：喜欢我的人 + 若干普通卡片随机混排
    front = liked + others[:max(0, 10 - len(liked))]
    random.shuffle(front)
    return front + others[max(0, 10 - len(liked)):]


def pass_card_filters(fields, f):
    """卡片筛选：全部条件满足才返回 True"""
    # 身高（范围：最低/最高可独立选填；未填身高的用户不参与身高筛选）
    h = bitable.get_field_number(fields, F_HEIGHT)
    if f.get("height_min") is not None or f.get("height_max") is not None:
        if h <= 0:
            return False
        if f.get("height_min") is not None and h < f["height_min"]:
            return False
        if f.get("height_max") is not None and h > f["height_max"]:
            return False
    # 出生年月区间（兼容旧参数 birth_min/birth_max，格式 YYYY-MM）
    bmin = f.get("birth_min")
    bmax = f.get("birth_max")
    if bmin or bmax:
        bday = bitable.get_datetime_value(fields, F_BIRTHDAY)  # "YYYY-MM-DD" or ""
        if not bday:
            return False
        if bmin and bday < bmin + "-01":
            return False
        if bmax and bday > bmax + "-31":
            return False
    # 年龄区间（age_min/age_max，单位：岁）
    age_min = f.get("age_min")
    age_max = f.get("age_max")
    if age_min is not None or age_max is not None:
        bday = bitable.get_datetime_value(fields, F_BIRTHDAY)  # "YYYY-MM-DD" or ""
        if not bday:
            return False
        try:
            by, bm, bd = int(bday[:4]), int(bday[5:7]), int(bday[8:10])
            today = datetime.now()
            age = today.year - by - ((today.month, today.day) < (bm, bd))
            if age_min is not None and age < age_min:
                return False
            if age_max is not None and age > age_max:
                return False
        except (ValueError, IndexError):
            return False
    # 多选匹配（列表为空则不过滤）
    if f.get("education") and bitable.get_select_value(fields, F_EDUCATION) not in f["education"]:
        return False
    if f.get("income") and bitable.get_select_value(fields, F_INCOME) not in f["income"]:
        return False
    # 文本包含匹配
    for key, fname in [
        ("user_id", F_USER_ID),
        ("church", F_CHURCH), ("native_place", F_NATIVE_PLACE), ("city", F_CITY),
        ("industry", F_INDUSTRY), ("house", F_HOUSE), ("driving", F_DRIVING),
    ]:
        if f.get(key) and f[key] not in bitable.get_field_text(fields, fname):
            return False
    return True


# ========== 页面路由 ==========

# index.html 里的 __FEISHU_APP_ID__ 占位符在服务时替换为当前环境 app_id（生产/测试各自不同）
_INDEX_HTML_CACHE = None


def _render_index():
    """读取单文件前端并注入飞书 app_id，返回不缓存的 HTML 响应。"""
    global _INDEX_HTML_CACHE
    if _INDEX_HTML_CACHE is None:
        with open(os.path.join(app.static_folder, "index.html"), "r", encoding="utf-8") as f:
            _INDEX_HTML_CACHE = f.read()
    resp = make_response(_INDEX_HTML_CACHE.replace("__FEISHU_APP_ID__", FEISHU_APP_ID))
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/")
def index():
    return _render_index()


# public.html 里的 __PUBLIC_QR_CODE__ 占位符在服务时替换为当前环境二维码（生产/测试各自不同）
_PUBLIC_HTML_CACHE = None


def _render_public():
    """读取 public.html 并注入当前环境二维码文件名，返回不缓存的 HTML 响应。"""
    global _PUBLIC_HTML_CACHE
    if _PUBLIC_HTML_CACHE is None:
        with open(os.path.join(app.static_folder, "public.html"), "r", encoding="utf-8") as f:
            _PUBLIC_HTML_CACHE = f.read()
    resp = make_response(_PUBLIC_HTML_CACHE.replace("__PUBLIC_QR_CODE__", PUBLIC_QR_CODE))
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/public.html")
def public_page():
    return _render_public()


@app.route("/<path:path>")
def static_files(path):
    # /api/ 开头的请求不应由 catch-all 接管，统一交给 API 路由与错误处理器处理
    if path.startswith("api") or path == "api":
        abort(404)
    if os.path.exists(os.path.join(app.static_folder, path)):
        resp = send_from_directory(app.static_folder, path)
    else:
        resp = _render_index()
    # HTML 一律不缓存（避免浏览器/飞书缓存旧版单文件应用，过滤项缺失等）
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


# ========== 全局错误处理器（仅对 /api/ 路由返回 JSON） ==========

@app.errorhandler(404)
def handle_404(error):
    if request.path.startswith("/api"):
        return jsonify({"error": "请求的资源不存在"}), 404
    return error


@app.errorhandler(500)
def handle_500(error):
    app.logger.error(f"服务器内部错误: {error}")
    if request.path.startswith("/api"):
        return jsonify({"error": "服务器内部错误，请稍后重试"}), 500
    return error


# ========== 图片代理（带磁盘缓存） ==========

IMAGE_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "image_cache")
os.makedirs(IMAGE_CACHE_DIR, exist_ok=True)


def cleanup_image_cache(max_age_days=7, max_files=1000):
    """过期与超量清理：删 7 天前文件，超 1000 个时删最旧的（后台线程每日执行）"""
    try:
        files = [(os.path.join(IMAGE_CACHE_DIR, f), os.path.getmtime(os.path.join(IMAGE_CACHE_DIR, f)))
                 for f in os.listdir(IMAGE_CACHE_DIR)]
        cutoff = time.time() - max_age_days * 86400
        for path, mtime in list(files):
            if mtime < cutoff:
                try:
                    os.remove(path)
                except Exception:
                    pass
        files = [(p, m) for p, m in files if os.path.exists(p)]
        if len(files) > max_files:
            files.sort(key=lambda x: x[1])
            for path, _ in files[:len(files) - max_files]:
                try:
                    os.remove(path)
                except Exception:
                    pass
    except Exception:
        pass


cleanup_image_cache()
def _image_cleanup_loop():
    while True:
        time.sleep(86400)
        cleanup_image_cache()
threading.Thread(target=_image_cleanup_loop, daemon=True).start()


def _get_cached_image(file_token):
    """检查磁盘缓存，返回 (path, content_type) 或 None"""
    for fname in os.listdir(IMAGE_CACHE_DIR):
        if fname.startswith(file_token + "."):
            fpath = os.path.join(IMAGE_CACHE_DIR, fname)
            ext = fname.rsplit(".", 1)[-1].lower()
            ct_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                      "gif": "image/gif", "webp": "image/webp", "heic": "image/heic"}
            return fpath, ct_map.get(ext, "image/jpeg")
    return None


def _compress_image(image_bytes, ext):
    """用 Pillow 压缩图片，返回压缩后的字节。
    - 最大宽度 1080px，等比缩放，不放大
    - JPEG 质量 85，PNG 优化
    - 保留原始格式（jpg/png/webp）
    - GIF / HEIC 不做处理，直接返回原字节
    """
    if ext in ("gif", "heic"):
        return image_bytes
    fmt_map = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}
    fmt = fmt_map.get(ext, "JPEG")
    try:
        img = Image.open(io.BytesIO(image_bytes))
        # 按 EXIF 方向校正，避免手机拍照图片旋转
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        # 等比缩放到最大宽度 1080px（不放大）
        if img.width > 1080:
            ratio = 1080.0 / img.width
            new_h = int(img.height * ratio)
            img = img.resize((1080, new_h), Image.LANCZOS)
        save_params = {}
        if fmt == "JPEG":
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            save_params = {"quality": 85, "optimize": True}
        elif fmt == "PNG":
            save_params = {"optimize": True}
        out = io.BytesIO()
        img.save(out, format=fmt, **save_params)
        return out.getvalue()
    except Exception:
        return image_bytes


# ========== 图片缓存预热（后台把快照里的照片/海报提前下载到磁盘，避免首屏等待） ==========
_image_warm_lock = threading.Lock()


def _download_and_cache_image(file_token):
    """下载并缓存单张图片（已缓存则跳过），返回是否新下载"""
    if _get_cached_image(file_token):
        return False
    token = bitable.get_token()
    if not token:
        return False
    url = "https://open.feishu.cn/open-apis/drive/v1/medias/%s/download" % file_token
    headers = {"Authorization": "Bearer " + token}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return False
        content_type = resp.headers.get("Content-Type", "image/jpeg")
        ext_map = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
                   "image/webp": "webp", "image/heic": "heic"}
        ext = ext_map.get(content_type, "jpg")
        compressed = _compress_image(resp.content, ext)
        cache_path = os.path.join(IMAGE_CACHE_DIR, "%s.%s" % (file_token, ext))
        tmp = cache_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(compressed)
        os.replace(tmp, cache_path)  # 原子替换，避免与请求线程并发写坏
        return True
    except Exception:
        return False


def warm_image_cache():
    """后台预热：把快照中所有用户照片、活动海报下载到磁盘缓存（已缓存自动跳过）"""
    tokens = set()
    for u in _snap("users"):
        tokens.update(bitable.get_attachment_tokens(u.get("fields", {}), F_PHOTO))
    for a in _snap("activities"):
        tokens.update(bitable.get_attachment_tokens(a.get("fields", {}), F_ACTIVITY_POSTER))
    warmed = 0
    for t in tokens:
        if not t:
            continue
        try:
            with _image_warm_lock:
                if _download_and_cache_image(t):
                    warmed += 1
        except Exception as e:
            logging.getLogger(__name__).warning(f"预热图片 {t} 失败: {e}")
    if warmed:
        logging.getLogger(__name__).info(f"图片预热完成，本次下载 {warmed} 张")


@app.route("/api/image/<file_token>")
def proxy_image(file_token):
    """代理飞书多维表格附件图片（带磁盘缓存，首次下载后直接读本地）"""
    # 1. 检查磁盘缓存（旧的大文件 >1MB 删除，触发重新下载并压缩）
    cached = _get_cached_image(file_token)
    if cached:
        fpath, content_type = cached
        resp = make_response(send_from_directory(IMAGE_CACHE_DIR, os.path.basename(fpath),
                                   mimetype=content_type))
        resp.headers["Cache-Control"] = "public, max-age=604800"
        return resp

    # 2. 从飞书下载并压缩后缓存
    token = bitable.get_token()
    if not token:
        return jsonify({"error": "服务异常"}), 500
    url = "https://open.feishu.cn/open-apis/drive/v1/medias/%s/download" % file_token
    headers = {"Authorization": "Bearer " + token}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return jsonify({"error": "图片不存在"}), 404
        content_type = resp.headers.get("Content-Type", "image/jpeg")
        # 根据content-type确定扩展名
        ext_map = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
                   "image/webp": "webp", "image/heic": "heic"}
        ext = ext_map.get(content_type, "jpg")
        # 压缩后保存（GIF/HEIC 原样保存）
        compressed = _compress_image(resp.content, ext)
        cache_path = os.path.join(IMAGE_CACHE_DIR, "%s.%s" % (file_token, ext))
        with open(cache_path, "wb") as f:
            f.write(compressed)
        return Response(compressed, content_type=content_type,
                        headers={"Cache-Control": "public, max-age=604800"})
    except Exception:
        return jsonify({"error": "图片加载失败"}), 500


# ========== 认证接口 ==========

@app.route("/api/auth/feishu", methods=["GET"])
def feishu_auth():
    """飞书OAuth免登回调"""
    code = request.args.get("code", "")
    if not code:
        return jsonify({"error": "缺少code参数"}), 400

    # 获取 app_access_token（authen接口需要）
    try:
        token_resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
            timeout=10
        )
        token_data = token_resp.json()
        app_access_token = token_data.get("app_access_token", "")
    except Exception as e:
        app.logger.error(f"获取app_access_token失败: {e}")
        return jsonify({"error": "服务异常"}), 500

    if not app_access_token:
        return jsonify({"error": "服务异常"}), 500

    open_id = None
    last_error = ""
    # 标准 access_token（与 authen/v1/authorize 配套）
    try:
        url = "https://open.feishu.cn/open-apis/authen/v1/access_token"
        headers = {"Authorization": f"Bearer {app_access_token}", "Content-Type": "application/json"}
        resp = requests.post(url, headers=headers, json={"grant_type": "authorization_code", "code": code}, timeout=15)
        result = resp.json()
        app.logger.info(f"Authen token response code={result.get('code')}, msg={result.get('msg')}")
        if result.get("code") == 0:
            open_id = result.get("data", {}).get("open_id")
            app.logger.info(f"Auth success, open_id={open_id}")
        else:
            last_error = f"{result.get('code')} {result.get('msg')}"
    except Exception as e:
        last_error = f"exception: {e}"

    if not open_id:
        app.logger.error(f"Feishu auth failed: {last_error}")
        return jsonify({"error": "免登失败，请重试"}), 401

    user = snap_find_user_by_openid(open_id)  # 登录态未建立(g无值)，按 open_id 直解主档
    if not user:
        return jsonify({"error": "尚未注册，请先在飞书中搜索「一线牵」机器人完成注册", "need_register": True}), 403

    # 双身份：默认进入「普通用户」，仅当无普通用户档案时才落到观察员
    roles = roles_of(open_id)
    default_role = "user" if "user" in roles else "observer"

    session_id = create_session(open_id, default_role)
    brief = format_user_brief(user)
    brief["available_roles"] = sorted(roles)
    resp = make_response(jsonify({"ok": True, "user": brief}))
    resp.set_cookie("yxq_session", session_id, httponly=True,
                    max_age=SESSION_EXPIRE_DAYS * 86400, samesite="Lax")
    return resp


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("yxq_session")
    return resp


@app.route("/api/home", methods=["GET"])
def home():
    """首页聚合接口：一次返回 我的信息 + 卡片 + 喜欢 + 活动（全部走本地快照）"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    gate = account_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]

    # 自己的信息优先读快照，避免每次登录直连飞书超时（写操作后已异步刷新快照）
    user = snap_self_user()
    if not user:
        return jsonify({"error": "用户不存在"}), 404
    my_gender = bitable.get_select_value(user.get("fields", {}), F_GENDER)
    target_gender = "女性" if my_gender == "男性" else "男性"
    is_observer = bitable.get_select_value(user.get("fields", {}), F_ACCOUNT_STATUS) == STATUS_OBSERVER

    # 卡片（不含筛选，默认展示全部异性；观察员展示全部活跃用户，男女均可浏览）
    # 只排除「未取消」的喜欢目标，取消喜欢后目标应重新回到卡片池
    liked_openids = {bitable.get_field_text(l.get("fields", {}), F_LIKE_TARGET_OPENID)
                     for l in snap_likes_by_initiator(open_id)
                     if bitable.get_select_value(l.get("fields", {}), F_LIKE_STATUS) != "已取消"}
    cards = []
    for u in snap_active_users():
        fields = u.get("fields", {})
        uid = bitable.get_field_text(fields, F_FEISHU_ID)
        if uid == open_id:
            continue
        if not is_observer and bitable.get_select_value(fields, F_GENDER) != target_gender:
            continue
        brief = format_user_brief(u, include_openid=True, full=True)
        brief["display_fields"] = build_display_fields(fields)
        brief["subtitle"] = build_subtitle(fields)
        brief["liked"] = uid in liked_openids
        cards.append(brief)

    # 喜欢（谁喜欢了我 / 相互喜欢）
    liked_me = snap_likes_by_target(open_id)
    # 喜欢我的异性卡片靠前（前10随机混排），匿名不被猜出
    liked_me_openids = {
        bitable.get_field_text(l.get("fields", {}), F_LIKE_INITIATOR_OPENID)
        for l in liked_me
        if bitable.get_select_value(l.get("fields", {}), F_LIKE_STATUS) != "已取消"
    }
    cards = order_cards_likes_first(cards, liked_me_openids)
    i_liked = snap_likes_by_initiator(open_id)
    i_liked_targets = {
        bitable.get_field_text(l.get("fields", {}), F_LIKE_TARGET_OPENID)
        for l in i_liked
        if bitable.get_select_value(l.get("fields", {}), F_LIKE_STATUS) != "已取消"
    }
    liked_me_list = []
    mutual_list = []
    for like in liked_me:
        fields = like.get("fields", {})
        status = bitable.get_select_value(fields, F_LIKE_STATUS)
        if status == "已取消":
            continue
        initiator_oid = bitable.get_field_text(fields, F_LIKE_INITIATOR_OPENID)
        is_mutual = initiator_oid in i_liked_targets or status == "相互喜欢"
        item = {
            "nickname": bitable.get_field_text(fields, F_LIKE_INITIATOR) if is_mutual else "匿名用户",
            "message": bitable.get_field_text(fields, F_LIKE_MESSAGE) if is_mutual else "",
            "status": status,
            "mutual": is_mutual,
            "initiator_openid": initiator_oid if is_mutual else ""
        }
        if is_mutual:
            mutual_list.append(item)
        else:
            liked_me_list.append(item)

    # 活动
    activities = []
    for item in snap_all_activities():
        act = format_activity(item)
        act["my_signup"] = bool(snap_signup(act["activity_id"], open_id))
        activities.append(act)

    user_brief = format_user_brief(user)
    user_brief["is_admin"] = open_id in ADMIN_OPEN_IDS
    user_brief["available_roles"] = sorted(roles_of(open_id))
    live_h = live_primary_hearts(open_id)
    if live_h is not None:
        user_brief["hearts"] = live_h
    return jsonify({
        "user": user_brief,
        "cards": cards,
        "likes": {"liked_me": liked_me_list, "mutual": mutual_list},
        "activities": activities,
        "register_form_url": REGISTER_FORM_URL,
    })


@app.route("/api/user/me", methods=["GET"])
def user_me():
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    user = snap_self_user()
    if not user:
        return jsonify({"error": "用户不存在"}), 404
    brief = format_user_brief(user)
    brief["is_admin"] = open_id in ADMIN_OPEN_IDS
    brief["available_roles"] = sorted(roles_of(open_id))
    live_h = live_primary_hearts(open_id)
    if live_h is not None:
        brief["hearts"] = live_h
    return jsonify(brief)


@app.route("/api/account/status", methods=["POST"])
def toggle_account_status():
    """切换账号状态：活跃 <-> 已隐藏（秒提交版：状态读快照，同步仅 1 次写入）"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    user = snap_self_user()
    if not user:
        return jsonify({"error": "用户不存在"}), 404
    cur = bitable.get_select_value(user.get("fields", {}), F_ACCOUNT_STATUS)
    if cur == STATUS_OBSERVER:
        return jsonify({"error": "观察员账号不可切换账号状态"}), 403
    if cur == "活跃":
        new_status = "已隐藏"
        # 已报名活动者不可自行隐藏（先取消报名）；读快照（≤15s 窗口，见文档说明）
        active_signups = [s for s in snap_signups_by_openid(open_id)
                          if bitable.get_field_text(s.get("fields", {}), F_SIGNUP_STATUS) == "已报名"]
        if active_signups:
            return jsonify({"error": "你已报名活动，请先取消报名再隐藏"}), 400
    elif cur == "已隐藏":
        new_status = "活跃"
    else:
        return jsonify({"error": "当前状态暂不支持切换"}), 400
    # 秒提交：spool 幂等写入绝对目标状态 + 本地快照立即生效
    _spool_append({"type": "status", "record_id": user["record_id"],
                   "to_status": new_status})
    for u in _snapshot.get("users", []):
        if u.get("record_id") == user["record_id"]:
            u.setdefault("fields", {})[F_ACCOUNT_STATUS] = new_status
    return jsonify({"ok": True, "account_status": new_status})


@app.route("/api/account/switch", methods=["POST"])
def switch_account_role():
    """切换当前身份：user(普通用户) <-> observer(观察员)。需该账号已分别注册对应身份。"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    data = request.get_json(silent=True) or {}
    role = data.get("role")
    if role not in ("user", "observer"):
        return jsonify({"error": "无效的身份类型"}), 400
    roles = roles_of(open_id)
    if role not in roles:
        return jsonify({"error": "你尚未注册该身份，请先完成对应注册"}), 403
    session_id = create_session(open_id, role)
    resp = make_response(jsonify({"ok": True, "role": role}))
    resp.set_cookie("yxq_session", session_id, httponly=True,
                    max_age=SESSION_EXPIRE_DAYS * 86400, samesite="Lax")
    return resp


# ========== 活动接口 ==========

@app.route("/api/activities", methods=["GET"])
def activities():
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401

    result = []
    for item in snap_all_activities():
        act = format_activity(item)
        act["my_signup"] = bool(snap_signup(act["activity_id"], open_id))
        result.append(act)
    return jsonify({"activities": result})


@app.route("/api/activities/<activity_id>", methods=["GET"])
def activity_detail(activity_id):
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401

    act_record, text_act_id = snap_resolve_activity(activity_id)
    if not act_record:
        return jsonify({"error": "活动不存在"}), 404

    act = format_activity(act_record)
    act["my_signup"] = bool(snap_signup(text_act_id, open_id))

    # 获取报名人数列表（不返回open_id）
    signups = snap_signups_by_activity(text_act_id)
    act["signup_users"] = [bitable.get_field_text(s.get("fields", {}), F_SIGNUP_NICKNAME) for s in signups]
    return jsonify(act)


@app.route("/api/activities/<activity_id>/signup", methods=["POST"])
def signup(activity_id):
    """报名（秒提交版）：查重/上限走快照，同步仅 1 次写入；
    人数与满员状态由机器人 auto_update_activity_signup_count 每30秒对账修正"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    gate = active_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]

    act_record, text_act_id = snap_resolve_activity(activity_id)
    if not act_record:
        return jsonify({"error": "活动不存在"}), 404

    act_fields = act_record.get("fields", {})
    status = bitable.get_select_value(act_fields, F_ACTIVITY_STATUS)
    if status != "报名中":
        return jsonify({"error": f"活动当前状态为「{status}」，无法报名"}), 400

    # 查重：快照优先，未命中回退实时（防「刚报名完立刻重复提交」误判）
    existing = snap_signup(text_act_id, open_id) or bitable.get_user_signup(text_act_id, open_id)
    if existing:
        return jsonify({"error": "你已经报名了这个活动"}), 400

    # 人数上限：快照口径，机器人会对账修正
    signups_snap = snap_signups_by_activity(text_act_id) or bitable.get_signups(text_act_id)
    max_signup = int(bitable.get_field_number(act_fields, F_ACTIVITY_MAX_SIGNUP, 0))
    if max_signup > 0 and len(signups_snap) >= max_signup:
        return jsonify({"error": "报名人数已满"}), 400

    user = snap_self_user()
    if not user:
        return jsonify({"error": "用户信息不存在"}), 404
    nickname = bitable.get_field_text(user.get("fields", {}), F_NICKNAME)

    with file_lock:
        signup_record = bitable.create_record(SIGNUP_TABLE_ID, {
            F_SIGNUP_ACTIVITY_ID: text_act_id,
            F_SIGNUP_OPENID: open_id,
            F_SIGNUP_NICKNAME: nickname,
            F_SIGNUP_STATUS: "已报名"
        })
    if not signup_record:
        return jsonify({"error": "报名失败，请重试"}), 500

    refresh_snapshot_table_async("signups")
    refresh_snapshot_table_async("activities")
    return jsonify({"ok": True, "message": "报名成功"})


@app.route("/api/activities/<activity_id>/signup", methods=["DELETE"])
def cancel_signup(activity_id):
    """取消报名（秒提交版）：快照定位 + 1 次写入；
    人数由机器人 auto_update_activity_signup_count 对账修正（含已满员→报名中回退）"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    gate = account_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]

    act_record, text_act_id = snap_resolve_activity(activity_id)
    if not act_record:
        return jsonify({"error": "活动不存在"}), 404
    existing = snap_signup(text_act_id, open_id) or bitable.get_user_signup(text_act_id, open_id)
    if not existing:
        return jsonify({"error": "你没有报名这个活动"}), 400
    act_fields = act_record.get("fields", {})
    status = bitable.get_select_value(act_fields, F_ACTIVITY_STATUS)
    if status != "报名中":
        return jsonify({"error": f"活动当前状态为「{status}」，无法取消报名"}), 400

    with file_lock:
        # 更新为已取消（保留历史，与 Bot 侧一致）
        ok = bitable.update_record(SIGNUP_TABLE_ID, existing["record_id"], {
            F_SIGNUP_STATUS: "已取消"
        })
    if not ok:
        return jsonify({"error": "取消失败，请稍后重试"}), 500

    refresh_snapshot_table_async("signups")
    refresh_snapshot_table_async("activities")
    return jsonify({"ok": True, "message": "已取消报名"})


# ========== 喜欢接口 ==========

@app.route("/api/cards", methods=["GET"])
def get_cards():
    """获取推荐的异性卡片"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    gate = account_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]

    user = snap_self_user()
    if not user:
        return jsonify({"error": "用户不存在"}), 404

    user_fields = user.get("fields", {})
    my_gender = bitable.get_select_value(user_fields, F_GENDER)
    target_gender = "女性" if my_gender == "男性" else "男性"
    is_observer = bitable.get_select_value(user_fields, F_ACCOUNT_STATUS) == STATUS_OBSERVER
    gender_filter = (request.args.get("gender") or "").strip()  # 仅观察员可用：男性/女性

    # 解析筛选参数（全部可选）
    def _num(key):
        v = request.args.get(key)
        try:
            return float(v) if v not in (None, "") else None
        except ValueError:
            return None

    filters = {
        "user_id": (request.args.get("user_id") or "").strip(),
        "height_min": _num("height_min"), "height_max": _num("height_max"),
        "age_min": _num("age_min"), "age_max": _num("age_max"),
        "birth_min": (request.args.get("birth_min") or "").strip(),
        "birth_max": (request.args.get("birth_max") or "").strip(),
        "education": [e.strip() for e in request.args.getlist("education") if e.strip()],
        "income": [i.strip() for i in request.args.getlist("income") if i.strip()],
        "church": (request.args.get("church") or "").strip(),
        "native_place": (request.args.get("native_place") or "").strip(),
        "city": (request.args.get("city") or "").strip(),
        "industry": (request.args.get("industry") or "").strip(),
        "house": (request.args.get("house") or "").strip(),
        "driving": (request.args.get("driving") or "").strip(),
    }

    all_users = snap_active_users()

    # 获取我已经喜欢过的人（从快照，实时性靠写操作后定向刷新）
    # 只排除「未取消」的喜欢目标，取消喜欢后目标应重新回到卡片池
    liked_openids = {
        bitable.get_field_text(like.get("fields", {}), F_LIKE_TARGET_OPENID)
        for like in snap_likes_by_initiator(open_id)
        if bitable.get_select_value(like.get("fields", {}), F_LIKE_STATUS) != "已取消"
    }

    cards = []
    for u in all_users:
        fields = u.get("fields", {})
        uid = bitable.get_field_text(fields, F_FEISHU_ID)
        gender = bitable.get_select_value(fields, F_GENDER)
        if uid == open_id:
            continue
        if is_observer:
            # 观察员可浏览男性+女性，可选 gender 参数进一步筛选
            if gender_filter and gender != gender_filter:
                continue
        elif gender != target_gender:
            continue
        if not pass_card_filters(fields, filters):
            continue
        brief = format_user_brief(u, include_openid=True, full=True)
        brief["display_fields"] = build_display_fields(fields)
        brief["subtitle"] = build_subtitle(fields)
        brief["liked"] = uid in liked_openids
        cards.append(brief)

    # 喜欢我的异性卡片靠前（前10随机混排），匿名不被猜出
    liked_me_openids = {
        bitable.get_field_text(l.get("fields", {}), F_LIKE_INITIATOR_OPENID)
        for l in snap_likes_by_target(open_id)
        if bitable.get_select_value(l.get("fields", {}), F_LIKE_STATUS) != "已取消"
    }
    cards = order_cards_likes_first(cards, liked_me_openids)

    return jsonify({"cards": cards})


# 进程内「点喜欢预留」计数：create 后、likes 快照刷新前的时间窗内，
# 快照看不到最新记录，用预留数兜底防双花；TTL 取机器人扣减周期，过期自然清零
_like_reserves = {}
_recent_cancels = {}  # record_id -> ts：取消意图已发出、表/快照尚未更新的宽限豁免






def _spool_append(op):
    line = json.dumps(op, ensure_ascii=False)
    with _spool_lock:
        _spool_queue.append(op)
        with open(_SPOOL_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())


def _spool_rewrite():
    with _spool_lock:
        ops = list(_spool_queue)
    tmp = _SPOOL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for op in ops:
            f.write(json.dumps(op, ensure_ascii=False) + "\n")
    os.replace(tmp, _SPOOL_FILE)


def _spool_process(op):
    """幂等执行：喜欢=先查同向活跃记录再建；取消=按 record_id 置已取消（天然幂等）"""
    t = op.get("type")
    try:
        if t == "like":
            dup = bitable.search_records(LIKE_TABLE_ID, {
                "conjunction": "and",
                "conditions": [
                    {"field_name": F_LIKE_INITIATOR_OPENID, "operator": "is",
                     "value": [op.get("initiator_oid")]},
                    {"field_name": F_LIKE_TARGET_OPENID, "operator": "is",
                     "value": [op.get("target_oid")]},
                    {"field_name": F_LIKE_STATUS, "operator": "isNot", "value": ["已取消"]},
                ]})
            if dup:
                # 幂等命中：登记已有记录 rid，供桥接计数
                e = _intent_likes.get(op.get("temp_key"))
                if e:
                    e["rid"] = dup[0].get("record_id")
                return True
            r = bitable.create_record(LIKE_TABLE_ID, op["fields"])
            if r:
                e = _intent_likes.get(op.get("temp_key"))
                if e:
                    e["rid"] = r.get("record_id")
            return bool(r)
        if t == "cancel":
            return bitable.update_record(LIKE_TABLE_ID, op["record_id"],
                                         {F_LIKE_STATUS: "已取消"}) is not None
        if t == "status":
            return bitable.update_record(USER_TABLE_ID, op["record_id"],
                                         {F_ACCOUNT_STATUS: op["to_status"]}) is not None
        if t == "cancel_pair":
            rows = bitable.search_records(LIKE_TABLE_ID, {
                "conjunction": "and",
                "conditions": [
                    {"field_name": F_LIKE_INITIATOR_OPENID, "operator": "is",
                     "value": [op.get("initiator_oid")]},
                    {"field_name": F_LIKE_TARGET_OPENID, "operator": "is",
                     "value": [op.get("target_oid")]},
                    {"field_name": F_LIKE_STATUS, "operator": "isNot", "value": ["已取消"]},
                ]})
            for row in rows:
                bitable.update_record(LIKE_TABLE_ID, row["record_id"],
                                      {F_LIKE_STATUS: "已取消"})
            return True  # 幂等：找不到即视为已完成
    except Exception:
        return False
    return False


def _spool_worker():
    while True:
        op = None
        with _spool_lock:
            if _spool_queue:
                op = _spool_queue.pop(0)
        if op is None:
            time.sleep(0.15)
            continue
        ok = False
        for attempt in range(3):
            if _spool_process(op):
                ok = True
                break
            time.sleep(2 ** attempt)
        if ok:
            _spool_rewrite()
        else:
            try:
                with open(_SPOOL_DEAD, "a", encoding="utf-8") as f:
                    f.write(json.dumps(op, ensure_ascii=False) + "\n")
            except Exception:
                pass
            _spool_rewrite()


def _spool_boot():
    if os.path.exists(_SPOOL_FILE):
        with open(_SPOOL_FILE, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        _spool_queue.append(json.loads(ln))
                    except Exception:
                        pass
    threading.Thread(target=_spool_worker, daemon=True, name="spool-writer").start()


_spool_boot()


@app.route("/api/like", methods=["POST"])
def like_user():
    """喜欢某人（v6：校验→spool落盘→立即回包；爱心为事件计算值，0秒精确）"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    gate = active_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]

    data = request.get_json() or {}
    target_openid = data.get("target_openid", "")
    message = data.get("message", "")
    like_type = data.get("like_type", "匿名")
    if like_type not in ("匿名", "实名"):
        like_type = "匿名"

    if not target_openid:
        return jsonify({"error": "缺少目标用户"}), 400
    if target_openid == open_id:
        return jsonify({"error": "不能喜欢自己"}), 400

    me = snap_self_user()
    target = snap_find_user_by_openid(target_openid)
    if not me or not target:
        return jsonify({"error": "用户不存在"}), 404

    me_fields = me.get("fields", {})
    target_fields = target.get("fields", {})
    my_gender = bitable.get_select_value(me_fields, F_GENDER)
    target_gender = bitable.get_select_value(target_fields, F_GENDER)
    if not my_gender or not target_gender:
        return jsonify({"error": "性别信息缺失，请先完善资料"}), 400
    if my_gender == target_gender:
        return jsonify({"error": "仅限异性之间喜欢"}), 400

    # 重复/相互检查（快照 + 取消意图豁免）
    _intent_prune()
    cancel_targets = {t for t, _ in _intent_cancels.get(open_id, [])}
    already = False
    likes_snap = _snap("likes")
    for l in likes_snap:
        lf = l.get("fields", {})
        if bitable.get_field_text(lf, F_LIKE_INITIATOR_OPENID) != open_id:
            continue
        if bitable.get_select_value(lf, F_LIKE_STATUS) == "已取消":
            continue
        if bitable.get_field_text(lf, F_LIKE_TARGET_OPENID) in cancel_targets:
            continue
        if bitable.get_field_text(lf, F_LIKE_TARGET_OPENID) == target_openid:
            already = True
            break
    if already:
        return jsonify({"error": "你已经喜欢过TA了"}), 400
    # 在途意图查重：快照尚未见新记录时，同目标连点直接拦截
    if any(it["target"] == target_openid for it in _intent_likes.values() if it["oid"] == open_id):
        return jsonify({"error": "你已经喜欢过TA了"}), 400
    if not likes_snap and bitable.find_like(open_id, target_openid):
        return jsonify({"error": "你已经喜欢过TA了"}), 400

    # 爱心充足性：事件计算值（含在途意图，天然防双花）
    if computed_hearts(open_id) <= 0:
        return jsonify({"error": "爱心不足，无法喜欢"}), 400

    has_like_type_field = bitable.field_exists(LIKE_TABLE_ID, F_LIKE_TYPE)
    like_fields = {
        F_LIKE_INITIATOR: bitable.get_field_text(me_fields, F_NICKNAME),
        F_LIKE_TARGET: bitable.get_field_text(target_fields, F_NICKNAME),
        F_LIKE_INITIATOR_OPENID: open_id,
        F_LIKE_TARGET_OPENID: target_openid,
        F_LIKE_INITIATOR_ID: bitable.get_field_text(me_fields, F_USER_ID),
        F_LIKE_TARGET_ID: bitable.get_field_text(target_fields, F_USER_ID),
        F_LIKE_STATUS: "单向喜欢",
    }
    if has_like_type_field:
        like_fields[F_LIKE_TYPE] = like_type
    if message:
        like_fields[F_LIKE_MESSAGE] = message

    temp_key = _uuid.uuid4().hex
    _spool_append({"type": "like", "temp_key": temp_key,
                   "initiator_oid": open_id, "target_oid": target_openid,
                   "fields": like_fields})
    _intent_likes[temp_key] = {"oid": open_id, "target": target_openid, "ts": time.time()}

    hearts_now = computed_hearts(open_id)
    return jsonify({"ok": True, "mutual": False, "message": "喜欢成功", "hearts": hearts_now})





@app.route("/api/feedback", methods=["POST"])
def feedback():
    """卡片反馈：写举报表 + 通知管理员"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401

    data = request.get_json() or {}
    target_openid = data.get("target_openid", "")
    reason = (data.get("reason", "") or "").strip()

    if not target_openid:
        return jsonify({"error": "缺少被反馈用户"}), 400
    if not reason:
        return jsonify({"error": "请填写反馈原因"}), 400

    me = snap_self_user()
    target = snap_find_user_by_openid(target_openid)
    if not me or not target:
        return jsonify({"error": "用户不存在"}), 404

    me_fields = me.get("fields", {})
    target_fields = target.get("fields", {})
    my_name = bitable.get_field_text(me_fields, F_NICKNAME)
    target_name = bitable.get_field_text(target_fields, F_NICKNAME)
    my_id = bitable.get_field_text(me_fields, F_USER_ID)
    target_id = bitable.get_field_text(target_fields, F_USER_ID)

    # 带用户ID，避免昵称重复时无法定位
    reporter_label = f"{my_name}（{my_id}）" if my_id else my_name
    target_label = f"{target_name}（{target_id}）" if target_id else target_name

    created = bitable.create_record(REPORT_TABLE_ID, {
        F_REPORT_REPORTER: reporter_label,
        F_REPORT_TARGET: target_label,
        F_REPORT_REASON: reason,
    })
    if not created:
        return jsonify({"error": "反馈提交失败，请稍后重试"}), 500

    # 通知管理员（后台线程发送，不阻塞回包）
    admin_msg = f"📮 收到用户反馈\n举报人：{reporter_label}\n被举报人：{target_label}\n原因：{reason}"

    def _notify_admins():
        for admin_oid in ADMIN_OPEN_IDS:
            send_text_message(admin_oid, admin_msg)

    threading.Thread(target=_notify_admins, daemon=True).start()

    return jsonify({"ok": True})


@app.route("/api/likes/me", methods=["GET"])
def my_likes():
    """查看谁喜欢了我 / 相互喜欢列表"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401

    # 谁喜欢了我
    liked_me = bitable.search_records(LIKE_TABLE_ID, [
        {"field_name": F_LIKE_TARGET_OPENID, "operator": "is", "value": [open_id]}])
    # 我喜欢的人
    i_liked = bitable.search_records(LIKE_TABLE_ID, [
        {"field_name": F_LIKE_INITIATOR_OPENID, "operator": "is", "value": [open_id]}])

    i_liked_targets = {
        bitable.get_field_text(l.get("fields", {}), F_LIKE_TARGET_OPENID)
        for l in i_liked
        if bitable.get_select_value(l.get("fields", {}), F_LIKE_STATUS) != "已取消"
    }

    liked_me_list = []
    mutual_list = []
    for like in liked_me:
        fields = like.get("fields", {})
        status = bitable.get_select_value(fields, F_LIKE_STATUS)
        if status == "已取消":
            continue
        initiator_oid = bitable.get_field_text(fields, F_LIKE_INITIATOR_OPENID)
        like_type = bitable.get_field_text(fields, F_LIKE_TYPE)
        is_real = like_type == "实名"
        is_mutual = initiator_oid in i_liked_targets or status == "相互喜欢"
        reveal = is_mutual or is_real
        item = {
            "nickname": bitable.get_field_text(fields, F_LIKE_INITIATOR) if reveal else "匿名用户",
            "message": bitable.get_field_text(fields, F_LIKE_MESSAGE) if is_mutual else "",
            "status": status,
            "mutual": is_mutual,
            "real": is_real,
            "initiator_openid": initiator_oid if reveal else ""
        }
        if is_mutual:
            mutual_list.append(item)
        else:
            liked_me_list.append(item)

    return jsonify({"liked_me": liked_me_list, "mutual": mutual_list})


# ========== 留言接口 ==========

@app.route("/api/messages", methods=["GET"])
def list_messages():
    """列出某卡片下的留言（target=目标用户 open_id），排除已删除/已举报"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    target = (request.args.get("target") or "").strip()
    if not target:
        return jsonify({"messages": []})
    items = bitable.search_records(MESSAGE_TABLE_ID, [
        {"field_name": F_MSG_TARGET_OID, "operator": "is", "value": [target]}
    ])
    # 头像映射（open_id -> 头像URL），从快照构建，避免逐条直查飞书
    avatar_map = {}
    for u in _snap("users"):
        uf = u.get("fields", {})
        oid = bitable.get_field_text(uf, F_FEISHU_ID)
        # keep-first：同号多档时取最早建档（=主档案规则的最小用户ID），与「我的」页一致
        if oid and oid not in avatar_map:
            toks = bitable.get_attachment_tokens(uf, F_PHOTO)
            avatar_map[oid] = "/api/image/" + toks[0] if toks else ""
    msgs = []
    for it in items:
        fields = it.get("fields", {})
        if bitable.get_select_value(fields, F_MSG_STATUS) != "正常":
            continue
        author_oid = bitable.get_field_text(fields, F_MSG_AUTHOR_OID)
        msgs.append({
            "id": it.get("record_id"),
            "author_nickname": bitable.get_field_text(fields, F_MSG_AUTHOR_NICKNAME),
            "author_uid": bitable.get_field_text(fields, F_MSG_AUTHOR_UID),
            "author_avatar": avatar_map.get(author_oid, ""),
            "content": bitable.get_field_text(fields, F_MSG_CONTENT),
            "parent_id": bitable.get_field_text(fields, F_MSG_PARENT_ID),
            "created_at": bitable.get_field_number(fields, F_MSG_CREATED_AT),
            "is_mine": author_oid == open_id,
            "can_delete": author_oid == open_id or target == open_id,
        })
    msgs.sort(key=lambda m: m["created_at"])
    return jsonify({"messages": msgs})


@app.route("/api/messages", methods=["POST"])
def create_message():
    """发留言/回帖（固定显示昵称+用户ID，无匿名）"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    # 留言属于互动行为：仅「活跃」可发（隐藏/待审核/已拒绝/已退出均拦截）
    _status, _u = _account_status(open_id)
    if _status == "已隐藏":
        return jsonify({"error": "你当前处于隐藏状态，不能留言；请先在「我的」页恢复活跃"}), 403
    gate = active_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]
    data = request.get_json(silent=True) or {}
    target = (data.get("target_openid") or "").strip()
    content = (data.get("content") or "").strip()
    parent_id = (data.get("parent_id") or "").strip()
    if not target or not content:
        return jsonify({"error": "内容不能为空"}), 400
    if len(content) > 500:
        return jsonify({"error": "留言最多500字"}), 400
    author = snap_self_user()
    if not author:
        return jsonify({"error": "用户不存在"}), 404
    af = author.get("fields", {})
    rec = bitable.create_record(MESSAGE_TABLE_ID, {
        F_MSG_TARGET_OID: target,
        F_MSG_AUTHOR_OID: open_id,
        F_MSG_AUTHOR_NICKNAME: bitable.get_field_text(af, F_NICKNAME),
        F_MSG_AUTHOR_UID: bitable.get_field_text(af, F_USER_ID),
        F_MSG_PARENT_ID: parent_id,
        F_MSG_CONTENT: content,
        F_MSG_CREATED_AT: int(time.time() * 1000),
        F_MSG_STATUS: "正常",
    })
    if not rec:
        return jsonify({"error": "留言失败"}), 500
    return jsonify({"ok": True, "id": rec.get("record_id")})


@app.route("/api/messages/<record_id>", methods=["DELETE"])
def delete_message(record_id):
    """删除留言（本人或卡片主人）"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    rec = bitable.get_record(MESSAGE_TABLE_ID, record_id)
    if not rec:
        return jsonify({"error": "留言不存在"}), 404
    fields = rec.get("fields", {})
    author_oid = bitable.get_field_text(fields, F_MSG_AUTHOR_OID)
    target_oid = bitable.get_field_text(fields, F_MSG_TARGET_OID)
    if open_id != author_oid and open_id != target_oid:
        return jsonify({"error": "无权删除"}), 403
    bitable.update_record(MESSAGE_TABLE_ID, record_id, {F_MSG_STATUS: "已删除"})
    return jsonify({"ok": True})


@app.route("/api/messages/<record_id>/report", methods=["POST"])
def report_message(record_id):
    """举报留言（置为已举报，待管理员处理）"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    rec = bitable.get_record(MESSAGE_TABLE_ID, record_id)
    if not rec:
        return jsonify({"error": "留言不存在"}), 404
    bitable.update_record(MESSAGE_TABLE_ID, record_id, {F_MSG_STATUS: "已举报"})
    return jsonify({"ok": True})


# ========== 分组接口 ==========

@app.route("/api/activities/<activity_id>/group/candidates", methods=["GET"])
def group_candidates(activity_id):
    """获取分组可选的异性列表"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401

    # 检查活动状态
    act_record, text_act_id = snap_resolve_activity(activity_id)
    if not act_record:
        return jsonify({"error": "活动不存在"}), 404
    group_status = bitable.get_select_value(act_record.get("fields", {}), F_ACTIVITY_GROUP_STATUS)
    if group_status != "收集中":
        return jsonify({"error": f"分组状态为「{group_status}」，无法选择"}), 400

    # 检查是否报名（直读飞书，避免报名快照 stale 导致误判未报名）
    signup = bitable.get_user_signup(text_act_id, open_id)
    if not signup:
        return jsonify({"error": "你未报名此活动"}), 403

    # 获取报名的异性（直读飞书，避免报名快照 stale 导致「0 可选人」——快照为跨 worker 内存态，
    # 刷新前新增的报名会被漏掉，历史 bug：提交志愿页一个候选都没有）
    signups = bitable.get_signups(text_act_id)
    me = snap_self_user()
    if not me:
        return jsonify({"error": "用户不存在，请确认已完善资料"}), 404
    my_gender = bitable.get_select_value(me.get("fields", {}), F_GENDER)
    target_gender = "女性" if my_gender == "男性" else "男性"

    candidates = []
    for s in signups:
        fields = s.get("fields", {})
        s_openid = bitable.get_field_text(fields, F_SIGNUP_OPENID)
        if s_openid == open_id:
            continue
        # 获取该用户的性别信息
        u = snap_find_user_by_openid(s_openid)
        if u:
            u_gender = bitable.get_select_value(u.get("fields", {}), F_GENDER)
            if u_gender == target_gender:
                uf = u.get("fields", {})
                tokens = bitable.get_attachment_tokens(uf, F_PHOTO)
                photo = "/api/image/" + tokens[0] if tokens else ""
                candidates.append({
                    "openid": s_openid,
                    "nickname": bitable.get_field_text(uf, F_NICKNAME),
                    "photo": photo,
                    "user_id": bitable.get_field_text(uf, F_USER_ID),
                    "baptismal_name": bitable.get_field_text(uf, F_BAPTISMAL_NAME),
                    "birthday": format_birthday(uf),
                    "education": bitable.get_select_value(uf, F_EDUCATION),
                    "native_place": bitable.get_field_text(uf, F_NATIVE_PLACE),
                    "city": bitable.get_field_text(uf, F_CITY),
                    "hobbies": "、".join(bitable.get_multi_select_value(uf, F_SELF_HOBBIES))
                })
    app.logger.warning(
        "[group_candidates] open_id=%s activity=%s text_act=%s group_status=%s "
        "my_gender=%s target=%s signups=%d candidates=%d",
        open_id, activity_id, text_act_id, group_status,
        my_gender, target_gender, len(signups), len(candidates))
    return jsonify({"candidates": candidates})


@app.route("/api/activities/<activity_id>/group/select", methods=["POST"])
def group_select(activity_id):
    """提交分组志愿选择（秒提交版：查活动/报名/本人全走快照，同步仅 1 次写入）"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    gate = active_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]

    data = request.get_json() or {}
    choices = data.get("choices", [])  # [openid1, openid2, ...] 最多7个

    if not choices or not isinstance(choices, list):
        return jsonify({"error": "请至少选择一个志愿"}), 400
    if len(choices) > 7:
        return jsonify({"error": "最多选择7个志愿"}), 400

    # 检查活动状态（快照）
    act_record, text_act_id = snap_resolve_activity(activity_id)
    if not act_record:
        return jsonify({"error": "活动不存在"}), 404
    group_status = bitable.get_select_value(act_record.get("fields", {}), F_ACTIVITY_GROUP_STATUS)
    if group_status != "收集中":
        return jsonify({"error": f"分组状态为「{group_status}」，无法选择"}), 400

    # 检查报名（快照优先，未命中回退实时，防刚报名即提交被误判）
    signup = snap_signup(text_act_id, open_id) or bitable.get_user_signup(text_act_id, open_id)
    if not signup:
        return jsonify({"error": "你未报名此活动"}), 403

    # 获取用户信息（快照）
    me = snap_self_user()
    me_fields = me.get("fields", {})
    my_name = bitable.get_field_text(me_fields, F_NICKNAME)
    my_gender = bitable.get_select_value(me_fields, F_GENDER)

    # 检查是否已提交过（允许覆盖；统一用 text_act_id 查询，修复旧代码混用 record_id 的不一致）
    existing = snap_group_selection(text_act_id, open_id) or bitable.get_user_group_selection(text_act_id, open_id)

    fields = {
        F_GS_ACTIVITY_ID: text_act_id,
        F_GS_SELECTOR_OID: open_id,
        F_GS_SELECTOR_NAME: my_name,
        F_GS_SELECTOR_GENDER: my_gender,
    }
    # 填充志愿（先清空 7 个志愿位，再填新值，避免脏数据残留）
    for i in range(len(F_GS_CHOICES)):
        fields[F_GS_CHOICES[i]] = ""
    for i, choice_oid in enumerate(choices):
        if i < len(F_GS_CHOICES):
            fields[F_GS_CHOICES[i]] = choice_oid

    with file_lock:
        if existing:
            ok = bitable.update_record(GROUP_SELECT_TABLE, existing["record_id"], fields)
        else:
            ok = bitable.create_record(GROUP_SELECT_TABLE, fields)
    if not ok:
        return jsonify({"error": "提交失败，请稍后重试"}), 500

    refresh_snapshot_table_async("group_selections")
    return jsonify({"ok": True, "message": "志愿提交成功"})


@app.route("/api/activities/<activity_id>/group/status", methods=["GET"])
def group_status(activity_id):
    """查询分组状态和我的选择"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401

    act_record, text_act_id = snap_resolve_activity(activity_id)
    if not act_record:
        return jsonify({"error": "活动不存在"}), 404

    act_fields = act_record.get("fields", {})
    group_status = bitable.get_select_value(act_fields, F_ACTIVITY_GROUP_STATUS)

    my_selection = snap_group_selection(text_act_id, open_id)
    my_choices = []
    if my_selection:
        s_fields = my_selection.get("fields", {})
        for choice_field in F_GS_CHOICES:
            val = bitable.get_field_text(s_fields, choice_field)
            if val:
                my_choices.append(val)

    return jsonify({
        "group_status": group_status,
        "my_selected": bool(my_selection),
        "my_choices": my_choices
    })


@app.route("/api/activities/<activity_id>/group/result", methods=["GET"])
def group_result(activity_id):
    """查询分组结果"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401

    act_record, text_act_id = snap_resolve_activity(activity_id)
    if not act_record:
        return jsonify({"error": "活动不存在"}), 404

    group_status = bitable.get_select_value(act_record.get("fields", {}), F_ACTIVITY_GROUP_STATUS)
    if group_status != "已完成":
        return jsonify({"error": "分组尚未完成", "group_status": group_status}), 400

    results = snap_group_results(text_act_id)

    # 轮次：默认展示最新一轮（round 最大）。旧无轮次记录(空)视为第1轮。
    def _round_of(f):
        rv = bitable.get_select_value(f, F_GR_ROUND)
        if rv:
            try:
                return int(rv)
            except ValueError:
                return 0
        return 1  # 无轮次的老数据视为第1轮
    available_rounds = sorted({_round_of(r.get("fields", {})) for r in results})
    if not available_rounds:
        available_rounds = [1]
    latest_round = available_rounds[-1]
    # 支持按轮次查看（多次分组）；无效或缺省回退到最新轮
    req_round = request.args.get("round", "")
    try:
        req_round = int(req_round) if str(req_round).strip() else latest_round
    except ValueError:
        req_round = latest_round
    if req_round not in available_rounds:
        req_round = latest_round
    round_results = [r for r in results if _round_of(r.get("fields", {})) == req_round]

    # 一次性把 users 快照映射为 open_id -> 用户ID（避免对每个成员逐条查库）
    users_snap = _snap("users")
    if not users_snap:
        users_snap = bitable.get_all_users()
    uid_by_oid = {}
    for u in users_snap:
        uf = u.get("fields", {})
        oid = bitable.get_field_text(uf, F_FEISHU_ID)
        if oid:
            uid_by_oid[oid] = bitable.get_field_text(uf, F_USER_ID)

    # 找到我的组号
    my_group = None
    groups = {}
    for r in round_results:
        fields = r.get("fields", {})
        group_no = int(bitable.get_field_number(fields, F_GR_GROUP_NO, 0))
        oid = bitable.get_field_text(fields, F_GR_USER_OID)
        member = {
            "nickname": bitable.get_field_text(fields, F_GR_USER_NAME),
            "gender": bitable.get_select_value(fields, F_GR_USER_GENDER),
            "openid": oid,
            "user_id": uid_by_oid.get(oid, "")
        }
        if member["openid"] == open_id:
            my_group = group_no
        groups.setdefault(group_no, []).append(member)

    # 只返回我所在的组
    my_group_members = groups.get(my_group, []) if my_group else []
    return jsonify({
        "my_group": my_group,
        "members": my_group_members,
        "total_groups": len(groups),
        "round": req_round,
        "rounds": available_rounds
    })


# ========== 个人中心 ==========

def _editable_value(fields, fname, ftype):
    """按类型读取可编辑字段当前值（返回给前端编辑表单）"""
    if ftype == "text":
        return bitable.get_field_text(fields, fname)
    if ftype == "number":
        n = bitable.get_field_number(fields, fname, 0)
        return "" if (n is None or n == 0) else (int(n) if float(n) == int(n) else n)
    if ftype == "select":
        return bitable.get_select_value(fields, fname)
    if ftype == "multi":
        return bitable.get_multi_select_value(fields, fname)
    if ftype == "phone":
        return bitable.get_phone_value(fields, fname)
    return ""


def _editable_schema(fields):
    """可编辑字段 schema + 当前值，前端据此动态渲染「修改资料」表单"""
    return [
        {"field": fname, "label": label, "type": ftype, "options": opts,
         "long": fname in LONG_TEXT_FIELDS,
         "value": _editable_value(fields, fname, ftype)}
        for fname, label, ftype, opts in EDITABLE_FIELDS
    ]


def _normalize_editable_update(data):
    """将前端提交的值按类型归一化为飞书写入格式；非法项静默忽略。"""
    by_name = {fname: ftype for fname, _lbl, ftype, _opts in EDITABLE_FIELDS}
    update_fields = {}
    for k, v in data.items():
        ftype = by_name.get(k)
        if ftype is None:
            continue
        if ftype in ("text", "phone"):
            update_fields[k] = str(v).strip() if v is not None else ""
        elif ftype == "select":
            update_fields[k] = str(v).strip() if v is not None else ""
        elif ftype == "number":
            if v in ("", None):
                continue  # 身高留空则不更新
            try:
                update_fields[k] = int(float(str(v)))
            except (ValueError, TypeError):
                continue
        elif ftype == "multi":
            vals = v if isinstance(v, list) else []
            vals = [str(x).strip() for x in vals if str(x).strip()]
            update_fields[k] = vals
    return update_fields


@app.route("/api/profile", methods=["GET"])
def get_profile():
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    user = snap_self_user()
    if not user:
        return jsonify({"error": "用户不存在"}), 404
    data = format_user_profile(user)
    data["editable"] = _editable_schema(user.get("fields", {}))
    return jsonify(data)


@app.route("/api/profile", methods=["PUT"])
def update_profile():
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401

    data = request.get_json() or {}
    user = snap_self_user()
    if not user:
        return jsonify({"error": "用户不存在"}), 404
    if _is_observer(user):
        return jsonify(OBSERVER_BLOCKED_MESSAGE), 403

    update_fields = _normalize_editable_update(data)
    if not update_fields:
        return jsonify({"error": "没有可更新的字段"}), 400

    ok = bitable.update_record(USER_TABLE_ID, user["record_id"], update_fields)
    if not ok:
        return jsonify({"error": "资料更新失败，请稍后重试"}), 500
    # 同步刷新快照：否则前端保存后立即 GET 会读到旧快照，表现为「保存后页面不刷新」
    refresh_snapshot_table("users")
    return jsonify({"ok": True, "message": "资料已更新"})


MAX_PHOTOS = 9  # 个人照片最多 9 张（飞书附件字段单格上限）


def _photo_tokens(user):
    """读取用户「个人照片」字段的全部附件 token（按展示顺序）"""
    return bitable.get_attachment_tokens(user.get("fields", {}), F_PHOTO)


def _photo_urls(tokens):
    return ["/api/image/" + t for t in tokens]


def _write_photos(user, tokens):
    """把一组 file_token 写回「个人照片」字段（多张照片，牵线首页可切换展示）。"""
    attachments = [{"file_token": t, "name": f"photo{i + 1}.jpg"} for i, t in enumerate(tokens)]
    ok = bitable.update_record(USER_TABLE_ID, user["record_id"], {F_PHOTO: attachments})
    if ok:
        refresh_snapshot_table("users")
    return ok


@app.route("/api/profile/photo", methods=["POST"])
def update_profile_photo():
    """新增一张照片：上传图片并追加到「个人照片」（牵线首页可切换展示的多张照片）。"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    user = snap_self_user()
    if not user:
        return jsonify({"error": "用户不存在"}), 404
    if _is_observer(user):
        return jsonify(OBSERVER_BLOCKED_MESSAGE), 403

    tokens = _photo_tokens(user)
    if len(tokens) >= MAX_PHOTOS:
        return jsonify({"error": f"最多只能上传 {MAX_PHOTOS} 张照片"}), 400

    f = request.files.get("file")
    if not f:
        return jsonify({"error": "未收到图片"}), 400
    data = f.read()
    if not data:
        return jsonify({"error": "图片为空"}), 400
    if len(data) > 10 * 1024 * 1024:
        return jsonify({"error": "图片不能超过10MB"}), 400
    try:
        Image.open(io.BytesIO(data)).verify()
    except Exception:
        return jsonify({"error": "文件不是有效图片"}), 400

    filename = (f.filename or "photo.jpg").rsplit("/", 1)[-1] or "photo.jpg"
    file_token = bitable.upload_attachment(data, filename, f.mimetype or "image/jpeg")
    if not file_token:
        return jsonify({"error": "图片上传失败，请稍后重试"}), 500

    tokens.append(file_token)
    if not _write_photos(user, tokens):
        return jsonify({"error": "资料更新失败，请稍后重试"}), 500
    return jsonify({"ok": True, "photos": _photo_urls(tokens)})


@app.route("/api/profile/photo", methods=["DELETE"])
def delete_profile_photo():
    """删除一张照片（按下标 0 起）。"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    user = snap_self_user()
    if not user:
        return jsonify({"error": "用户不存在"}), 404
    if _is_observer(user):
        return jsonify(OBSERVER_BLOCKED_MESSAGE), 403

    body = request.get_json(silent=True) or {}
    try:
        idx = int(body.get("index", -1))
    except (ValueError, TypeError):
        idx = -1

    tokens = _photo_tokens(user)
    if idx < 0 or idx >= len(tokens):
        return jsonify({"error": "照片不存在"}), 400
    tokens.pop(idx)
    if not _write_photos(user, tokens):
        return jsonify({"error": "资料更新失败，请稍后重试"}), 500
    return jsonify({"ok": True, "photos": _photo_urls(tokens)})


@app.route("/api/profile/photo/cover", methods=["POST"])
def set_profile_cover():
    """把某张照片设为封面（头像）：移到第一张。"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    user = snap_self_user()
    if not user:
        return jsonify({"error": "用户不存在"}), 404
    if _is_observer(user):
        return jsonify(OBSERVER_BLOCKED_MESSAGE), 403

    body = request.get_json(silent=True) or {}
    try:
        idx = int(body.get("index", -1))
    except (ValueError, TypeError):
        idx = -1

    tokens = _photo_tokens(user)
    if idx < 0 or idx >= len(tokens):
        return jsonify({"error": "照片不存在"}), 400
    if idx != 0:
        tokens.insert(0, tokens.pop(idx))
        if not _write_photos(user, tokens):
            return jsonify({"error": "资料更新失败，请稍后重试"}), 500
    return jsonify({"ok": True, "photos": _photo_urls(tokens)})


# ========== 我的喜欢 ==========

@app.route("/api/likes/mine", methods=["GET"])
def my_liked_list():
    """我喜欢的人列表（可取消喜欢）"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401

    my_likes = bitable.search_records(LIKE_TABLE_ID, [
        {"field_name": F_LIKE_INITIATOR_OPENID, "operator": "is", "value": [open_id]}])

    # 谁喜欢了我（用于判断相互喜欢；过滤已取消）
    liked_me = bitable.search_records(LIKE_TABLE_ID, [
        {"field_name": F_LIKE_TARGET_OPENID, "operator": "is", "value": [open_id]}])
    liked_me_oids = {
        bitable.get_field_text(l.get("fields", {}), F_LIKE_INITIATOR_OPENID)
        for l in liked_me
        if bitable.get_select_value(l.get("fields", {}), F_LIKE_STATUS) != "已取消"
    }

    result = []
    for like in my_likes:
        fields = like.get("fields", {})
        status = bitable.get_select_value(fields, F_LIKE_STATUS)
        if status == "已取消":
            continue
        target_oid = bitable.get_field_text(fields, F_LIKE_TARGET_OPENID)
        is_mutual = target_oid in liked_me_oids or status == "相互喜欢"
        # 获取对方信息
        target = snap_find_user_by_openid(target_oid) if target_oid else None
        if target:
            brief = format_user_brief(target, include_openid=True)
        else:
            # 目标用户解析失败（如尚未绑定飞书）时，用喜欢记录兜底，保证列表不空
            brief = {
                "openid": target_oid,
                "nickname": bitable.get_field_text(fields, F_LIKE_TARGET) or "用户",
                "gender": "", "church": "", "photo": "",
                "hearts": 0, "user_id": "",
            }
        brief["mutual"] = is_mutual
        brief["like_record_id"] = like["record_id"]
        brief["message"] = bitable.get_field_text(fields, F_LIKE_MESSAGE) if is_mutual else ""
        result.append(brief)
    return jsonify({"likes": result})


@app.route("/api/like/<target_openid>", methods=["DELETE"])
def cancel_like(target_openid):
    """取消喜欢（v6：定位→spool→立即回包；爱心自动随事件计算恢复）"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    gate = account_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]

    _intent_prune()
    cancel_rids = {rid for rid, _ in _intent_cancels.get(open_id, [])}
    existing = None
    for l in _snap("likes"):
        if l.get("record_id") in cancel_rids:
            continue
        f = l.get("fields", {})
        if (bitable.get_field_text(f, F_LIKE_INITIATOR_OPENID) == open_id
                and bitable.get_field_text(f, F_LIKE_TARGET_OPENID) == target_openid
                and bitable.get_select_value(f, F_LIKE_STATUS) != "已取消"):
            existing = l
            break
    pending_like = any(it["oid"] == open_id and it["target"] == target_openid
                       for it in _intent_likes.values())
    if not existing and not pending_like:
        existing = bitable.find_like(open_id, target_openid)
    if not existing and not pending_like:
        return jsonify({"error": "未找到喜欢记录"}), 404

    rid = existing["record_id"] if existing else None
    # 按对象幂等入队：worker 落库后或立即找到活跃记录执行取消；响应零等待
    _spool_append({"type": "cancel_pair",
                   "initiator_oid": open_id, "target_oid": target_openid})
    _intent_cancels.setdefault(open_id, []).append((target_openid, time.time()))
    # 消费对应喜欢意图：否则其桥接计数会让取消后仍少显示一颗
    for k in list(_intent_likes):
        it = _intent_likes[k]
        if it.get("oid") == open_id and it.get("target") == target_openid:
            _intent_likes.pop(k, None)

    # 本地快照即时置灰：保证紧随其后的 /api/cards 立即把对方放回卡片池
    for l in _snapshot.get("likes", []):
        lf = l.get("fields", {})
        if bitable.get_field_text(lf, F_LIKE_INITIATOR_OPENID) == open_id and \
                bitable.get_field_text(lf, F_LIKE_TARGET_OPENID) == target_openid:
            l.setdefault("fields", {})[F_LIKE_STATUS] = "已取消"

    # 反向切断（后台线程，幂等）
    def _cancel_reverse():
        try:
            reverse_likes = bitable.search_records(LIKE_TABLE_ID, [
                {"field_name": F_LIKE_INITIATOR_OPENID, "operator": "is", "value": [target_openid]},
                {"field_name": F_LIKE_TARGET_OPENID, "operator": "is", "value": [open_id]},
                {"field_name": F_LIKE_STATUS, "operator": "isNot", "value": ["已取消"]}
            ])
            for rl in reverse_likes:
                bitable.update_record(LIKE_TABLE_ID, rl["record_id"],
                                      {F_LIKE_STATUS: "已取消"})
        except Exception as e:
            app.logger.warning(f"反向取消喜欢失败: {e}")

    threading.Thread(target=_cancel_reverse, daemon=True).start()

    hearts_now = computed_hearts(open_id)
    return jsonify({"ok": True, "message": "已取消喜欢", "hearts": hearts_now})





# ========== 我的活动 ==========

@app.route("/api/activities/mine", methods=["GET"])
def my_activities():
    """我报名的活动列表"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401

    signups = snap_signups_by_openid(open_id)

    result = []
    for s in signups:
        fields = s.get("fields", {})
        act_id = bitable.get_field_text(fields, F_SIGNUP_ACTIVITY_ID)
        act = snap_find_activity(act_id)
        if act:
            act_data = format_activity(act)
            act_data["my_signup"] = True
            act_data["signup_status"] = bitable.get_field_text(fields, F_SIGNUP_STATUS)
            result.append(act_data)
    return jsonify({"activities": result})


@app.route("/api/activities/mine/groups", methods=["GET"])
def my_groups():
    """我报名的、已开启分组功能的活动列表（用于「我的分组」页面）"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401

    signups = snap_signups_by_openid(open_id)

    # 一次性查所有「我的分组志愿」，避免每个活动各查一次（N+1 慢查询）
    my_selections = snap_group_selections_by_selector(open_id)
    selection_by_act = {}
    for sel in my_selections:
        s_fields = sel.get("fields", {})
        aid = bitable.get_field_text(s_fields, F_GS_ACTIVITY_ID)
        if aid:
            selection_by_act[aid] = s_fields

    result = []
    for s in signups:
        fields = s.get("fields", {})
        act_id = bitable.get_field_text(fields, F_SIGNUP_ACTIVITY_ID)
        act = snap_find_activity(act_id)
        if not act:
            continue
        group_status = bitable.get_select_value(act.get("fields", {}), F_ACTIVITY_GROUP_STATUS)
        # 只展示已开启分组（非未开始）的活动
        if group_status in ("", "未开始"):
            continue
        act_data = format_activity(act)
        # 我的已提交志愿数量
        s_fields = selection_by_act.get(act_id)
        my_count = 0
        if s_fields:
            my_count = sum(1 for cf in F_GS_CHOICES if bitable.get_field_text(s_fields, cf))
        act_data["my_choice_count"] = my_count
        result.append(act_data)
    return jsonify({"activities": result})


@app.route("/api/activities/mine/groups/flag", methods=["GET"])
def my_groups_flag():
    """我报名过的活动中，是否存在「分组功能开启=是」的活动（控制「我的」页是否显示分组入口）"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401

    signups = snap_signups_by_openid(open_id)
    for s in signups:
        act_id = bitable.get_field_text(s.get("fields", {}), F_SIGNUP_ACTIVITY_ID)
        act = snap_find_activity(act_id)
        if not act:
            continue
        if bitable.get_select_value(act.get("fields", {}), F_ACTIVITY_GROUP_FLAG) == "是":
            return jsonify({"has_group_activity": True})
    return jsonify({"has_group_activity": False})


# ========== 活动报名名单 / 他人资料 / 通知 ==========

def load_notifications():
    """读取共享通知文件（机器人写入、H5 读取）"""
    return storage.load_json(NOTIFICATIONS_FILE, {"items": []}).get("items", [])


@app.route("/api/activities/<activity_id>/signups", methods=["GET"])
def activity_signups(activity_id):
    oid = require_login()
    if not oid:
        return jsonify({"error": "未登录"}), 401
    # 仅已报名该活动的用户可查看名单
    _, _text_act_id_chk = snap_resolve_activity(activity_id)
    if _text_act_id_chk and not bitable.get_user_signup(_text_act_id_chk, oid):
        return jsonify({"error": "未报名该活动，无权查看名单"}), 403
    act_record, text_act_id = snap_resolve_activity(activity_id)
    if not act_record:
        return jsonify({"error": "活动不存在"}), 404
    signups = snap_signups_by_activity(text_act_id)
    result = []
    for s in signups:
        f = s.get("fields", {})
        s_openid = bitable.get_field_text(f, F_SIGNUP_OPENID)
        if not s_openid:
            continue
        nick = bitable.get_field_text(f, F_SIGNUP_NICKNAME)
        u = snap_find_user_by_openid(s_openid)
        photo = ""
        user_id = ""
        gender = ""
        if u:
            uf = u.get("fields", {})
            tokens = bitable.get_attachment_tokens(uf, F_PHOTO)
            photo = "/api/image/" + tokens[0] if tokens else ""
            user_id = bitable.get_field_text(uf, F_USER_ID)
            gender = bitable.get_select_value(uf, F_GENDER)
        result.append({"openid": s_openid, "nickname": nick, "user_id": user_id, "photo": photo, "gender": gender})
    return jsonify({"signups": result})


@app.route("/api/users/<openid>/public", methods=["GET"])
def get_user_public(openid):
    oid = require_login()
    if not oid:
        return jsonify({"error": "未登录"}), 401
    u = snap_find_user_by_openid(openid)
    if not u:
        return jsonify({"error": "用户不存在"}), 404
    fields = u.get("fields", {})
    data = {
        "openid": bitable.get_field_text(fields, F_FEISHU_ID),
        "nickname": bitable.get_field_text(fields, F_NICKNAME),
        "gender": bitable.get_select_value(fields, F_GENDER),
        "photos": ["/api/image/" + t for t in bitable.get_attachment_tokens(fields, F_PHOTO)],
        "display_fields": build_display_fields(fields),
    }
    return jsonify(data)


@app.route("/api/notifications", methods=["GET"])
def get_notifications():
    oid = require_login()
    if not oid:
        return jsonify({"error": "未登录"}), 401
    items = [n for n in load_notifications() if n.get("recipient") == oid]
    items.sort(key=lambda n: n.get("time", ""), reverse=True)
    return jsonify({"notifications": items})


# ========== 引流埋点统计 ==========

def _load_track():
    return storage.load_json(TRACK_FILE, {"events": []})


@app.route("/api/track", methods=["POST"])
def track_event():
    """引流页埋点：记录访问/点击事件（公开接口，无需登录）"""
    data = request.get_json(silent=True) or {}
    event = data.get("event", "")
    source = data.get("from", "") or data.get("source", "") or "public"
    if event not in ("page_view", "join_click"):
        return jsonify({"ok": False, "error": "无效事件"}), 400

    def _add(track):
        track.setdefault("events", []).append({
            "event": event,
            "source": source,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ip": request.headers.get("X-Forwarded-For", request.remote_addr or ""),
        })
        # 磁盘恒定：超 20000 条时截断至最新 15000 条
        if len(track["events"]) > 20000:
            track["events"] = track["events"][-15000:]
        return track

    # update_json 跨进程 flock + 原子写，避免 gunicorn 多 worker 并发丢事件
    storage.update_json(TRACK_FILE, {"events": []}, _add)
    return jsonify({"ok": True})


@app.route("/api/track/stats", methods=["GET"])
def track_stats():
    """查看引流统计（按天/按事件聚合）——仅管理员可访问"""
    open_id = require_login()
    if not open_id or open_id not in ADMIN_OPEN_IDS:
        return jsonify({"error": "未授权"}), 403
    track = _load_track()
    events = track.get("events", [])
    by_day = {}
    by_event = {}
    for e in events:
        day = (e.get("time") or "")[:10]
        by_day[day] = by_day.get(day, 0) + 1
        ev = e.get("event", "")
        by_event[ev] = by_event.get(ev, 0) + 1
    return jsonify({"total": len(events), "by_day": by_day, "by_event": by_event})


@app.route("/api/public/users", methods=["GET"])
def public_users():
    """公开接口：返回活跃用户列表（引流页展示用，不含敏感信息；走本地快照）"""
    all_users = snap_active_users()
    result = []
    for u in all_users:
        fields = u.get("fields", {})
        result.append({
            "nickname": bitable.get_field_text(fields, F_NICKNAME),
            "gender": bitable.get_select_value(fields, F_GENDER),
            "photos": ["/api/image/" + t for t in bitable.get_attachment_tokens(fields, F_PHOTO)],
            "display_fields": build_display_fields(fields),
        })
    return jsonify({"users": result})


if __name__ == "__main__":
    logger = logging.getLogger(__name__)
    # 启动时先同步拉一次快照，保证读接口立即可用；随后后台定时刷新
    refresh_snapshot()
    start_snapshot_loop()
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False, threaded=True)
