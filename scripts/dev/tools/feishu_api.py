#!/usr/bin/env python3
"""飞书多维表格CLI工具 - Claude Code使用
用法:
  python3 feishu_api.py tables                          # 列出所有数据表
  python3 feishu_api.py views <table_id>                # 列出表的视图
  python3 feishu_api.py list-users                      # 列出所有用户
  python3 feishu_api.py query <table_id> '<filter_json>' # 查询记录
  python3 feishu_api.py create <table_id> '<fields_json>' # 创建记录
  python3 feishu_api.py update <table_id> <record_id> '<fields_json>' # 更新记录
  python3 feishu_api.py get <table_id> <record_id>      # 获取单条记录
  python3 feishu_api.py delete <table_id> <record_id>   # 删除记录
  python3 feishu_api.py send <open_id> '<text>'         # 发送飞书消息
  python3 feishu_api.py token                           # 获取tenant_access_token
"""
import sys
import json
import requests

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import local_config as _cfg
APP_ID = _cfg.APP_ID
APP_SECRET = _cfg.APP_SECRET
BASE_TOKEN = _cfg.BASE_TOKEN
BASE_URL = "https://open.feishu.cn/open-apis"

_token_cache = {"token": None, "expire": 0}

def get_token():
    import time
    if _token_cache["token"] and _token_cache["expire"] > time.time() + 60:
        return _token_cache["token"]
    resp = requests.post(f"{BASE_URL}/auth/v3/tenant_access_token/internal", json={
        "app_id": APP_ID, "app_secret": APP_SECRET
    })
    data = resp.json()
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expire"] = time.time() + data.get("expire", 7200)
    return _token_cache["token"]

def headers():
    return {"Authorization": f"Bearer {get_token()}", "Content-Type": "application/json"}

def cmd_tables():
    resp = requests.get(f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables", headers=headers())
    for t in resp.json().get("data", {}).get("items", []):
        print(f"{t['table_id']}  {t['name']}")

def cmd_views(table_id):
    resp = requests.get(f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/views", headers=headers())
    for v in resp.json().get("data", {}).get("items", []):
        print(f"{v['view_id']}  {v['view_name']}  type={v['view_type']}")

def cmd_list_users():
    table_id = "tblsecbZZv0thaPe"
    items = []
    page_token = None
    while True:
        url = f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records?page_size=100"
        if page_token:
            url += f"&page_token={page_token}"
        resp = requests.get(url, headers=headers())
        data = resp.json().get("data", {})
        items.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
    for item in items:
        f = item.get("fields", {})
        nickname = f.get("昵称", "?")
        gender = f.get("性别", "?")
        status = f.get("审核状态", "?")
        open_id = f.get("飞书用户ID", "?")
        hearts = f.get("爱心数量", "?")
        print(f"{item['record_id']}  {nickname}  {gender}  状态={status}  爱心={hearts}  open_id={open_id}")
    print(f"\n共 {len(items)} 条记录")

def cmd_query(table_id, filter_json=None):
    body = {}
    if filter_json:
        body["filter"] = json.loads(filter_json)
    resp = requests.post(
        f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/search?page_size=50",
        headers=headers(), json=body
    )
    data = resp.json()
    if data.get("code") != 0:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    items = data.get("data", {}).get("items", [])
    for item in items:
        print(json.dumps({"record_id": item["record_id"], "fields": item.get("fields", {})}, ensure_ascii=False, indent=2))
    print(f"\n共 {len(items)} 条记录")

def cmd_create(table_id, fields_json):
    fields = json.loads(fields_json)
    resp = requests.post(
        f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records",
        headers=headers(), json={"fields": fields}
    )
    print(json.dumps(resp.json(), ensure_ascii=False, indent=2))

def cmd_update(table_id, record_id, fields_json):
    fields = json.loads(fields_json)
    resp = requests.put(
        f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/{record_id}",
        headers=headers(), json={"fields": fields}
    )
    print(json.dumps(resp.json(), ensure_ascii=False, indent=2))

def cmd_get(table_id, record_id):
    resp = requests.get(
        f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/{record_id}",
        headers=headers()
    )
    print(json.dumps(resp.json(), ensure_ascii=False, indent=2))

def cmd_delete(table_id, record_id):
    resp = requests.delete(
        f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/{record_id}",
        headers=headers()
    )
    print(json.dumps(resp.json(), ensure_ascii=False, indent=2))

def cmd_send(open_id, text):
    resp = requests.post(
        f"{BASE_URL}/im/v1/messages?receive_id_type=open_id",
        headers=headers(),
        json={"receive_id": open_id, "msg_type": "text", "content": json.dumps({"text": text})}
    )
    print(json.dumps(resp.json(), ensure_ascii=False, indent=2))

def cmd_token():
    print(get_token())

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "tables":
        cmd_tables()
    elif cmd == "views":
        cmd_views(sys.argv[2])
    elif cmd == "list-users":
        cmd_list_users()
    elif cmd == "query":
        cmd_query(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    elif cmd == "create":
        cmd_create(sys.argv[2], sys.argv[3])
    elif cmd == "update":
        cmd_update(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "get":
        cmd_get(sys.argv[2], sys.argv[3])
    elif cmd == "delete":
        cmd_delete(sys.argv[2], sys.argv[3])
    elif cmd == "send":
        cmd_send(sys.argv[2], sys.argv[3])
    elif cmd == "token":
        cmd_token()
    else:
        print(f"未知命令: {cmd}")
        print(__doc__)
