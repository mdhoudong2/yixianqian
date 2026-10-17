#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性脚本：给志愿表(GROUP_SELECT_TABLE)添加「轮次」数字字段。

跨轮差异约束（2026-10-16）依赖它：行记录保存时的提交轮次 = 该活动已执行最新轮次+1。
旧数据行该栏为空按第 1 轮处理；表里没有该栏时提交会因写栏位失败被拒——
**先跑本脚本、后发码**。test 与 prod 两个 base 都要加各自执行一次。

用法（在仓库根目录）：
  python scripts/ops/add_select_round_field.py find     # 列出志愿表现有字段（确认目标表）
  python scripts/ops/add_select_round_field.py check    # 检查缺不缺（默认）
  python scripts/ops/add_select_round_field.py apply    # 真的加（幂等，重复跑安全）

表 ID / 凭证按所在仓库的 web/backend/local_config.py（测试服=测试 base，生产=生产 base）。
生产默认拦截（_prod_guard）；确需在生产执行时显式设置 YX_DEV_ALLOW=1。
"""
import json
import os
import sys

_D = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_D))
for _p in (_D, os.path.join(_ROOT, "scripts", "dev"),
           os.path.join(_ROOT, "web", "backend"), _ROOT):
    sys.path.insert(0, _p)
from _prod_guard import guard
guard(os.path.basename(__file__))

import requests

import config as _cfg

TABLE_ID = _cfg.GROUP_SELECT_TABLE
FIELD_NAME = _cfg.F_GS_ROUND
APP_ID = _cfg.FEISHU_APP_ID
APP_SECRET = _cfg.FEISHU_APP_SECRET
BASE_TOKEN = _cfg.BASE_TOKEN


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
           f"/tables/{TABLE_ID}/fields")
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
           f"/tables/{TABLE_ID}/fields")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {
        "field_name": FIELD_NAME,
        "type": 2,  # 数字
        "property": {"formatter": "0"},  # 整数显示（轮次为 ≥1 的整数）
    }
    result = requests.post(url, headers=headers, json=data).json()
    if result.get("code") == 0:
        print(f"✅ 已添加字段：{FIELD_NAME}（数字，整数显示）")
        return True
    print(f"❌ 添加字段失败：{result.get('code')} {result.get('msg')}")
    return False


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "check").lower()
    if mode not in ("find", "check", "apply"):
        print(__doc__)
        sys.exit(1)
    print(f"环境：{os.environ.get('YIXIANQIAN_ENV', 'prod')}   模式：{mode}")
    print(f"目标：志愿表 {TABLE_ID}\n")

    token = get_token()
    fields = list_fields(token)

    if mode == "find":
        print(f"志愿表现有 {len(fields)} 个字段：")
        for f in fields:
            print(f"  - {f.get('field_name')}（type={f.get('type')}）")
        return

    existing = next((f for f in fields if f.get("field_name") == FIELD_NAME), None)
    if existing:
        print(f"✅ 字段已存在，跳过：{FIELD_NAME}（type={existing.get('type')}）")
        return

    print(f"⬜ 缺字段：{FIELD_NAME}（数字）")
    if mode == "apply":
        if not create_field(token):
            sys.exit(1)
        print("（可选）重跑 check 验证字段已就绪。")
    else:
        print("（check 模式不动表；确认后执行 apply）")


if __name__ == "__main__":
    main()
