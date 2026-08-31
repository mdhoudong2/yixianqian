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
from PIL import ImageOps

# 共享库 lib/ 位于仓库根目录（web/backend 的上两级）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)
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
    "users": [], "observers": [], "activities": [], "signups": [],
    "likes": [], "group_selections": [], "group_results": [],
    "messages": [],
}
_snapshot_lock = threading.RLock()
# 已成功加载过的表：区分「未加载」与「加载后为空」。
# 飞书表合法地没有记录时（如活动表空档期），空快照是权威结果，不应每次请求回退实时查询。
_snapshot_loaded = set()

_SNAPSHOT_FETCHERS = {
    "users": lambda: bitable.raw_search_records(USER_TABLE_ID),
    "observers": (lambda: bitable.raw_search_records(OBSERVER_TABLE_ID)) if OBSERVER_TABLE_ID else None,
    "activities": lambda: bitable.raw_search_records(ACTIVITY_TABLE_ID),
    "signups": lambda: bitable.raw_search_records(SIGNUP_TABLE_ID),
    "likes": lambda: bitable.raw_search_records(LIKE_TABLE_ID),
    "group_selections": lambda: bitable.raw_search_records(GROUP_SELECT_TABLE),
    "group_results": lambda: bitable.raw_search_records(GROUP_RESULT_TABLE),
    "messages": lambda: bitable.raw_search_records(MESSAGE_TABLE_ID),
}
_SNAPSHOT_FETCHERS = {k: v for k, v in _SNAPSHOT_FETCHERS.items() if v}


def refresh_snapshot_table(key):
    """只刷新快照中的单个表（写操作后调用，保证读到的数据最新）"""
    fetcher = _SNAPSHOT_FETCHERS.get(key)
    if not fetcher:
        return
    try:
        data = fetcher()
        if data is None:
            logging.getLogger(__name__).warning(f"刷新快照表 {key} 跳过：飞书查询失败，保留旧快照 {len(_snapshot.get(key, []))} 条")
            return
        with _snapshot_lock:
            _snapshot[key] = data
            _snapshot_loaded.add(key)
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


def _snap_ready(key):
    """该表快照是否已成功加载过（空表也算就绪，无需回退实时查询）"""
    with _snapshot_lock:
        return key in _snapshot_loaded


# ---- 快照读取辅助（镜像 bitable 常用查询；快照为空时回退到飞书，保证启动初期可用） ----

def _pick_primary_user(records):
    """同一 open_id 多条记录时取主档案：单身优先，其次用户ID最小（与 bot/queries 同规则）"""
    if not records:
        return None

    def rank(u):
        uf = u.get("fields", {})
        st = bitable.get_select_value(uf, F_ACCOUNT_STATUS)
        m = re.match(r"[Uu]-?(\d+)", bitable.get_field_text(uf, F_USER_ID))
        uid_num = int(m.group(1)) if m else 10 ** 9
        return (0 if st == "单身" else 1, uid_num)

    return sorted(records, key=rank)[0]


# ========== v6：事件溯源爱心态 + 提交管线（spool 幂等异步写入） ==========
import uuid as _uuid

_spool_lock = threading.Lock()
_spool_queue = []
_SPOOL_FILE = os.path.join(SHARED_DATA_DIR, "yixianqian_spool.jsonl")
_SPOOL_DEAD = os.path.join(SHARED_DATA_DIR, "yixianqian_spool_failed.log")
_BALANCE_FILE = os.path.join(SHARED_DATA_DIR, "yixianqian_balances.json")
_REPORTED_HISTORY_FILE = os.path.join(SHARED_DATA_DIR, "yixianqian_reported_history.json")

# 在途意图（进程级，页面重载不丢失）：
#   _intent_likes    oid -> [(temp_key, ts)]  喜欢已受理、尚未在快照可见（TTL 20s，快照15s周期+余量）
#   _intent_cancels  oid -> [(record_id, ts)] 取消已受理、快照可能仍显示单身（TTL 60s）
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


def hearts_total(open_id):
    """爱心总额 = 初始 + 邀请奖励（上限 MAX_HEARTS），不含已使用"""
    invites = _balances_file().get("invites", {}).get(open_id, 0)
    return min(MAX_HEARTS, INITIAL_HEARTS + invites)


def _pick_primary_user(records):
    """同一 open_id 多条记录时取主档案：单身优先，其次用户ID最小（与 bot/queries 同规则）"""
    if not records:
        return None

    def rank(u):
        uf = u.get("fields", {})
        st = bitable.get_select_value(uf, F_ACCOUNT_STATUS)
        m = re.match(r"[Uu]-?(\d+)", bitable.get_field_text(uf, F_USER_ID))
        uid_num = int(m.group(1)) if m else 10 ** 9
        return (0 if st == "单身" else 1, uid_num)

    return sorted(records, key=rank)[0]


def snap_find_user_by_openid(open_id):
    """快照优先、实时兜底；同号多档统一解析到主档案（用户表 + 村情六处独立表）。
    纯观察员（仅观察员表有记录）也能被解析，否则登录入口会误判「尚未注册」。"""
    users = _snap("users")
    matches = [u for u in users
               if bitable.get_field_text(u.get("fields", {}), F_FEISHU_ID) == open_id]
    if OBSERVER_TABLE_ID:
        obs = _snap("observers")
        matches += [o for o in obs
                    if bitable.get_field_text(o.get("fields", {}), F_FEISHU_ID) == open_id]
    if matches:
        return _pick_primary_user(matches)
    # 快照为空或未命中（如快照在用户写入前加载、尚未刷新）：回退到飞书直查，避免误判「用户不存在」
    recs = bitable.search_records(USER_TABLE_ID, [
        {"field_name": F_FEISHU_ID, "operator": "is", "value": [open_id]}])
    if OBSERVER_TABLE_ID:
        recs += bitable.search_records(OBSERVER_TABLE_ID, [
            {"field_name": F_FEISHU_ID, "operator": "is", "value": [open_id]}])
    return _pick_primary_user(recs)


def snap_active_users():
    users = _snap("users")
    if not users and not _snap_ready("users"):
        return bitable.get_all_users()
    return [u for u in users
            if bitable.get_select_value(u.get("fields", {}), F_ACCOUNT_STATUS) == "单身"]


def snap_find_activity(act_id):
    activities = _snap("activities")
    if not activities and not _snap_ready("activities"):
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
    if not activities and not _snap_ready("activities"):
        return bitable.get_activities()
    return activities


def snap_likes_by_target(open_id):
    likes = _snap("likes")
    if not likes and not _snap_ready("likes"):
        return bitable.search_records(LIKE_TABLE_ID, [
            {"field_name": F_LIKE_TARGET_OPENID, "operator": "is", "value": [open_id]}])
    return [l for l in likes
            if bitable.get_field_text(l.get("fields", {}), F_LIKE_TARGET_OPENID) == open_id]


def snap_likes_by_initiator(open_id):
    likes = _snap("likes")
    if not likes and not _snap_ready("likes"):
        return bitable.search_records(LIKE_TABLE_ID, [
            {"field_name": F_LIKE_INITIATOR_OPENID, "operator": "is", "value": [open_id]}])
    return [l for l in likes
            if bitable.get_field_text(l.get("fields", {}), F_LIKE_INITIATOR_OPENID) == open_id]


def snap_signups_by_openid(open_id):
    signups = _snap("signups")
    if not signups and not _snap_ready("signups"):
        return bitable.search_records(SIGNUP_TABLE_ID, [
            {"field_name": F_SIGNUP_OPENID, "operator": "is", "value": [open_id]}])
    return [s for s in signups
            if bitable.get_field_text(s.get("fields", {}), F_SIGNUP_OPENID) == open_id]


def snap_signups_by_activity(act_id):
    signups = _snap("signups")
    if not signups and not _snap_ready("signups"):
        return bitable.get_signups(act_id)
    return [s for s in signups
            if bitable.get_field_text(s.get("fields", {}), F_SIGNUP_ACTIVITY_ID) == act_id
            and bitable.get_select_value(s.get("fields", {}), F_SIGNUP_STATUS) != "已取消"]


def snap_signup(act_id, open_id):
    signups = _snap("signups")
    if not signups and not _snap_ready("signups"):
        return bitable.get_user_signup(act_id, open_id)
    for s in signups:
        f = s.get("fields", {})
        if (bitable.get_field_text(f, F_SIGNUP_ACTIVITY_ID) == act_id
                and bitable.get_field_text(f, F_SIGNUP_OPENID) == open_id
                and bitable.get_select_value(f, F_SIGNUP_STATUS) != "已取消"):
            return s
    return None


def snap_group_selections_by_selector(open_id):
    gs = _snap("group_selections")
    if not gs and not _snap_ready("group_selections"):
        return bitable.search_records(GROUP_SELECT_TABLE, [
            {"field_name": F_GS_SELECTOR_OID, "operator": "is", "value": [open_id]}])
    return [g for g in gs
            if bitable.get_field_text(g.get("fields", {}), F_GS_SELECTOR_OID) == open_id]


def snap_group_selection(act_id, open_id):
    gs = _snap("group_selections")
    if not gs and not _snap_ready("group_selections"):
        return bitable.get_user_group_selection(act_id, open_id)
    for sg in gs:
        f = sg.get("fields", {})
        if bitable.get_field_text(f, F_GS_ACTIVITY_ID) == act_id and bitable.get_field_text(f, F_GS_SELECTOR_OID) == open_id:
            return sg
    return None


def snap_group_results(act_id):
    gr = _snap("group_results")
    if not gr and not _snap_ready("group_results"):
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
    """返回该 open_id 拥有的角色集合 {"user","observer"}（快照优先、实时兜底）。

    「user」角色仅在存在有效普通用户档案时授予：单身/已脱单/待审核。
    「审核不通过」「已退出」不算有效用户档案——否则用户先提交了被拒/退出记录、
    后成功注册为观察员时，会被误判同时拥有 user 角色，登录默认进 user 档而被「未通过审核」门禁拦住。
    「observer」角色：村情六处独立表里有该 open_id 的记录。
    """
    roles = set()
    users = _snap("users")
    user_matches = [u for u in users
                    if bitable.get_field_text(u.get("fields", {}), F_FEISHU_ID) == open_id]
    if not user_matches:
        user_matches = bitable.search_records(USER_TABLE_ID, [
            {"field_name": F_FEISHU_ID, "operator": "is", "value": [open_id]}])
    for u in user_matches:
        st = bitable.get_select_value(u.get("fields", {}), F_ACCOUNT_STATUS)
        if st in ("单身", "已脱单", "待审核"):
            roles.add("user")
    if OBSERVER_TABLE_ID:
        obs = _snap("observers")
        obs_matches = [o for o in obs
                       if bitable.get_field_text(o.get("fields", {}), F_FEISHU_ID) == open_id]
        if not obs_matches:
            obs_matches = bitable.search_records(OBSERVER_TABLE_ID, [
                {"field_name": F_FEISHU_ID, "operator": "is", "value": [open_id]}])
        if obs_matches:
            roles.add("observer")
    return roles


