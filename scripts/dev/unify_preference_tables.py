#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""志愿表统一迁移（2026-10-13）：一对一旧志愿表 → 统一志愿表（分组选择表）补录。

背景：志愿已统一为一张表（config.ONETOONE_SELECT_TABLE 已指向 GROUP_SELECT_TABLE），
但历史上「一对一旧志愿表」里可能还有统一表没有的（活动, 人）行（早前提交或种子）。
本脚本把缺失行按栏位逐字复制进统一表；已存在的以统一表为准（不覆盖、不删除）。
两表栏位逐字同构（活动ID/选择人open_id/选择人昵称/选择人性别/第1-7志愿）。

用法（仓库根，测试服）：
  YIXIANQIAN_ENV=dev web/backend/venv/bin/python scripts/dev/unify_preference_tables.py            # 干跑
  YIXIANQIAN_ENV=dev web/backend/venv/bin/python scripts/dev/unify_preference_tables.py --apply    # 写入
生产（发版时）需显式 YX_DEV_ALLOW=1 才允许执行。
"""

import argparse
import os
import sys

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写入（默认只干跑）")
    args = ap.parse_args()

    unified = config.GROUP_SELECT_TABLE
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
    for key, fields in todo:
        if bitable.create_record(unified, fields):
            ok += 1
        else:
            print("  写入失败:", key)
    print("补录完成：成功 %d / %d" % (ok, len(todo)))
    return 0 if ok == len(todo) else 1


if __name__ == "__main__":
    sys.exit(main())
