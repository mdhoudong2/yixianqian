# -*- coding: utf-8 -*-
import bitable, requests, json
from config import *

token = bitable.get_token()
headers = {'Authorization': 'Bearer ' + token}

# 查看有照片的用户记录
print('=== 用户记录示例（含照片） ===')
users = bitable.search_records(USER_TABLE_ID, page_size=3)
for u in users[:2]:
    f = u.get('fields', {})
    photo = f.get('个人照片')
    nickname = bitable.get_field_text(f, '昵称')
    print('昵称: %s' % nickname)
    print('个人照片原始值: %s' % json.dumps(photo, ensure_ascii=False, indent=2)[:500] if photo else '无照片')
    print('关于自己: %s' % bitable.get_field_text(f, '关于自己')[:50])
    print('身高: %s' % f.get('身高（cm）'))
    print('圣名: %s' % bitable.get_field_text(f, '圣名'))
    print('---')

# 查看活动海报
print()
print('=== 活动记录示例（含海报） ===')
acts = bitable.search_records(ACTIVITY_TABLE_ID, page_size=3)
for a in acts[:2]:
    f = a.get('fields', {})
    name = bitable.get_field_text(f, '活动名称')
    poster = f.get('活动海报')
    print('活动: %s' % name)
    print('海报原始值: %s' % json.dumps(poster, ensure_ascii=False, indent=2)[:500] if poster else '无海报')
    print('---')

# 尝试获取附件临时URL
if users:
    f = users[0].get('fields', {})
    photo = f.get('个人照片')
    if photo and isinstance(photo, list) and len(photo) > 0:
        file_token = photo[0].get('file_token') or photo[0].get('token')
        print()
        print('=== 尝试获取附件URL ===')
        print('file_token: %s' % file_token)
        # 方式1: 通过drive API获取临时下载URL
        url = 'https://open.feishu.cn/open-apis/drive/v1/medias/%s/download' % file_token
        resp = requests.get(url, headers=headers, allow_redirects=False, timeout=15)
        print('download API status: %s' % resp.status_code)
        print('Location: %s' % resp.headers.get('Location', 'N/A')[:200])
