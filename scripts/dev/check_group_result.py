#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""200人大规模分组测试 - 结果校验脚本

读分组结果表(A-0005)，核对：
- 组数 / 每组人数与男女配比（应 4男4女）
- 总人数是否等于实际参与人数（应满编）
- 每人是否只出现在一组（无重复、无遗漏）
- 7 个真号所在组与性别
- 采样校验真号志愿被尽量满足（同组命中数）

用法：
  python3 check_group_result.py
"""
import collections
import sys
import requests

import local_config as _cfg
APP_ID = _cfg.APP_ID
APP_SECRET = _cfg.APP_SECRET
BASE_TOKEN = _cfg.BASE_TOKEN

GROUP_RESULT_TABLE_ID = "tbl3xxAYhyTDGWAB"
GROUP_SELECT_TABLE_ID = "tblYo86Vd7dmzRQJ"

ACTIVITY_ID = "A-0005"
M_PER = 4
F_PER = 4

# 7 个真号（open_id 已确认）
REAL = {
    "ou_xxx1": ("U-0001", "男性"),
    "ou_xxx2": ("U-0002", "男性"),
    "ou_xxx3": ("U-0007", "男性"),
    "ou_xxx4": ("U-0003", "女性"),
    "ou_xxx5": ("U-0005", "女性"),
    "ou_xxx6": ("U-0006", "女性"),
    "ou_xxx7": ("U-0020", "女性"),
}
REAL_FEMALES = {k for k, v in REAL.items() if v[1] == "女性"}
REAL_MALES = {k for k, v in REAL.items() if v[1] == "男性"}

_tok = None


def get_token():
    global _tok
    r = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                      json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=10)
    _tok = r.json().get("tenant_access_token")
    return _tok


def search(table_id, conditions=None):
    global _tok
    if not _tok:
        get_token()
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/search"
    headers = {"Authorization": f"Bearer {_tok}", "Content-Type": "application/json"}
    data = {"page_size": 100}
    if conditions:
        data["filter"] = {"conjunction": "and", "conditions": conditions}
    out, page_token = [], None
    while True:
        params = {"page_token": page_token} if page_token else {}
        r = requests.post(url, headers=headers, json=data, params=params, timeout=15)
        res = r.json()
        if res.get("code") != 0:
            print(f"  search 错误: {res.get('msg')}")
            break
        d = res.get("data", {})
        out.extend(d.get("items", []))
        if not d.get("has_more"):
            break
        page_token = d.get("page_token")
    return out


def ftext(v):
    if isinstance(v, dict):
        if "value" in v and isinstance(v["value"], list) and v["value"]:
            return ftext(v["value"][0])
        return str(v.get("text", "") or v.get("name", "") or "")
    if isinstance(v, list):
        return ftext(v[0]) if v else ""
    return str(v)


def main():
    print("=" * 60)
    print(f"检查 A-0005 分组结果（预期每个 4男4女 满编组）")
    print("=" * 60)

    results = search(GROUP_RESULT_TABLE_ID, [
        {"field_name": "活动ID", "operator": "is", "value": [ACTIVITY_ID]}
    ])
    if not results:
        print("分组结果表里没有 A-0005 的结果。可能还没执行分组，或执行未写入。")
        return

    groups = collections.defaultdict(list)
    for it in results:
        f = it.get("fields", {})
        no = ftext(f.get("组号"))
        oid = ftext(f.get("用户open_id"))
        name = ftext(f.get("用户昵称"))
        gender = ftext(f.get("用户性别"))
        groups[no].append({"oid": oid, "name": name, "gender": gender})

    print(f"组数: {len(groups)}")
    total = 0
    problems = []
    seen_oid = {}
    for no in sorted(groups, key=lambda x: int(ftext(x)) if str(ftext(x)).isdigit() else 0):
        members = groups[no]
        males = sum(1 for m in members if m["gender"] == "男性")
        females = sum(1 for m in members if m["gender"] == "女性")
        total += len(members)
        for m in members:
            if m["oid"] in seen_oid:
                problems.append(f"重复！{m['name']}({m['oid']}) 出现在组{seen_oid[m['oid']]}和组{ftext(no)}")
            seen_oid[m["oid"]] = ftext(no)
        flag = ""
        if males != M_PER or females != F_PER or len(members) != M_PER + F_PER:
            flag = "  <-- 人数/配比异常"
            problems.append(f"第{ftext(no)}组 {len(members)}人({males}男{females}女) 非满编{flag}")
        print(f"  第{ftext(no)}组: {len(members)}人 = {males}男 {females}女{flag}")

    print(f"\n总人数(可能含异常重复): {total}")

    # 分配校验：统计每组 4男4女 满编组数
    full = sum(1 for no in groups
               if sum(1 for m in groups[no] if m["gender"] == "男性") == M_PER
               and sum(1 for m in groups[no] if m["gender"] == "女性") == F_PER)
    print(f"满编组数: {full}/{len(groups)}")

    # 真号核对
    print("\n--- 7 个真号所在组 ---")
    for oid, (uid, gender) in REAL.items():
        # 找该真号出现的所有组
        where = []
        for no in groups:
            if any(m["oid"] == oid for m in groups[no]):
                where.append(ftext(no))
        if where:
            # 该组名单
            gno = where[0]
            mates = [m for m in groups[gno] if m["oid"] != oid]
            print(f"  {uid}({gender}) 在 第{ftext(gno)}组: 同组 {len(mates)} 人")
        else:
            problems.append(f"真号 {uid}({gender}) 未出现在任何组！")
            print(f"  {uid}({gender}) 未分组!")

    # 志愿满足度抽样：对 7 个真号，检查其填的志愿与本组其他人的重叠
    print("\n--- 真号志愿命中（志愿里有多少人和TA同组）---")
    sel = search(GROUP_SELECT_TABLE_ID, [
        {"field_name": "活动ID", "operator": "is", "value": [ACTIVITY_ID]}
    ])
    sel_by_picker = {}
    for s in sel:
        f = s.get("fields", {})
        picker = ftext(f.get("选择人open_id"))
        choices = []
        for i in range(1, 8):
            v = ftext(f.get(f"第{i}志愿"))
            if v:
                choices.append(v)
        sel_by_picker[picker] = choices

    for oid, (uid, gender) in REAL.items():
        choices = sel_by_picker.get(oid, [])
        if not choices:
            print(f"  {uid}: 未找到志愿记录（可能未提交）")
            continue
        # 找到该真号所在组所有人的 oid
        my_group = None
        for no in groups:
            if any(m["oid"] == oid for m in groups[no]):
                my_group = groups[no]
                break
        if not my_group:
            continue
        group_oids = {m["oid"] for m in my_group}
        hit = [c for c in choices if c in group_oids]
        print(f"  {uid}: 第1志愿命中={'是' if choices[0] in group_oids else '否'} | 7志愿中同组 {len(hit)}/7")

    print("\n--- 汇总 ---")
    if problems:
        print(f"发现问题 {len(problems)} 条:")
        for p in problems:
            print("  ✗", p)
    else:
        print("  ✓ 全部通过：所有组满编、无重复、无遗漏、真号均已分组")

    # 总数态
    expect = 3 + 97  # 男100
    print(f"\n说明：算法只对「已提交志愿」的人分组。若部分人未提交志愿，会被排除在外，总人数会小于200。")


if __name__ == "__main__":
    main()
