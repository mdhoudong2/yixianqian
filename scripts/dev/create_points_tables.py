#!/usr/bin/env python3
"""麦穗积分要用的多维表格改动（幂等，重复跑安全）。

做五件事：
  1. 用户表「账号状态」加上「封禁」选项 —— 只有这个新状态触发邀请奖励收回；
  2. 活动表「活动状态」加上「已取消」选项 —— 机器人扫到它才退优先名额 / 费用减免；
  3. 报名表加「签到状态」字段（按时/迟到/未到）—— 考勤，供退穗判定；
  4. 建「麦穗心愿单」表；
  5. 建「麦穗红娘推荐单」表。

用法（在仓库根目录）：
  python scripts/dev/create_points_tables.py check     # 只看看现在缺什么
  python scripts/dev/create_points_tables.py apply     # 真的改

跑完把打印出来的两个表 ID 填进服务器 local_config.py 的 WISH_TABLE_ID /
MATCHMAKER_ORDER_TABLE_ID，再重启机器人。**不填就是留空**：机器人会明确说
「这张表还没配置」，而不是往一个不存在的表里写。

为什么单写一个脚本而不是让代码自己建表：建表/改单选选项是不可逆的 schema
动作，飞书那边也没有事务。混在启动流程里，一次手滑就是一次线上事故。
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

APP_ID = getattr(_cfg, "FEISHU_APP_ID", None) or getattr(_cfg, "APP_ID", None)
APP_SECRET = getattr(_cfg, "FEISHU_APP_SECRET", None) or getattr(_cfg, "APP_SECRET", None)
BASE_TOKEN = _cfg.BASE_TOKEN
# 表 ID 一律从 local_config 取，**不给生产兜底值**：表 ID 是按 base 分配的，
# 拿着测试服的 token 配一个生产表 ID，轻则 404，重则改错环境。缺就报错退出。
USER_TABLE_ID = getattr(_cfg, "USER_TABLE_ID", "")
SIGNUP_TABLE_ID = getattr(_cfg, "SIGNUP_TABLE_ID", "")

BASE_URL = "https://open.feishu.cn/open-apis"
STATUS_FIELD = "账号状态"
STATUS_BANNED = "封禁"
ATTENDANCE_FIELD = "签到状态"
ATTENDANCE_OPTIONS = ["按时", "迟到", "未到"]
# 活动取消。机器人扫到这个状态就退该活动名下的优先名额 / 费用减免，
# 字面量必须与 bot/constants.py 的 ACTIVITY_STATUS_CANCELLED 一致。
ACTIVITY_TABLE_ID = getattr(_cfg, "ACTIVITY_TABLE_ID", "")
ACTIVITY_STATUS_FIELD = "活动状态"
ACTIVITY_STATUS_CANCELLED = "已取消"

# type: 1=文本 2=数字 3=单选 5=日期 1005=自动编号
WISH_TABLE_NAME = "麦穗心愿单"
WISH_FIELDS = [
    ("活动ID", 1, None),
    ("发起人open_id", 1, None),
    ("发起人昵称", 1, None),
    ("指定人open_id", 1, None),
    ("指定人昵称", 1, None),
    ("状态", 3, ["待安排", "已安排", "无法安排"]),
    ("兑换单号", 1, None),
    ("处理人", 1, None),
    ("处理时间", 1, None),
    ("创建时间", 1, None),
]

MATCHMAKER_TABLE_NAME = "麦穗红娘推荐单"
MATCHMAKER_FIELDS = [
    ("发起人open_id", 1, None),
    ("发起人昵称", 1, None),
    ("择偶条件", 1, None),
    ("状态", 3, ["待处理", "已推荐", "已完成", "无法推荐"]),
    ("兑换单号", 1, None),
    ("处理人", 1, None),
    ("处理时间", 1, None),
    ("处理说明", 1, None),
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


def _ensure_select_option(table_id, table_label, field_name, option_name,
                          apply_changes):
    """给某个单选字段补一个选项。已有选项原样保留——飞书这个接口是整体覆盖
    options 的，漏传一个就等于把那个状态删了，那一堆记录的状态会变成空。"""
    fields = _list_fields(table_id)
    target = next((f for f in fields if f.get("field_name") == field_name), None)
    if not target:
        print(f"❌ {table_label}没有「{field_name}」字段，先手工确认表对不对")
        return False
    options = (target.get("property") or {}).get("options") or []
    names = [o.get("name") for o in options]
    if option_name in names:
        print(f"✅ 「{field_name}」已有「{option_name}」选项：{names}")
        return True
    if not apply_changes:
        print(f"⬜ 「{field_name}」缺「{option_name}」选项，现有：{names}")
        return True

    # 保留原有 id，只追加新选项：飞书按 name 匹配，带 id 传回去等于「这个选项没动」。
    new_options = [{"id": o.get("id"), "name": o.get("name")} for o in options]
    new_options.append({"name": option_name})
    r = requests.put(
        f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/fields/"
        f"{target.get('field_id')}",
        headers=_h(),
        json={"field_name": field_name, "type": target.get("type"),
              "property": {"options": new_options}})
    data = r.json()
    if data.get("code") != 0:
        print(f"❌ 加选项失败：{json.dumps(data, ensure_ascii=False)}")
        return False
    print(f"✅ 「{field_name}」已加上「{option_name}」")
    return True


def _ensure_attendance_field(apply_changes):
    fields = _list_fields(SIGNUP_TABLE_ID)
    if any(f.get("field_name") == ATTENDANCE_FIELD for f in fields):
        print(f"✅ 报名表已有「{ATTENDANCE_FIELD}」字段")
        return True
    if not apply_changes:
        print(f"⬜ 报名表缺「{ATTENDANCE_FIELD}」字段")
        return True
    r = requests.post(
        f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{SIGNUP_TABLE_ID}/fields",
        headers=_h(),
        json={"field_name": ATTENDANCE_FIELD, "type": 3,
              "property": {"options": [{"name": n} for n in ATTENDANCE_OPTIONS]}})
    data = r.json()
    if data.get("code") != 0:
        print(f"❌ 建字段失败：{json.dumps(data, ensure_ascii=False)}")
        return False
    print(f"✅ 报名表已加「{ATTENDANCE_FIELD}」")
    return True


def _create_table(name, field_defs, apply_changes):
    existing = _find_table(name)
    if existing:
        print(f"✅ 「{name}」已存在：{existing}")
        return existing
    if not apply_changes:
        print(f"⬜ 「{name}」不存在")
        return ""
    # 只传 name：带上 default_view_name 会被飞书判成 WrongRequestBody(1254001)。
    r = requests.post(f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables", headers=_h(),
                      json={"table": {"name": name}})
    data = r.json()
    if data.get("code") != 0:
        print(f"❌ 建表失败 {name}：{json.dumps(data, ensure_ascii=False)}")
        return ""
    table_id = data["data"]["table_id"]
    print(f"✅ 已建表「{name}」：{table_id}")

    for fname, ftype, opts in field_defs:
        payload = {"field_name": fname, "type": ftype}
        if opts:
            payload["property"] = {"options": [{"name": o} for o in opts]}
        rr = requests.post(
            f"{BASE_URL}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/fields",
            headers=_h(), json=payload)
        rd = rr.json()
        if rd.get("code") != 0:
            print(f"   ❌ 字段「{fname}」建失败：{json.dumps(rd, ensure_ascii=False)}")
        else:
            print(f"   + {fname}")
    return table_id


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "check").lower()
    if mode not in ("check", "apply"):
        print(__doc__)
        sys.exit(1)
    apply_changes = mode == "apply"
    guard("create_points_tables.py")
    if not (USER_TABLE_ID and SIGNUP_TABLE_ID and ACTIVITY_TABLE_ID):
        print("⛔ local_config.py 里缺 USER_TABLE_ID / SIGNUP_TABLE_ID / "
              "ACTIVITY_TABLE_ID，先补上再跑。")
        sys.exit(1)

    print(f"环境：{os.environ.get('YIXIANQIAN_ENV', 'prod')}   模式：{mode}\n")
    ok = _ensure_select_option(USER_TABLE_ID, "用户表", STATUS_FIELD, STATUS_BANNED,
                               apply_changes)
    # 活动取消是「优先名额/费用减免自动退穗」的唯一触发点，没有这个选项那条规则
    # 就没有输入端（机器人按状态字面匹配）。
    ok = _ensure_select_option(ACTIVITY_TABLE_ID, "活动表", ACTIVITY_STATUS_FIELD,
                               ACTIVITY_STATUS_CANCELLED, apply_changes) and ok
    ok = _ensure_attendance_field(apply_changes) and ok
    wish_id = _create_table(WISH_TABLE_NAME, WISH_FIELDS, apply_changes)
    mm_id = _create_table(MATCHMAKER_TABLE_NAME, MATCHMAKER_FIELDS, apply_changes)

    print("\n" + "=" * 60)
    if not ok:
        print("有步骤失败了，先看上面的报错。")
    print("把下面两行填进服务器 local_config.py（没有就留空，功能会提示未配置）：")
    print(f'WISH_TABLE_ID = "{wish_id}"')
    print(f'MATCHMAKER_ORDER_TABLE_ID = "{mm_id}"')
    print("填完重启机器人：systemctl restart yixianqian")


if __name__ == "__main__":
    main()
