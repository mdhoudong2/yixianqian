#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性脚本：给一对一结果表加「轮次」单选字段(1..10)，并把既有空轮次行回填为「1」。

多轮一对一上线后，「执行一对一」的幂等按 (活动, 轮次) 判断；迁移前写入的结果行
没有轮次值——若不回填，第 1 轮会被认为「没跑过」，重跑会重复写结果。回填 = 把空
轮次行标为第 1 轮（历史结果本来就是第一轮），**只更新轮次一个栏位、不删任何行**。

字段名与 bot/constants.py 的 FIELD_OTO_ROUND、web/backend/config.py 的 F_OTO_ROUND
共用同一常量源——写错一个字就是「读到的永远是空、幂等永远失效」且不报错。

生产该表尚未建（由 create_onetoone_tables.py 建，字段清单里已含轮次）；本脚本是
**测试 base 专用**迁移：既有数据在测试服，生产不需要。

用法（在仓库根目录）：
  python scripts/dev/add_onetoone_result_round.py find     # 列出结果表现有字段/选项
  python scripts/dev/add_onetoone_result_round.py check    # 检查缺不缺字段、多少行要回填（默认）
  python scripts/dev/add_onetoone_result_round.py apply    # 真的加 + 回填（幂等，重复跑安全）

安全防护：字段已存在但缺选项时，若该字段已有非空值 → 拒绝补齐选项（name-only 的
options 覆写 PUT 会清空该字段所有既有值，审计已实证）——必须人工处理。

生产默认拦截（_prod_guard）；只允许 YIXIANQIAN_ENV=test 执行。
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

if os.environ.get("YIXIANQIAN_ENV", "prod") != "test" and os.environ.get("YX_DEV_ALLOW") != "1":
    print("⛔ 本脚本是测试 base 专用迁移（YIXIANQIAN_ENV 不是 test）。如确需执行，请设 YX_DEV_ALLOW=1")
    sys.exit(1)

import requests

import constants as _c
import local_config as _cfg

APP_ID = getattr(_cfg, "FEISHU_APP_ID", None) or getattr(_cfg, "APP_ID", None)
APP_SECRET = getattr(_cfg, "FEISHU_APP_SECRET", None) or getattr(_cfg, "APP_SECRET", None)
BASE_TOKEN = _cfg.BASE_TOKEN
TABLE_ID = getattr(_cfg, "ONETOONE_RESULT_TABLE", None) or getattr(_c, "ONETOONE_RESULT_TABLE", "")
FIELD_NAME = _c.FIELD_OTO_ROUND
OPTIONS = [str(i) for i in range(1, 11)]

BASE_URL = "https://open.feishu.cn/open-apis"
BATCH = 100  # 每次 batch_update 的行数（远低于接口上限，稳）


def get_token():
    resp = requests.post(f"{BASE_URL}/auth/v3/tenant_access_token/internal",
                         json={"app_id": APP_ID, "app_secret": APP_SECRET})
    data = resp.json()
    if not data.get("tenant_access_token"):
        print("拿不到 tenant_access_token:", json.dumps(data, ensure_ascii=False))
        sys.exit(1)
    return data["tenant_access_token"]


def _headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def list_fields(token):
    url = f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/fields"
    out, page_token = [], None
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        d = requests.get(url, headers=_headers(token), params=params).json().get("data", {})
        out.extend(d.get("items", []))
        if not d.get("has_more"):
            break
        page_token = d.get("page_token")
    return out


def create_field(token):
    url = f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/fields"
    data = {"field_name": FIELD_NAME, "type": 3,
            "property": {"options": [{"name": o} for o in OPTIONS]}}
    result = requests.post(url, headers=_headers(token), json=data).json()
    if result.get("code") == 0:
        print(f"✅ 已添加字段：{FIELD_NAME}（单选：{'/'.join(OPTIONS)}）")
        return True
    print(f"❌ 添加字段失败：{result.get('code')} {result.get('msg')}")
    return False


def options_overwrite_allowed(total, empty_count):
    """字段缺选项时的硬防护：该字段已有非空值 → 禁止覆盖选项。

    飞书 PUT fields 的 options 不带 id 等于「整组替换」；审计用 scratch 字段实证：
    name-only 的 PUT 会清空该字段所有既有值。补齐分支只在整字段零值时才安全，
    有值一律拒绝，要求人工在飞书表格里处理。
    """
    return total - empty_count == 0


def update_field_options(token, field, missing):
    """字段已存在但缺选项时补齐（保留既有选项与颜色）。"""
    keep = []
    for o in (field.get("property") or {}).get("options") or []:
        item = {"name": o.get("name")}
        if o.get("color"):
            item["color"] = o["color"]
        keep.append(item)
    keep.extend({"name": m} for m in missing)
    url = f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/fields/{field.get('field_id')}"
    data = {"field_name": FIELD_NAME, "type": 3, "property": {"options": keep}}
    result = requests.put(url, headers=_headers(token), json=data).json()
    if result.get("code") == 0:
        print(f"✅ 已补齐字段选项：{FIELD_NAME} -> {[i['name'] for i in keep]}")
        return True
    print(f"❌ 补齐选项失败：{result.get('code')} {result.get('msg')}")
    return False


