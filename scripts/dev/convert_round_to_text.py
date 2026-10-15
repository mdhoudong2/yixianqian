#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性脚本：把「分组结果表」「一对一结果表」的「轮次」字段由单选转文本(type=1)。

背景：「轮次」原为单选栏位（飞书侧只配了 1..10），写入超范围值会整批失败并触发回滚；
现改为真正「不限轮次」：字段转文本、代码只要求 ≥1 的整数。飞书接口已实测：
PUT /bitable/v1/apps/{app}/tables/{table}/fields/{field_id}
body {"field_name":"轮次","type":1} 可把单选转文本且**保留既有值**。

用法（在仓库根目录）：
  YIXIANQIAN_ENV=dev bot/venv/bin/python scripts/dev/convert_round_to_text.py check
  YIXIANQIAN_ENV=dev bot/venv/bin/python scripts/dev/convert_round_to_text.py apply

模式：
  check  只读：打印两表「轮次」字段当前 type/options 与待转换项
  apply  逐表转文本（已是文本则跳过）；PUT 失败如实报错 exit 1；写后随机抽 3 行
         非空值打印，确认既有值原样保留

测试服与生产各需执行一次；生产默认拦截（_prod_guard，需 YX_DEV_ALLOW=1 显式授权）。
未显式授权时拒绝 BASE_TOKEN=生产 base 的执行（防误连生产）。
"""
import json
import os
import random
import re
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

# 每张表：(表名, 字段名, 表 ID)。表 ID 一律从 local_config 取（constants 兜底），
# 缺失/为空即报错退出：表 ID 按 base 分配，拿着一个 base 的 token 配另一个 base 的表 ID，
# 轻则 404，重则改错环境。
TARGETS = [
    ("分组结果表", _c.FIELD_GR_ROUND,
     getattr(_cfg, "GROUP_RESULT_TABLE", None) or getattr(_c, "GROUP_RESULT_TABLE", "")),
    ("一对一结果表", _c.FIELD_OTO_ROUND,
     getattr(_cfg, "ONETOONE_RESULT_TABLE", None) or getattr(_c, "ONETOONE_RESULT_TABLE", "")),
]

BASE_URL = "https://open.feishu.cn/open-apis"
SAMPLE_ROWS = 3
_PROD_CFG_PATH = "/opt/yixianqian/bot/local_config.py"


def _parse_base(path):
    """从 local_config.py 文本解析 BASE_TOKEN（不 import，避免读到错误环境的配置）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            m = re.search(r'^BASE_TOKEN\s*=\s*"([^"]+)"', fh.read(), re.M)
    except OSError:
        return None
    return m.group(1) if m else ""


_AUTHORIZED_PROD = (os.environ.get("YIXIANQIAN_ENV", "prod") == "prod"
                    and os.environ.get("YX_DEV_ALLOW") == "1")

if not _AUTHORIZED_PROD:
    _prod_base = _parse_base(_PROD_CFG_PATH)
    if _prod_base == "":
        print(f"⛔ 守卫失败：无法从 {_PROD_CFG_PATH} 解析 BASE_TOKEN，无法自证非生产")
        sys.exit(1)
    if _prod_base and BASE_TOKEN == _prod_base:
        print("⛔ 守卫失败：BASE_TOKEN 与生产 base 相同（拒绝连生产；生产执行需显式授权）")
        sys.exit(1)


def get_token():
    url = f"{BASE_URL}/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET})
    data = resp.json()
    if not data.get("tenant_access_token"):
        print("拿不到 tenant_access_token:", json.dumps(data, ensure_ascii=False))
        sys.exit(1)
    return data["tenant_access_token"]


def _headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def list_fields(token, table_id):
    """GET fields（分页）；失败返回 None。"""
    url = f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/fields"
    out, page_token = [], None
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        payload = requests.get(url, headers=_headers(token), params=params).json()
        if payload.get("code") != 0:
            print(f"❌ 读取字段失败（表 {table_id}）：{payload.get('code')} {payload.get('msg')}")
            return None
        d = payload.get("data") or {}
        out.extend(d.get("items") or [])
        if not d.get("has_more"):
            break
        page_token = d.get("page_token")
    return out


