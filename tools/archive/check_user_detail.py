#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查看用户完整字段"""
import requests
import json

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
# 查看U-0012和U-0013的完整记录
for uid in ["U-0012", "U-0013"]:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{USER_TABLE_ID}/records/search"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"filter": {"conjunction": "and", "conditions": [{"field_name": "用户ID", "operator": "is", "value": [uid]}]}}
    resp = requests.post(url, headers=headers, json=data)
    result = resp.json()
    items = result.get("data", {}).get("items", [])
    print(f"\n=== {uid} ===")
    for item in items:
        for k, v in item.get("fields", {}).items():
            print(f"  {k}: {v}")
