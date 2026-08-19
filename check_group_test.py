#!/usr/bin/env python3
import json
# -*- coding: utf-8 -*-
"""深入检查A-0005报名者与用户表匹配情况"""
import requests

import local_config as _cfg
APP_ID = _cfg.APP_ID
APP_SECRET = _cfg.APP_SECRET
BASE_TOKEN = _cfg.BASE_TOKEN
SIGNUP_TABLE_ID = 'tblNVJCnohVaWf8t'
USER_TABLE_ID = 'tblsecbZZv0thaPe'

_token = None
def get_token():
    global _token
    r = requests.post('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
                      json={'app_id': APP_ID, 'app_secret': APP_SECRET}, timeout=10)
    return r.json().get('tenant_access_token')

def api(method, url, **kw):
    global _token
    if not _token:
        _token = get_token()
    headers = {'Authorization': f'Bearer {_token}', 'Content-Type': 'application/json'}
    return requests.request(method, url, headers=headers, **kw).json()

def list_all(table_id):
    out = []; page_token = ""
    while True:
        url = f'https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records?page_size=100'
        if page_token: url += f'&page_token={page_token}'
        r = api('GET', url)
        if r.get('code') != 0:
            print(f'list error: {r.get("msg")}'); break
        items = r.get('data', {}).get('items') or []
        out.extend(items)
        if r.get('data', {}).get('has_more'):
            page_token = r.get('data', {}).get('page_token')
        else: break
    return out

all_signups = list_all(SIGNUP_TABLE_ID)
signups = [s for s in all_signups if s.get('fields', {}).get('活动ID') == 'A-0005'
           and s.get('fields', {}).get('状态') == '已报名']

all_users = list_all(USER_TABLE_ID)
print(f"用户表总记录数: {len(all_users)}")
# 收集用户表所有oid
user_oids = set()
for u in all_users:
    oid = u.get('fields', {}).get('飞书用户ID')
    if oid:
        user_oids.add(oid)

print(f"\n--- 报名者open_id与用户表匹配情况 ---")
matched = 0
for s in signups[:50]:
    oid = s.get('fields', {}).get('报名人open_id')
    nick = s.get('fields', {}).get('报名人昵称')
    in_user = oid in user_oids
    if in_user: matched += 1
    print(f"  oid={oid} 昵称={nick} 在用户表={in_user}")
print(f"\n报名者中能在用户表匹配到的: {matched}/{len(signups)}")

# 看用户表前几个记录字段
print(f"\n--- 用户表前3条记录字段 ---")
for u in all_users[:3]:
    print(json.dumps(u.get('fields', {}), ensure_ascii=False, default=str)[:500])
