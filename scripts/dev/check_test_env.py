#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大规模分组测试环境健全性检查 (修正字段名，复用 bot 常量)。"""
import os, sys, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yixianqian_bot_ws as bot

BASE_TOKEN = bot.BASE_TOKEN
USER_TABLE = bot.USER_TABLE_ID
ACTIVITY_TABLE = bot.ACTIVITY_TABLE_ID
SIGNUP_TABLE = bot.SIGNUP_TABLE_ID
SELECT_TABLE = bot.GROUP_SELECT_TABLE
RESULT_TABLE = bot.GROUP_RESULT_TABLE

REAL_ACCOUNTS = {"U-0001","U-0002","U-0007","U-0003","U-0005","U-0006","U-0020"}

def fetch_all(table_id, page_size=500):
    import requests
    token = bot.get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    all_records, page_token = [], None
    while True:
        params = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records"
        r = requests.get(url, headers=headers, params=params, timeout=60)
        try:
            d = r.json()
        except Exception as e:
            print(f"  !! JSON解析失败 {table_id}: {r.status_code} {r.text[:200]}")
            return None
        if d.get("code") != 0:
            print(f"  !! 读取失败 {table_id}: code={d.get('code')} msg={d.get('msg')}")
            return None
        all_records.extend(d["data"].get("items") or [])  # 空表 items 可能为 null
        if d["data"].get("has_more"):
            page_token = d["data"].get("page_token")
        else:
            break
    return all_records

def fv(fields, key):
    v = fields.get(key)
    if isinstance(v, list):
        if not v:
            return ""
        if isinstance(v[0], dict):
            return v[0].get("text", "")
        return str(v[0])
    return v

