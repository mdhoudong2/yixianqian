"""用户/活动查询辅助（基于共享 BitableClient）。"""
import re

from clients import *
from constants import *


def _uid_sort_key(rec):
    """用户ID数值排序键（U-0012 -> 12；无ID排最后）"""
    m = re.match(r"[Uu]-?(\d+)", str(get_field_text(rec.get("fields", {}), "用户ID")))
    return int(m.group(1)) if m else 10 ** 9


def pick_primary_record(records):
    """同一 open_id 多条记录时取主档案：单身优先，其次用户ID最小。
    全系统身份解析唯一入口——头像/资料/记账/归属一致性都依赖它。"""
    if not records:
        return records

    def rank(r):
        st = get_field_text(r.get("fields", {}), FIELD_ACCOUNT_STATUS)
        return (0 if st == "单身" else 1, _uid_sort_key(r))

    return [sorted(records, key=rank)[0]]


def find_user_by_nickname(nickname):
    """通过昵称查找用户，返回记录列表"""
    return search_records(USER_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_NICKNAME, "operator": "is", "value": [nickname]}]
    })


def find_user_by_openid(open_id):
    """通过 open_id 查找用户（普通用户表 + 村情六处独立表）。同号多档时返回主档案（单元素列表），
    调用方无需感知重复注册的存在。pick_primary_record 单身优先，双身份仍回落 user 档。"""
    cond = {"field_name": FIELD_FEISHU_ID, "operator": "is", "value": [open_id]}
    items = search_records(USER_TABLE_ID, {"conjunction": "and", "conditions": [cond]})
    if OBSERVER_TABLE_ID:
        items += search_records(OBSERVER_TABLE_ID, {"conjunction": "and", "conditions": [cond]})
    return pick_primary_record(items)


def update_user_feishu_id(record_id, open_id):
    return update_record(USER_TABLE_ID, record_id, {FIELD_FEISHU_ID: open_id})




def get_creator_openid(fields):
    """从创建人字段获取open_id"""
    creators = fields.get(FIELD_CREATOR, [])
    if not creators:
        return None
    creator = creators[0] if isinstance(creators, list) else creators
    return creator.get("id") if isinstance(creator, dict) else None


def get_creator_name(fields):
    """从创建人字段获取name（外部访客提交的创建人名形如「访客 XXXX」）"""
    creators = fields.get(FIELD_CREATOR, [])
    if not creators:
        return None
    creator = creators[0] if isinstance(creators, list) else creators
    return creator.get("name") if isinstance(creator, dict) else None




def find_user_by_id_or_name(keyword):
    """通过用户ID（如U-0003）或昵称查找用户"""
    # 自动编号字段搜索需要数字，先尝试提取数字
    m = re.match(r"[Uu]-?(\d+)", keyword)
    if m:
        items = search_records(USER_TABLE_ID, {
            "conjunction": "and",
            "conditions": [{"field_name": "用户ID", "operator": "is", "value": [m.group(1)]}]
        })
        if items:
            return items
    # 再按昵称查
    items = find_user_by_nickname(keyword)
    if items:
        return items
    # 昵称没命中时，再按「姓名」兜底（去空白比对）；既有昵称命中行为不变
    return find_user_by_real_name(keyword)


def find_user_by_real_name(name):
    """按「姓名」查用户：去掉全部空白后精确比对（姓名存得较随意时也不会误伤）。
    仅在昵称查找为空时被 find_user_by_id_or_name 调用，用于现场活动的姓名找人。"""
    target = re.sub(r"\s+", "", str(name or ""))
    if not target:
        return []
    return search_records(USER_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_REAL_NAME, "operator": "is", "value": [target]}]
    })




def find_activity_by_id(activity_id):
    """通过活动ID查找活动（自动编号字段需传数字）"""
    if not activity_id:
        return None
    # A-0002 -> 2
    m = re.search(r'(\d+)', str(activity_id))
    if not m:
        return None
    num = int(m.group(1))
    items = search_records(ACTIVITY_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": "活动ID", "operator": "is", "value": [str(num)]}]
    })
    return items[0] if items else None


