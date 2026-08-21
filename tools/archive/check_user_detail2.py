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
headers = {"Authorization": f"Bearer {token}"}

# 查看U-0012和U-0013的完整记录
for rid in ["rec27Zt4MSDEDd", "rec27ZtkecKhT5"]:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{USER_TABLE_ID}/records/{rid}"
    resp = requests.get(url, headers=headers)
    result = resp.json()
    if result.get("code") == 0:
        fields = result["data"]["record"]["fields"]
        print(f"\n=== record_id={rid} ===")
        for k, v in fields.items():
            print(f"  {k}: {v}")
    else:
        print(f"Error: {result}")
