"""飞书多维表格 API 封装 —— 薄封装：实际实现位于共享库 lib/bitable_client.py。

保留本模块以兼容 app.py 的全部 `bitable.xxx` 调用，避免大范围改动。
"""
import logging
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

from config import *

from lib import praise, quota
from lib.bitable_client import (  # noqa: F401 — re-export for app.py via bitable.*
    BitableClient,
    get_attachment_tokens,
    get_date_value,
    get_datetime_value,
    get_field_number,
    get_field_text,
    get_id_valid,
    get_multi_select_value,
    get_phone_value,
    get_select_value,
    get_timestamp,
    validate_id_card,
)

_logger = logging.getLogger("bitable").warning

_client = BitableClient(FEISHU_APP_ID, FEISHU_APP_SECRET, BASE_TOKEN, logger=_logger)

get_token = _client.get_token

def search_records(*args, **kwargs):
    """包装 _client.search_records：失败返回 None 时对外转为 []，保持旧调用方兼容；快照需区分失败/空表，应直接调用 _client.search_records。"""
    result = _client.search_records(*args, **kwargs)
    return result if result is not None else []


# 供快照使用的原始查询（失败返回 None，调用方需区分空表与失败）
raw_search_records = _client.search_records

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
        if status == "单身":
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

def like_month(fields):
    """喜欢的归属月份：优先显式字段（受理时刻钉死），回退创建时间的 %Y-%m。

    与 bot/auto_tasks.py 的 _like_month 同口径。绝不能只用创建时间——那记的是
    spool 落库那一刻，23:59 点的喜欢会算进下个月、白耗上个月的额度。
    """
    m = get_field_text(fields, F_LIKE_MONTH)
    if m:
        return m
    created = get_datetime_value(fields, F_LIKE_CREATED_AT)
    return created[:7] if created else ""


def like_is_active(fields):
    """这条喜欢现在还算数吗？口径唯一来源 lib.quota.is_like_active。

    全站读喜欢状态的地方都该走这里，不要各写各的 `!= "被驳回"`：
    否定式会把「以后新增的任何状态」都当成有效，静默多算额度、多算配对。
    """
    return quota.is_like_active(
        get_select_value(fields, F_LIKE_STATUS),
        get_field_text(fields, F_LIKE_TYPE) or quota.LIKE_TYPE_ANON,
        like_month(fields))


def find_like(initiator_openid, target_openid):
    """查找仍然有效的喜欢记录（单向/相互，且匿名未满 3 个月）"""
    # 状态白名单交给下面的 like_is_active：飞书单选框 operator=is 的 value 只能一个值，
    # 塞 list(LIKE_STATUS_ACTIVE) 会被判 InvalidFilter(1254018)，而本模块把查询失败
    # 转成空列表——重复喜欢拦截就静默失效了。
    items = search_records(LIKE_TABLE_ID, [
        {"field_name": F_LIKE_INITIATOR_OPENID, "operator": "is", "value": [initiator_openid]},
        {"field_name": F_LIKE_TARGET_OPENID, "operator": "is", "value": [target_openid]},
    ])
    for it in items:
        if like_is_active(it.get("fields", {})):
            return it
    return None


# ========== 点赞相关 ==========

def praise_week(fields):
    """点赞的归属周（受理那一刻钉死）。

    和喜欢表的「归属月份」同一个道理：不读「创建时间」自动字段，那是落库时刻，
    周日 23:59 点的赞会被算进下一周，两家都发错。
    """
    return get_field_text(fields, F_PRAISE_WEEK)


def praise_is_active(fields):
    """这条点赞现在还算数吗？口径唯一来源 lib.praise.is_praise_active。

    全站读点赞状态的地方都走这里，不要各写各的 `!= "已取消"`：否定式会把以后
    新增的任何状态都当成有效，静默多算额度、多发一条周汇总。"""
    return praise.is_praise_active(get_select_value(fields, F_PRAISE_STATUS))


def find_praise(initiator_openid, target_openid):
    """查找仍然有效的点赞记录（一人对一人只有一条有效的）"""
    # 状态白名单交给下面的 praise_is_active：飞书单选框 operator=is 的 value 只能
    # 一个值，塞 list(...) 会被判 InvalidFilter(1254018)，而本模块把查询失败转成
    # 空列表——重复点赞拦截就静默失效了。
    items = search_records(PRAISE_TABLE_ID, [
        {"field_name": F_PRAISE_INITIATOR_OPENID, "operator": "is",
         "value": [initiator_openid]},
        {"field_name": F_PRAISE_TARGET_OPENID, "operator": "is",
         "value": [target_openid]},
    ])
    for it in items:
        if praise_is_active(it.get("fields", {})):
            return it
    return None


def prays_today(openid, day):
    """某人某天点过的赞 → [(状态, 归属日期)]，喂给 lib.praise.daily_left。

    **只查当天**：一个人的点赞记录是只增不减的，时间一长「按 open_id 拉全部再
    本地过滤」会变成每点一次赞就拉几百条。归属日期是受理那刻写进去的文本，
    直接当成过滤条件用。

    失败模式的取舍：本模块把查询失败转成空列表（见 search_records 的包装），
    于是查不到就等价于「今天一个没点」——用户能多点几个赞，而不是被误拒。
    点赞不涉及任何稀缺资源，这个方向是安全的那一边。**注意别反过来**：
    要是哪天拿它去卡某个稀缺资源，得先让它能区分「失败」和「空」。
    """
    items = search_records(PRAISE_TABLE_ID, [
        {"field_name": F_PRAISE_INITIATOR_OPENID, "operator": "is", "value": [openid]},
        {"field_name": F_PRAISE_DAY, "operator": "is", "value": [day]},
    ])
    return [(get_select_value(it.get("fields", {}), F_PRAISE_STATUS), day)
            for it in items]


def received_praise(openid, week=None):
    """某人收到的有效点赞 → [(状态, 归属周, 被点赞人open_id)]，喂给 received_by_week。"""
    items = search_records(PRAISE_TABLE_ID, [
        {"field_name": F_PRAISE_TARGET_OPENID, "operator": "is", "value": [openid]},
    ])
    return [(get_select_value(it.get("fields", {}), F_PRAISE_STATUS),
             praise_week(it.get("fields", {})), openid) for it in items]


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