def snap_self_user():
    """按当前会话 role 解析「本人」档案：
    observer → 村情六处独立表中的记录；user → 用户表主档（单身优先）。
    快照优先；若快照显示门禁态（待审核/审核不通过/已退出），实时直查兜底，
    避免刚被审核通过（待审核→单身）时仍读到旧快照的「审核中」。"""
    open_id = g.yxq_open_id
    if not open_id:
        return None

    if g.yxq_role == "observer":
        obs = _snap("observers")
        matches = [o for o in obs
                   if bitable.get_field_text(o.get("fields", {}), F_FEISHU_ID) == open_id]
        if not matches and OBSERVER_TABLE_ID:
            matches = bitable.search_records(OBSERVER_TABLE_ID, [
                {"field_name": F_FEISHU_ID, "operator": "is", "value": [open_id]}])
        return matches[0] if matches else None

    users = _snap("users")
    matches = [u for u in users
               if bitable.get_field_text(u.get("fields", {}), F_FEISHU_ID) == open_id]
    rec = _pick_primary_user(matches) if matches else None
    # 快照未命中，或快照显示门禁态（可能已被审核通过但快照未刷新）→ 实时直查
    gated = bool(rec) and bitable.get_select_value(
        rec.get("fields", {}), F_ACCOUNT_STATUS) in ("待审核", "审核不通过", "已退出")
    if not rec or gated:
        live = bitable.search_records(USER_TABLE_ID, [
            {"field_name": F_FEISHU_ID, "operator": "is", "value": [open_id]}])
        if live:
            rec = _pick_primary_user(live)
    return rec


# ========== 账号状态门禁 ==========
# 三级权限：
#   单身        → 全功能
#   已脱单(自隐) → 可浏览、可取消喜欢/取消报名；不可点爱心、不可报名、不可提交志愿
#   待审核/审核不通过/已退出 → 仅可登录与查看「我的资料」，内容与操作全拦
GATE_MESSAGES = {
    "待审核": {"error": "资料审核中，通过后即可使用一线牵App", "gate": "待审核"},
    "审核不通过": {"error": "很抱歉，你的资料未通过审核，如有疑问请联系管理员", "gate": "审核不通过"},
    "已退出": {"error": "你已暂时退出相亲市场，如需恢复请联系管理员", "gate": "已退出"},
}
LIKE_BLOCKED_MESSAGE = {"error": "你当前处于已脱单状态（他人看不到你），请先在「我的」页恢复单身后再操作", "gate": "已脱单"}
OBSERVER_BLOCKED_MESSAGE = {"error": "你是村情六处账号，无此操作权限", "gate": "观察员"}


def _account_status(open_id):
    user = snap_self_user()
    if not user:
        return None, None
    return bitable.get_select_value(user.get("fields", {}), F_ACCOUNT_STATUS), user


def account_gate(open_id):
    """浏览级门禁：待审核/审核不通过/已退出 全拦。返回 None 放行；否则 (body, status)"""
    status, _ = _account_status(open_id)
    if status is None:
        return {"error": "用户不存在"}, 404
    body = GATE_MESSAGES.get(status)
    if body:
        return body, 403
    return None


def active_gate(open_id):
    """操作级门禁：仅「单身」可用（点爱心/报名/提交志愿）。"""
    status, _ = _account_status(open_id)
    if status is None:
        return {"error": "用户不存在"}, 404
    if status == "已脱单":
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
    photo_url = ("/api/image/" + photos[0] + "?fv6") if photos else ""
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
            "house": _select_with_note(fields, F_HOUSE, F_HOUSE_NOTE_HAVE, F_HOUSE_NOTE_NONE),
            "driving": _select_with_note(fields, F_DRIVING, F_DRIVING_NOTE_HAVE, F_DRIVING_NOTE_NONE),
            "family": bitable.get_field_text(fields, F_FAMILY),
            "live_with_parents": bitable.get_select_value(fields, F_LIVE_WITH_PARENTS),
            "birthday": format_birthday(fields),
            "photos": ["/api/image/" + t + "?fv6" for t in photos],
            "faces": [get_face_center(t) for t in photos],
        })
    if include_openid:
        data["openid"] = bitable.get_field_text(fields, F_FEISHU_ID)
    return data


def _select_with_note(fields, select_key, note_have_key, note_none_key):
    """单选值 + 对应补充内容：如「有 · 补充内容」，补充内容为空则只返回「有」/「无」"""
    val = bitable.get_select_value(fields, select_key)
    if not val:
        return ""
    note_key = note_have_key if val == "有" else note_none_key
    note = bitable.get_field_text(fields, note_key).strip()
    return f"{val} · {note}" if note else val


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
    poster_url = ("/api/image/" + posters[0] + "?fv6") if posters else ""
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
    # 房产/汽车：单选值 + 补充内容（如「有 · 已购2套」，补充为空则只显示「有」/「无」）
    if fname == F_HOUSE:
        return _select_with_note(fields, F_HOUSE, F_HOUSE_NOTE_HAVE, F_HOUSE_NOTE_NONE)
    if fname == F_DRIVING:
        return _select_with_note(fields, F_DRIVING, F_DRIVING_NOTE_HAVE, F_DRIVING_NOTE_NONE)
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


def order_cards(cards, liked_me_openids):
    """牵线卡片排序：整副牌随机打乱，避免按用户ID连号泄露身份。

    喜欢我的人不集中放在最前，而是随机前移到前 30% 区域（随机散落、位置随机），
    既能被较快翻到，又不会因「前几张/前10张都是喜欢我的人」暴露是谁。
    """
    if not cards or not liked_me_openids:
        random.shuffle(cards)
        return cards
    liked = [c for c in cards if c.get("openid") in liked_me_openids]
    others = [c for c in cards if c.get("openid") not in liked_me_openids]
    random.shuffle(liked)
    random.shuffle(others)
    n = len(cards)
    front_n = max(1, int(n * 0.3))
    take_liked = min(len(liked), front_n)
    positions = set(random.sample(range(front_n), take_liked))
    front = [None] * front_n
    li = oi = 0
    for i in range(front_n):
        if i in positions:
            front[i] = liked[li]
            li += 1
        else:
            front[i] = others[oi]
            oi += 1
    rest = liked[li:] + others[oi:]
    random.shuffle(rest)
    return front + rest


