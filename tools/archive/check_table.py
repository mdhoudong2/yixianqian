#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查表是否存在"""
import requests
import json

import local_config as _cfg
APP_ID = _cfg.APP_ID
APP_SECRET = _cfg.APP_SECRET
BASE_TOKEN = _cfg.BASE_TOKEN

def get_token():
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET})
    return resp.json()["tenant_access_token"]

token = get_token()
headers = {"Authorization": f"Bearer {token}"}

# 列出所有表
url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables"
resp = requests.get(url, headers=headers)
result = resp.json()
if result.get("code") == 0:
    for t in result["data"]["items"]:
        print(f"table_id={t['table_id']} | name={t['name']}")
else:
    print(f"Error: {result}")
