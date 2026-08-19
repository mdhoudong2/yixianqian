#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修正用户「爱心剩余」基数错误（初始应为3，实际被写成2）

用法:
  python3 fix_hearts.py                 # 只读：统计爱心分布，不修改
  python3 fix_hearts.py --apply         # 全体 +1（上限30），跳过「已隐藏」
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
MAX_HEARTS = 30

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
    apply = "--apply" in sys.argv
    items = list_all()
    print(f"共 {len(items)} 条用户记录\n")

    dist = {}
    updatable = []
    for it in items:
        fields = it.get("fields", {})
        h = hearts_of(fields)
        status = fields.get(FIELD_STATUS, "")
        # 状态字段可能是单选对象，统一取文本
        if isinstance(status, dict):
            status = status.get("text", "")
        elif isinstance(status, list) and status:
            s0 = status[0]
            status = s0.get("text", "") if isinstance(s0, dict) else str(s0)
        dist[h] = dist.get(h, 0) + 1
        if h is not None and str(status) != "已隐藏":
            new = min(h + 1, MAX_HEARTS)
            if new != h:
                updatable.append((it["record_id"], h, new))

    print("爱心分布：")
    for k in sorted(dist, key=lambda x: (x is None, x or 0)):
        print(f"  爱心={k}: {dist[k]} 人")
    print(f"\n可修正（+1，上限30，跳过已隐藏）: {len(updatable)} 人")

    if not apply:
        print("\n[只读模式] 未做任何修改。确认无误后运行: python3 fix_hearts.py --apply")
        for rid, old, new in updatable[:20]:
            print(f"  预览: {rid}  {old} -> {new}")
        if len(updatable) > 20:
            print(f"  ... 其余 {len(updatable) - 20} 条")
        return

    ok = fail = 0
    for rid, old, new in updatable:
        resp = requests.put(
            f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{USER_TABLE_ID}/records/{rid}",
            headers=headers(), json={"fields": {FIELD_HEART: new}}, timeout=15)
        if resp.json().get("code") == 0:
            ok += 1
        else:
            fail += 1
            print(f"  失败 {rid}: {resp.json().get('msg')}", file=sys.stderr)
    print(f"\n完成：成功 {ok}，失败 {fail}")


if __name__ == "__main__":
    main()