def _coerce_filter_num(v):
    """筛选数值参数安全转 float：None/空/非数值（含 bool/nan/inf）一律视为未填。

    用于年龄/身高等区间筛选，杜绝字符串或异常值与年龄整数比较时抛 TypeError 导致 500。
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        num = float(v)
    except (TypeError, ValueError):
        return None
    if num != num or abs(num) == float("inf"):
        return None
    return num


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
    age_min = _coerce_filter_num(f.get("age_min"))
    age_max = _coerce_filter_num(f.get("age_max"))
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
    if f.get("income"):
        inc = f["income"]
        if isinstance(inc, str):
            inc = [inc] if inc else []
        if inc:
            iv = bitable.get_select_value(fields, F_INCOME) or "未填"
            if iv not in inc:
                return False
    # 多选精确匹配（房产状况/是否有车），支持「未填」（空值），可叠加
    if f.get("house"):
        hf = f["house"]
        if isinstance(hf, str):
            hf = [hf] if hf else []
        if hf:
            hv = bitable.get_select_value(fields, F_HOUSE) or "未填"
            if hv not in hf:
                return False
    if f.get("driving"):
        df = f["driving"]
        if isinstance(df, str):
            df = [df] if df else []
        if df:
            dv = bitable.get_select_value(fields, F_DRIVING) or "未填"
            if dv not in df:
                return False
    # 文本包含匹配
    for key, fname in [
        ("user_id", F_USER_ID),
        ("church", F_CHURCH), ("native_place", F_NATIVE_PLACE), ("city", F_CITY),
        ("industry", F_INDUSTRY),
    ]:
        if f.get(key) and f[key] not in bitable.get_field_text(fields, fname):
            return False
    return True


def _has_active_filter(filters):
    """是否启用了任一筛选条件（列表非空 / 标量非空）。
    观察员自预览卡片仅在无任何筛选时置顶，否则会混进筛选结果造成「筛 U-0022 却看到自己」。"""
    for v in filters.values():
        if isinstance(v, list):
            if v:
                return True
        elif v not in (None, ""):
            return True
    return False


# ========== 页面路由 ==========

# index.html 里的 __FEISHU_APP_ID__ 占位符在服务时替换为当前环境 app_id（生产/测试各自不同）
_INDEX_HTML_CACHE = None


def _app_version():
    """前端版本号：取 index.html 的 mtime（整数秒）。每次 git pull 部署后 mtime 变化，
    前端据此检测到新版本并自动刷新，规避飞书 WebView 长期驻留旧 JS 的缓存问题。"""
    try:
        return str(int(os.path.getmtime(os.path.join(app.static_folder, "index.html"))))
    except OSError:
        return "0"


def _render_index():
    """读取单文件前端并注入飞书 app_id 与前端版本号，返回不缓存的 HTML 响应。"""
    global _INDEX_HTML_CACHE
    if _INDEX_HTML_CACHE is None:
        with open(os.path.join(app.static_folder, "index.html"), "r", encoding="utf-8") as f:
            _INDEX_HTML_CACHE = f.read()
    html = _INDEX_HTML_CACHE.replace("__FEISHU_APP_ID__", FEISHU_APP_ID)
    html = html.replace("__APP_VERSION__", _app_version())
    resp = make_response(html)
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/api/version", methods=["GET"])
def api_version():
    """当前前端版本号，供前端轮询比对、发现新版本后自动刷新。"""
    return jsonify({"version": _app_version()})


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


def cleanup_image_cache(max_age_days=3, max_files=500):
    """过期与超量清理：删 7 天前文件，超 1000 个时删最旧的（后台线程每日执行）"""
    try:
        files = [(os.path.join(IMAGE_CACHE_DIR, f), os.path.getmtime(os.path.join(IMAGE_CACHE_DIR, f)))
                 for f in os.listdir(IMAGE_CACHE_DIR)
                 if not os.path.isdir(os.path.join(IMAGE_CACHE_DIR, f))]
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
                    # 同步清理对应人脸 sidecar，避免孤儿
                    token = os.path.basename(path).split(".")[0]
                    sidecar = os.path.join(_FACE_DIR, token + ".json")
                    if os.path.exists(sidecar):
                        os.remove(sidecar)
                except Exception:
                    pass
        # 額外：清理孤儿 sidecar（图已删但 sidecar 仍在）
        try:
            if os.path.exists(_FACE_DIR):
                for sf in os.listdir(_FACE_DIR):
                    if not sf.endswith(".json"):
                        continue
                    tok = sf[:-5]
                    if not _get_cached_image(tok):
                        try:
                            os.remove(os.path.join(_FACE_DIR, sf))
                        except Exception:
                            pass
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
        if fname.endswith(".tmp"):
            continue
        if fname.startswith(file_token + "."):
            fpath = os.path.join(IMAGE_CACHE_DIR, fname)
            ext = fname.rsplit(".", 1)[-1].lower()
            ct_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                      "gif": "image/gif", "webp": "image/webp", "heic": "image/heic",
                      "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm"}
            return fpath, ct_map.get(ext, "image/jpeg")
    return None


def _compress_image(image_bytes, ext):
    """用 Pillow 压缩图片，返回压缩后的字节。
    - 最大宽度 1080px，等比缩放，不放大
    - JPEG 质量 85，PNG 优化
    - GIF 原样返回；HEIC 转 JPEG（pillow-heif）
    """
    if ext == "gif":
        return image_bytes
    if ext == "heic":
        try:
            import io

            import pillow_heif
            from PIL import Image
            heif = pillow_heif.open_heif(io.BytesIO(image_bytes))
            im = Image.frombytes(heif.mode, heif.size, heif.data, "raw", heif.mode, heif.stride)
            if max(im.size) > 1080:
                ratio = 1080.0 / im.width
                new_h = int(im.height * ratio)
                im = im.resize((1080, new_h), Image.LANCZOS)
            if im.mode != "RGB":
                im = im.convert("RGB")
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=85, optimize=True)
            return out.getvalue()
        except Exception:
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
            # 大图二次压：超过 1M 或宽度仍>720 的用更低质量
            is_large = len(image_bytes) > 1024*1024
            if is_large and img.width > 720:
                ratio = 720.0 / img.width
                new_h = int(img.height * ratio)
                img = img.resize((720, new_h), Image.LANCZOS)
            save_params = {"quality": 75 if is_large else 85, "optimize": True}
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
                   "image/webp": "webp", "image/heic": "heic",
                   "video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm"}
        ext = ext_map.get(content_type, "jpg")
        if content_type.startswith("video/"):
            compressed = resp.content
        else:
            compressed = _compress_image(resp.content, ext)
        cache_path = os.path.join(IMAGE_CACHE_DIR, "%s.%s" % (file_token, ext))
        tmp = cache_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(compressed)
        os.replace(tmp, cache_path)  # 原子替换，避免与请求线程并发写坏
        return True
    except Exception:
        return False


def warm_image_cache(max_per_run=30):
    """后台预热：增量下载 + 人脸异步补齐，避免长时间持有锁阻塞快照"""
    tokens = set()
    for u in _snap("users"):
        tokens.update(bitable.get_attachment_tokens(u.get("fields", {}), F_PHOTO))
    for a in _snap("activities"):
        tokens.update(bitable.get_attachment_tokens(a.get("fields", {}), F_ACTIVITY_POSTER))
    # 仅处理未缓存的，限制每轮数量，避免一次性下载数百张阻塞快照循环
    pending = [tok for tok in tokens if tok and not _get_cached_image(tok)]
    to_download = pending[:max_per_run]
    warmed = 0
    for t in to_download:
        try:
            with _image_warm_lock:
                if _download_and_cache_image(t):
                    warmed += 1
        except Exception as e:
            logging.getLogger(__name__).warning(f"预热图片 {t} 失败: {e}")
    if warmed:
        logging.getLogger(__name__).info(f"图片预热完成，本次下载 {warmed} 张，待下载 {len(pending)-warmed} 张")
    # 人脸检测异步批量，不阻塞快照循环；有效 sidecar 已存在则跳过
    def _bg_face_batch():
        for tok in list(tokens)[:max_per_run]:
            if not tok:
                continue
            try:
                if not _get_cached_image(tok):
                    continue
                p = _face_sidecar_path(tok)
                if os.path.exists(p):
                    try:
                        with open(p) as f:
                            d = json.load(f)
                        if _face_sidecar_valid(d):
                            continue
                    except Exception:
                        pass
                ensure_face_center(tok)
            except Exception:
                pass
    try:
        threading.Thread(target=_bg_face_batch, daemon=True).start()
    except Exception:
        pass


# ========== 人脸检测（卡片照片智能对准） ==========
# YuNet（精度高、支持侧脸，模型约 350KB，首次从 opencv_zoo 下载缓存）；
# 下载/加载失败时回退 OpenCV 自带 Haar 级联；两者都不可用则整体静默降级为居中显示。

_FACE_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "face_detection_yunet.onnx")
_FACE_MODEL_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
                   "face_detection_yunet/face_detection_yunet_2023mar.onnx")
_FACE_DIR = os.path.join(IMAGE_CACHE_DIR, ".face")
_FACE_SIDECAR_VERSION = 9  # 检测参数变更时递增，旧 sidecar 自动重检；v9 多脸主体权重（面积×中心）

_face_cache = {}          # token -> [cx, cy]（比例 0~1）；None 表示已检测但无可用人脸
_face_cache_lock = threading.Lock()
_face_detector_state = None  # (kind, detector) kind in {"yunet", "haar", "none"}


def _load_face_detector():
    """懒加载人脸检测器（单例）。返回 (kind, detector)，不可用时 ("none", None)"""
    global _face_detector_state
    if _face_detector_state is not None:
        return _face_detector_state
    try:
        import cv2
    except ImportError:
        _face_detector_state = ("none", None)
        return _face_detector_state
    # YuNet 优先
    if not os.path.exists(_FACE_MODEL_PATH):
        try:
            r = requests.get(_FACE_MODEL_URL, timeout=20)
            if r.status_code == 200 and len(r.content) > 10000:
                tmp = _FACE_MODEL_PATH + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(r.content)
                os.replace(tmp, _FACE_MODEL_PATH)
        except Exception:
            pass
    if os.path.exists(_FACE_MODEL_PATH):
        try:
            det = cv2.FaceDetectorYN.create(_FACE_MODEL_PATH, "", (320, 320), score_threshold=0.6)
            _face_detector_state = ("yunet", det)
            return _face_detector_state
        except Exception:
            pass
    try:
        haar = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if haar.empty():
            _face_detector_state = ("none", None)
        else:
            _face_detector_state = ("haar", haar)
    except Exception:
        _face_detector_state = ("none", None)
    return _face_detector_state


def _face_sidecar_path(token):
    return os.path.join(_FACE_DIR, token + ".json")


def _detect_content_margins(gray):
    """检测上下左右黑边比例（连续边缘行/列最大亮度 < 16 视为黑边）。
    返回 (top, bottom, left, right) 比例；任一侧黑边超 45% 视为整体过暗，放弃该侧。"""
    h, w = gray.shape[:2]

    def dark_row(y):
        return int(gray[y].max()) < 16

    def dark_col(x):
        return int(gray[:, x].max()) < 16
    top = 0
    while top < h and dark_row(top):
        top += 1
    bottom = 0
    while bottom < h - top - 1 and dark_row(h - 1 - bottom):
        bottom += 1
    left = 0
    while left < w and dark_col(left):
        left += 1
    right = 0
    while right < w - left - 1 and dark_col(w - 1 - right):
        right += 1
    if top > h * 0.45:
        top = 0
    if bottom > h * 0.45:
        bottom = 0
    if left > w * 0.45:
        left = 0
    if right > w * 0.45:
        right = 0
    return (top / h, bottom / h, left / w, right / w)


def _analyze_photo(img_path):
    """分析照片：检测最大「完整可信」人脸 + 上下左右黑边比例。
    返回 (face, tb, lr)：face 为 {'c':[cx,cy], 'box':[bw,bh]} 或 None（无脸）；
    tb=[top,bottom]、lr=[left,right] 黑边比例（无脸时也检测黑边）。
    完整性规则（宁缺毋滥）：框与图像交叠面积 < 70% → 丢弃；轻贴边可接受。
    黑边不得侵入人脸附近（框顶-0.3框高约束，避免黑色头发被误判为黑边）。"""
    kind, det = _load_face_detector()
    if kind == "none":
        return None, [0.0, 0.0], [0.0, 0.0]
    try:
        import cv2
        import numpy as np
        from PIL import Image
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        with Image.open(img_path) as im0:
            im0 = im0.convert("RGB")
            w0, h0 = im0.size
            if w0 < 40 or h0 < 40:
                return None, [0.0, 0.0], [0.0, 0.0]
            # 检测用缩略图（最长边 1080），坐标按比例映射回原图；旧缓存大图（3072+）避免全尺寸检测
            if max(w0, h0) > 1080:
                r = 1080.0 / max(w0, h0)
                im0 = im0.resize((max(1, int(w0 * r)), max(1, int(h0 * r))), Image.BILINEAR)
            img = cv2.cvtColor(np.array(im0), cv2.COLOR_RGB2BGR)
            h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        tb, bb, lb, rb = _detect_content_margins(gray)
        boxes = []
        if kind == "yunet":
            det.setInputSize((w, h))
            _, faces = det.detect(img)
            if faces is not None:
                for f in faces:
                    if len(f) >= 4:
                        score = float(f[14]) if len(f) >= 15 else 1.0
                        boxes.append([float(f[0]), float(f[1]), float(f[2]), float(f[3]), score])
        else:
            for (x, y, bw, bh) in det.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                                       minSize=(40, 40)):
                boxes.append([float(x), float(y), float(bw), float(bh), 1.0])
        if not boxes:
            return None, [tb, bb], [lb, rb]
        valid = []
        for b in boxes:
            x, y, bw, bh = b[:4]
            # 框与图像的交叠比例：可见部分面积 / 框面积，低于 70% 视为严重裁切不可信
            vis_w = max(0.0, min(x + bw, w) - max(x, 0.0))
            vis_h = max(0.0, min(y + bh, h) - max(y, 0.0))
            ratio = (vis_w * vis_h) / (bw * bh) if bw > 0 and bh > 0 else 0.0
            if ratio < 0.7:
                continue  # 脸被大面积裁切，不可信
            valid.append(b)
        if not valid:
            return None, [tb, bb], [lb, rb]
        # 选择目标脸：
        # 1) 置信度显著最高者优先（真人脸置信度通常明显高于卡通/画像，差>=0.12）
        # 2) 置信度接近（都是真人）时，按「面积 × 靠近画面中心程度」选主体
        #    （一图多脸本人/多人合照：主体通常在画面中心附近且较大）
        import math
        best_score = max(valid, key=lambda b: b[4])
        best_area = max(valid, key=lambda b: b[2] * b[3])
        if best_score[4] - best_area[4] >= 0.12:
            b = best_score
        else:
            def _subject_weight(box):
                cx = (box[0] + box[2] / 2) / w
                cy = (box[1] + box[3] / 2) / h
                d2 = (cx - 0.5) ** 2 + (cy - 0.5) ** 2
                return math.exp(-d2 / 0.16)  # σ≈0.28：越靠中心权重越大
            b = max(valid, key=lambda b: b[2] * b[3] * _subject_weight(b))
        cx = (b[0] + b[2] / 2) / w
        cy = (b[1] + b[3] / 2) / h
        # 黑边不得侵入人脸附近（黑色头发可能被边缘扫描误判为黑边）：
        # 顶部黑边不超过 框顶-0.15框高，底部黑边不超过 1-(框底+0.15框高)
        box_top = b[1] / h
        box_bot = (b[1] + b[3]) / h
        tb = min(tb, max(0.0, box_top - 0.15 * (b[3] / h)))
        bb = min(bb, max(0.0, 1.0 - (box_bot + 0.15 * (b[3] / h))))
        return ({"c": [round(cx, 4), round(cy, 4)],
                 "box": [round(b[2] / w, 4), round(b[3] / h, 4)]},
                [round(tb, 4), round(bb, 4)], [round(lb, 4), round(rb, 4)])
    except Exception as e:
        logging.getLogger(__name__).warning(f"照片分析失败 {img_path}: {e}")
        return None, [0.0, 0.0], [0.0, 0.0]


def _crop_photo_margins(img_path, tb, lr):
    """按黑边比例裁切缓存图片（原子替换）。任一方向裁后剩余 < 40px 则放弃。"""
    t, b = tb
    l, r = lr
    if t + b + l + r <= 0.004:
        return False
    try:
        from PIL import Image
        with Image.open(img_path) as im:
            im = im.convert("RGB")
            w, h = im.size
            box = (int(l * w), int(t * h), int((1 - r) * w), int((1 - b) * h))
            if box[2] - box[0] < 40 or box[3] - box[1] < 40:
                return False
            crop = im.crop(box)
            ext = img_path.rsplit(".", 1)[-1].lower()
            fmt = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}.get(ext, "JPEG")
            tmp = img_path + ".tmp"
            crop.save(tmp, format=fmt, quality=85, optimize=True)
            os.replace(tmp, img_path)
            return True
    except Exception as e:
        logging.getLogger(__name__).warning(f"裁切黑边失败 {img_path}: {e}")
    return False


def _face_sidecar_valid(d):
    """sidecar 数据是否完整且为当前版本（版本升级后旧数据自动重检；c/box 可为 None 表示已检测无脸）"""
    return (isinstance(d, dict) and "c" in d and "box" in d
            and d.get("v") == _FACE_SIDECAR_VERSION)


def ensure_face_center(token):
    """确保照片处理完毕（人脸数据 + 黑边已裁切）：内存 → 完整 sidecar → 分析并裁切。
    仅后台路径调用；旧版 sidecar 自动重新处理；裁切后缓存文件即无黑边版。"""
    with _face_cache_lock:
        if token in _face_cache:
            return _face_cache[token]
    try:
        p = _face_sidecar_path(token)
        if os.path.exists(p):
            with open(p) as f:
                d = json.load(f)
            if _face_sidecar_valid(d):
                face = ({"c": d["c"], "box": d["box"], "tb": [0.0, 0.0], "lr": [0.0, 0.0]}
                        if d.get("c") else None)
                with _face_cache_lock:
                    _face_cache[token] = face
                return face
    except Exception:
        pass
    cached = _get_cached_image(token)
    if not cached:
        return None
    fpath = cached[0]
    face, tb, lr = _analyze_photo(fpath)
    cropped = False
    # 最多两轮裁切：首轮按检测值裁，裁后重检（黑边可能因脸约束/检测粒度残留），再裁一次收敛
    for _round in range(2):
        if tb[0] + tb[1] + lr[0] + lr[1] <= 0.004:
            break
        if not _crop_photo_margins(fpath, tb, lr):
            break
        cropped = True
        if face:
            # 人脸坐标换算到裁切后坐标系
            t, b = tb
            l, r = lr
            cw = 1.0 - l - r
            chh = 1.0 - t - b
            face["c"] = [round((face["c"][0] - l) / cw, 4),
                         round((face["c"][1] - t) / chh, 4)]
            face["box"] = [round(face["box"][0] / cw, 4),
                           round(face["box"][1] / chh, 4)]
        face2, tb, lr = _analyze_photo(fpath)
        if face2:
            face = face2
    out = face if face else None
    try:
        os.makedirs(_FACE_DIR, exist_ok=True)
        tmp = _face_sidecar_path(token) + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"v": _FACE_SIDECAR_VERSION,
                       "c": face["c"] if face else None,
                       "box": face["box"] if face else None,
                       "cropped": cropped}, f)
        os.replace(tmp, _face_sidecar_path(token))
    except Exception:
        pass
    with _face_cache_lock:
        _face_cache[token] = out
    return out


def get_face_center(token):
    """读人脸数据（内存 → sidecar → None）。不做实时检测，保证请求路径零开销"""
    with _face_cache_lock:
        if token in _face_cache:
            return _face_cache[token]
    face = None
    try:
        p = _face_sidecar_path(token)
        if os.path.exists(p):
            with open(p) as f:
                d = json.load(f)
            if _face_sidecar_valid(d):
                face = ({"c": d["c"], "box": d["box"], "tb": [0.0, 0.0], "lr": [0.0, 0.0]}
                        if d.get("c") else None)
    except Exception:
        face = None
    with _face_cache_lock:
        _face_cache[token] = face
    return face


def user_has_face(user_record, is_observer=False):
    """本人是否至少有 1 张可检出真人脸的照片（观察员免验；无照片/全无脸为 False）。
    仅读内存/sidecar 缓存，无实时检测开销；新上传照片由后台秒级检测补齐。"""
    if is_observer:
        return True
    return _tokens_has_face(bitable.get_attachment_tokens(user_record.get("fields", {}), F_PHOTO))


def _tokens_has_face(tokens, min_area=0.003):
    """token 列表是否含至少一张「清晰可见」人脸的照片：检出脸且最大脸框面积占比 >= 0.3%。
    远景小脸/群像中看不清的人脸视为不合格（读缓存，零检测开销）"""
    if not tokens:
        return False
    for t in tokens:
        face = get_face_center(t)
        if face and face.get("box"):
            if face["box"][0] * face["box"][1] >= min_area:
                return True
    return False


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
                   "image/webp": "webp", "image/heic": "heic",
                   "video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm"}
        ext = ext_map.get(content_type, "jpg")
        # 视频原样保存，图片再压缩（HEIC 已在 _compress_image 内转 JPEG）
        if content_type.startswith("video/"):
            compressed = resp.content
        else:
            compressed = _compress_image(resp.content, ext)
        cache_path = os.path.join(IMAGE_CACHE_DIR, "%s.%s" % (file_token, ext))
        tmp = cache_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(compressed)
        os.replace(tmp, cache_path)  # 原子替换，避免与请求线程并发写坏
        return Response(compressed, content_type=content_type,
                        headers={"Cache-Control": "public, max-age=604800"})
    except Exception:
        return jsonify({"error": "图片加载失败"}), 500


# ========== 认证接口 ==========

# ========== 最近活跃回写（打开 App 首页/登录时触发，同一用户 10 分钟内只写一次） ==========
_LAST_ACTIVE_TTL = 600
_last_active_written = {}


def touch_last_active(open_id, user, role):
    """后台异步把「最近活跃」写入用户/观察员表（按角色选表）。节流防首页刷新刷爆飞书 API。"""
    key = (open_id, role)
    now = time.time()
    if now - _last_active_written.get(key, 0) < _LAST_ACTIVE_TTL:
        return
    _last_active_written[key] = now
    table = USER_TABLE_ID if role == "user" else OBSERVER_TABLE_ID
    rid = (user or {}).get("record_id")
    if not table or not rid:
        return
    ts = int(now * 1000)

    def _w():
        try:
            bitable.update_record(table, rid, {F_LAST_ACTIVE: ts})
        except Exception as e:
            app.logger.warning(f"回写最近活跃失败: {e}")

    threading.Thread(target=_w, daemon=True).start()


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

    touch_last_active(open_id, user, default_role)

    session_id = create_session(open_id, default_role)
    brief = format_user_brief(user)
    brief["available_roles"] = sorted(roles)
    resp = make_response(jsonify({"ok": True, "user": brief}))
    resp.set_cookie("yxq_session", session_id, httponly=True, secure=True,
                    max_age=SESSION_EXPIRE_DAYS * 86400, samesite="Lax")
    return resp


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("yxq_session")
    return resp


def _build_self_card(open_id, active_users, msg_counts):
    """观察员预览自己的「普通用户」卡片（别人看我的样子）。

    同一 open_id 下若有单身档案，则把它按卡片同格式拼出，标 is_self=True，
    前端据此展示「这是你」角标并隐藏喜欢/留言等自操作。无单身档案返回 None。
    """
    for u in active_users:
        if bitable.get_field_text(u.get("fields", {}), F_FEISHU_ID) == open_id:
            fields = u.get("fields", {})
            brief = format_user_brief(u, include_openid=True, full=True)
            brief["display_fields"] = build_display_fields(fields)
            brief["subtitle"] = build_subtitle(fields)
            brief["liked"] = False
            brief["msg_count"] = msg_counts.get(open_id, 0)
            brief["is_self"] = True
            return brief
    return None


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
    touch_last_active(open_id, user, g.yxq_role)
    my_gender = bitable.get_select_value(user.get("fields", {}), F_GENDER)
    target_gender = "女性" if my_gender == "男性" else "男性"
    is_observer = bitable.get_select_value(user.get("fields", {}), F_ACCOUNT_STATUS) == STATUS_OBSERVER
    # 供前端全局门禁：任何链路进 app 即验（仅普通用户强验身份证，观察员免验）
    try:
        g._id_valid = True if is_observer else bitable.get_id_valid(user.get("fields", {}))
    except Exception:
        g._id_valid = True

    # 卡片（不含筛选，默认展示全部异性；观察员展示全部单身用户，男女均可浏览）
    # 只排除「未取消」的喜欢目标，取消喜欢后目标应重新回到卡片池
    liked_openids = {bitable.get_field_text(l.get("fields", {}), F_LIKE_TARGET_OPENID)
                     for l in snap_likes_by_initiator(open_id)
                     if bitable.get_select_value(l.get("fields", {}), F_LIKE_STATUS) != "已取消"}
    # 留言数：一次性按目标 open_id 计数（排除已删除，举报后仍计数待管理员处理）
    msg_counts = {}
    for m in _snap("messages"):
        mf = m.get("fields", {})
        if bitable.get_select_value(mf, F_MSG_STATUS) == "已删除":
            continue
        t = bitable.get_field_text(mf, F_MSG_TARGET_OID)
        if t:
            msg_counts[t] = msg_counts.get(t, 0) + 1
    active_users = snap_active_users()
    cards = []
    for u in active_users:
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
        brief["msg_count"] = msg_counts.get(uid, 0)
        cards.append(brief)

    # 喜欢（谁喜欢了我 / 相互喜欢）
    liked_me = snap_likes_by_target(open_id)
    # 喜欢我的人随机前移到前30%，其余整副牌打乱
    liked_me_openids = {
        bitable.get_field_text(l.get("fields", {}), F_LIKE_INITIATOR_OPENID)
        for l in liked_me
        if bitable.get_select_value(l.get("fields", {}), F_LIKE_STATUS) != "已取消"
    }
    cards = order_cards(cards, liked_me_openids)
    # 观察员预览自己的普通用户卡片（别人看我的样子），置顶展示
    if is_observer:
        self_card = _build_self_card(open_id, active_users, msg_counts)
        if self_card:
            cards.insert(0, self_card)
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
    user_brief["hearts"] = computed_hearts(open_id)
    user_brief["hearts_total"] = hearts_total(open_id)
    user_brief["has_face"] = user_has_face(user, is_observer)
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
    brief["hearts"] = computed_hearts(open_id)
    brief["hearts_total"] = hearts_total(open_id)
    return jsonify(brief)


@app.route("/api/account/status", methods=["POST"])
def toggle_account_status():
    """切换账号状态：单身 <-> 已脱单（秒提交版：状态读快照，同步仅 1 次写入）"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    user = snap_self_user()
    if not user:
        return jsonify({"error": "用户不存在"}), 404
    cur = bitable.get_select_value(user.get("fields", {}), F_ACCOUNT_STATUS)
    if cur == STATUS_OBSERVER:
        return jsonify({"error": "村情六处账号不可切换账号状态"}), 403
    if cur == "单身":
        new_status = "已脱单"
        # 已报名活动者不可自行脱单（先取消报名）；读快照（≤15s 窗口，见文档说明）
        active_signups = [s for s in snap_signups_by_openid(open_id)
                          if bitable.get_field_text(s.get("fields", {}), F_SIGNUP_STATUS) == "已报名"]
        if active_signups:
            return jsonify({"error": "你已报名活动，请先取消报名再脱单"}), 400
    elif cur == "已脱单":
        new_status = "单身"
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
    resp.set_cookie("yxq_session", session_id, httponly=True, secure=True,
                    max_age=SESSION_EXPIRE_DAYS * 86400, samesite="Lax")
    return resp


