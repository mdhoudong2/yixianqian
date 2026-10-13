#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""志愿表统一迁移（2026-10-13）：一对一旧志愿表 → 统一志愿表（分组选择表）补录。

背景：志愿已统一为一张表（config.ONETOONE_SELECT_TABLE 已指向 GROUP_SELECT_TABLE），
但历史上「一对一旧志愿表」里可能还有统一表没有的（活动, 人）行（早前提交或种子）。
本脚本把缺失行按栏位逐字复制进统一表；已存在的以统一表为准（不覆盖、不删除）。
两表栏位逐字同构（活动ID/选择人open_id/选择人昵称/选择人性别/第1-7志愿）。

运行有进程级互斥锁（同机并发跑两次会拒绝第二个）——2026-10-13 曾因并发双跑产生
重复行，另提供 --dedupe 清理（同 活动ID+选择人open_id 多行时保留第一条，删除多余）。

用法（仓库根，测试服）：
  YIXIANQIAN_ENV=dev web/backend/venv/bin/python scripts/dev/unify_preference_tables.py            # 干跑
  YIXIANQIAN_ENV=dev web/backend/venv/bin/python scripts/dev/unify_preference_tables.py --dedupe   # 清理重复行
  YIXIANQIAN_ENV=dev web/backend/venv/bin/python scripts/dev/unify_preference_tables.py --apply    # 写入
生产（发版时）需显式 YX_DEV_ALLOW=1 才允许执行。
"""

import argparse
import fcntl
import os
import sys
import tempfile

_D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _D)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(_D)), "web", "backend"))

from _prod_guard import guard  # noqa: E402

guard(os.path.basename(__file__))

import bitable  # noqa: E402
import config  # noqa: E402
import local_config  # noqa: E402

TEXT_FIELDS = ["活动ID", "选择人open_id", "选择人昵称"] + ["第%d志愿" % i for i in range(1, 8)]
SELECT_FIELDS = ["选择人性别"]


def _row_key(fields):
    return (bitable.get_field_text(fields, "活动ID"),
            bitable.get_field_text(fields, "选择人open_id"))


def _acquire_lock():
    """同机互斥：防止两个迁移进程并发各插一遍（2026-10-13 事故）。"""
    fh = open(os.path.join(tempfile.gettempdir(), "yxq_unify_preference_tables.lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("⛔ 已有另一个迁移进程在运行（锁被占用），退出。")
        sys.exit(2)
    return fh


def dedupe(table_id):
    """同（活动ID, 选择人open_id）多行时保留第一条，删除其余。"""
    rows = bitable.raw_search_records(table_id) or []
    seen, extras = set(), []
    for r in rows:
        key = _row_key(r.get("fields", {}))
        if not all(key):
            continue
        if key in seen:
            extras.append((key, r.get("record_id")))
        else:
            seen.add(key)
    print("统一表行数=%d，唯一键=%d，重复多余=%d" % (len(rows), len(seen), len(extras)))
    for key, rid in extras[:20]:
        print("  删除重复:", key, rid)
    if len(extras) > 20:
        print("  ...（共 %d 行）" % len(extras))
    ok = 0
    for key, rid in extras:
        if bitable._client.delete_record(table_id, rid):
            ok += 1
        else:
            print("  删除失败:", key, rid)
    print("去重完成：成功 %d / %d" % (ok, len(extras)))
    return 0 if ok == len(extras) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写入（默认只干跑）")
    ap.add_argument("--dedupe", action="store_true", help="清理统一表里的重复行（保留第一条）")
    args = ap.parse_args()

    _acquire_lock()

    unified = config.GROUP_SELECT_TABLE
    if args.dedupe:
        return dedupe(unified)

    legacy = getattr(local_config, "ONETOONE_SELECT_TABLE", "") or ""
    if not legacy or legacy == unified:
        print("旧一对一志愿表未配置或已与统一表相同（legacy=%r），无需迁移" % legacy)
        return 0

    rows_unified = bitable.raw_search_records(unified) or []
    rows_legacy = bitable.raw_search_records(legacy) or []
    have = {_row_key(r.get("fields", {})) for r in rows_unified
            if all(_row_key(r.get("fields", {})))}

    todo = []
    for r in rows_legacy:
        f = r.get("fields", {})
        key = _row_key(f)
        if not all(key) or key in have:
            continue
        fields = {name: bitable.get_field_text(f, name) for name in TEXT_FIELDS}
        fields.update({name: bitable.get_select_value(f, name) for name in SELECT_FIELDS})
        todo.append((key, fields))

    print("统一表（%s）行数=%d，旧表（%s）行数=%d，待补录=%d"
          % (unified, len(rows_unified), legacy, len(rows_legacy), len(todo)))
    for key, _ in todo[:20]:
        print("  补:", key)
    if len(todo) > 20:
        print("  ...（共 %d 行）" % len(todo))
    if not todo:
        return 0
    if not args.apply:
        print("干跑结束（未写入）。加 --apply 实际补录。")
        return 0

    ok = 0
    for i, (key, fields) in enumerate(todo, 1):
        if bitable.create_record(unified, fields):
            ok += 1
        else:
            print("  写入失败:", key)
        if i % 20 == 0:
            print("  ...已处理 %d/%d（成功 %d）" % (i, len(todo), ok), flush=True)
    print("补录完成：成功 %d / %d" % (ok, len(todo)))
    return 0 if ok == len(todo) else 1


if __name__ == "__main__":
    sys.exit(main())
