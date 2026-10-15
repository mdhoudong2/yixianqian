#!/usr/bin/env python3
#!/usr/bin/env python3
import os, sys
_D = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_D))
for _p in (_D, os.path.dirname(_D), os.path.join(_ROOT, "bot"), _ROOT):
    sys.path.insert(0, _p)
from _prod_guard import guard
guard(os.path.basename(__file__))
# -*- coding: utf-8 -*-
"""一次性脚本：给分组结果表(GROUP_RESULT_TABLE)添加「轮次」文本字段（不设上限）。

轮次字段=文本、不限轮次；存量单选环境用 convert_round_to_text.py 转换。

用法: ./venv/bin/python add_group_result_round.py
幂等：字段已存在则跳过。
"""
import requests

import constants as _c
import local_config as _cfg
APP_ID = getattr(_cfg, "FEISHU_APP_ID", None) or getattr(_cfg, "APP_ID", None)
APP_SECRET = getattr(_cfg, "FEISHU_APP_SECRET", None) or getattr(_cfg, "APP_SECRET", None)
BASE_TOKEN = _cfg.BASE_TOKEN
GROUP_RESULT_TABLE_ID = (getattr(_cfg, "GROUP_RESULT_TABLE", None)
                         or getattr(_c, "GROUP_RESULT_TABLE", ""))  # 分组结果表
FIELD_NAME = "轮次"


def get_token():
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET})
    return resp.json()["tenant_access_token"]


def list_fields(token):
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{GROUP_RESULT_TABLE_ID}/fields"
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
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{GROUP_RESULT_TABLE_ID}/fields"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    # type=1 文本：不限轮次（任何 ≥1 的整数写入都不会失败）
    data = {"field_name": FIELD_NAME, "type": 1}
    resp = requests.post(url, headers=headers, json=data)
    result = resp.json()
    if result.get("code") == 0:
        print(f"✅ 已添加字段: {FIELD_NAME} (文本，不设上限)")
    else:
        print(f"❌ 添加字段失败: {result.get('code')} {result.get('msg')}")


def main():
    token = get_token()
    existing = {f["field_name"]: f for f in list_fields(token)}
    if FIELD_NAME in existing:
        ftype = existing[FIELD_NAME].get("type")
        print(f"字段已存在，跳过: {FIELD_NAME}（type={ftype}）")
        if ftype == 3:
            print("（仍是单选：不限轮次方案请运行 scripts/dev/convert_round_to_text.py apply 转文本）")
        return
    create_field(token)


if __name__ == "__main__":
    main()
