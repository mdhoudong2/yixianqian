#!/usr/bin/env python3
"""村情六处独立表一次性搭建/迁移脚本（Claude 运维用，可删除）。

用法（在服务器 /opt/yixianqian 下，PYTHONPATH=/opt/yixianqian 用 venv python 跑）：
  python observer_table_setup.py fields               # 列出用户表字段（name type options）
  python observer_table_setup.py create               # 建「村情六处」表并镜像用户表字段，打印新表ID
  python observer_table_setup.py migrate <表ID>        # 把用户表里村情六处记录搬进新表并更新绑定
  python observer_table_setup.py migrate <表ID> --dry  # 干跑，只打印不落库
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import requests
import local_config as _cfg

from _prod_guard import guard

APP_ID = getattr(_cfg, "FEISHU_APP_ID", None) or getattr(_cfg, "APP_ID", None)
APP_SECRET = getattr(_cfg, "FEISHU_APP_SECRET", None) or getattr(_cfg, "APP_SECRET", None)
BASE_TOKEN = _cfg.BASE_TOKEN
USER_TABLE_ID = getattr(_cfg, "USER_TABLE_ID", "tblsecbZZv0thaPe")
BINDING_FILE = os.path.join(
    os.environ.get("YXQ_DATA_DIR")
    or getattr(_cfg, "SHARED_DATA_DIR", None)
    or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data"),
    "yixianqian_bindings.json")

BASE_URL = "https://open.feishu.cn/open-apis"
OBSERVER_TABLE_NAME = "村情六处"
STATUS_OBSERVER = "村情六处"

# 不可写入/自动字段类型：20公式 1001创建时间 1002修改时间 1003创建人 1004修改人 1005自动编号 1006按钮
AUTO_TYPES = {20, 1001, 1002, 1003, 1004, 1005, 1006}


def _token():
    r = requests.post(f"{BASE_URL}/auth/v3/tenant_access_token/internal",
                      json={"app_id": APP_ID, "app_secret": APP_SECRET})
    return r.json()["tenant_access_token"]


def _h():
    return {"Authorization": "Bearer " + _token(), "Content-Type": "application/json"}


def _list_fields(table_id):
    r = requests.get(f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/fields?page_size=200", headers=_h())
    return r.json().get("data", {}).get("items", [])


def cmd_fields():
    for f in _list_fields(USER_TABLE_ID):
        p = f.get("property", {}) or {}
        opts = p.get("options") or []
        extra = " options=" + ",".join(o.get("name", "") for o in opts) if opts else ""
        print(f"{f.get('field_name')}\ttype={f.get('type')}{extra}")


def _create_table():
    r = requests.post(f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables", headers=_h(),
                      json={"table": {"name": OBSERVER_TABLE_NAME}})
    data = r.json()
    if data.get("code") != 0:
        print("建表失败:", json.dumps(data, ensure_ascii=False))
        sys.exit(1)
    return data["data"]["table_id"]


def _create_field(table_id, name, ftype, prop):
    body = {"field_name": name, "type": ftype}
    if prop:
        body["property"] = prop
    r = requests.post(f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/fields",
                      headers=_h(), json=body)
    d = r.json()
    if d.get("code") != 0:
        print(f"  字段创建失败 {name}: {d.get('msg')}")


def cmd_create():
    new_id = _create_table()
    print("新表ID:", new_id)
    src = _list_fields(USER_TABLE_ID)
    created = 0
    for f in src:
        ftype = f.get("type")
        if ftype in AUTO_TYPES:
            continue
        prop = None
        p = f.get("property", {}) or {}
        if ftype in (3, 4) and p.get("options"):  # 单选/多选：复制选项
            prop = {"options": [{"name": o.get("name")} for o in p["options"]]}
        _create_field(new_id, f.get("field_name"), ftype, prop)
        created += 1
    print(f"已镜像 {created} 个字段")


def _records(table_id):
    items, page_token = [], None
    while True:
        url = f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records?page_size=100"
        if page_token:
            url += f"&page_token={page_token}"
        r = requests.get(url, headers=_h())
        d = r.json().get("data", {})
        items.extend(d.get("items", []))
        if not d.get("has_more"):
            break
        page_token = d.get("page_token")
    return items


def cmd_migrate(obs_table_id, dry=False):
    print(f"目标观察员表: {obs_table_id}")
    users = _records(USER_TABLE_ID)
    obs_records = [it for it in users if (it.get("fields", {}).get("账号状态") or "") == STATUS_OBSERVER]
    print(f"用户表共 {len(users)} 条，其中村情六处 {len(obs_records)} 条")

    # 加载绑定
    bindings = {}
    if os.path.exists(BINDING_FILE):
        with open(BINDING_FILE, "r", encoding="utf-8") as fp:
            bindings = json.load(fp)

    for it in obs_records:
        old_rid = it.get("record_id")
        fields = it.get("fields", {})
        open_id = fields.get("飞书用户ID", "")
        nickname = fields.get("昵称", "")
        move = {k: v for k, v in fields.items()
                if k not in ("创建人", "用户ID", "注册时间", "资料更新时间", "喜欢（可点击）")
                and v is not None and v != ""}
        move["账号状态"] = STATUS_OBSERVER
        move["爱心剩余"] = 0
        print(f"  {old_rid}  昵称={nickname}  open_id={open_id}")
        if dry:
            continue
        r = requests.post(f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{obs_table_id}/records",
                          headers=_h(), json={"fields": move})
        d = r.json()
        if d.get("code") != 0:
            print(f"    搬移建新记录失败: {d.get('msg')}")
            continue
        new_rid = d["data"]["record"]["record_id"]
        requests.delete(f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{USER_TABLE_ID}/records/{old_rid}", headers=_h())
        if open_id in bindings:
            bindings[open_id]["record_id"] = new_rid
            bindings[open_id]["bind_type"] = "observer"
        print(f"    已搬移 -> {new_rid}")

    if not dry:
        with open(BINDING_FILE, "w", encoding="utf-8") as fp:
            json.dump(bindings, fp, ensure_ascii=False, indent=2)
        print(f"绑定已更新: {BINDING_FILE}")
    print("完成")


if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        sys.exit(0)
    cmd = argv[0]
    if cmd == "fields":
        cmd_fields()
    elif cmd == "create":
        guard("observer_table_setup.py create")
        cmd_create()
    elif cmd == "migrate":
        dry = "--dry" in argv
        tid = argv[1] if len(argv) > 1 and argv[1] != "--dry" else None
        if not tid:
            print("用法: migrate <表ID> [--dry]")
            sys.exit(1)
        if not dry:
            guard("observer_table_setup.py migrate")
        cmd_migrate(tid, dry)
    else:
        print(__doc__)
