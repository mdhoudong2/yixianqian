#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性清理：测试 base 上 E2E 留言污染（内容以「E2E一对一已聊测试」开头）。

背景：scripts/dev/e2e_onetoone_h5.py 早期版本创建的测试留言没有清理，它们会被
`/result` 的 chatted 推导当成「已聊」命中，使验收现场出现假阳性。e2e 脚本已加
自清理（只删自己创建的），本脚本清历史残留。

只允许在 /opt/yixianqian-test 且 YIXIANQIAN_ENV=dev 下运行。默认 dry-run（只列不删），
确认后加 --apply。删除口径与 App 的 DELETE /api/messages 一致：状态 →「已删除」
（软删除，留言列表与 chatted 推导都不再可见）；--hard 则物理删除记录（不可恢复）。

用法:
    YIXIANQIAN_ENV=dev python3 scripts/dev/cleanup_e2e_onetoone_messages.py          # 预览
    YIXIANQIAN_ENV=dev python3 scripts/dev/cleanup_e2e_onetoone_messages.py --apply  # 软删除
    YIXIANQIAN_ENV=dev python3 scripts/dev/cleanup_e2e_onetoone_messages.py --apply --hard
"""
import os
import sys

_D = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_D))
_EXPECTED_ROOT = "/opt/yixianqian-test"
if os.path.realpath(_ROOT) != _EXPECTED_ROOT:
    print(f"⛔ 仓库根必须是 {_EXPECTED_ROOT}，实际 {os.path.realpath(_ROOT)}")
    sys.exit(1)
if os.environ.get("YIXIANQIAN_ENV", "prod") != "dev":
    print("⛔ 必须 YIXIANQIAN_ENV=dev（本脚本只清测试服数据）")
    sys.exit(1)

for _p in (os.path.join(_ROOT, "web", "backend"), _ROOT):
    sys.path.insert(0, _p)

import config as CFG  # noqa: E402
import app as h5_mod  # noqa: E402

PREFIX = "E2E一对一已聊测试"


def main(argv):
    apply = "--apply" in argv
    hard = "--hard" in argv
    if hard and not apply:
        print("⛔ --hard 必须与 --apply 一起用（避免误删）")
        return 1

    bitable = h5_mod.bitable
    rows = bitable._client.search_records(CFG.MESSAGE_TABLE_ID)
    if rows is None:
        print("⛔ 读取留言表失败，未做任何改动")
        return 1

    hits = []
    for r in rows:
        f = r.get("fields", {})
        content = bitable.get_field_text(f, CFG.F_MSG_CONTENT)
        if content.startswith(PREFIX):
            hits.append(r)

    already = [r for r in hits
               if bitable.get_select_value(r.get("fields", {}), CFG.F_MSG_STATUS) == "已删除"]
    deleted_ids = {r.get("record_id") for r in already}
    live = [r for r in hits if r.get("record_id") not in deleted_ids]
    print(f"扫描留言 {len(rows)} 条；前缀「{PREFIX}」命中 {len(hits)} 条"
          f"（已删除 {len(already)} 条，待处理 {len(live)} 条）")

    if not live:
        print("✅ 无待处理留言，无需清理")
        return 0
    if not apply:
        for r in live:
            f = r.get("fields", {})
            print(f"  [DRY] {r.get('record_id')}  "
                  f"{bitable.get_field_text(f, CFG.F_MSG_AUTHOR_NICKNAME) or '-'} → "
                  f"{bitable.get_field_text(f, CFG.F_MSG_CONTENT)[:40]}")
        print("以上为预览；确认后加 --apply 执行（默认软删除，--hard 物理删除）")
        return 0

    ok = 0
    for r in live:
        rid = r.get("record_id")
        if hard:
            done = bool(bitable._client.delete_record(CFG.MESSAGE_TABLE_ID, rid))
        else:
            done = bool(bitable.update_record(CFG.MESSAGE_TABLE_ID, rid,
                                              {CFG.F_MSG_STATUS: "已删除"}))
        if done:
            ok += 1
        else:
            print(f"  ⛔ 处理失败：{rid}")
    action = "物理删除" if hard else "软删除（状态→已删除）"
    print(f"✅ 已处理 {ok}/{len(live)} 条（{action}）")
    return 0 if ok == len(live) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
