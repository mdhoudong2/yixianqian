#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查看表字段"""
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

# 查看活动报名表字段
for table_id, name in [("tblNVJCnohVaWf8t", "活动报名表"), ("tblsecbZZv0thaPe", "用户表"), ("tblHLltReY8xHTfu", "活动表")]:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/fields"
    resp = requests.get(url, headers=headers)
    result = resp.json()
    print(f"\n=== {name} ({table_id}) ===")
    if result.get("code") == 0:
        for f in result["data"]["items"]:
            print(f"  field_name={f['field_name']} | type={f['type']}")
    else:
        print(f"  Error: {result}")
