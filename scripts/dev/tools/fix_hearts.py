#!/usr/bin/env python3
#!/usr/bin/env python3
import os, sys
_D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _D)
sys.path.insert(0, os.path.dirname(_D))
from _prod_guard import guard
guard(os.path.basename(__file__))
# -*- coding: utf-8 -*-
"""额度字段诊断（只读，不修改任何数据）

用法:
  python3 fix_hearts.py     # 统计「爱心剩余」分布与账号状态分布

v7 起「爱心剩余」= 本月剩余匿名额度，取值范围 0..10（= lib.quota.MONTHLY_ANON_HEARTS），
每月月初由机器人 reconcile_hearts 补满、不累积。**看到大于 10 的值就说明
对账没跑或跑的还是旧代码**——旧模型是初始 3 颗、上限 30。

本脚本仅作只读诊断，不提供任何写操作。
"""
import sys
import os
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import local_config as _cfg

APP_ID = _cfg.APP_ID
APP_SECRET = _cfg.APP_SECRET
BASE_TOKEN = _cfg.BASE_TOKEN
BASE_URL = "https://open.feishu.cn/open-apis"

USER_TABLE_ID = "tblsecbZZv0thaPe"
FIELD_HEART = "爱心剩余"
FIELD_STATUS = "账号状态"
# 本月匿名额度上限（lib.quota.MONTHLY_ANON_HEARTS）。超出即为旧模型的残留。
ANON_MAX = 10

_token = {"token": None, "expire": 0}


def get_token():
    import time
    if _token["token"] and _token["expire"] > time.time() + 60:
        return _token["token"]
    resp = requests.post(f"{BASE_URL}/auth/v3/tenant_access_token/internal",
                         json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=15)
    data = resp.json()
    _token["token"] = data["tenant_access_token"]
    _token["expire"] = time.time() + data.get("expire", 7200)
    return _token["token"]


def headers():
    return {"Authorization": f"Bearer {get_token()}", "Content-Type": "application/json"}


def list_all():
    items, page_token = [], None
    while True:
        url = f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{USER_TABLE_ID}/records?page_size=100"
        if page_token:
            url += f"&page_token={page_token}"
        resp = requests.get(url, headers=headers(), timeout=30)
        data = resp.json()
        if data.get("code") != 0:
            print(f"读取失败: {data.get('msg')}", file=sys.stderr)
            sys.exit(1)
        items.extend(data.get("data", {}).get("items", []))
        if not data.get("data", {}).get("has_more"):
            break
        page_token = data.get("data", {}).get("page_token")
    return items


def hearts_of(fields):
    v = fields.get(FIELD_HEART)
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, dict) and isinstance(v.get("value"), list):
        v = v["value"]
    if isinstance(v, list) and v:
        v = v[0]
        if isinstance(v, dict):
            v = v.get("value", v.get("text"))
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def main():
    items = list_all()
    print(f"共 {len(items)} 条用户记录\n")

    dist = {}
    status_dist = {}
    for it in items:
        fields = it.get("fields", {})
        h = hearts_of(fields)
        status = fields.get(FIELD_STATUS, "")
        if isinstance(status, dict):
            status = status.get("text", "")
        elif isinstance(status, list) and status:
            s0 = status[0]
            status = s0.get("text", "") if isinstance(s0, dict) else str(s0)
        dist[h] = dist.get(h, 0) + 1
        status_dist[str(status)] = status_dist.get(str(status), 0) + 1

    print("本月匿名额度（爱心剩余）分布：")
    for k in sorted(dist, key=lambda x: (x is None, x or 0)):
        print(f"  {k}: {dist[k]} 人")
    over = sum(v for k, v in dist.items() if k is not None and k > ANON_MAX)
    if over:
        print(f"\n⚠️  {over} 人的额度超过本月上限 {ANON_MAX}——"
              f"说明机器人对账没跑，或跑的仍是旧版（初始 3 颗、上限 30）。")
    print("\n账号状态分布：")
    for k, v in sorted(status_dist.items()):
        print(f"  {k}: {v} 人")
    print("\n[只读] 未做任何修改。")


if __name__ == "__main__":
    main()