def find_round_field(token, table_id, field_name):
    """取「轮次」字段；表里没有则报错返回 None。"""
    fields = list_fields(token, table_id)
    if fields is None:
        return None
    field = next((f for f in fields if f.get("field_name") == field_name), None)
    if field is None:
        print(f"   ❌ 表 {table_id} 里找不到字段「{field_name}」，请核对表 ID")
        return None
    return field


def convert_to_text(token, table_id, field):
    """PUT fields/{field_id}：单选 -> 文本（既有值保留，接口已实测）。"""
    url = (f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}"
           f"/fields/{field.get('field_id')}")
    data = {"field_name": field.get("field_name"), "type": 1}
    result = requests.put(url, headers=_headers(token), json=data).json()
    if result.get("code") != 0:
        print(f"   ❌ 转换失败：{result.get('code')} {result.get('msg')}")
        return False
    return True


def sample_values(token, table_id, field_name):
    """随机抽 SAMPLE_ROWS 行该字段的非空值（校验既有值原样保留）；读失败返回 None。"""
    url = f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records"
    values, page_token = [], None
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        payload = requests.get(url, headers=_headers(token), params=params).json()
        if payload.get("code") != 0:
            print(f"   ⚠ 抽样读取失败：{payload.get('code')} {payload.get('msg')}")
            return None
        d = payload.get("data") or {}
        for item in d.get("items") or []:
            val = (item.get("fields") or {}).get(field_name)
            if val not in (None, "", []):
                values.append((item.get("record_id"), val))
        if not d.get("has_more"):
            break
        page_token = d.get("page_token")
    if not values:
        return []
    return random.sample(values, min(SAMPLE_ROWS, len(values)))


def process_table(token, label, field_name, table_id, mode):
    """处理单表；返回 True=成功（check 模式 = 可读且无异常）。"""
    print(f"—— {label}（{table_id}）")
    field = find_round_field(token, table_id, field_name)
    if field is None:
        return False
    ftype = field.get("type")
    opts = [o.get("name") for o in (field.get("property") or {}).get("options") or []]
    print(f"   字段「{field_name}」当前 type={ftype}" + (f"，options={opts}" if opts else ""))
    if ftype == 1:
        print("   ✅ 已是文本，无需转换")
        return True
    if ftype != 3:
        print(f"   ❌ 不是单选/文本（type={ftype}），拒绝转换，请人工处理")
        return False
    print(f"   ⬜ 待转换：单选 -> 文本（PUT fields/{field.get('field_id')} type=1）")
    if mode == "check":
        print("   （check 模式不动表；确认后执行 apply）")
        return True
    if not convert_to_text(token, table_id, field):
        return False
    field2 = find_round_field(token, table_id, field_name)
    if not field2 or field2.get("type") != 1:
        print(f"   ❌ 写入后重读 type={field2.get('type') if field2 else '<重读失败>'}，转换未生效")
        return False
    print("   ✅ 已转文本，重读确认 type=1")
    sample = sample_values(token, table_id, field_name)
    if sample is None:
        print("   ⚠ 抽样验证未完成（不影响转换结果，请人工抽查）")
    elif not sample:
        print("   （抽样：该字段暂无既有值行）")
    else:
        for rid, val in sample:
            print(f"   抽样保留：{rid} -> {val!r}")
    return True


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "check").lower()
    if mode not in ("check", "apply"):
        print(__doc__)
        sys.exit(1)
    print(f"环境：{os.environ.get('YIXIANQIAN_ENV', 'prod')}   模式：{mode}")
    print(f"目标 base：{BASE_TOKEN}\n")

    for label, _field_name, table_id in TARGETS:
        if not table_id:
            print(f"❌ {label} 表 ID 未配置（bot/local_config.py），拒绝执行")
            sys.exit(1)

    token = get_token()
    results = {}
    for label, field_name, table_id in TARGETS:
        results[label] = process_table(token, label, field_name, table_id, mode)

    print("\n===== 结果 =====")
    for label, ok in results.items():
        print(f"{'✅' if ok else '❌'} {label}")
    if not all(results.values()):
        print("存在失败项，退出码 1（可重跑 apply；本脚本幂等）。")
        sys.exit(1)
    if mode == "check":
        print("检查完成（待转换项见上；确认后执行 apply）。")
    else:
        print("完成：两表「轮次」均已为文本。可重跑 check 复核。")


if __name__ == "__main__":
    main()
