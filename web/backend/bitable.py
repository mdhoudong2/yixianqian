"""飞书多维表格 API 封装 —— 薄封装：实际实现位于共享库 lib/bitable_client.py。

保留本模块以兼容 app.py 的全部 `bitable.xxx` 调用，避免大范围改动。
"""
import logging
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from config import *

from lib.bitable_client import (
    BitableClient,
    get_field_text,
    get_select_value,
)

_logger = logging.getLogger("bitable").warning

_client = BitableClient(FEISHU_APP_ID, FEISHU_APP_SECRET, BASE_TOKEN, logger=_logger)

get_token = _client.get_token
search_records = _client.search_records
get_record = _client.get_record
create_record = _client.create_record
update_record = _client.update_record
field_exists = _client.field_exists


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
