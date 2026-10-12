#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性脚本：给活动表(ACTIVITY_TABLE)添加「签到功能开启」单选字段(是/否)。

现场签到（R3）零 schema 变更，但需要一个**每活动开关**，否则签到准入会突然
改变所有活动行为（含 bot 红线）。字段默认空 = 关；只有在控制台显式开启后才
生效。字段名与 bot/constants.py 的 FIELD_ACT_SIGNIN_FLAG、web/backend/config.py
的 F_ACTIVITY_SIGNIN_FLAG 共用同一常量源——写错一个字就是「读到的永远是空、
开关永远不生效」且不报错。

用法（在仓库根目录）：
  python scripts/dev/add_signin_flag_field.py find     # 列出活动表现有字段（确认目标表）
  python scripts/dev/add_signin_flag_field.py check    # 检查缺不缺（默认）
  python scripts/dev/add_signin_flag_field.py apply    # 真的加（幂等，重复跑安全）

生产默认拦截（_prod_guard）；测试服执行需显式 YX_DEV_ALLOW=1。
"""
import json
import os
import sys

_D = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_D))
for _p in (_D, os.path.dirname(_D), os.path.join(_ROOT, "bot"), _ROOT):
    sys.path.insert(0, _p)
from _prod_guard import guard
guard(os.path.basename(__file__))

import requests

import constants as _c
import local_config as _cfg

APP_ID = getattr(_cfg, "FEISHU_APP_ID", None) or getattr(_cfg, "APP_ID", None)
APP_SECRET = getattr(_cfg, "FEISHU_APP_SECRET", None) or getattr(_cfg, "APP_SECRET", None)
BASE_TOKEN = _cfg.BASE_TOKEN
ACTIVITY_TABLE_ID = getattr(_cfg, "ACTIVITY_TABLE_ID", None) or "tblHLltReY8xHTfu"  # 活动表

FIELD_NAME = _c.FIELD_ACT_SIGNIN_FLAG
OPTIONS = ["是", "否"]


def get_token():
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET})
    data = resp.json()
    if not data.get("tenant_access_token"):
        print("拿不到 tenant_access_token:", json.dumps(data, ensure_ascii=False))
        sys.exit(1)
    return data["tenant_access_token"]


def list_fields(token):
    url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}"
           f"/tables/{ACTIVITY_TABLE_ID}/fields")
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


def create_field(token):
    url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}"
           f"/tables/{ACTIVITY_TABLE_ID}/fields")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {
        "field_name": FIELD_NAME,
        "type": 3,  # 单选
        "property": {"options": [{"name": o} for o in OPTIONS]},
    }
    result = requests.post(url, headers=headers, json=data).json()
    if result.get("code") == 0:
        print(f"✅ 已添加字段：{FIELD_NAME}（单选：{'/'.join(OPTIONS)}）")
        return True
    print(f"❌ 添加字段失败：{result.get('code')} {result.get('msg')}")
    return False


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "check").lower()
    if mode not in ("find", "check", "apply"):
        print(__doc__)
        sys.exit(1)
    print(f"环境：{os.environ.get('YIXIANQIAN_ENV', 'prod')}   模式：{mode}")
    print(f"目标：活动表 {ACTIVITY_TABLE_ID}\n")

    token = get_token()
    fields = list_fields(token)
    names = [f.get("field_name") for f in fields]

    if mode == "find":
        print(f"活动表现有 {len(names)} 个字段：")
        for n in names:
            print("  -", n)
        return

    if FIELD_NAME in names:
        opt = next((f for f in fields if f.get("field_name") == FIELD_NAME), {})
        options = [o.get("name") for o in (opt.get("property") or {}).get("options") or []]
        print(f"✅ 字段已存在：{FIELD_NAME}（选项：{options}）")
        return

    print(f"⬜ 缺字段：{FIELD_NAME}（单选：{'/'.join(OPTIONS)}）")
    if mode == "apply":
        if not create_field(token):
            sys.exit(1)
        print("（可选）重跑 check 验证字段已就绪。")
    else:
        print("（check 模式不动表；确认后执行 apply）")


if __name__ == "__main__":
    main()