def iter_records(token):
    """分页拉全表记录（GET records 列表接口）。"""
    url = f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/records"
    page_token = None
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        d = requests.get(url, headers=_headers(token), params=params).json().get("data", {})
        for item in d.get("items", []):
            yield item
        if not d.get("has_more"):
            break
        page_token = d.get("page_token")


def scan(token):
    """返回 (总行数, 空轮次行的 record_id 列表)；空轮次 = 无值/空串。"""
    total = 0
    empty_ids = []
    for rec in iter_records(token):
        total += 1
        val = (rec.get("fields") or {}).get(FIELD_NAME)
        if val is None or val == "" or val == []:
            empty_ids.append(rec["record_id"])
    return total, empty_ids


def backfill(token, record_ids):
    """把空轮次行逐批更新为「1」；返回成功更新的行数。"""
    updated = 0
    for i in range(0, len(record_ids), BATCH):
        chunk = record_ids[i:i + BATCH]
        url = f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/records/batch_update"
        body = {"records": [{"record_id": rid, "fields": {FIELD_NAME: "1"}} for rid in chunk]}
        result = requests.post(url, headers=_headers(token), json=body).json()
        if result.get("code") != 0:
            print(f"❌ 回填失败（第 {i // BATCH + 1} 批）：{result.get('code')} {result.get('msg')}")
            return updated
        updated += len(chunk)
        print(f"   ... 已回填 {updated}/{len(record_ids)} 行")
    return updated


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "check").lower()
    if mode not in ("find", "check", "apply"):
        print(__doc__)
        sys.exit(1)
    print(f"环境：{os.environ.get('YIXIANQIAN_ENV', 'prod')}   模式：{mode}")
    print(f"目标：一对一结果表 {TABLE_ID}\n")
    if not TABLE_ID:
        print("❌ 未配置 ONETOONE_RESULT_TABLE（bot/local_config.py）")
        sys.exit(1)

    token = get_token()
    fields = list_fields(token)
    names = [f.get("field_name") for f in fields]

    if mode == "find":
        print(f"结果表现有 {len(names)} 个字段：")
        for f in fields:
            opts = [o.get("name") for o in (f.get("property") or {}).get("options") or []]
            print(f"  - {f.get('field_name')}" + (f"（单选：{'/'.join(opts)}）" if opts else ""))
        return

    field = next((f for f in fields if f.get("field_name") == FIELD_NAME), None)
    if field is None:
        print(f"⬜ 缺字段：{FIELD_NAME}（单选：{'/'.join(OPTIONS)}）")
    else:
        opts = [o.get("name") for o in (field.get("property") or {}).get("options") or []]
        missing = [o for o in OPTIONS if o not in opts]
        if missing:
            print(f"⚠ 字段已存在但缺选项：{missing}（现有：{opts}）")
        else:
            print(f"✅ 字段已存在，选项齐全：{FIELD_NAME}")

    total, empty_ids = scan(token)
    print(f"结果表共 {total} 行；空轮次（待回填为「1」）{len(empty_ids)} 行")

    if mode == "check":
        print("（check 模式不动表；确认后执行 apply）")
        return

    # ---- apply ----
    if field is None:
        if not create_field(token):
            sys.exit(1)
    else:
        opts = [o.get("name") for o in (field.get("property") or {}).get("options") or []]
        missing = [o for o in OPTIONS if o not in opts]
        if missing:
            if not options_overwrite_allowed(total, len(empty_ids)):
                print(f"⛔ 字段「{FIELD_NAME}」已有 {total - len(empty_ids)} 行非空值；"
                      f"覆盖选项会清空该字段所有既有值，拒绝补齐。请人工在飞书表格里补选项。")
                sys.exit(1)
            if not update_field_options(token, field, missing):
                sys.exit(1)

    if empty_ids:
        updated = backfill(token, empty_ids)
        if updated != len(empty_ids):
            print(f"❌ 回填不完整：成功 {updated}/{len(empty_ids)}，请重跑 apply")
            sys.exit(1)
    else:
        print("无需回填（没有空轮次行）")

    total2, empty2 = scan(token)
    print("\n===== 回填后验证 =====")
    print(f"总行数：{total} -> {total2}（{'一致 ✅' if total2 == total else '不一致 ❌'}）")
    print(f"空轮次：{len(empty_ids)} -> {len(empty2)}（{'已清空 ✅' if not empty2 else '仍有残留 ❌'}）")
    if total2 != total or empty2:
        sys.exit(1)
    print("完成。")


if __name__ == "__main__":
    main()