# ========== 活动接口 ==========

@app.route("/api/activities", methods=["GET"])
def activities():
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    gate = account_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]

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
    gate = account_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]

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

    # 查重：实时为准（快照 ≤15s 滞后于「取消报名」，若依赖快照会导致取消后无法重新报名）
    if bitable.get_user_signup(text_act_id, open_id):
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

    # 本地快照立即生效（与状态切换同款秒提交），避免取消后 15s 窗口内重复报名被误拦
    for s in _snapshot.get("signups", []):
        if s.get("record_id") == existing["record_id"]:
            s.setdefault("fields", {})[F_SIGNUP_STATUS] = "已取消"

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
        "house": [h.strip() for h in request.args.getlist("house") if h.strip()],
        "driving": [d.strip() for d in request.args.getlist("driving") if d.strip()],
    }

    all_users = snap_active_users()

    # 获取我已经喜欢过的人（从快照，实时性靠写操作后定向刷新）
    # 只排除「未取消」的喜欢目标，取消喜欢后目标应重新回到卡片池
    liked_openids = {
        bitable.get_field_text(like.get("fields", {}), F_LIKE_TARGET_OPENID)
        for like in snap_likes_by_initiator(open_id)
        if bitable.get_select_value(like.get("fields", {}), F_LIKE_STATUS) != "已取消"
    }

    # 留言数：一次性按目标 open_id 计数（排除已删除，举报后仍计数待管理员处理）
    msg_counts = {}
    for m in _snap("messages"):
        mf = m.get("fields", {})
        if bitable.get_select_value(mf, F_MSG_STATUS) == "已删除":
            continue
        t = bitable.get_field_text(mf, F_MSG_TARGET_OID)
        if t:
            msg_counts[t] = msg_counts.get(t, 0) + 1

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
        brief["msg_count"] = msg_counts.get(uid, 0)
        cards.append(brief)

    # 喜欢我的人随机前移到前30%，其余整副牌打乱
    liked_me_openids = {
        bitable.get_field_text(l.get("fields", {}), F_LIKE_INITIATOR_OPENID)
        for l in snap_likes_by_target(open_id)
        if bitable.get_select_value(l.get("fields", {}), F_LIKE_STATUS) != "已取消"
    }
    cards = order_cards(cards, liked_me_openids)
    # 观察员预览自己的普通用户卡片（别人看我的样子），仅无任何筛选时置顶展示
    if is_observer and not gender_filter and not _has_active_filter(filters):
        self_card = _build_self_card(open_id, all_users, msg_counts)
        if self_card:
            cards.insert(0, self_card)

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


