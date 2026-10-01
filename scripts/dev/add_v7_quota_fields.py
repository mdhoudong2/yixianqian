#!/usr/bin/env python3
"""月度额度（v7）上线要用的多维表格字段（幂等，重复跑安全）。

做三件事：
  1. 用户表加「实名总量」「实名剩余」两个数字字段 —— 机器人对账时写入；
  2. 喜欢表加「归属月份」文本字段 —— 受理时刻钉死，之后按它算月度实名次数；
  3. 喜欢表「状态」单选里把「已取消」改名为「被驳回」—— 机器人拦下的无效喜欢
     （重复 / 自喜欢 / 昵称撞车）写的字面量是「被驳回」（见 lib/quota.py），
     表里没有这个选项就会静默写不进去。

「邀请名额」字段**不加也不写**：v7 起邀请改发麦穗，不再发实名名额，代码里已经
不再读/写这个字段（见 bot/commands.py、bot/auto_tasks.py）。留着它反而会把
老数据当权威。

用法（在仓库根目录）：
  python scripts/dev/add_v7_quota_fields.py check     # 只看看现在缺什么
  python scripts/dev/add_v7_quota_fields.py apply     # 真的改

字段名/选项名必须与 bot/constants.py 与 lib/quota.py 逐字一致，所以这里全部从
constants / quota 取，不抄字面量——改个名两边对不上，是那种「写进去全是空、
但不报错」的坏法。
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, "bot"))
sys.path.insert(0, _ROOT)

import requests

import constants as _c
from _prod_guard import guard
from lib import quota

BASE_URL = "https://open.feishu.cn/open-apis"
BASE_TOKEN = _c.BASE_TOKEN
USER_TABLE_ID = _c.USER_TABLE_ID
LIKE_TABLE_ID = _c.LIKE_TABLE_ID

# type: 1=文本 2=数字 3=单选
NEW_NUMBER_FIELDS = [
    (_c.FIELD_REAL_TOTAL, "用户表", USER_TABLE_ID),
    (_c.FIELD_REAL_REMAIN, "用户表", USER_TABLE_ID),
]
NEW_TEXT_FIELDS = [
    (_c.FIELD_LIKE_MONTH, "喜欢表", LIKE_TABLE_ID),
]
# 状态改名：喜欢表「状态」里的 已取消 → 被驳回（保留原 id，历史记录跟着变）
RENAME = (LIKE_TABLE_ID, "喜欢表", _c.FIELD_LIKE_STATUS, "已取消", quota.LIKE_STATUS_REJECTED)


def _token():
    r = requests.post(f"{BASE_URL}/auth/v3/tenant_access_token/internal",
                      json={"app_id": _c.APP_ID, "app_secret": _c.APP_SECRET})
    data = r.json()
    if not data.get("tenant_access_token"):
        print("拿不到 tenant_access_token:", json.dumps(data, ensure_ascii=False))
        sys.exit(1)
    return data["tenant_access_token"]


def _h():
    return {"Authorization": "Bearer " + _token(), "Content-Type": "application/json"}


def _list_fields(table_id):
    r = requests.get(
        f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/fields?page_size=200",
        headers=_h())
    return r.json().get("data", {}).get("items", [])


def _ensure_field(table_id, table_label, field_name, ftype, apply_changes):
    fields = _list_fields(table_id)
    if any(f.get("field_name") == field_name for f in fields):
        print(f"✅ {table_label}已有「{field_name}」字段")
        return True
    if not apply_changes:
        print(f"⬜ {table_label}缺「{field_name}」字段（type={ftype}）")
        return True
    r = requests.post(
        f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/fields",
        headers=_h(), json={"field_name": field_name, "type": ftype})
    data = r.json()
    if data.get("code") != 0:
        print(f"❌ {table_label}建「{field_name}」失败：{json.dumps(data, ensure_ascii=False)}")
        return False
    print(f"✅ {table_label}已加「{field_name}」（type={ftype}）")
    return True


def _rename_option(table_id, table_label, field_name, old_name, new_name, apply_changes):
    """把单选字段里的 old_name 改名为 new_name，保留原 id。

    只改名、不新增：这些是同一件事（机器人拦下的无效喜欢），旧记录本来就该是
    新名字。找不到旧选项就停下来报错，而不是自作主张加一个「被驳回」——
    那样会把历史数据留在「已取消」里，对账口径直接裂开。
    """
    fields = _list_fields(table_id)
    target = next((f for f in fields if f.get("field_name") == field_name), None)
    if not target:
        print(f"❌ {table_label}没有「{field_name}」字段，先手工确认表对不对")
        return False
    options = (target.get("property") or {}).get("options") or []
    names = [o.get("name") for o in options]
    if new_name in names and old_name not in names:
        print(f"✅ 「{field_name}」已经是「{new_name}」：{names}")
        return True
    if old_name not in names:
        print(f"❌ 「{field_name}」里既没有「{old_name}」也没有「{new_name}」，"
              f"现有：{names}——先手工确认，别乱改")
        return False
    if new_name in names:
        print(f"❌ 「{field_name}」里「{old_name}」和「{new_name}」同时存在，"
              f"现有：{names}——需要手工合并，脚本不动")
        return False
    if not apply_changes:
        print(f"⬜ 「{field_name}」需把「{old_name}」改名为「{new_name}」：{names}")
        return True

    # 保留原 id，只改 name：飞书按 id 匹配，带 id 传回去等于「这个选项改名」，
    # 历史记录会跟着显示成新名字。
    new_options = [
        {"id": o.get("id"), "name": new_name if o.get("name") == old_name else o.get("name")}
        for o in options
    ]
    r = requests.put(
        f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/fields/"
        f"{target.get('field_id')}",
        headers=_h(),
        json={"field_name": field_name, "type": target.get("type"),
              "property": {"options": new_options}})
    data = r.json()
    if data.get("code") != 0:
        print(f"❌ 改名失败：{json.dumps(data, ensure_ascii=False)}")
        return False
    print(f"✅ 「{field_name}」的「{old_name}」已改名为「{new_name}」")
    return True


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "check").lower()
    if mode not in ("check", "apply"):
        print(__doc__)
        sys.exit(1)
    apply_changes = mode == "apply"
    guard("add_v7_quota_fields.py")

    print(f"环境：{os.environ.get('YIXIANQIAN_ENV', 'prod')}   模式：{mode}\n")
    ok = True
    for field_name, label, tid in NEW_NUMBER_FIELDS:
        ok = _ensure_field(tid, label, field_name, 2, apply_changes) and ok
    for field_name, label, tid in NEW_TEXT_FIELDS:
        ok = _ensure_field(tid, label, field_name, 1, apply_changes) and ok
    tid, label, field_name, old_name, new_name = RENAME
    ok = _rename_option(tid, label, field_name, old_name, new_name, apply_changes) and ok

    print("\n" + "=" * 60)
    if ok:
        print("v7 额度字段就绪。")
    else:
        print("有步骤失败，先看上面的报错。")


if __name__ == "__main__":
    main()
