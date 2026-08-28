#!/usr/bin/env python3
"""回填数字红娘推荐表的 open_id 字段。

背景：红娘推荐原先只用昵称关联（推荐给用户/被推荐用户），用户改名或删号后
推荐记录悬空/重复。现新增「推荐给用户open_id」「被推荐用户open_id」两个字段，
生成侧已改为昵称+open_id 双写、open_id 对去重。

本脚本把存量记录的 open_id 字段按「昵称 → 用户表 open_id」映射回填：
- 昵称在用户表不存在 → 跳过并计数（悬空数据，应已由数据修复清理）
- 已回填且值一致 → 跳过（幂等，可重复执行）
- 昵称重复（同一昵称多个用户）→ 跳过并计数（需人工处理）
"""
import os
import sys

_D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _D)
from _prod_guard import guard

guard(os.path.basename(__file__))

_REPO = os.path.dirname(os.path.dirname(_D))
_BOT = os.path.join(_REPO, "bot")
sys.path.insert(0, _BOT)
sys.path.insert(0, _REPO)

import local_config
import requests
from constants import (
    BASE_TOKEN,
    FIELD_FEISHU_ID,
    FIELD_MATCH_FOR_USER,
    FIELD_MATCH_TARGET_USER,
    FIELD_NICKNAME,
    MATCH_TABLE_ID,
    USER_TABLE_ID,
)

from lib.bitable_client import BitableClient, get_field_text

# 新字段名（main 分支的 constants 尚未定义，脚本内自持，兼容两分支运行）
FIELD_MATCH_FOR_OPENID = "推荐给用户open_id"
FIELD_MATCH_TARGET_OPENID = "被推荐用户open_id"

_client = BitableClient(local_config.FEISHU_APP_ID, local_config.FEISHU_APP_SECRET, BASE_TOKEN)


def _get_records(table_id):
    """用 GET /records 分页拉全表（search API 分页在部分表有 bug，GET 稳定）。"""
    token = _client.get_token()
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records"
    headers = {"Authorization": f"Bearer {token}"}
    out, page_token = [], None
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        result = requests.get(url, headers=headers, params=params, timeout=15).json()
        out.extend(result.get("data", {}).get("items", []))
        if not result.get("data", {}).get("has_more"):
            break
        page_token = result["data"].get("page_token")
    return out


def _batch_update(table_id, updates):
    token = _client.get_token()
    url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}"
           f"/tables/{table_id}/records/batch_update")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    done = 0
    for i in range(0, len(updates), 500):
        chunk = updates[i:i + 500]
        result = requests.post(url, headers=headers,
                               json={"records": chunk}, timeout=30).json()
        if result.get("code") == 0:
            done += len(chunk)
        else:
            print(f"批量更新失败(第{i // 500 + 1}批): {result}")
    return done


def main():
    users = _client.search_records(USER_TABLE_ID, page_size=100)
    nick_to_openid = {}
    dup_nicks = []
    for item in users:
        fields = item.get("fields", {})
        nick = get_field_text(fields, FIELD_NICKNAME)
        open_id = get_field_text(fields, FIELD_FEISHU_ID)
        if not nick or not open_id:
            continue
        if nick in nick_to_openid:
            dup_nicks.append(nick)
        else:
            nick_to_openid[nick] = open_id
    print(f"用户表 {len(users)} 条，昵称→open_id 映射 {len(nick_to_openid)}，重复昵称 {len(set(dup_nicks))}")

    records = _get_records(MATCH_TABLE_ID)
    updates = []
    unmapped, skipped, already = 0, 0, 0
    for rec in records:
        rid = rec.get("record_id")
        fields = rec.get("fields", {})
        for_user = get_field_text(fields, FIELD_MATCH_FOR_USER)
        target_user = get_field_text(fields, FIELD_MATCH_TARGET_USER)
        new = {}
        if for_user:
            if for_user in dup_nicks:
                skipped += 1
                continue
            oid = nick_to_openid.get(for_user)
            if oid:
                if get_field_text(fields, FIELD_MATCH_FOR_OPENID) != oid:
                    new[FIELD_MATCH_FOR_OPENID] = oid
            else:
                unmapped += 1
        if target_user:
            if target_user in dup_nicks:
                skipped += 1
                continue
            oid = nick_to_openid.get(target_user)
            if oid:
                if get_field_text(fields, FIELD_MATCH_TARGET_OPENID) != oid:
                    new[FIELD_MATCH_TARGET_OPENID] = oid
            else:
                unmapped += 1
        if new:
            updates.append({"record_id": rid, "fields": new})
        else:
            already += 1
    done = _batch_update(MATCH_TABLE_ID, updates)
    print(f"红娘表 {len(records)} 条：待更新 {len(updates)} 实际成功 {done}，"
          f"已一致跳过 {already}，昵称悬空 {unmapped}，重复昵称跳过 {skipped}")


if __name__ == "__main__":
    main()