_dead_alert_state = {"last_ts": 0.0}


def _alert_spool_dead(op):
    """死信告警：spool 重试耗尽、写入失败记录落入死信文件后，通知管理员。

    限频：10 分钟内最多一条，避免飞书 API 抖动时对管理员消息轰炸。
    告警内容只含操作概要，不带用户字段，避免隐私信息外发。
    """
    now = time.time()
    if now - _dead_alert_state["last_ts"] < 600:
        return
    _dead_alert_state["last_ts"] = now
    brief = {
        "type": op.get("type"),
        "initiator_oid": op.get("initiator_oid"),
        "target_oid": op.get("target_oid"),
        "record_id": op.get("record_id"),
    }
    text = ("[一线牵] H5 写入飞书连续失败，已进入死信队列，请检查："
            + json.dumps(brief, ensure_ascii=False))
    for admin_oid in ADMIN_OPEN_IDS:
        send_text_message(admin_oid, text)


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
            _alert_spool_dead(op)


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

    # 实名喜欢：按自然月限一次（不可取消，用完锁定）
    if like_type == "实名":
        if any(it.get("oid") == open_id and it.get("type") == "实名"
               for it in _intent_likes.values()):
            return jsonify({"error": "本月已使用过实名喜欢，每月仅一次机会"}), 400
        this_month = time.strftime("%Y-%m")
        for l in likes_snap:
            lf = l.get("fields", {})
            if bitable.get_field_text(lf, F_LIKE_INITIATOR_OPENID) != open_id:
                continue
            if bitable.get_select_value(lf, F_LIKE_STATUS) == "已取消":
                continue
            if bitable.get_field_text(lf, F_LIKE_TYPE) != "实名":
                continue
            created = bitable.get_datetime_value(lf, F_LIKE_CREATED_AT)
            if created and created[:7] == this_month:
                return jsonify({"error": "本月已使用过实名喜欢，每月仅一次机会"}), 400

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
    if bitable.field_exists(LIKE_TABLE_ID, F_LIKE_INITIATOR_GENDER):
        like_fields[F_LIKE_INITIATOR_GENDER] = my_gender
    if bitable.field_exists(LIKE_TABLE_ID, F_LIKE_TARGET_GENDER):
        like_fields[F_LIKE_TARGET_GENDER] = target_gender
    if message:
        like_fields[F_LIKE_MESSAGE] = message

    temp_key = _uuid.uuid4().hex
    _spool_append({"type": "like", "temp_key": temp_key,
                   "initiator_oid": open_id, "target_oid": target_openid,
                   "fields": like_fields})
    _intent_likes[temp_key] = {"oid": open_id, "target": target_openid, "ts": time.time(), "type": like_type}

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


SUGGESTION_TYPES = ("功能建议", "问题反馈", "其他")
SUGGESTION_MAX_LEN = 500


def validate_suggestion(data):
    """校验意见反馈请求体，返回 (错误信息, 类型, 内容)；通过时错误为 None"""
    sg_type = data.get("type")
    content = data.get("content")
    if not isinstance(sg_type, str) or sg_type.strip() not in SUGGESTION_TYPES:
        return "请选择反馈类型", "", ""
    if not isinstance(content, str):
        return "请填写反馈内容", "", ""
    sg_type = sg_type.strip()
    content = content.strip()
    if not content:
        return "请填写反馈内容", "", ""
    if len(content) > SUGGESTION_MAX_LEN:
        return f"反馈内容最多 {SUGGESTION_MAX_LEN} 字", "", ""
    return None, sg_type, content


@app.route("/api/suggestions", methods=["POST"])
def suggestions():
    """产品意见反馈：写意见反馈表 + 通知管理员（一期仅收集，不做站内回复）"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401

    data = request.get_json() or {}
    err, sg_type, content = validate_suggestion(data)
    if err:
        return jsonify({"error": err}), 400

    me = snap_self_user()
    if not me:
        return jsonify({"error": "用户不存在"}), 404

    me_fields = me.get("fields", {})
    my_name = bitable.get_field_text(me_fields, F_NICKNAME)
    my_id = bitable.get_field_text(me_fields, F_USER_ID)

    created = bitable.create_record(SUGGESTION_TABLE_ID, {
        F_SG_AUTHOR: my_name,
        F_SG_UID: my_id,
        F_SG_TYPE: sg_type,
        F_SG_CONTENT: content,
        F_SG_CREATED_AT: int(time.time() * 1000),
    })
    if not created:
        return jsonify({"error": "提交失败，请稍后重试"}), 500

    author_label = f"{my_name}（{my_id}）" if my_id else my_name
    admin_msg = f"📮 收到意见反馈\n提交人：{author_label}\n类型：{sg_type}\n内容：{content}"

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
    gate = account_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]

    # 谁喜欢了我（读快照，60s 循环 + 写操作后定向刷新保证新鲜度；快照未就绪时实时兜底）
    liked_me = snap_likes_by_target(open_id)
    # 我喜欢的人
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
    """列出某卡片下的留言（target=目标用户 open_id），排除已删除，举报后仍可见待管理员处理"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    gate = account_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]
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
            avatar_map[oid] = ("/api/image/" + toks[0] + "?fv6") if toks else ""
    msgs = []
    for it in items:
        fields = it.get("fields", {})
        if bitable.get_select_value(fields, F_MSG_STATUS) == "已删除":
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


@app.route("/api/messages/mine", methods=["GET"])
def list_my_messages():
    """列出当前用户发出的全部留言（跨卡片），按时间倒序，附带所在卡片主人昵称。"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    gate = account_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]
    items = bitable.search_records(MESSAGE_TABLE_ID, [
        {"field_name": F_MSG_AUTHOR_OID, "operator": "is", "value": [open_id]}
    ])
    # open_id -> 昵称/头像（用户表+观察员表合并）
    nick_map, avatar_map = {}, {}
    for key in ("users", "observers"):
        for u in _snap(key):
            uf = u.get("fields", {})
            oid = bitable.get_field_text(uf, F_FEISHU_ID)
            if oid and oid not in nick_map:
                nick_map[oid] = bitable.get_field_text(uf, F_NICKNAME)
                toks = bitable.get_attachment_tokens(uf, F_PHOTO)
                avatar_map[oid] = ("/api/image/" + toks[0] + "?fv6") if toks else ""
    msgs = []
    for it in items:
        fields = it.get("fields", {})
        if bitable.get_select_value(fields, F_MSG_STATUS) == "已删除":
            continue
        target_oid = bitable.get_field_text(fields, F_MSG_TARGET_OID)
        msgs.append({
            "id": it.get("record_id"),
            "target_openid": target_oid,
            "target_nickname": nick_map.get(target_oid, "未知用户"),
            "target_avatar": avatar_map.get(target_oid, ""),
            "content": bitable.get_field_text(fields, F_MSG_CONTENT),
            "parent_id": bitable.get_field_text(fields, F_MSG_PARENT_ID),
            "created_at": bitable.get_field_number(fields, F_MSG_CREATED_AT),
        })
    msgs.sort(key=lambda m: m["created_at"], reverse=True)
    return jsonify({"messages": msgs})


@app.route("/api/messages/received", methods=["GET"])
def list_received_messages():
    """列出别人发给当前用户的留言（target=本人 open_id），按时间倒序，附带作者昵称头像。"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    gate = account_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]
    items = bitable.search_records(MESSAGE_TABLE_ID, [
        {"field_name": F_MSG_TARGET_OID, "operator": "is", "value": [open_id]}
    ])
    nick_map, avatar_map = {}, {}
    for key in ("users", "observers"):
        for u in _snap(key):
            uf = u.get("fields", {})
            oid = bitable.get_field_text(uf, F_FEISHU_ID)
            if oid and oid not in nick_map:
                nick_map[oid] = bitable.get_field_text(uf, F_NICKNAME)
                toks = bitable.get_attachment_tokens(uf, F_PHOTO)
                avatar_map[oid] = ("/api/image/" + toks[0] + "?fv6") if toks else ""
    msgs = []
    for it in items:
        fields = it.get("fields", {})
        if bitable.get_select_value(fields, F_MSG_STATUS) == "已删除":
            continue
        author_oid = bitable.get_field_text(fields, F_MSG_AUTHOR_OID)
        msgs.append({
            "id": it.get("record_id"),
            "author_openid": author_oid,
            "author_nickname": nick_map.get(author_oid, "匿名用户"),
            "author_avatar": avatar_map.get(author_oid, ""),
            "author_uid": bitable.get_field_text(fields, F_MSG_AUTHOR_UID),
            "content": bitable.get_field_text(fields, F_MSG_CONTENT),
            "parent_id": bitable.get_field_text(fields, F_MSG_PARENT_ID),
            "created_at": bitable.get_field_number(fields, F_MSG_CREATED_AT),
        })
    msgs.sort(key=lambda m: m["created_at"], reverse=True)
    return jsonify({"messages": msgs})


@app.route("/api/messages", methods=["POST"])
def create_message():
    """发留言/回帖（固定显示昵称+用户ID，无匿名）"""
    open_id = require_login()
    if not open_id:
        app.logger.warning("留言拒绝: 未登录")
        return jsonify({"error": "未登录"}), 401
    # 留言属于互动行为：「单身」与「观察员」可发（已脱单/待审核/审核不通过/已退出均拦截）
    _status, _u = _account_status(open_id)
    app.logger.info(f"留言请求: role={g.yxq_role} status={_status} open_id={open_id[:20]}")
    if _status == "已脱单":
        return jsonify({"error": "你当前处于已脱单状态，不能留言；请先在「我的」页恢复单身"}), 403
    if _status in ("待审核", "审核不通过", "已退出"):
        return jsonify(GATE_MESSAGES[_status]), 403
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
        app.logger.error(f"留言拒绝: snap_self_user返回None role={g.yxq_role} open_id={open_id[:20]}")
        return jsonify({"error": "用户不存在"}), 404
    now_ms = int(time.time() * 1000)
    dup_rows = bitable.search_records(MESSAGE_TABLE_ID, {
        "conjunction": "and",
        "conditions": [
            {"field_name": F_MSG_AUTHOR_OID, "operator": "is", "value": [open_id]},
            {"field_name": F_MSG_TARGET_OID, "operator": "is", "value": [target]},
            {"field_name": F_MSG_CONTENT, "operator": "is", "value": [content]},
            {"field_name": F_MSG_STATUS, "operator": "is", "value": ["正常"]},
        ]})
    for row in dup_rows:
        f = row.get("fields", {})
        if bitable.get_field_text(f, F_MSG_PARENT_ID) == parent_id and \
                now_ms - bitable.get_field_number(f, F_MSG_CREATED_AT) <= 5000:
            return jsonify({"ok": True, "id": row.get("record_id")})
    af = author.get("fields", {})
    rec = bitable.create_record(MESSAGE_TABLE_ID, {
        F_MSG_TARGET_OID: target,
        F_MSG_AUTHOR_OID: open_id,
        F_MSG_AUTHOR_NICKNAME: bitable.get_field_text(af, F_NICKNAME),
        F_MSG_AUTHOR_UID: bitable.get_field_text(af, F_USER_ID),
        F_MSG_PARENT_ID: parent_id,
        F_MSG_CONTENT: content,
        F_MSG_CREATED_AT: now_ms,
        F_MSG_STATUS: "正常",
    })
    if not rec:
        app.logger.error(f"留言写入失败: role={g.yxq_role} open_id={open_id[:20]} target={target[:20]}")
        return jsonify({"error": "留言失败"}), 500
    app.logger.info(f"留言成功: role={g.yxq_role} id={rec.get('record_id')}")
    return jsonify({"ok": True, "id": rec.get("record_id")})


