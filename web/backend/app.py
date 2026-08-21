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
SNAPSHOT_REFRESH_INTERVAL = 15  # 秒

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

def snap_find_user_by_openid(open_id):
    users = _snap("users")
    if not users:
        return bitable.find_user_by_openid(open_id)
    for u in users:
        if bitable.get_field_text(u.get("fields", {}), F_FEISHU_ID) == open_id:
            return u
    # 快照非空但查不到（如快照在用户写入前加载、尚未刷新）：回退到飞书直查，避免误判「用户不存在」
    return bitable.find_user_by_openid(open_id)


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


def create_session(open_id):
    """创建会话：open_id 签名后直接写入 cookie（无服务端状态，重启不丢失）"""
    return _session_signer.dumps(open_id)


def get_session():
    """从cookie读取并校验当前会话 open_id"""
    token = request.cookies.get("yxq_session")
    if not token:
        return None
    try:
        return _session_signer.loads(token, max_age=SESSION_EXPIRE_DAYS * 86400)
    except (BadSignature, SignatureExpired):
        return None


def require_login():
    """要求登录，返回open_id或None"""
    return get_session()


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
    # 身高
    h = bitable.get_field_number(fields, F_HEIGHT)
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
        if os.path.getsize(fpath) > 1024 * 1024:
            try:
                os.remove(fpath)
            except Exception:
                pass
        else:
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

    user = bitable.find_user_by_openid(open_id)
    if not user:
        return jsonify({"error": "尚未注册，请先在飞书中搜索「一线牵」机器人完成注册", "need_register": True}), 403

    session_id = create_session(open_id)
    resp = make_response(jsonify({"ok": True, "user": format_user_brief(user)}))
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

    # 自己的信息（含爱心）必须直查飞书，避免快照陈旧导致爱心显示多一颗
    user = bitable.find_user_by_openid(open_id)
    if not user:
        return jsonify({"error": "用户不存在"}), 404
    my_gender = bitable.get_select_value(user.get("fields", {}), F_GENDER)
    target_gender = "女性" if my_gender == "男性" else "男性"

    # 卡片（不含筛选，默认展示全部异性）
    # 只排除「未取消」的喜欢目标，取消喜欢后目标应重新回到卡片池
    liked_openids = {bitable.get_field_text(l.get("fields", {}), F_LIKE_TARGET_OPENID)
                     for l in snap_likes_by_initiator(open_id)
                     if bitable.get_select_value(l.get("fields", {}), F_LIKE_STATUS) != "已取消"}
    cards = []
    for u in snap_active_users():
        fields = u.get("fields", {})
        uid = bitable.get_field_text(fields, F_FEISHU_ID)
        if uid == open_id or bitable.get_select_value(fields, F_GENDER) != target_gender or uid in liked_openids:
            continue
        brief = format_user_brief(u, include_openid=True, full=True)
        brief["display_fields"] = build_display_fields(fields)
        brief["subtitle"] = build_subtitle(fields)
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

    return jsonify({
        "user": format_user_brief(user),
        "cards": cards,
        "likes": {"liked_me": liked_me_list, "mutual": mutual_list},
        "activities": activities,
    })


@app.route("/api/user/me", methods=["GET"])
def user_me():
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    # 直查飞书，保证爱心等实时数据不滞后
    user = bitable.find_user_by_openid(open_id)
    if not user:
        return jsonify({"error": "用户不存在"}), 404
    return jsonify(format_user_brief(user))


