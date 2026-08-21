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

from lib.bitable_client import (  # noqa: F401 — re-export for app.py via bitable.*
    BitableClient,
    get_attachment_tokens,
    get_date_value,
    get_datetime_value,
    get_field_number,
    get_field_text,
    get_multi_select_value,
    get_phone_value,
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
upload_attachment = _client.upload_attachment


# ========== 用户相关 ==========

def find_user_by_openid(open_id):
    """通过 open_id 查找用户"""
    items = search_records(USER_TABLE_ID, [
        {"field_name": F_FEISHU_ID, "operator": "is", "value": [open_id]}
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


# ========== 报名相关 ==========

def get_signups(activity_id):
    """获取活动的所有报名记录（仅已报名，排除已取消）"""
    return search_records(SIGNUP_TABLE_ID, [
        {"field_name": F_SIGNUP_ACTIVITY_ID, "operator": "is", "value": [activity_id]},
        {"field_name": F_SIGNUP_STATUS, "operator": "isNot", "value": ["已取消"]}
    ])


def get_user_signup(activity_id, open_id):
    """获取用户在某活动的报名记录（仅已报名）"""
    items = search_records(SIGNUP_TABLE_ID, [
        {"field_name": F_SIGNUP_ACTIVITY_ID, "operator": "is", "value": [activity_id]},
        {"field_name": F_SIGNUP_OPENID, "operator": "is", "value": [open_id]},
        {"field_name": F_SIGNUP_STATUS, "operator": "isNot", "value": ["已取消"]}
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


# ========== 分组相关 ==========

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