@app.route("/api/messages/<record_id>", methods=["DELETE"])
def delete_message(record_id):
    """删除留言（本人或卡片主人，管理员可删任意）"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    rec = bitable.get_record(MESSAGE_TABLE_ID, record_id)
    if not rec:
        return jsonify({"error": "留言不存在"}), 404
    fields = rec.get("fields", {})
    author_oid = bitable.get_field_text(fields, F_MSG_AUTHOR_OID)
    target_oid = bitable.get_field_text(fields, F_MSG_TARGET_OID)
    if open_id != author_oid and open_id != target_oid and open_id not in ADMIN_OPEN_IDS:
        return jsonify({"error": "无权删除"}), 403
    bitable.update_record(MESSAGE_TABLE_ID, record_id, {F_MSG_STATUS: "已删除"})
    # 级联删除该留言下的回复，避免产生「父留言已删、回复仍挂 parent_id」的孤儿，导致角标比可见条数多
    replies = bitable.search_records(MESSAGE_TABLE_ID, [
        {"field_name": F_MSG_PARENT_ID, "operator": "is", "value": [record_id]}
    ])
    for r in replies:
        bitable.update_record(MESSAGE_TABLE_ID, r.get("record_id"), {F_MSG_STATUS: "已删除"})
    return jsonify({"ok": True})


@app.route("/api/messages/<record_id>/report", methods=["POST"])
def report_message(record_id):
    """举报留言（置为已举报，待管理员处理，飞书通知管理员）"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    rec = bitable.get_record(MESSAGE_TABLE_ID, record_id)
    if not rec:
        return jsonify({"error": "留言不存在"}), 404
    fields = rec.get("fields", {})
    status = bitable.get_select_value(fields, F_MSG_STATUS)
    if status == "已举报":
        return jsonify({"ok": True, "already": True})
    if status == "已删除":
        return jsonify({"error": "该留言已删除"}), 400
    author_oid = bitable.get_field_text(fields, F_MSG_AUTHOR_OID)
    if author_oid == open_id:
        return jsonify({"error": "不能举报自己的留言"}), 400
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    if reason and len(reason) > 500:
        return jsonify({"error": "举报原因最多500字"}), 400
    if not bitable.update_record(MESSAGE_TABLE_ID, record_id, {F_MSG_STATUS: "已举报"}):
        return jsonify({"error": "举报失败，请稍后重试"}), 500
    # 记录举报时间到历史文件（用于按举报时间排序，最新在前）
    report_time = int(time.time()*1000)
    try:
        def _mut_pending(data):
            if not isinstance(data, dict):
                data = {"items": []}
            items = data.get("items", [])
            # 去重：同一 record_id 仅保留最新
            items = [x for x in items if x.get("id") != record_id]
            # 收集信息用于历史展示（即使尚未处理也保留）
            author_oid_p = bitable.get_field_text(fields, F_MSG_AUTHOR_OID)
            target_oid_p = bitable.get_field_text(fields, F_MSG_TARGET_OID)
            content_p = bitable.get_field_text(fields, F_MSG_CONTENT)
            parent_id_p = bitable.get_field_text(fields, F_MSG_PARENT_ID)
            created_at_p = bitable.get_field_number(fields, F_MSG_CREATED_AT)
            author_nick_p = bitable.get_field_text(fields, F_MSG_AUTHOR_NICKNAME)
            author_uid_p = bitable.get_field_text(fields, F_MSG_AUTHOR_UID)
            author_real_p = ""
            target_real_p = ""
            target_uid_p = ""
            # 查被举报人真名
            for key in ("users", "observers"):
                for u in _snap(key):
                    uf = u.get("fields", {})
                    if bitable.get_field_text(uf, F_FEISHU_ID) == author_oid_p:
                        author_real_p = bitable.get_field_text(uf, F_REAL_NAME) or bitable.get_field_text(uf, F_NICKNAME)
                        if not author_uid_p:
                            author_uid_p = bitable.get_field_text(uf, F_USER_ID)
                        break
                if author_real_p:
                    break
            for key in ("users", "observers"):
                for u in _snap(key):
                    uf = u.get("fields", {})
                    if bitable.get_field_text(uf, F_FEISHU_ID) == target_oid_p:
                        target_real_p = bitable.get_field_text(uf, F_REAL_NAME) or bitable.get_field_text(uf, F_NICKNAME)
                        target_uid_p = bitable.get_field_text(uf, F_USER_ID)
                        break
                if target_real_p:
                    break
            if not author_real_p:
                author_real_p = author_nick_p
            if not target_real_p:
                target_real_p = target_oid_p[:8] if target_oid_p else "未知"
            items.append({
                "id": record_id,
                "author_openid": author_oid_p,
                "author_nickname": author_nick_p,
                "author_realname": author_real_p,
                "author_uid": author_uid_p,
                "author_avatar": "",
                "target_openid": target_oid_p,
                "target_nickname": target_real_p,
                "target_realname": target_real_p,
                "target_uid": target_uid_p,
                "content": content_p,
                "parent_id": parent_id_p,
                "created_at": created_at_p,
                "report_time": report_time,
                "handled_at": None,
                "status": "待处理",
                "action": None,
                "handler": None,
                "handler_name": "",
                "handler_uid": "",
                "reason": reason,
            })
            items.sort(key=lambda x: x.get("report_time",0), reverse=True)
            data["items"] = items[:500]
            return data
        storage.update_json(_REPORTED_HISTORY_FILE, {"items": []}, _mut_pending)
    except Exception:
        pass
    # 异步通知管理员（真名+编号，去掉记录ID）
    try:
        reporter = snap_self_user()
        reporter_fields = reporter.get("fields", {}) if reporter else {}
        # 真名优先，回退昵称
        reporter_real = bitable.get_field_text(reporter_fields, F_REAL_NAME) if reporter else ""
        if not reporter_real:
            reporter_real = bitable.get_field_text(reporter_fields, F_NICKNAME) if reporter else ""
        reporter_uid = bitable.get_field_text(reporter_fields, F_USER_ID) if reporter else ""
        reporter_label = f"{reporter_real}（{reporter_uid}）" if reporter_uid else (reporter_real or open_id[:8])
        # 被举报人真名
        author_oid_n = bitable.get_field_text(fields, F_MSG_AUTHOR_OID)
        author_real = ""
        author_uid_n = bitable.get_field_text(fields, F_MSG_AUTHOR_UID)
        # 从快照查被举报人真名
        for key in ("users", "observers"):
            for u in _snap(key):
                uf = u.get("fields", {})
                if bitable.get_field_text(uf, F_FEISHU_ID) == author_oid_n:
                    author_real = bitable.get_field_text(uf, F_REAL_NAME) or bitable.get_field_text(uf, F_NICKNAME)
                    if not author_uid_n:
                        author_uid_n = bitable.get_field_text(uf, F_USER_ID)
                    break
            if author_real:
                break
        if not author_real:
            author_real = bitable.get_field_text(fields, F_MSG_AUTHOR_NICKNAME)
        author_label = f"{author_real}（{author_uid_n}）" if author_uid_n else author_real
        # 所在卡片真名
        target_oid_n = bitable.get_field_text(fields, F_MSG_TARGET_OID)
        target_real = ""
        target_uid_n = ""
        for key in ("users", "observers"):
            for u in _snap(key):
                uf = u.get("fields", {})
                if bitable.get_field_text(uf, F_FEISHU_ID) == target_oid_n:
                    target_real = bitable.get_field_text(uf, F_REAL_NAME) or bitable.get_field_text(uf, F_NICKNAME)
                    target_uid_n = bitable.get_field_text(uf, F_USER_ID)
                    break
            if target_real:
                break
        if not target_real:
            target_real = target_oid_n[:8] if target_oid_n else "未知"
        target_label = f"{target_real}（{target_uid_n}）" if target_uid_n else target_real
        content_n = bitable.get_field_text(fields, F_MSG_CONTENT)
        if len(content_n) > 200:
            content_n = content_n[:200] + "…"
        admin_msg = f"📮 收到留言举报\n举报人：{reporter_label}\n被举报人：{author_label}\n所在卡片：{target_label}\n内容：{content_n}"
        if reason:
            admin_msg += f"\n原因：{reason[:200]}"

        def _notify():
            for admin_oid in ADMIN_OPEN_IDS:
                try:
                    send_text_message(admin_oid, admin_msg)
                except Exception:
                    pass
        threading.Thread(target=_notify, daemon=True).start()
    except Exception:
        pass
    return jsonify({"ok": True})



