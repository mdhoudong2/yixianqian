#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查用户表当前所有用户"""
import requests
import json
import time

import local_config as _cfg
APP_ID = _cfg.APP_ID
APP_SECRET = _cfg.APP_SECRET
BASE_TOKEN = _cfg.BASE_TOKEN
USER_TABLE_ID = "tblsecbZZv0thaPe"

def get_token():
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET})
    return resp.json()["tenant_access_token"]

token = get_token()
url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{USER_TABLE_ID}/records"
headers = {"Authorization": f"Bearer {token}"}

all_items = []
page_token = None
while True:
    params = {"page_size": 100}
    if page_token:
        params["page_token"] = page_token
    resp = requests.get(url, headers=headers, params=params)
    result = resp.json()
    if result.get("code") != 0:
        print(f"Error: {result}")
        break
    data = result.get("data", {})
    items = data.get("items", [])
    all_items.extend(items)
    if data.get("has_more"):
        page_token = data.get("page_token")
    else:
        break

print(f"用户表总记录数: {len(all_items)}")
print("=" * 80)
for item in all_items:
    fields = item.get("fields", {})
    uid = fields.get("用户ID", "")
    nickname = fields.get("昵称", "")
    name = fields.get("姓名", "")
    gender = fields.get("性别", "")
    status = fields.get("账号状态", "")
    print(f"record_id={item['record_id']} | 用户ID={uid} | 昵称={nickname} | 姓名={name} | 性别={gender} | 状态={status}")
