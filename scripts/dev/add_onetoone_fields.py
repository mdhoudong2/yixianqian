#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性脚本：给活动表(ACTIVITY_TABLE)添加一对一相关字段。

- 一对一状态（单选：收集中/已截止/已完成）—— 空值即「未开始」
- 一对一功能开启（单选：是/否）

用法: ./venv/bin/python add_onetoone_fields.py
幂等：字段已存在则跳过。
"""
import os
import sys

_D = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_D))
for _p in (_D, os.path.dirname(_D), os.path.join(_ROOT, "bot"), _ROOT):
    sys.path.insert(0, _p)
from _prod_guard import guard
guard(os.path.basename(__file__))

import requests

import local_config as _cfg

APP_ID = getattr(_cfg, "FEISHU_APP_ID", None) or getattr(_cfg, "APP_ID", None)
APP_SECRET = getattr(_cfg, "FEISHU_APP_SECRET", None) or getattr(_cfg, "APP_SECRET", None)
BASE_TOKEN = _cfg.BASE_TOKEN
ACTIVITY_TABLE_ID = getattr(_cfg, "ACTIVITY_TABLE_ID", None) or "tblHLltReY8xHTfu"  # 活动表

# (字段名, type, 选项)；type=3 单选
FIELDS = [
    ("一对一状态", 3, ["收集中", "已截止", "已完成"]),
    ("一对一功能开启", 3, ["是", "否"]),
]


def get_token():
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET})
    return resp.json()["tenant_access_token"]


def list_fields(token):
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{ACTIVITY_TABLE_ID}/fields"
    headers = {"Authorization": f"Bearer {token}"}
    out, page_token = [], None
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(url, headers=headers, params=params)
        d = r.json().get("data", {})
        out.extend(d.get("items", []))
        if not d.get("has_more"):
            break
        page_token = d.get("page_token")
    return out


def create_field(token, name, ftype, options):
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{ACTIVITY_TABLE_ID}/fields"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"field_name": name, "type": ftype}
    if options:
        data["property"] = {"options": [{"name": o} for o in options]}
    resp = requests.post(url, headers=headers, json=data)
    result = resp.json()
    if result.get("code") == 0:
        print(f"✅ 已添加字段: {name}")
    else:
        print(f"❌ 添加字段失败 {name}: {result.get('code')} {result.get('msg')}")


def main():
    token = get_token()
    existing = {f["field_name"] for f in list_fields(token)}
    for name, ftype, options in FIELDS:
        if name in existing:
            print(f"字段已存在，跳过: {name}")
            continue
        create_field(token, name, ftype, options)


if __name__ == "__main__":
    main()