@app.route("/api/messages/reported", methods=["GET"])
def list_reported_messages():
    """管理员：列出已举报留言（待处理 + 已处理历史，最新在前）"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    if open_id not in ADMIN_OPEN_IDS:
        return jsonify({"error": "未授权"}), 403
    items = bitable.search_records(MESSAGE_TABLE_ID, [
        {"field_name": F_MSG_STATUS, "operator": "is", "value": ["已举报"]}
    ])
    # 头像映射
    avatar_map = {}
    for key in ("users", "observers"):
        for u in _snap(key):
            uf = u.get("fields", {})
            oid = bitable.get_field_text(uf, F_FEISHU_ID)
            if oid and oid not in avatar_map:
                toks = bitable.get_attachment_tokens(uf, F_PHOTO)
                avatar_map[oid] = ("/api/image/" + toks[0] + "?fv6") if toks else ""
    # 昵称映射（用于 target 展示）
    nick_map = {}
    for key in ("users", "observers"):
        for u in _snap(key):
            uf = u.get("fields", {})
            oid = bitable.get_field_text(uf, F_FEISHU_ID)
            if oid and oid not in nick_map:
                nick_map[oid] = bitable.get_field_text(uf, F_NICKNAME)
    # 构建真实姓名映射
    real_map = {}
    uid_map = {}
    for key in ("users", "observers"):
        for u in _snap(key):
            uf = u.get("fields", {})
            oid = bitable.get_field_text(uf, F_FEISHU_ID)
            if oid and oid not in real_map:
                real_map[oid] = bitable.get_field_text(uf, F_REAL_NAME) or bitable.get_field_text(uf, F_NICKNAME)
                uid_map[oid] = bitable.get_field_text(uf, F_USER_ID)
    msgs = []
    for it in (items or []):
        fields = it.get("fields", {})
        author_oid = bitable.get_field_text(fields, F_MSG_AUTHOR_OID)
        target_oid = bitable.get_field_text(fields, F_MSG_TARGET_OID)
        author_real = real_map.get(author_oid, bitable.get_field_text(fields, F_MSG_AUTHOR_NICKNAME))
        target_real = real_map.get(target_oid, nick_map.get(target_oid, target_oid[:8] if target_oid else ""))
        msgs.append({
            "id": it.get("record_id"),
            "author_openid": author_oid,
            "author_nickname": bitable.get_field_text(fields, F_MSG_AUTHOR_NICKNAME),
            "author_realname": author_real,
            "author_uid": bitable.get_field_text(fields, F_MSG_AUTHOR_UID) or uid_map.get(author_oid, ""),
            "author_avatar": avatar_map.get(author_oid, ""),
            "target_openid": target_oid,
            "target_nickname": target_real,
            "target_realname": target_real,
            "target_uid": uid_map.get(target_oid, ""),
            "content": bitable.get_field_text(fields, F_MSG_CONTENT),
            "parent_id": bitable.get_field_text(fields, F_MSG_PARENT_ID),
            "created_at": bitable.get_field_number(fields, F_MSG_CREATED_AT),
            "status": "待处理",
            "reason": "",  # 待处理原因从历史文件补充
        })
    # 已处理历史（文件持久化，保留回溯）+ 报告时间映射
    hist_items = []
    try:
        hist_data = storage.load_json(_REPORTED_HISTORY_FILE, {"items": []})
        hist_items = hist_data.get("items", []) if isinstance(hist_data, dict) else []
    except Exception:
        hist_items = []
    # 建立 report_time 映射（用于待处理的排序）
    hist_map = {}
    try:
        for h in hist_items:
            if h.get("id") and h.get("report_time"):
                hist_map[h["id"]] = h["report_time"]
    except Exception:
        pass
    # 待处理追加 report_time
    for m in msgs:
        if m.get("id") in hist_map:
            m["report_time"] = hist_map[m["id"]]
        elif not m.get("report_time"):
            m["report_time"] = m.get("created_at", 0)
    # 历史追加（去重：已在待处理中的不重复追加，保留历史状态）
    pending_ids = {m.get("id") for m in msgs}
    for h in hist_items:
        if h.get("id") in pending_ids:
            continue
        # 补头像/昵称/真名
        if not h.get("author_avatar"):
            h["author_avatar"] = avatar_map.get(h.get("author_openid",""), "")
        if not h.get("author_realname"):
            h["author_realname"] = real_map.get(h.get("author_openid",""), h.get("author_nickname",""))
        if not h.get("author_uid"):
            h["author_uid"] = uid_map.get(h.get("author_openid",""), h.get("author_uid",""))
        if not h.get("target_nickname"):
            h["target_nickname"] = real_map.get(h.get("target_openid",""), nick_map.get(h.get("target_openid",""), h.get("target_openid","")[:8] if h.get("target_openid") else ""))
        if not h.get("target_realname"):
            h["target_realname"] = h.get("target_nickname","")
        if not h.get("target_uid"):
            h["target_uid"] = uid_map.get(h.get("target_openid",""), h.get("target_uid",""))
        # 补处理人真名
        if h.get("handler") and not h.get("handler_name"):
            h["handler_name"] = real_map.get(h["handler"], h["handler"][:8] if h["handler"] else "")
            h["handler_uid"] = uid_map.get(h["handler"], "")
        # 确保有 report_time
        if not h.get("report_time"):
            h["report_time"] = h.get("created_at", 0)
        msgs.append(h)
    # 为待处理补充 reason/report_time 来自历史
    hist_reason_map = {h["id"]: h.get("reason","") for h in hist_items if h.get("id")}
    hist_handler_map = {h["id"]: (h.get("handler_name",""), h.get("handler_uid",""), h.get("handled_at")) for h in hist_items}
    for m in msgs:
        if m.get("status")=="待处理" and m.get("id") in hist_reason_map:
            if not m.get("reason"):
                m["reason"] = hist_reason_map[m["id"]]
        # 已处理的 handler 补全
        if m.get("status") in ("已同意","已驳回") and m.get("handler") and not m.get("handler_name"):
            hn, hu, _ = hist_handler_map.get(m["id"], ("","",""))
            m["handler_name"] = hn
            m["handler_uid"] = hu
    msgs.sort(key=lambda x: x.get("report_time") or x.get("handled_at") or x.get("created_at") or 0, reverse=True)
    return jsonify({"messages": msgs})


@app.route("/api/messages/<record_id>/handle", methods=["POST"])
def handle_reported_message(record_id):
    """管理员：处理已举报留言 同意=删除 驳回=恢复"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    if open_id not in ADMIN_OPEN_IDS:
        return jsonify({"error": "未授权"}), 403
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "").strip()
    if action not in ("approve", "delete"):
        return jsonify({"error": "action 需为 approve(驳回) 或 delete(同意)"}), 400
    rec = bitable.get_record(MESSAGE_TABLE_ID, record_id)
    if not rec:
        return jsonify({"error": "留言不存在"}), 404
    fields = rec.get("fields", {})
    if bitable.get_select_value(fields, F_MSG_STATUS) != "已举报":
        return jsonify({"error": "该留言不是已举报状态"}), 400
    # 取原记录信息用于历史存档
    author_oid = bitable.get_field_text(fields, F_MSG_AUTHOR_OID)
    target_oid = bitable.get_field_text(fields, F_MSG_TARGET_OID)
    content = bitable.get_field_text(fields, F_MSG_CONTENT)
    parent_id = bitable.get_field_text(fields, F_MSG_PARENT_ID)
    created_at = bitable.get_field_number(fields, F_MSG_CREATED_AT)
    author_nick = bitable.get_field_text(fields, F_MSG_AUTHOR_NICKNAME)
    author_uid = bitable.get_field_text(fields, F_MSG_AUTHOR_UID)
    target_nick = ""
    # 尝试从快照补 target 昵称
    for key in ("users", "observers"):
        for u in _snap(key):
            uf = u.get("fields", {})
            if bitable.get_field_text(uf, F_FEISHU_ID) == target_oid:
                target_nick = bitable.get_field_text(uf, F_NICKNAME)
                break
        if target_nick:
            break
    if action == "approve":
        ok = bitable.update_record(MESSAGE_TABLE_ID, record_id, {F_MSG_STATUS: "正常"})
        handle_status = "已驳回"
    else:
        ok = bitable.update_record(MESSAGE_TABLE_ID, record_id, {F_MSG_STATUS: "已删除"})
        handle_status = "已同意"
        if ok:
            # 级联删除回复
            try:
                replies = bitable.search_records(MESSAGE_TABLE_ID, [
                    {"field_name": F_MSG_PARENT_ID, "operator": "is", "value": [record_id]}
                ])
                for r in replies:
                    bitable.update_record(MESSAGE_TABLE_ID, r.get("record_id"), {F_MSG_STATUS: "已删除"})
            except Exception:
                pass
    if ok:
        try:
            def _mut(data):
                if not isinstance(data, dict):
                    data = {"items": []}
                items = data.get("items", [])
                # 找到原有待处理记录以保留 report_time
                old_report_time = None
                old_reason = ""
                for it in items:
                    if it.get("id") == record_id:
                        old_report_time = it.get("report_time")
                        old_reason = it.get("reason", "")
                        break
                # 去重
                items = [x for x in items if x.get("id") != record_id]
                # 若原有 report_time 则沿用，否则用当前时间
                rt = old_report_time or int(time.time()*1000)
                # 查真实姓名
                handler_real = ""
                handler_uid = ""
                for key in ("users", "observers"):
                    for u in _snap(key):
                        uf = u.get("fields", {})
                        if bitable.get_field_text(uf, F_FEISHU_ID) == open_id:
                            handler_real = bitable.get_field_text(uf, F_REAL_NAME) or bitable.get_field_text(uf, F_NICKNAME)
                            handler_uid = bitable.get_field_text(uf, F_USER_ID)
                            break
                    if handler_real:
                        break
                # 被举报人/卡片真实姓名
                author_real = ""
                target_real = ""
                target_uid2 = ""
                for key in ("users", "observers"):
                    for u in _snap(key):
                        uf = u.get("fields", {})
                        if bitable.get_field_text(uf, F_FEISHU_ID) == author_oid:
                            author_real = bitable.get_field_text(uf, F_REAL_NAME) or bitable.get_field_text(uf, F_NICKNAME)
                            break
                    if author_real:
                        break
                for key in ("users", "observers"):
                    for u in _snap(key):
                        uf = u.get("fields", {})
                        if bitable.get_field_text(uf, F_FEISHU_ID) == target_oid:
                            target_real = bitable.get_field_text(uf, F_REAL_NAME) or bitable.get_field_text(uf, F_NICKNAME)
                            target_uid2 = bitable.get_field_text(uf, F_USER_ID)
                            break
                    if target_real:
                        break
                if not author_real:
                    author_real = author_nick
                if not target_real:
                    target_real = target_nick or target_oid[:8] if target_oid else ""
                items.append({
                    "id": record_id,
                    "author_openid": author_oid,
                    "author_nickname": author_nick,
                    "author_realname": author_real,
                    "author_uid": author_uid,
                    "author_avatar": "",
                    "target_openid": target_oid,
                    "target_nickname": target_real,
                    "target_realname": target_real,
                    "target_uid": target_uid2,
                    "content": content,
                    "parent_id": parent_id,
                    "created_at": created_at,
                    "report_time": rt,
                    "handled_at": int(time.time()*1000),
                    "status": handle_status,
                    "action": action,
                    "handler": open_id,
                    "handler_name": handler_real,
                    "handler_uid": handler_uid,
                    "reason": old_reason,
                })
                # 按举报时间倒序保留
                items.sort(key=lambda x: x.get("report_time",0), reverse=True)
                data["items"] = items[:500]
                return data
            storage.update_json(_REPORTED_HISTORY_FILE, {"items": []}, _mut)
        except Exception:
            pass
    return jsonify({"ok": bool(ok)})


# ========== 分组接口 ==========

