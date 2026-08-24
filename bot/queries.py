"""用户/活动查询辅助（基于共享 BitableClient）。"""
import re

from clients import *
from constants import *


def _uid_sort_key(rec):
    """用户ID数值排序键（U-0012 -> 12；无ID排最后）"""
    m = re.match(r"[Uu]-?(\d+)", str(get_field_text(rec.get("fields", {}), "用户ID")))
    return int(m.group(1)) if m else 10 ** 9


def pick_primary_record(records):
    """同一 open_id 多条记录时取主档案：活跃优先，其次用户ID最小。
    全系统身份解析唯一入口——头像/资料/记账/归属一致性都依赖它。"""
    if not records:
        return records

    def rank(r):
        st = get_field_text(r.get("fields", {}), FIELD_ACCOUNT_STATUS)
        return (0 if st == "活跃" else 1, _uid_sort_key(r))

    return [sorted(records, key=rank)[0]]


def find_user_by_nickname(nickname):
    """通过昵称查找用户，返回记录列表"""
    return search_records(USER_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_NICKNAME, "operator": "is", "value": [nickname]}]
    })


def find_user_by_openid(open_id):
    """通过 open_id 查找用户。同号多档时返回主档案（单元素列表），
    调用方无需感知重复注册的存在。"""
    items = search_records(USER_TABLE_ID, {
        "conjunction": "and",
        "conditions": [{"field_name": FIELD_FEISHU_ID, "operator": "is", "value": [open_id]}]
    })
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
    return find_user_by_nickname(keyword)




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


