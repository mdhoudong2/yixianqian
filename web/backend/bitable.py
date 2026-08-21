# -*- coding: utf-8 -*-
"""飞书多维表格 API 封装"""
import time
import requests
from config import *

_token_cache = {"token": None, "expire_time": 0}


def get_token():
    """获取 tenant_access_token，带缓存"""
    now = time.time()
    if _token_cache["token"] and _token_cache["expire_time"] > now + 60:
        return _token_cache["token"]
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    for attempt in range(3):
        try:
            resp = requests.post(url, json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=15)
            result = resp.json()
            if result.get("code") == 0:
                _token_cache["token"] = result["tenant_access_token"]
                _token_cache["expire_time"] = now + result.get("expire", 7200)
                return _token_cache["token"]
        except Exception:
            pass
        time.sleep(2 ** attempt)
    return None


def _headers():
    token = get_token()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def search_records(table_id, filter_conditions=None, page_size=100, field_names=None):
    """搜索记录（支持分页获取全部）"""
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/search"
    all_items = []
    page_token = None
    body = {"page_size": page_size}
    if filter_conditions:
        body["filter"] = {"conjunction": "and", "conditions": filter_conditions}
    if field_names:
        body["field_names"] = field_names

    while True:
        params = {}
        if page_token:
            params["page_token"] = page_token
        try:
            resp = requests.post(url, headers=_headers(), json=body, params=params, timeout=15)
            result = resp.json()
            if result.get("code") != 0:
                break
            data = result.get("data", {})
            all_items.extend(data.get("items", []))
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
        except Exception:
            break
    return all_items


def get_record(table_id, record_id):
    """获取单条记录"""
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/{record_id}"
    try:
        resp = requests.get(url, headers=_headers(), timeout=15)
        result = resp.json()
        if result.get("code") == 0:
            return result.get("data", {}).get("record")
    except Exception:
        pass
    return None


_field_cache = {}  # (table_id, field_name) -> (exists, expire_time)


def field_exists(table_id, field_name):
    """判断表里是否存在某字段（带缓存）。schema 变更前的防御：字段尚未新增时跳过写入，避免整条记录创建失败。"""
    now = time.time()
    key = (table_id, field_name)
    cached = _field_cache.get(key)
    if cached and cached[1] > now:
        return cached[0]
    exists = True  # 查询失败时假定存在，避免误伤正常写入
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/fields"
    try:
        resp = requests.get(url, headers=_headers(), params={"page_size": 100}, timeout=15)
        result = resp.json()
        if result.get("code") == 0:
            items = result.get("data", {}).get("items", [])
            exists = any(f.get("field_name") == field_name for f in items)
    except Exception:
        pass
    _field_cache[key] = (exists, now + 300)
    return exists


def create_record(table_id, fields):
    """创建记录"""
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records"
    try:
        resp = requests.post(url, headers=_headers(), json={"fields": fields}, timeout=15)
        result = resp.json()
        if result.get("code") == 0:
            return result.get("data", {}).get("record")
    except Exception:
        pass
    return None


def update_record(table_id, record_id, fields):
    """更新记录"""
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/{record_id}"
    try:
        resp = requests.put(url, headers=_headers(), json={"fields": fields}, timeout=15)
        result = resp.json()
        if result.get("code") == 0:
            return result.get("data", {}).get("record")
    except Exception:
        pass
    return None


def _unwrap_formula(val):
    """公式字段值 {'type': N, 'value': [...]} -> 提取 value 列表"""
    if isinstance(val, dict) and "value" in val and isinstance(val["value"], list):
        return val["value"]
    return val


def _text_of(item):
    """从单元格元素提取文本（dict 或 str）"""
    if isinstance(item, dict):
        return str(item.get("text") or item.get("name") or "")
    return str(item)


def get_field_text(fields, key, default=""):
    """从字段值中提取文本（兼容文本/公式字段）"""
    val = _unwrap_formula(fields.get(key))
    if val is None:
        return default
    if isinstance(val, list):
        return "".join(_text_of(item) for item in val)
    if isinstance(val, dict):
        return _text_of(val)
    return str(val)


def get_field_number(fields, key, default=0):
    """从字段值中提取数字（兼容数字/公式字段）"""
    val = _unwrap_formula(fields.get(key))
    if val is None:
        return default
    if isinstance(val, list):
        if not val:
            return default
        val = val[0]
        if isinstance(val, dict):
            val = val.get("value") if val.get("value") is not None else (val.get("text") or val.get("name"))
    if isinstance(val, dict):
        val = val.get("value") if val.get("value") is not None else (val.get("text") or val.get("name"))
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def get_select_value(fields, key, default=""):
    """获取单选字段值（兼容单选/公式字段）"""
    val = _unwrap_formula(fields.get(key))
    if val is None:
        return default
    if isinstance(val, list) and val:
        return _text_of(val[0])
    if isinstance(val, dict):
        return _text_of(val)
    return str(val)


def get_multi_select_value(fields, key):
    """获取多选字段值，返回选项名列表"""
    val = _unwrap_formula(fields.get(key))
    if val is None:
        return []
    if isinstance(val, list):
        return [t for t in (_text_of(item) for item in val) if t]
    if isinstance(val, (str, int, float)) and val not in ("", None):
        return [str(val)]
    return []


def get_attachment_tokens(fields, key):
    """获取附件字段的file_token列表"""
    val = fields.get(key)
    if not val or not isinstance(val, list):
        return []
    tokens = []
    for item in val:
        if isinstance(item, dict):
            token = item.get("file_token") or item.get("token")
            if token:
                tokens.append(token)
    return tokens


def get_date_value(fields, key, default=""):
    """获取日期字段值（时间戳毫秒），返回 %Y-%m-%d"""
    return get_datetime_value(fields, key, default)


