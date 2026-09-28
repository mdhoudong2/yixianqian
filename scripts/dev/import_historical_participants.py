#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导入「历史参与名单」——判断邀请对象算不算新人的依据。

需求：被邀请人必须是新人（不是 App 用户，**也没参加过以往活动**）。以往活动的
名单只在运营手里（表格/导出的 CSV），所以做成一次性导入，键是手机号。

用法：
    python scripts/dev/import_historical_participants.py 名单.csv [--dry-run] [--source 2023-2024]

CSV 要求：至少两列，第一列手机号、第二列姓名；有表头就加 --header。
分隔符逗号或制表符都认（从 Excel 里直接复制出来的是制表符）。

幂等：同一个手机号重复导入只算一次，可以放心重跑。
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from _prod_guard import guard  # noqa: E402

guard(os.path.basename(__file__))

from lib import points_db, points_invite  # noqa: E402


def read_rows(path, has_header):
    """读 CSV。分隔符按第一行里逗号和制表符谁多用谁——Excel 复制出来的
    是制表符，导出的是逗号，让脚本自己认，别逼人手改文件。"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        sample = f.readline()
        f.seek(0)
        delim = "\t" if sample.count("\t") > sample.count(",") else ","
        reader = csv.reader(f, delimiter=delim)
        if has_header:
            next(reader, None)
        for row in reader:
            if len(row) < 2:
                continue
            yield row[0].strip(), row[1].strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--header", action="store_true", help="第一行是表头，跳过")
    ap.add_argument("--source", default="import", help="来源标记，便于事后追溯")
    ap.add_argument("--dry-run", action="store_true", help="只看会导入多少，不写库")
    args = ap.parse_args()

    rows = list(read_rows(args.csv_path, args.header))
    if not rows:
        print("CSV 里没读到任何 (手机号, 姓名) 行")
        return 1

    if args.dry_run:
        ok = sum(1 for p, _ in rows if points_invite.normalize_phone(p))
        print(f"共 {len(rows)} 行，其中手机号合法 {ok} 行，非法 {len(rows) - ok} 行（非法行会被跳过）")
        print("--dry-run：没有写库")
        return 0

    points_db.migrate()
    added, skipped = points_invite.add_historical_participants(rows, source=args.source)
    print(f"导入完成：新增 {added} 条，跳过 {skipped} 条（号码非法或已存在）")
    print(f"名单现有 {points_invite.historical_count()} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