@app.route("/api/activities/<activity_id>/group/candidates", methods=["GET"])
def group_candidates(activity_id):
    """获取分组可选的异性列表"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    gate = account_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]

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
                photo = ("/api/image/" + tokens[0] + "?fv6") if tokens else ""
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
    gate = account_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]

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
    gate = account_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]

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


def _editable_schema(fields, skip_id_card=False):
    """可编辑字段 schema + 当前值，前端据此动态渲染「修改资料」表单"""
    base = [
        {"field": fname, "label": label, "type": ftype, "options": opts,
         "long": fname in LONG_TEXT_FIELDS,
         "value": _editable_value(fields, fname, ftype)}
        for fname, label, ftype, opts in EDITABLE_FIELDS
    ]
    # 身份证缺/错时临时暴露，修后即消（任何链路门禁用；仅普通用户强验）
    try:
        if not skip_id_card and not bitable.get_id_valid(fields):
            # 脱敏回显：前3后4，中间*；空则空
            _idc = bitable.get_field_text(fields, F_ID_CARD).strip()
            _mask = (_idc[:3] + "****" + _idc[-4:]) if len(_idc) >= 7 else _idc
            # 前端以 text 展示，value 仍为原文以便校验对比
            base.insert(0, {"field": F_ID_CARD, "label": "身份证号", "type": "text", "options": None, "long": False, "value": _idc, "mask": _mask, "hint": "身份证号有误或缺失，请重新填写正确的18位身份证号"})
    except Exception:
        pass
    return base


def _normalize_editable_update(data, fields=None):
    """将前端提交的值按类型归一化为飞书写入格式；非法项静默忽略。

    身份证仅当原记录 id_valid==False 时放行，且强验格式与生日一致。
    """
    by_name = {fname: ftype for fname, _lbl, ftype, _opts in EDITABLE_FIELDS}
    # 临时放行身份证（缺/错时）
    _allow_id = False
    try:
        if fields is not None and not bitable.get_id_valid(fields):
            _allow_id = True
            by_name[F_ID_CARD] = "text"
    except Exception:
        pass
    update_fields = {}
    for k, v in data.items():
        ftype = by_name.get(k)
        if ftype is None:
            continue
        if k == F_ID_CARD and not _allow_id:
            continue
        if k == F_ID_CARD and _allow_id:
            val = str(v).strip().upper().replace(" ", "")
            ok, msg = bitable.validate_id_card(val)
            if not ok:
                # 交由上层以 400 提示，前端展示 msg
                raise ValueError("身份证有误")
            # 联动校验生日：若前端同时提交了生日则以提交值为准，否则取原表生日
            bday_val = data.get(F_BIRTHDAY)
            # 生日字段在 EDITABLE_FIELDS 中不可编辑（系统字段），此处仅校验身份证内生日与表生日一致性
            # 若表生日与证生日不一致，提示需同步修正（前端已提示）
            try:
                _id_birth = val[6:10] + "-" + val[10:12] + "-" + val[12:14] if len(val)==18 else ("19"+val[6:8]+"-"+val[8:10]+"-"+val[10:12] if len(val)==15 else "")
                if _id_birth:
                    # 对比现有生日（表内）
                    _cur_bday = bitable.get_datetime_value(fields, F_BIRTHDAY) if fields is not None else ""
                    if _cur_bday and _cur_bday != _id_birth:
                        # 允许本次一并修正：自动带上生日更新（无需前端另传）
                        # 生日为 Date 字段，需毫秒时间戳
                        import calendar as _cal
                        import datetime as _dt
                        _bd = _dt.datetime.strptime(_id_birth, "%Y-%m-%d")
                        update_fields[F_BIRTHDAY] = _cal.timegm(_bd.timetuple())*1000
            except Exception:
                pass
            update_fields[k] = val
            continue
        if ftype == "phone":
            raw = str(v).strip() if v is not None else ""
            if not raw:
                update_fields[k] = ""
                continue
            sanitized = re.sub(r"[^0-9+]", "", raw)
            if sanitized.count("+") > 1 or ("+" in sanitized and not sanitized.startswith("+")):
                raise ValueError("手机号格式有误")
            if not re.match(r"^\+?\d{7,15}$", sanitized):
                raise ValueError("手机号格式有误")
            update_fields[k] = sanitized
        elif ftype in ("text",):
            val = str(v).strip() if v is not None else ""
            if k == F_NICKNAME and not val:
                continue  # 昵称不可清空：bot 按昵称查找/展示，清空会导致功能失效
            update_fields[k] = val
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
    fields = user.get("fields", {})
    is_observer = g.yxq_role == "observer"
    if is_observer:
        data["id_valid"] = True
        data["id_card_mask"] = ""
    else:
        try:
            _idc = bitable.get_field_text(fields, F_ID_CARD).strip()
            data["id_valid"] = bitable.get_id_valid(fields)
            data["id_card_mask"] = (_idc[:3] + "****" + _idc[-4:]) if len(_idc) >= 7 else _idc
        except Exception:
            data["id_valid"] = True
            data["id_card_mask"] = ""
    data["editable"] = _editable_schema(fields, skip_id_card=is_observer)
    data["has_face"] = user_has_face(user, is_observer)
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

    try:
        update_fields = _normalize_editable_update(data, user.get("fields", {}))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
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
    return ["/api/image/" + t + "?fv6" for t in tokens]


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
    # 视频直接拒绝
    if (f.mimetype or "").startswith("video/") or (f.filename or "").lower().endswith((".mp4",".mov",".avi",".webm")):
        return jsonify({"error": "请上传 JPG/PNG 图片，暂不支持视频"}), 400
    # HEIC 转 JPEG（iPhone 实况）
    if (f.mimetype == "image/heic" or (f.filename or "").lower().endswith((".heic",".heif"))):
        try:
            import pillow_heif
            from PIL import Image
            heif = pillow_heif.open_heif(io.BytesIO(data))
            im = Image.frombytes(heif.mode, heif.size, heif.data, "raw", heif.mode, heif.stride)
            if max(im.size) > 1080:
                ratio = 1080.0 / im.width
                new_h = int(im.height * ratio)
                im = im.resize((1080, new_h), Image.LANCZOS)
            if im.mode != "RGB":
                im = im.convert("RGB")
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=85, optimize=True)
            data = out.getvalue()
            f.mimetype = "image/jpeg"
            filename = (f.filename or "photo.jpg").rsplit(".",1)[0] + ".jpg"
        except Exception:
            return jsonify({"error": "HEIC 图片处理失败，请转成 JPG 后重试"}), 400
    try:
        from PIL import Image  # 函数内局部导入（HEIC 分支之外的 JPEG/PNG 路径也依赖）
        _prev = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = None  # 高像素手机原图（>89M 像素）不应被误拒，展示端会压缩
        try:
            Image.open(io.BytesIO(data)).verify()
        finally:
            Image.MAX_IMAGE_PIXELS = _prev
    except Exception as e:
        logging.getLogger(__name__).warning(
            "图片上传校验失败 mimetype=%s filename=%s size=%s err=%s",
            f.mimetype, f.filename, len(data), e)
        return jsonify({"error": "文件不是有效图片"}), 400

    filename = (f.filename or "photo.jpg").rsplit("/", 1)[-1] or "photo.jpg"
    file_token = bitable.upload_attachment(data, filename, f.mimetype or "image/jpeg")
    if not file_token:
        return jsonify({"error": "图片上传失败，请稍后重试"}), 500

    tokens.append(file_token)
    if not _write_photos(user, tokens):
        return jsonify({"error": "资料更新失败，请稍后重试"}), 500
    # 异步预热：先快速缓存图片，人脸后台计算，避免接口阻塞 2-4s；has_face 稍后通过快照刷新补齐
    try:
        _download_and_cache_image(file_token)
    except Exception:
        pass
    try:
        threading.Thread(target=ensure_face_center, args=(file_token,), daemon=True).start()
    except Exception:
        pass
    # 同步刷新用户快照，保证 /api/profile、/api/home 的基础信息与本响应一致（人脸异步）
    try:
        refresh_snapshot_table("users")
    except Exception:
        pass
    return jsonify({"ok": True, "photos": _photo_urls(tokens),
                    "has_face": _tokens_has_face(tokens)})


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
    try:
        refresh_snapshot_table("users")
    except Exception:
        pass
    return jsonify({"ok": True, "photos": _photo_urls(tokens),
                    "has_face": _tokens_has_face(tokens)})


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
    gate = account_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]

    my_likes = snap_likes_by_initiator(open_id)

    # 谁喜欢了我（用于判断相互喜欢；过滤已取消）
    liked_me = snap_likes_by_target(open_id)
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
        brief["like_type"] = bitable.get_field_text(fields, F_LIKE_TYPE)
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

    # 实名喜欢不可取消（每月仅一次，用完锁定）
    if existing and bitable.get_field_text(existing.get("fields", {}), F_LIKE_TYPE) == "实名":
        return jsonify({"error": "实名喜欢不可取消"}), 400
    if pending_like and any(it.get("oid") == open_id and it.get("target") == target_openid
                            and it.get("type") == "实名" for it in _intent_likes.values()):
        return jsonify({"error": "实名喜欢不可取消"}), 400

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
    gate = account_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]

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
    gate = account_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]

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
    gate = account_gate(open_id)
    if gate:
        return jsonify(gate[0]), gate[1]

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
    gate = account_gate(oid)
    if gate:
        return jsonify(gate[0]), gate[1]
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
            photo = ("/api/image/" + tokens[0] + "?fv6") if tokens else ""
            user_id = bitable.get_field_text(uf, F_USER_ID)
            gender = bitable.get_select_value(uf, F_GENDER)
        result.append({"openid": s_openid, "nickname": nick, "user_id": user_id, "photo": photo, "gender": gender})
    return jsonify({"signups": result})


@app.route("/api/users/<openid>/public", methods=["GET"])
def get_user_public(openid):
    oid = require_login()
    if not oid:
        return jsonify({"error": "未登录"}), 401
    gate = account_gate(oid)
    if gate:
        return jsonify(gate[0]), gate[1]
    u = snap_find_user_by_openid(openid)
    if not u:
        return jsonify({"error": "用户不存在"}), 404
    fields = u.get("fields", {})
    data = {
        "openid": bitable.get_field_text(fields, F_FEISHU_ID),
        "nickname": bitable.get_field_text(fields, F_NICKNAME),
        "gender": bitable.get_select_value(fields, F_GENDER),
        "photos": ["/api/image/" + t + "?fv6" for t in bitable.get_attachment_tokens(fields, F_PHOTO)],
        "display_fields": build_display_fields(fields),
    }
    return jsonify(data)


@app.route("/api/notifications", methods=["GET"])
def get_notifications():
    oid = require_login()
    if not oid:
        return jsonify({"error": "未登录"}), 401
    gate = account_gate(oid)
    if gate:
        return jsonify(gate[0]), gate[1]
    items = [n for n in load_notifications() if n.get("recipient") == oid]
    items.sort(key=lambda n: n.get("time", ""), reverse=True)
    return jsonify({"notifications": items})


# ========== 引流埋点统计 ==========

def _track_daily_file(date_str=None):
    """按天切分埋点文件，避免单文件无限增长导致解析变慢"""
    ds = date_str or time.strftime("%Y-%m-%d")
    return os.path.join(SHARED_DATA_DIR, f"yixianqian_track-{ds}.json")

def _load_track(days=7):
    """聚合最近 days 天的埋点，兼容旧单文件 yixianqian_track.json"""
    events = []
    # 兼容旧文件：若存在则一次性读入（后续逐步由按天文件替代）
    old = storage.load_json(TRACK_FILE, None)
    if old and isinstance(old.get("events"), list):
        events.extend(old["events"][-5000:])
    # 按天文件聚合
    import datetime as _dt
    for i in range(days):
        ds = (_dt.date.today() - _dt.timedelta(days=i)).strftime("%Y-%m-%d")
        d = storage.load_json(_track_daily_file(ds), None)
        if d and isinstance(d.get("events"), list):
            events.extend(d["events"])
    return {"events": events}

def _cleanup_old_tracks(keep_days=7):
    """清理超过 keep_days 的旧按天文件"""
    try:
        import datetime as _dt
        cutoff = _dt.date.today() - _dt.timedelta(days=keep_days)
        for fname in os.listdir(SHARED_DATA_DIR):
            if not fname.startswith("yixianqian_track-") or not fname.endswith(".json"):
                continue
            try:
                ds = fname[len("yixianqian_track-"):-5]
                d = _dt.datetime.strptime(ds, "%Y-%m-%d").date()
                if d < cutoff:
                    os.remove(os.path.join(SHARED_DATA_DIR, fname))
            except Exception:
                pass
    except Exception:
        pass

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
        # 单天文件也做截断，避免单天被刷爆
        if len(track["events"]) > 5000:
            track["events"] = track["events"][-3000:]
        return track

    # 按天文件原子写，旧单文件不再写入，仅保留读取兼容
    daily = _track_daily_file()
    storage.update_json(daily, {"events": []}, _add)
    # 概率性清理旧文件（1/100 概率，避免每次请求都扫目录）
    if random.random() < 0.01:
        try:
            threading.Thread(target=_cleanup_old_tracks, daemon=True).start()
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/api/track/stats", methods=["GET"])
def track_stats():
    """查看引流统计（按天/按事件聚合）——仅管理员可访问"""
    open_id = require_login()
    if not open_id or open_id not in ADMIN_OPEN_IDS:
        return jsonify({"error": "未授权"}), 403
    track = _load_track(days=14)
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
    """公开接口：返回单身用户列表（引流页展示用，不含敏感信息；走本地快照）"""
    all_users = snap_active_users()
    result = []
    for u in all_users:
        fields = u.get("fields", {})
        result.append({
            "nickname": bitable.get_field_text(fields, F_NICKNAME),
            "gender": bitable.get_select_value(fields, F_GENDER),
            "photos": ["/api/image/" + t + "?fv6" for t in bitable.get_attachment_tokens(fields, F_PHOTO)],
            "display_fields": build_display_fields(fields),
        })
    return jsonify({"users": result})


if __name__ == "__main__":
    logger = logging.getLogger(__name__)
    # 启动时先同步拉一次快照，保证读接口立即可用；随后后台定时刷新
    refresh_snapshot()
    start_snapshot_loop()
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False, threaded=True)
