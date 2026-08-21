#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复假测试账号的性别为空问题。

根因：用户表「性别」是公式字段，公式依赖「身份证号」字段的第17位数字奇偶：
    IF(身份证号="","",LIST("女性","男性").NTH(MID(身份证号,17,1)%2+1))
setup_test_200.py 创建假用户时没有写身份证号，导致性别公式算出空值，
193 个假用户全都没有性别。而分组算法 run_grouping_algorithm 依赖性别，
会导致假用户全部漏分、无法形成满编组。

修复：给 193 个假用户补身份证号，使其第17位数字为奇数(男)/偶数(女)。
"""
import os, sys, time, random, requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yixianqian_bot_ws as bot

BASE_TOKEN = bot.BASE_TOKEN
USER_TABLE = bot.USER_TABLE_ID
FIELD_ID_CARD = "身份证号"
FIELD_FEISHU = bot.FIELD_FEISHU_ID  # 飞书用户ID

def get_token():
    return bot.get_tenant_access_token()

def fetch_fake_users():
    """取所有 ou_fake_* 假用户（含 record_id + open_id）。"""
    token = get_token()
    headers = {"Authorization": f"Bearer {token}"}
    all_records, page_token = [], None
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{USER_TABLE}/records"
        d = requests.get(url, headers=headers, params=params, timeout=60).json()
        if d.get("code") != 0:
            print(f"读取用户表失败: {d.get('msg')}")
            return []
        all_records.extend(d["data"]["items"])
        if d["data"].get("has_more"):
            page_token = d["data"].get("page_token")
        else:
            break
    out = []
    for it in all_records:
        f = it["fields"]
        oid_raw = f.get(FIELD_FEISHU)
        oid = oid_raw[0]["text"] if isinstance(oid_raw, list) else oid_raw
        if oid and str(oid).startswith("ou_fake_"):
            out.append({"record_id": it["record_id"], "open_id": str(oid)})
    return out

def gen_id_card(gender, uid_num):
    """生成18位测试身份证号，使第17位数字为 奇数(男)/偶数(女)，触发性别公式。
    前16位：地区6位 + 生日8位 + 顺序码前2位；第17位按性别；第18位校验位固定。
    """
    region = "440000"  # 广东（纯测试前缀，非真实号码）
    # 8位生日，测试用固定范围递增，保证在99年后不重复
    birth = f"{19900101 + uid_num}"[:8]
    # 2位顺序码前段（仅用于凑足位数）
    order16 = f"{uid_num % 100:02d}"
    front16 = region + birth + order16
    assert len(front16) == 16, f"前16位长度异常: {len(front16)}"
    # 第17位：奇数=男性 (3), 偶数=女性 (2)
    digit17 = "3" if gender == "男性" else "2"
    check = "1"  # 校验位（飞书不校验，仅占位）
    id18 = front16 + digit17 + check
    assert len(id18) == 18, f"身份证号长度异常: {len(id18)}"
    return id18

def update_record(table_id, record_id, fields):
    tok = get_token()
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/{record_id}"
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    r = requests.put(url, headers=headers, json={"fields": fields}, timeout=20)
    return r.json().get("code") == 0

def main():
    fake = fetch_fake_users()
    print(f"找到假用户 {len(fake)} 个")
    if not fake:
        print("未找到假用户，检查 open_id 字段")
        return 1

    # 确认性别公式在 update 身份证后是否生效
    male_oid, female_oid = [], []
    for u in fake:
        if u["open_id"].startswith("ou_fake_male"):
            male_oid.append(u)
        else:
            female_oid.append(u)
    print(f"  男假号 {len(male_oid)}，女假号 {len(female_oid)}")

    ok = 0
    for i, u in enumerate(fake, 1):
        gender = "男性" if u["open_id"].startswith("ou_fake_male") else "女性"
        idc = gen_id_card(gender, i)
        if update_record(USER_TABLE, u["record_id"], {FIELD_ID_CARD: idc}):
            ok += 1
        if i % 40 == 0:
            print(f"  ...已更新 {i}/{len(fake)}")
        time.sleep(0.03)
    print(f"更新完成: {ok}/{len(fake)}")

    # 抽样验证性别是否自动算出来
    time.sleep(2)
    token = get_token()
    headers = {"Authorization": f"Bearer {token}"}
    for oid in ["ou_fake_male_001", "ou_fake_female_001"]:
        d = requests.post(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{USER_TABLE}/records/search",
            headers={**headers, "Content-Type": "application/json"},
            json={"filter": {"conjunction": "and", "conditions": [{"field_name": FIELD_FEISHU, "operator": "is", "value": [oid]}]}, "page_size": 1},
            timeout=60,
        ).json()
        items = d.get("data", {}).get("items") or []
        if items:
            f = items[0]["fields"]
            g_raw = f.get("性别")
            g = g_raw[0]["text"] if isinstance(g_raw, list) else g_raw
            idc_raw = f.get(FIELD_ID_CARD)
            idc = idc_raw[0]["text"] if isinstance(idc_raw, list) else idc_raw
            print(f"  {oid}: 身份证={idc} -> 性别={g}")
    return 0 if ok == len(fake) else 1

if __name__ == "__main__":
    sys.exit(main())