def get_datetime_value(fields, key, default=""):
    """获取 DateTime 字段值，返回 %Y-%m-%d。

    兼容 Feishu Bitable 日期的多种返回形态：
    - 列表 [{ "timestamp": 秒 }, ...]（新 API）
    - dict {"value": 毫秒}
    - 直接毫秒整数 len==13；秒整数 len==10
    - 公式包装 {"type":..,"value":[...]}
    """
    val = fields.get(key)
    if val is None:
        return default
    val = _unwrap_formula(val)
    ts = None
    try:
        if isinstance(val, list) and val:
            first = val[0]
            if isinstance(first, dict):
                ts = first.get("timestamp") or first.get("value") or first.get("time")
            else:
                ts = first
        elif isinstance(val, dict):
            ts = val.get("value") or val.get("timestamp")
        else:
            ts = val
        if ts is None:
            return default
        ts = int(ts)
        if ts > 10_000_000_000:   # 毫秒
            ts = ts / 1000
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    except (ValueError, TypeError, OverflowError):
        return str(val)


def get_phone_value(fields, key, default=""):
    """获取电话字段值"""
    val = fields.get(key)
    if val is None:
        return default
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return val.get("text", str(val))
    return str(val)


# ========== 用户相关 ==========

def find_user_by_openid(open_id):
    """通过 open_id 查找用户"""
    items = search_records(USER_TABLE_ID, [
        {"field_name": F_FEISHU_ID, "operator": "is", "value": [open_id]}
    ])
    return items[0] if items else None


def find_user_by_id(user_id):
    """通过用户ID查找用户"""
    items = search_records(USER_TABLE_ID, [
        {"field_name": F_USER_ID, "operator": "is", "value": [user_id]}
    ])
    return items[0] if items else None


def get_all_users():
    """获取所有正常状态用户"""
    items = search_records(USER_TABLE_ID)
    users = []
    for item in items:
        fields = item.get("fields", {})
        status = get_select_value(fields, F_ACCOUNT_STATUS)
        if status == "活跃":
            users.append(item)
    return users


def get_next_user_id():
    """生成下一个用户ID（U-xxxx）"""
    items = search_records(USER_TABLE_ID, field_names=[F_USER_ID])
    max_num = 0
    for item in items:
        uid = get_field_text(item.get("fields", {}), F_USER_ID)
        if uid.startswith("U-"):
            try:
                num = int(uid[2:])
                max_num = max(max_num, num)
            except ValueError:
                pass
    return f"U-{max_num + 1:04d}"


# ========== 活动相关 ==========

def get_activities(status=None):
    """获取活动列表"""
    if status:
        items = search_records(ACTIVITY_TABLE_ID, [
            {"field_name": F_ACTIVITY_STATUS, "operator": "is", "value": [status]}
        ])
    else:
        items = search_records(ACTIVITY_TABLE_ID)
    return items


def get_activity(activity_id):
    """通过活动ID获取活动"""
    items = search_records(ACTIVITY_TABLE_ID, [
        {"field_name": F_ACTIVITY_ID, "operator": "is", "value": [activity_id]}
    ])
    return items[0] if items else None


def get_activity_by_record(record_id):
    """通过record_id获取活动"""
    return get_record(ACTIVITY_TABLE_ID, record_id)


# ========== 报名相关 ==========

def get_signups(activity_id):
    """获取活动的所有报名记录"""
    return search_records(SIGNUP_TABLE_ID, [
        {"field_name": F_SIGNUP_ACTIVITY_ID, "operator": "is", "value": [activity_id]}
    ])


def get_user_signup(activity_id, open_id):
    """获取用户在某活动的报名记录"""
    items = search_records(SIGNUP_TABLE_ID, [
        {"field_name": F_SIGNUP_ACTIVITY_ID, "operator": "is", "value": [activity_id]},
        {"field_name": F_SIGNUP_OPENID, "operator": "is", "value": [open_id]}
    ])
    return items[0] if items else None


# ========== 喜欢相关 ==========

def find_like(initiator_openid, target_openid):
    """查找喜欢记录（仅活跃状态：单向/相互；忽略已取消）"""
    items = search_records(LIKE_TABLE_ID, [
        {"field_name": F_LIKE_INITIATOR_OPENID, "operator": "is", "value": [initiator_openid]},
        {"field_name": F_LIKE_TARGET_OPENID, "operator": "is", "value": [target_openid]},
        {"field_name": F_LIKE_STATUS, "operator": "isNot", "value": ["已取消"]}
    ])
    return items[0] if items else None


def find_mutual_like(openid_a, openid_b):
    """查找相互喜欢（A喜欢B 且 B喜欢A）"""
    like_ab = find_like(openid_a, openid_b)
    like_ba = find_like(openid_b, openid_a)
    return like_ab and like_ba


# ========== 分组相关 ==========

def get_group_selections(activity_id):
    """获取活动的所有分组选择"""
    return search_records(GROUP_SELECT_TABLE, [
        {"field_name": F_GS_ACTIVITY_ID, "operator": "is", "value": [activity_id]}
    ])


def get_user_group_selection(activity_id, open_id):
    """获取用户的分组选择"""
    items = search_records(GROUP_SELECT_TABLE, [
        {"field_name": F_GS_ACTIVITY_ID, "operator": "is", "value": [activity_id]},
        {"field_name": F_GS_SELECTOR_OID, "operator": "is", "value": [open_id]}
    ])
    return items[0] if items else None


def get_group_results(activity_id):
    """获取活动分组结果"""
    return search_records(GROUP_RESULT_TABLE, [
        {"field_name": F_GR_ACTIVITY_ID, "operator": "is", "value": [activity_id]}
    ])
