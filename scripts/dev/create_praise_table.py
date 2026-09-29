#!/usr/bin/env python3
"""点赞要用的多维表格（幂等，重复跑安全）。

只做一件事：建「点赞表」。点赞不进 SQLite——它不是账本，`lib/points_db.py`
开头写死了「只把积分账本放进 SQLite」，而管理员需要能直接查/改这张表。

用法（在仓库根目录）：
  python scripts/dev/create_praise_table.py check     # 只看看现在缺什么
  python scripts/dev/create_praise_table.py apply     # 真的建

跑完把打印出来的表 ID 填进服务器 local_config.py 的 PRAISE_TABLE_ID（bot 与
web/backend 各一份），再重启机器人 + H5。**不填就是留空**：功能会明确说
「这张表还没配置」，而不是往一个不存在的表里写。

## 字段为什么长这样

「归属日期」「归属周」是文本字段，**受理点赞那一刻写死**，之后所有统计都按它们数。
喜欢表踩过这个坑：`创建时间` 是飞书自动字段，既不稳定也不好过滤，所以那边专门
有「归属月份」。点赞照抄这个口径。

「点赞对象类型」现在只有「用户资料」一个选项。以后要赞问答内容时加一个选项，
把内容 ID 写进「点赞对象ID」即可，统计口径不用动。

字段名与选项名必须与 `bot/constants.py` / `web/backend/config.py` / `lib/praise.py`
逐字一致，所以这里的状态和对象类型直接从 `lib.praise` 取，不抄字面量——
改个名两边对不上，是那种「写进去全是空、但不报错」的坏法。
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# local_config.py 放在 bot/ 且被 gitignore（机器人以 bot/ 为工作目录跑）。
# 从仓库根目录直接跑这个脚本时，bot/ 不在 sys.path 上，得自己补。
sys.path.insert(0, os.path.join(_ROOT, "bot"))
sys.path.insert(0, _ROOT)

import local_config as _cfg
import requests
from _prod_guard import guard

from lib.praise import (
    OBJECT_USER_PROFILE,
    PRAISE_OBJECT_TYPES,
    PRAISE_STATUS_ACTIVE,
    PRAISE_STATUS_CANCELLED,
    PRAISE_STATUSES_ACTIVE,
)

APP_ID = getattr(_cfg, "FEISHU_APP_ID", None) or getattr(_cfg, "APP_ID", None)
APP_SECRET = getattr(_cfg, "FEISHU_APP_SECRET", None) or getattr(_cfg, "APP_SECRET", None)
BASE_TOKEN = _cfg.BASE_TOKEN

BASE_URL = "https://open.feishu.cn/open-apis"

# type: 1=文本 2=数字 3=单选 5=日期 1005=自动编号
TABLE_NAME = "点赞表"
# 状态选项按「有效在前」排列：表里按状态筛选时，第一个就是最常用的那个。
_STATUS_OPTIONS = list(PRAISE_STATUSES_ACTIVE) + [PRAISE_STATUS_CANCELLED]
_OBJECT_OPTIONS = list(PRAISE_OBJECT_TYPES)
assert _STATUS_OPTIONS[0] == PRAISE_STATUS_ACTIVE        # 顺序变了要不报错地放行
assert _OBJECT_OPTIONS[0] == OBJECT_USER_PROFILE         # 新类型加在后面，不动第一个

FIELDS = [
    # 点赞人。性别是**快照**：统计要按男女分开，而被点赞人过几天可能把性别改了，
    # 回头补算就会把历史数据算进另一个桶里。
    ("点赞人open_id", 1, None),
    ("点赞人昵称", 1, None),
    ("点赞人性别", 1, None),
    # 被点赞人。周汇总按「被点赞人open_id」分组，这条不能少。
    ("被点赞人open_id", 1, None),
    ("被点赞人昵称", 1, None),
    ("被点赞人性别", 1, None),
    # 取消 = 改成「已取消」，不删记录。删了就查不出「点过又反悔」这件事。
    ("状态", 3, _STATUS_OPTIONS),
    ("点赞对象类型", 3, _OBJECT_OPTIONS),
    ("点赞对象ID", 1, None),
    # 受理那一刻钉死，之后统计只认这两个字段，不回头读创建时间。
    ("归属日期", 1, None),          # YYYY-MM-DD，每日 10 次的上限按它数
    ("归属周", 1, None),            # YYYY-Www（ISO），每周汇总按它数
    ("创建时间", 1, None),
]


def _token():
    r = requests.post(f"{BASE_URL}/auth/v3/tenant_access_token/internal",
                      json={"app_id": APP_ID, "app_secret": APP_SECRET})
    data = r.json()
    if not data.get("tenant_access_token"):
        print("拿不到 tenant_access_token:", json.dumps(data, ensure_ascii=False))
        sys.exit(1)
    return data["tenant_access_token"]


def _h():
    return {"Authorization": "Bearer " + _token(), "Content-Type": "application/json"}


def _list_tables():
    r = requests.get(f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables?page_size=200",
                     headers=_h())
    return r.json().get("data", {}).get("items", [])


def _list_fields(table_id):
    r = requests.get(
        f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/fields?page_size=200",
        headers=_h())
    return r.json().get("data", {}).get("items", [])


def _find_table(name):
    for t in _list_tables():
        if t.get("name") == name:
            return t.get("table_id")
    return ""


def _add_field(table_id, fname, ftype, opts, apply_changes):
    payload = {"field_name": fname, "type": ftype}
    if opts:
        payload["property"] = {"options": [{"name": o} for o in opts]}
    if not apply_changes:
        print(f"   ⬜ 缺字段「{fname}」")
        return True
    rd = requests.post(
        f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/fields",
        headers=_h(), json=payload).json()
    if rd.get("code") != 0:
        print(f"   ❌ 字段「{fname}」建失败：{json.dumps(rd, ensure_ascii=False)}")
        return False
    print(f"   + {fname}")
    return True


def _ensure_fields(table_id, apply_changes):
    """把缺的字段补上。

    已存在的表也要走这一步，而不是建完就走：以后加「点赞对象类型」的新用法
    可能要补字段，重跑一次脚本就该补齐，不该逼人去手工点。
    """
    have = {f.get("field_name") for f in _list_fields(table_id)}
    missing = [(n, t, o) for n, t, o in FIELDS if n not in have]
    if not missing:
        print(f"✅ 「{TABLE_NAME}」字段齐全（{len(FIELDS)} 个）")
        return True
    ok = True
    for fname, ftype, opts in missing:
        ok = _add_field(table_id, fname, ftype, opts, apply_changes) and ok
    return ok


def _ensure_table(apply_changes):
    existing = _find_table(TABLE_NAME)
    if existing:
        print(f"✅ 「{TABLE_NAME}」已存在：{existing}")
        _ensure_fields(existing, apply_changes)
        return existing
    if not apply_changes:
        print(f"⬜ 「{TABLE_NAME}」不存在")
        return ""
    # 只传 name：带上 default_view_name 会被飞书判成 WrongRequestBody(1254001)。
    data = requests.post(f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables", headers=_h(),
                         json={"table": {"name": TABLE_NAME}}).json()
    if data.get("code") != 0:
        print(f"❌ 建表失败：{json.dumps(data, ensure_ascii=False)}")
        return ""
    table_id = data["data"]["table_id"]
    print(f"✅ 已建表「{TABLE_NAME}」：{table_id}")
    _ensure_fields(table_id, apply_changes)
    return table_id


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "check").lower()
    if mode not in ("check", "apply"):
        print(__doc__)
        sys.exit(1)
    apply_changes = mode == "apply"
    guard("create_praise_table.py")

    print(f"环境：{os.environ.get('YIXIANQIAN_ENV', 'prod')}   模式：{mode}\n")
    table_id = _ensure_table(apply_changes)

    print("\n" + "=" * 60)
    print("把下面这行填进服务器 local_config.py（bot/ 与 web/backend/ 各一份）"
          "，没有就留空——功能会提示「还没配置」，不会硬写：")
    print(f'PRAISE_TABLE_ID = "{table_id}"')
    print("填完重启：systemctl restart yixianqian yixianqian-h5")
    if not table_id and not apply_changes:
        print("（check 模式本来就不建表，加 apply 才真建。）")


if __name__ == "__main__":
    main()
