#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一对一要用的两张多维表格（幂等，重复跑安全）。

只做两件事：建「一对一选择表」和「一对一结果表」。一对一不进 SQLite——它不是账本，
管理员需要能直接查/改这两张表（照着分组的表来）。

用法（在仓库根目录）：
  python scripts/dev/create_onetoone_tables.py check     # 只看看现在缺什么
  python scripts/dev/create_onetoone_tables.py apply     # 真的建

跑完把打印出来的表 ID 填进服务器 local_config.py（bot/ 与 web/backend/ 各一份）：
  ONETOONE_SELECT_TABLE = "tbl..."
  ONETOONE_RESULT_TABLE = "tbl..."
**不填就是留空**：功能会明确说「还没配置」，而不是往一个不存在的表里写。

## 字段为什么长这样

「一对一选择表」与「分组选择表」字段完全同构：活动ID + 选择人三件套 + 第1..第7志愿
（位置即优先级）。「一对一结果表」是「每人每对象一行」：本人三件套 + 排名 + 必聊对象
open_id/昵称，管理员可直查某人有哪些必聊对象、谁进了前几名。

字段名必须与 `bot/constants.py` / `web/backend/config.py` 逐字一致——写错一个字就是
「写进去全是空、但不报错」的坏法（飞书多维表格一次 PUT 是整笔事务）。
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, "bot"))
sys.path.insert(0, _ROOT)

import local_config as _cfg
import requests
from _prod_guard import guard

APP_ID = getattr(_cfg, "FEISHU_APP_ID", None) or getattr(_cfg, "APP_ID", None)
APP_SECRET = getattr(_cfg, "FEISHU_APP_SECRET", None) or getattr(_cfg, "APP_SECRET", None)
BASE_TOKEN = _cfg.BASE_TOKEN

BASE_URL = "https://open.feishu.cn/open-apis"

# type: 1=文本 2=数字 3=单选 5=日期 1005=自动编号
# 每张表：(表名, [(字段名, type, 选项列表或 None), ...])
_TABLES = [
    ("一对一选择表", [
        ("活动ID", 1, None),
        ("选择人open_id", 1, None),
        ("选择人昵称", 1, None),
        ("选择人性别", 1, None),
        ("第1志愿", 1, None),
        ("第2志愿", 1, None),
        ("第3志愿", 1, None),
        ("第4志愿", 1, None),
        ("第5志愿", 1, None),
        ("第6志愿", 1, None),
        ("第7志愿", 1, None),
    ]),
    ("一对一结果表", [
        ("活动ID", 1, None),
        ("排名", 2, None),
        ("用户open_id", 1, None),
        ("用户昵称", 1, None),
        ("用户性别", 1, None),
        ("必聊对象open_id", 1, None),
        ("必聊对象昵称", 1, None),
    ]),
]


def _token():
    r = requests.post(f"{BASE_URL}/auth/v3/tenant_access_token/internal",
                      json={"app_id": APP_ID, "app_secret": APP_SECRET})
    data = r.json()
    if not data.get("tenant_access_token"):
        print("拿不到 tenant_access_token:", json.dumps(data, ensure_ascii=False))
        sys.exit(1)
    return data["tenant_access_token"]


def _h():
    return {"Authorization": "Bearer " + _token(), "Content-Type": "application/json"}


def _list_tables():
    r = requests.get(f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables?page_size=200",
                     headers=_h())
    return r.json().get("data", {}).get("items", [])


def _list_fields(table_id):
    r = requests.get(
        f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/fields?page_size=200",
        headers=_h())
    return r.json().get("data", {}).get("items", [])


def _find_table(name):
    for t in _list_tables():
        if t.get("name") == name:
            return t.get("table_id")
    return ""


def _add_field(table_id, fname, ftype, opts, apply_changes):
    payload = {"field_name": fname, "type": ftype}
    if opts:
        payload["property"] = {"options": [{"name": o} for o in opts]}
    if not apply_changes:
        print(f"   ⬜ 缺字段「{fname}」")
        return True
    rd = requests.post(
        f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/fields",
        headers=_h(), json=payload).json()
    if rd.get("code") != 0:
        print(f"   ❌ 字段「{fname}」建失败：{json.dumps(rd, ensure_ascii=False)}")
        return False
    print(f"   + {fname}")
    return True


def _ensure_fields(table_id, fields, apply_changes):
    have = {f.get("field_name") for f in _list_fields(table_id)}
    missing = [(n, t, o) for n, t, o in fields if n not in have]
    if not missing:
        print(f"✅ 字段齐全（{len(fields)} 个）")
        return True
    ok = True
    for fname, ftype, opts in missing:
        ok = _add_field(table_id, fname, ftype, opts, apply_changes) and ok
    return ok


def _ensure_table(name, fields, apply_changes):
    existing = _find_table(name)
    if existing:
        print(f"✅ 「{name}」已存在：{existing}")
        _ensure_fields(existing, fields, apply_changes)
        return existing
    if not apply_changes:
        print(f"⬜ 「{name}」不存在")
        return ""
    data = requests.post(f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables", headers=_h(),
                         json={"table": {"name": name}}).json()
    if data.get("code") != 0:
        print(f"❌ 建表「{name}」失败：{json.dumps(data, ensure_ascii=False)}")
        return ""
    table_id = data["data"]["table_id"]
    print(f"✅ 已建表「{name}」：{table_id}")
    _ensure_fields(table_id, fields, apply_changes)
    return table_id


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "check").lower()
    if mode not in ("check", "apply"):
        print(__doc__)
        sys.exit(1)
    apply_changes = mode == "apply"
    guard("create_onetoone_tables.py")

    print(f"环境：{os.environ.get('YIXIANQIAN_ENV', 'prod')}   模式：{mode}\n")
    ids = {name: _ensure_table(name, fields, apply_changes) for name, fields in _TABLES}

    print("\n" + "=" * 60)
    print("把下面两行填进服务器 local_config.py（bot/ 与 web/backend/ 各一份），"
          "没有就留空——功能会提示「还没配置」，不会硬写：")
    print(f'ONETOONE_SELECT_TABLE = "{ids["一对一选择表"]}"')
    print(f'ONETOONE_RESULT_TABLE = "{ids["一对一结果表"]}"')
    print("填完重启：systemctl restart yixianqian yixianqian-h5")
    if not any(ids.values()) and not apply_changes:
        print("（check 模式本来就不建表，加 apply 才真建。）")


if __name__ == "__main__":
    main()