@app.route("/api/account/status", methods=["POST"])
def toggle_account_status():
    """切换账号状态：活跃 <-> 已隐藏（隐藏后不再出现在他人牵线卡片中）"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401
    user = bitable.find_user_by_openid(open_id)
    if not user:
        return jsonify({"error": "用户不存在"}), 404
    cur = bitable.get_select_value(user.get("fields", {}), F_ACCOUNT_STATUS)
    if cur == "活跃":
        new_status = "已隐藏"
    elif cur == "已隐藏":
        new_status = "活跃"
    else:
        return jsonify({"error": "当前状态暂不支持切换"}), 400
    ok = bitable.update_record(USER_TABLE_ID, user["record_id"], {F_ACCOUNT_STATUS: new_status})
    if not ok:
        return jsonify({"error": "状态更新失败，请稍后重试"}), 500
    refresh_snapshot_table_async("users")
    return jsonify({"ok": True, "account_status": new_status})


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
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401

    with file_lock:
        # 读取走快照，省实时API（人数/状态有 ≤15s 延迟，飞书侧自会修正）
        act_record, text_act_id = snap_resolve_activity(activity_id)
        if not act_record:
            return jsonify({"error": "活动不存在"}), 404

        act_fields = act_record.get("fields", {})
        status = bitable.get_select_value(act_fields, F_ACTIVITY_STATUS)
        if status != "报名中":
            return jsonify({"error": f"活动当前状态为「{status}」，无法报名"}), 400

        # 检查是否已报名
        existing = bitable.get_user_signup(text_act_id, open_id)
        if existing:
            return jsonify({"error": "你已经报名了这个活动"}), 400

        # 检查人数上限（用实时报名记录数，避免快照陈旧导致超卖）
        current = len(bitable.get_signups(text_act_id))
        max_signup = int(bitable.get_field_number(act_fields, F_ACTIVITY_MAX_SIGNUP, 0))
        if max_signup > 0 and current >= max_signup:
            return jsonify({"error": "报名人数已满"}), 400

        # 获取用户信息
        user = bitable.find_user_by_openid(open_id)
        if not user:
            return jsonify({"error": "用户信息不存在"}), 404
        user_fields = user.get("fields", {})
        nickname = bitable.get_field_text(user_fields, F_NICKNAME)

        # 创建报名记录
        signup_record = bitable.create_record(SIGNUP_TABLE_ID, {
            F_SIGNUP_ACTIVITY_ID: text_act_id,
            F_SIGNUP_OPENID: open_id,
            F_SIGNUP_NICKNAME: nickname,
            F_SIGNUP_STATUS: "已报名"
        })
        if not signup_record:
            return jsonify({"error": "报名失败，请重试"}), 500

        # 更新报名人数
        bitable.update_record(ACTIVITY_TABLE_ID, act_record["record_id"], {
            F_ACTIVITY_CURRENT_SIGNUP: current + 1
        })

    refresh_snapshot_table_async("signups")
    refresh_snapshot_table_async("activities")
    return jsonify({"ok": True, "message": "报名成功"})


@app.route("/api/activities/<activity_id>/signup", methods=["DELETE"])
def cancel_signup(activity_id):
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401

    with file_lock:
        # 读取走快照，省实时API（状态有 ≤15s 延迟）
        act_record, text_act_id = snap_resolve_activity(activity_id)
        if not act_record:
            return jsonify({"error": "活动不存在"}), 404
        existing = bitable.get_user_signup(text_act_id, open_id)
        if not existing:
            return jsonify({"error": "你没有报名这个活动"}), 400
        if act_record:
            act_fields = act_record.get("fields", {})
            status = bitable.get_select_value(act_fields, F_ACTIVITY_STATUS)
            if status != "报名中":
                return jsonify({"error": f"活动当前状态为「{status}」，无法取消报名"}), 400
            current = int(bitable.get_field_number(act_fields, F_ACTIVITY_CURRENT_SIGNUP, 0))
            bitable.update_record(ACTIVITY_TABLE_ID, act_record["record_id"], {
                F_ACTIVITY_CURRENT_SIGNUP: max(0, current - 1)
            })

        # 更新为已取消（保留历史，与 Bot 侧一致）
        bitable.update_record(SIGNUP_TABLE_ID, existing["record_id"], {
            F_SIGNUP_STATUS: "已取消"
        })

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

    user = snap_find_user_by_openid(open_id)
    if not user:
        return jsonify({"error": "用户不存在"}), 404

    user_fields = user.get("fields", {})
    my_gender = bitable.get_select_value(user_fields, F_GENDER)
    target_gender = "女性" if my_gender == "男性" else "男性"

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
        if uid == open_id or gender != target_gender or uid in liked_openids:
            continue
        if not pass_card_filters(fields, filters):
            continue
        brief = format_user_brief(u, include_openid=True, full=True)
        brief["display_fields"] = build_display_fields(fields)
        brief["subtitle"] = build_subtitle(fields)
        cards.append(brief)

    # 喜欢我的异性卡片靠前（前10随机混排），匿名不被猜出
    liked_me_openids = {
        bitable.get_field_text(l.get("fields", {}), F_LIKE_INITIATOR_OPENID)
        for l in snap_likes_by_target(open_id)
        if bitable.get_select_value(l.get("fields", {}), F_LIKE_STATUS) != "已取消"
    }
    cards = order_cards_likes_first(cards, liked_me_openids)

    return jsonify({"cards": cards})


@app.route("/api/like", methods=["POST"])
def like_user():
    """喜欢某人"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401

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

    with file_lock:
        # 检查是否已经喜欢过
        existing = bitable.find_like(open_id, target_openid)
        if existing:
            return jsonify({"error": "你已经喜欢过TA了"}), 400

        # 获取双方信息
        me = bitable.find_user_by_openid(open_id)
        target = bitable.find_user_by_openid(target_openid)
        if not me or not target:
            return jsonify({"error": "用户不存在"}), 404

        me_fields = me.get("fields", {})
        target_fields = target.get("fields", {})
        my_name = bitable.get_field_text(me_fields, F_NICKNAME)
        target_name = bitable.get_field_text(target_fields, F_NICKNAME)
        my_id = bitable.get_field_text(me_fields, F_USER_ID)
        target_id = bitable.get_field_text(target_fields, F_USER_ID)
        my_hearts = bitable.get_field_number(me_fields, F_HEART_REMAIN, INITIAL_HEARTS)
        my_gender = bitable.get_select_value(me_fields, F_GENDER)
        target_gender = bitable.get_select_value(target_fields, F_GENDER)

        # 同性不能喜欢（仅限异性），性别缺失先提示完善
        if not my_gender or not target_gender:
            return jsonify({"error": "性别信息缺失，请先完善资料"}), 400
        if my_gender == target_gender:
            return jsonify({"error": "仅限异性之间喜欢"}), 400

        if my_hearts <= 0:
            return jsonify({"error": "爱心不足，无法喜欢"}), 400

        # 喜欢类型字段（schema 变更前的防御：表里还没有「喜欢类型」字段时，匿名喜欢照常，实名暂不可用）
        has_like_type_field = bitable.field_exists(LIKE_TABLE_ID, F_LIKE_TYPE)

        # 实名喜欢：每月限 1 次
        if like_type == "实名":
            if not has_like_type_field:
                return jsonify({"error": "实名喜欢功能暂未开启，请先用匿名喜欢"}), 400
            cur_month = time.strftime("%Y-%m")
            my_real_likes = bitable.search_records(LIKE_TABLE_ID, [
                {"field_name": F_LIKE_INITIATOR_OPENID, "operator": "is", "value": [open_id]},
                {"field_name": F_LIKE_TYPE, "operator": "is", "value": ["实名"]},
            ])
            for l in my_real_likes:
                ct_str = bitable.get_datetime_value(l.get("fields", {}), "创建时间")
                if ct_str and ct_str[:7] == cur_month:
                    return jsonify({"error": "本月已用过实名喜欢，每月只有一次机会"}), 400

        # 检查是否相互喜欢
        target_likes_me = bitable.find_like(target_openid, open_id)
        is_mutual = bool(target_likes_me)

        # 创建喜欢记录（状态需使用飞书表格中已有的单选选项：单向喜欢/相互喜欢/已取消）
        like_fields = {
            F_LIKE_INITIATOR: my_name,
            F_LIKE_TARGET: target_name,
            F_LIKE_INITIATOR_OPENID: open_id,
            F_LIKE_TARGET_OPENID: target_openid,
            F_LIKE_INITIATOR_ID: my_id,
            F_LIKE_TARGET_ID: target_id,
            F_LIKE_STATUS: "相互喜欢" if is_mutual else "单向喜欢",
            F_LIKE_HEART_DEDUCTED: True,
        }
        if has_like_type_field:
            like_fields[F_LIKE_TYPE] = like_type
        if message:
            like_fields[F_LIKE_MESSAGE] = message
        created = bitable.create_record(LIKE_TABLE_ID, like_fields)
        if not created:
            return jsonify({"error": "喜欢失败，请稍后重试"}), 500

        # 扣减爱心
        bitable.update_record(USER_TABLE_ID, me["record_id"], {
            F_HEART_REMAIN: my_hearts - 1
        })

        # 如果对方也喜欢了我，更新对方的喜欢记录状态
        if target_likes_me:
            bitable.update_record(LIKE_TABLE_ID, target_likes_me["record_id"], {
                F_LIKE_STATUS: "相互喜欢"
            })

    # 发送通知（在锁外执行，避免长时间阻塞）
    if is_mutual:
        # 相互喜欢：推送双方名片，各自加飞书好友
        send_text_message(open_id, f"💕 恭喜！你和 {target_name} 相互喜欢了！名片已推送，快去加飞书好友吧～")
        send_text_message(target_openid, f"💕 恭喜！你和 {my_name} 相互喜欢了！名片已推送，快去加飞书好友吧～")
        send_user_card(open_id, target_openid)
        send_user_card(target_openid, open_id)
    else:
        # 通知对方（实名喜欢会透露身份；匿名喜欢维持匿名）
        h5_link = f"{H5_BASE_URL}/#/likes"
        if like_type == "实名":
            title = "💌 有人实名喜欢你了"
            body = f"「{my_name}」（用户ID {my_id}）实名喜欢了你，快去看看吧～"
        else:
            title = "💌 有人喜欢你了"
            body = "有一位用户喜欢了你，快去看看是谁吧～"
        card = {
            "header": {"title": {"tag": "plain_text", "content": title}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body}},
                {"tag": "action", "actions": [
                    {"tag": "button", "text": {"tag": "plain_text", "content": "查看是谁"},
                     "type": "primary", "url": h5_link}
                ]}
            ]
        }
        send_card_message(target_openid, card)

    refresh_snapshot_table_async("likes")
    refresh_snapshot_table_async("users")
    return jsonify({"ok": True, "mutual": is_mutual, "message": "相互喜欢！" if is_mutual else "喜欢成功"})


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

    me = snap_find_user_by_openid(open_id)
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

    # 通知管理员
    admin_msg = f"📮 收到用户反馈\n举报人：{reporter_label}\n被举报人：{target_label}\n原因：{reason}"
    for admin_oid in ADMIN_OPEN_IDS:
        send_text_message(admin_oid, admin_msg)

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
    me = snap_find_user_by_openid(open_id)
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
    """提交分组志愿选择"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401

    data = request.get_json() or {}
    choices = data.get("choices", [])  # [openid1, openid2, ...] 最多7个

    if not choices or not isinstance(choices, list):
        return jsonify({"error": "请至少选择一个志愿"}), 400
    if len(choices) > 7:
        return jsonify({"error": "最多选择7个志愿"}), 400

    with file_lock:
        # 检查活动状态
        act_record, text_act_id = resolve_activity(activity_id)
        if not act_record:
            return jsonify({"error": "活动不存在"}), 404
        group_status = bitable.get_select_value(act_record.get("fields", {}), F_ACTIVITY_GROUP_STATUS)
        if group_status != "收集中":
            return jsonify({"error": f"分组状态为「{group_status}」，无法选择"}), 400

        # 检查报名
        signup = bitable.get_user_signup(text_act_id, open_id)
        if not signup:
            return jsonify({"error": "你未报名此活动"}), 403

        # 获取用户信息
        me = bitable.find_user_by_openid(open_id)
        me_fields = me.get("fields", {})
        my_name = bitable.get_field_text(me_fields, F_NICKNAME)
        my_gender = bitable.get_select_value(me_fields, F_GENDER)

        # 检查是否已提交过（允许覆盖）
        existing = bitable.get_user_group_selection(activity_id, open_id)

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

        if existing:
            bitable.update_record(GROUP_SELECT_TABLE, existing["record_id"], fields)
        else:
            bitable.create_record(GROUP_SELECT_TABLE, fields)

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
    user = snap_find_user_by_openid(open_id)
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
    user = bitable.find_user_by_openid(open_id)
    if not user:
        return jsonify({"error": "用户不存在"}), 404

    update_fields = _normalize_editable_update(data)
    if not update_fields:
        return jsonify({"error": "没有可更新的字段"}), 400

    ok = bitable.update_record(USER_TABLE_ID, user["record_id"], update_fields)
    if not ok:
        return jsonify({"error": "资料更新失败，请稍后重试"}), 500
    refresh_snapshot_table_async("users")
    return jsonify({"ok": True, "message": "资料已更新"})


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
    """取消喜欢（软取消双向，允许取消相互喜欢）"""
    open_id = require_login()
    if not open_id:
        return jsonify({"error": "未登录"}), 401

    with file_lock:
        existing = bitable.find_like(open_id, target_openid)
        if not existing:
            return jsonify({"error": "未找到喜欢记录"}), 404

        # 软取消：把「我->TA」的记录置为已取消；同时把「爱心已扣减」置 False，
        # 避免机器人后台 auto_deduct_hearts 的返还循环二次返还爱心（否则会双倍返还）。
        # 注意：飞书多维表格更新记录接口只支持 PUT，PATCH 会 404（历史 bug：取消后刷新又出现）。
        bitable.update_record(LIKE_TABLE_ID, existing["record_id"], {
            F_LIKE_STATUS: "已取消", F_LIKE_HEART_DEDUCTED: False})

        # 同时切断反向：把「TA->我」的活跃记录也软取消，避免残留导致误报双向喜欢/报名通知
        reverse_likes = bitable.search_records(LIKE_TABLE_ID, [
            {"field_name": F_LIKE_INITIATOR_OPENID, "operator": "is", "value": [target_openid]},
            {"field_name": F_LIKE_TARGET_OPENID, "operator": "is", "value": [open_id]},
            {"field_name": F_LIKE_STATUS, "operator": "isNot", "value": ["已取消"]}
        ])
        for rl in reverse_likes:
            bitable.update_record(LIKE_TABLE_ID, rl["record_id"], {
                F_LIKE_STATUS: "已取消", F_LIKE_HEART_DEDUCTED: False})

        # 退还爱心（仅发起取消方；对方爱心不退还——产品决策）
        me = bitable.find_user_by_openid(open_id)
        if me:
            my_hearts = bitable.get_field_number(me.get("fields", {}), F_HEART_REMAIN, INITIAL_HEARTS)
            new_hearts = min(30, my_hearts + 1)
            bitable.update_record(USER_TABLE_ID, me["record_id"], {F_HEART_REMAIN: new_hearts})

    refresh_snapshot_table_async("likes")
    refresh_snapshot_table_async("users")
    return jsonify({"ok": True, "message": "已取消喜欢"})


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
