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
_intent_likes = {}
_intent_cancels = {}


def _intent_prune():
    now = time.time()
    for d, ttl in ((_intent_likes, 20), (_intent_cancels, 60)):
        for k in list(d):
            d[k] = [(a, b) for a, b in d[k] if now - b < ttl]


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
    cancel_rids = {rid for rid, _ in _intent_cancels.get(open_id, [])}
    cnt = 0
    for l in _snap("likes"):
        f = l.get("fields", {})
        if bitable.get_field_text(f, F_LIKE_INITIATOR_OPENID) != open_id:
            continue
        if bitable.get_select_value(f, F_LIKE_STATUS) == "已取消":
            continue
        if l.get("record_id") in cancel_rids:
            continue
        cnt += 1
    pending = len(_intent_likes.get(open_id, []))
    invites = _balances_file().get("invites", {}).get(open_id, 0)
    return max(0, min(MAX_HEARTS, INITIAL_HEARTS + invites - cnt - pending))


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
                return True  # 幂等命中：重复投递视为已完成
            return bool(bitable.create_record(LIKE_TABLE_ID, op["fields"]))
        if t == "cancel":
            return bitable.update_record(LIKE_TABLE_ID, op["record_id"],
                                         {F_LIKE_STATUS: "已取消"}) is not None
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

    me = snap_find_user_by_openid(open_id)
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
    cancel_rids = {rid for rid, _ in _intent_cancels.get(open_id, [])}
    already = False
    likes_snap = _snap("likes")
    for l in likes_snap:
        if l.get("record_id") in cancel_rids:
            continue
        lf = l.get("fields", {})
        if bitable.get_field_text(lf, F_LIKE_INITIATOR_OPENID) != open_id:
            continue
        if bitable.get_select_value(lf, F_LIKE_STATUS) == "已取消":
            continue
        if bitable.get_field_text(lf, F_LIKE_TARGET_OPENID) == target_openid:
            already = True
            break
    if already:
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
    _intent_likes.setdefault(open_id, []).append((temp_key, time.time()))

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
    data = request.get_json(silent=True) or {}
    target = (data.get("target_openid") or "").strip()
    content = (data.get("content") or "").strip()
    parent_id = (data.get("parent_id") or "").strip()
    if not target or not content:
        return jsonify({"error": "内容不能为空"}), 400
    if len(content) > 500:
        return jsonify({"error": "留言最多500字"}), 400
    author = snap_find_user_by_openid(open_id)
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
    me = snap_find_user_by_openid(open_id)
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
    user = snap_find_user_by_openid(open_id)
    if not user:
        return jsonify({"error": "用户不存在"}), 404

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
    user = snap_find_user_by_openid(open_id)
    if not user:
        return jsonify({"error": "用户不存在"}), 404

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
    user = snap_find_user_by_openid(open_id)
    if not user:
        return jsonify({"error": "用户不存在"}), 404

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
    user = snap_find_user_by_openid(open_id)
    if not user:
        return jsonify({"error": "用户不存在"}), 404

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
    if not existing:
        existing = bitable.find_like(open_id, target_openid)
    if not existing:
        return jsonify({"error": "未找到喜欢记录"}), 404

    rid = existing["record_id"]
    _spool_append({"type": "cancel", "record_id": rid})
    _intent_cancels.setdefault(open_id, []).append((rid, time.time()))

    # 本地快照即时置灰：保证紧随其后的 /api/cards 立即把对方放回卡片池
    for l in _snapshot.get("likes", []):
        if l.get("record_id") == rid:
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
