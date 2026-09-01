#!/usr/bin/env python3
"""清理压测假号（按飞书用户ID前缀批量删除）。

用法：
  python3 cleanup_test_users.py plan            # 只打印计划
  python3 cleanup_test_users.py run             # 真实删除
  python3 cleanup_test_users.py run --prefix=ou_fake_load_ --confirm
"""
import os
import sys
import time

_D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _D)
sys.path.insert(0, os.path.dirname(_D))
from _prod_guard import guard  # noqa: E402

guard(os.path.basename(__file__))

sys.path.insert(0, os.path.join(os.path.dirname(_D), "..", "web", "backend"))

from config import BASE_TOKEN, FEISHU_APP_ID, FEISHU_APP_SECRET, USER_TABLE_ID  # noqa: E402

from lib.bitable_client import BitableClient, get_field_text  # noqa: E402

PREFIX = "ou_fake_load_"
BATCH = 10


def collect(client, prefix):
    """按前缀 contains 搜索全部匹配记录（分页拉全）。"""
    recs = client.search_records(
        USER_TABLE_ID,
        [{"field_name": "飞书用户ID", "operator": "contains", "value": [prefix]}],
        page_size=100,
    )
    return recs or []


def main():
    mode = "plan"
    prefix = PREFIX
    confirm = "--confirm" in sys.argv
    for a in sys.argv[1:]:
        if a == "plan" or a == "run":
            mode = a
        if a.startswith("--prefix="):
            prefix = a.split("=", 1)[1]
    client = BitableClient(FEISHU_APP_ID, FEISHU_APP_SECRET, BASE_TOKEN)
    recs = collect(client, prefix)
    if not recs:
        print("没有匹配的压测号，前缀:", prefix)
        return
    print(f"发现 {len(recs)} 条前缀={prefix} 的记录")
    if mode == "plan":
        for r in recs[:10]:
            f = r.get("fields", {})
            print(" ", r.get("record_id"), get_field_text(f, "昵称"), get_field_text(f, "飞书用户ID")[:20])
        print(f"（仅前10条预览）执行: python3 {os.path.basename(__file__)} run --prefix={prefix}")
        return
    if not confirm:
        print("run 模式需追加 --confirm 才会真实删除")
        return
    deleted = 0
    for i in range(0, len(recs), BATCH):
        ids = [r.get("record_id") for r in recs[i:i + BATCH]]
        deleted += client.batch_delete_records(USER_TABLE_ID, ids, batch_size=BATCH)
        print(f"已删 {deleted}/{len(recs)}")
        time.sleep(0.3)
    print(f"完成，共删除 {deleted} 条")


if __name__ == "__main__":
    main()
