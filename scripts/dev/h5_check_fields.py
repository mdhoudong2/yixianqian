# -*- coding: utf-8 -*-
import bitable, requests, json
from config import *

token = bitable.get_token()
headers = {'Authorization': 'Bearer ' + token}

def get_fields(table_id):
    url = 'https://open.feishu.cn/open-apis/bitable/v1/apps/%s/tables/%s/fields' % (BASE_TOKEN, table_id)
    resp = requests.get(url, headers=headers, timeout=15)
    return resp.json().get('data', {}).get('items', [])

def get_views(table_id):
    url = 'https://open.feishu.cn/open-apis/bitable/v1/apps/%s/tables/%s/views' % (BASE_TOKEN, table_id)
    resp = requests.get(url, headers=headers, timeout=15)
    return resp.json().get('data', {}).get('items', [])

def get_view_fields(table_id, view_id):
    """获取视图中显示的字段（通过view的visible_field_ids）"""
    url = 'https://open.feishu.cn/open-apis/bitable/v1/apps/%s/tables/%s/views/%s' % (BASE_TOKEN, table_id, view_id)
    resp = requests.get(url, headers=headers, timeout=15)
    return resp.json().get('data', {}).get('view', {})

print('=== 用户表字段 ===')
for f in get_fields(USER_TABLE_ID):
    print('  %s (type=%s)' % (f['field_name'], f['type']))

print()
print('=== 用户表视图 ===')
for v in get_views(USER_TABLE_ID):
    print('  %s (id=%s, type=%s)' % (v['view_name'], v['view_id'], v['view_type']))

print()
print('=== 活动表字段 ===')
for f in get_fields(ACTIVITY_TABLE_ID):
    print('  %s (type=%s)' % (f['field_name'], f['type']))

# 查看活跃男生/女生视图的可见字段
print()
views = get_views(USER_TABLE_ID)
for v in views:
    if '活跃' in v['view_name']:
        print('=== %s 视图详情 ===' % v['view_name'])
        detail = get_view_fields(USER_TABLE_ID, v['view_id'])
        visible = detail.get('visible_field_ids', [])
        print('  可见字段IDs: %s' % visible)
        # 获取字段名映射
        all_fields = get_fields(USER_TABLE_ID)
        id_to_name = {f['field_id']: f['field_name'] for f in all_fields}
        for fid in visible:
            print('    - %s' % id_to_name.get(fid, fid))