def main():
    ok = True
    def check(cond, msg):
        nonlocal ok
        if not cond:
            ok = False
        print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")

    print("=" * 60)
    print("大规模分组测试环境健全性检查 (A-0005, 200人)")
    print("=" * 60)

    # 1. 活动 A-0005
    print("\n[1] 活动 A-0005")
    acts = fetch_all(ACTIVITY_TABLE)
    act = None
    for a in acts or []:
        nm = str(fv(a["fields"], bot.FIELD_ACTIVITY_NAME)).strip()
        # 活动ID 是 auto-number，通过名称反查
        if "大规模模拟" in nm:
            act = a
            break
    if act is None:
        check(False, "未找到 A-0005「测试活动-大规模模拟」")
    else:
        st = str(fv(act["fields"], bot.FIELD_ACTIVITY_STATUS)).strip()
        m_per = fv(act["fields"], bot.FIELD_ACT_MALE_PER_GROUP)
        f_per = fv(act["fields"], bot.FIELD_ACT_FEMALE_PER_GROUP)
        cur = fv(act["fields"], bot.FIELD_ACTIVITY_CURRENT_SIGNUP)
        check(True, f"找到活动, 活动状态={st}, 每组男生数={m_per}, 每组女生数={f_per}, 当前报名={cur}")
        # 状态应为「未开始」或「报名中」——开始填志愿前应是未开始/报名中皆可

    # 2. 假账号
    print("\n[2] 假测试账号 (用户表 ou_fake_*)")
    users = fetch_all(USER_TABLE)
    fake = [u for u in users or [] if "ou_fake_" in str(fv(u["fields"], bot.FIELD_FEISHU_ID))]
    check(len(fake) == 193, f"应有 193 个假账号，实际 {len(fake)}")
    male_fake = [u for u in fake if str(fv(u["fields"], bot.FIELD_GENDER)).strip()=="男性"]
    female_fake = [u for u in fake if str(fv(u["fields"], bot.FIELD_GENDER)).strip()=="女性"]
    check(male_fake and female_fake, f"假账号性别分布: 男={len(male_fake)}, 女={len(female_fake)}")

    # 3. 报名表 A-0005
    print("\n[3] A-0005 报名数据")
    signs = fetch_all(SIGNUP_TABLE)
    a5 = [s for s in signs or [] if str(fv(s["fields"], bot.FIELD_SIGNUP_ACTIVITY_ID)).find("0005")>=0]
    check(len(a5) == 200, f"A-0005 应报名 200 人，实际 {len(a5)}")
    s_male = [s for s in a5 if str(fv(s["fields"], "报名人性别")).strip()=="男性" or str(fv(s["fields"], bot.FIELD_SIGNUP_NICKNAME)).startswith("测试男") or str(fv(s["fields"], "报名人open_id")).startswith("ou_fake_male")]
    # 依据 open_id 前缀统计
    a5m = [s for s in a5 if str(fv(s["fields"], bot.FIELD_SIGNUP_OPENID)).startswith("ou_fake_male")]
    a5f = [s for s in a5 if str(fv(s["fields"], bot.FIELD_SIGNUP_OPENID)).startswith("ou_fake_female")]
    real_in_a5 = [s for s in a5 if str(fv(s["fields"], bot.FIELD_SIGNUP_OPENID)).startswith("ou_") and "ou_fake_" not in str(fv(s["fields"], bot.FIELD_SIGNUP_OPENID))]
    check(len(a5m)==97 and len(a5f)==96, f"报名中假账号: 男={len(a5m)}(应97), 女={len(a5f)}(应96)")
    check(len(real_in_a5)==7, f"报名中真实账号: {len(real_in_a5)} (应7)")

    # 4. 分组选择表 A-0005
    print("\n[4] A-0005 分组选择表")
    sels = fetch_all(SELECT_TABLE)
    a5sels = [s for s in sels or [] if str(fv(s["fields"], bot.FIELD_GS_ACTIVITY_ID)).find("0005")>=0]
    fake_sels = [s for s in a5sels if str(fv(s["fields"], bot.FIELD_GS_SELECTOR_OID)).startswith("ou_fake_")]
    real_sels = [s for s in a5sels if str(fv(s["fields"], bot.FIELD_GS_SELECTOR_OID)).startswith("ou_") and not str(fv(s["fields"], bot.FIELD_GS_SELECTOR_OID)).startswith("ou_fake_")]
    check(len(fake_sels)==193, f"假账号预填志愿应 193 条，实际 {len(fake_sels)}")
    check(len(real_sels)==0, f"真实账号不应预填（等 bot 提交），实际 {len(real_sels)}")
    if fake_sels:
        sample = fv(fake_sels[0]["fields"], bot.FIELD_GS_CHOICES[0])
        print(f"    (抽样一条假志愿: 第1志愿={sample})")

    # 5. 分组结果表 A-0005（应空）
    print("\n[5] A-0005 分组结果表")
    res = fetch_all(RESULT_TABLE)
    if res is None:
        check(False, "分组结果表读取失败")
    else:
        a5res = [r for r in res if str(fv(r["fields"], bot.FIELD_GR_ACTIVITY_ID)).find("0005")>=0]
        check(len(a5res)==0, f"A-0005 分组结果应为空，实际 {len(a5res)} 条")

    # 6. bot 守卫
    print("\n[6] bot 代码守卫生效")
    src_path = os.path.join(os.path.dirname(os.path.abspath(bot.__file__)), "yixianqian_bot_ws.py")
    src = open(src_path, encoding="utf-8").read()
    # send_text_message 前有 is_test_fake_openid 短路
    import re
    send_m = re.search(r"def send_text_message.*?(?=\ndef |\Z)", src, re.S)
    check(send_m is not None and "is_test_fake_openid" in send_m.group(0), "send_text_message 内含假号短路守卫生效")
    check("def is_test_fake_openid" in src, "is_test_fake_openid 函数存在")
    check("run_grouping_algorithm" in src and "def run_grouping_algorithm" in src, "分组算法存在")

    print("\n" + "=" * 60)
    print("结论: " + ("✅ 环境健全，可以发送「开始填志愿 A-0005 4 4」" if ok else "❌ 存在 FAIL，请先处理"))
    print("=" * 60)
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
