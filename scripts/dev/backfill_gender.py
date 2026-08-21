#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回填性别数据。

背景：产品把用户表「性别」从公式字段改成了手动单选字段，改完之后：
- 单选选项现在是 ['男','女']（但全代码用 男性/女性）
- 原公式派生值全部丢失 → 209 个用户性别全空

本脚本：
1) 把「性别」单选字段选项改成 ['男性','女性']（匹配代码约定）
2) 用「身份证号」第17位数字奇偶回填全部用户的性别（奇→男性, 偶→女性）
   —— 与旧公式 IF(id="","",LIST("女性","男性").NTH(MID(id,17,1)%2+1)) 逻辑一致
只回填「性别」为空或值不在 男性/女性 里的用户；已有值的跳过。
"""
import os, sys, time, json, requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yixianqian_bot_ws as bot

BASE_TOKEN = bot.BASE_TOKEN
USER_TABLE = bot.USER_TABLE_ID
FIELD_GENDER = bot.FIELD_GENDER        # "性别"
FIELD_ID_CARD = "身份证号"
FIELD_FEISHU = bot.FIELD_FEISHU_ID     # "飞书用户ID"
GENDER_FIELD_ID = "fldWWeeBMG"         # 性别字段 id

def get_token():
    return bot.get_tenant_access_token()

def fetch_all_records():
    token = get_token(); h = {"Authorization": f"Bearer {token}"}
    out, pt = [], None
    while True:
        p = {"page_size": 500}
        if pt: p["page_token"] = pt
        d = requests.get(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{USER_TABLE}/records",
            headers=h, params=p, timeout=60).json()
        if d.get("code") != 0:
            print(f"读取失败: {d.get('msg')}"); return []
        out.extend(d["data"]["items"])
        if d["data"].get("has_more"): pt = d["data"].get("page_token")
        else: break
    return out

def ftext(v):
    if isinstance(v, list):
        return v[0].get("text") if v and isinstance(v[0], dict) else (v[0] if v else "")
    if isinstance(v, dict): return v.get("text", v.get("name", ""))
    return v

def update_field_options():
    """把「性别」单选字段选项改为 [男性,女性]。"""
    token = get_token()
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{USER_TABLE}/fields/{GENDER_FIELD_ID}"
    body = {"field_name": FIELD_GENDER, "type": 3, "property": {"options": [{"name": "男性", "color": 0}, {"name": "女性", "color": 1}]}}
    d = requests.put(url, headers=h, json=body, timeout=30).json()
    if d.get("code") == 0:
        print("✔ 性别字段选项已改为 [男性,女性]")
        return True
    print(f"✘ 更新性别选项失败: code={d.get('code')} msg={d.get('msg')}")
    return False

def derive_gender(idc):
    """身份证第17位(0-based 16)奇→男性, 偶→女性；非法返回 None。"""
    s = str(idc)
    if len(s) < 18:
        return None
    c = s[16]
    if not c.isdigit():
        return None
    return "男性" if int(c) % 2 == 1 else "女性"

def update_record(rid, fields):
    token = get_token()
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{USER_TABLE}/records/{rid}"
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    d = requests.put(url, headers=h, json={"fields": fields}, timeout=30).json()
    return d.get("code") == 0

def main():
    print("=" * 50)
    print("性别数据回填")
    print("=" * 50)

    # 1. 改选项
    if not update_field_options():
        print("终止：性别选项未改成功，先人工处理。")
        return 1

    time.sleep(1)

    # 2. 回填
    records = fetch_all_records()
    total = len(records)
    filled = 0; skipped = 0; already = 0; noid = 0; fail = 0
    for it in records:
        f = it["fields"]
        rid = it["record_id"]
        cur = ftext(f.get(FIELD_GENDER))
        if cur in ("男性", "女性"):
            already += 1
            continue
        idc = ftext(f.get(FIELD_ID_CARD))
        g = derive_gender(idc) if idc else None
        if g is None:
            noid += 1
            continue
        if update_record(rid, {FIELD_GENDER: g}):
            filled += 1
        else:
            fail += 1
            print(f"  更新失败: {it.get('record_id')}")
        if filled % 40 == 0:
            print(f"  ...已回填 {filled} / {total}")
        time.sleep(0.03)
    print(f"总用户={total}  已回填={filled}  跳过(已有值)={already}  无有效身份证={noid}  更新失败={fail}")

    # 3. 抽样验证
    time.sleep(2)
    token = get_token(); h = {"Authorization": f"Bearer {token}"}
    sample_oids = ["ou_fake_male_001", "ou_fake_female_001", "ou_xxx_admin"]
    for oid in sample_oids:
        d = requests.post(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{USER_TABLE}/records/search",
            headers={**h, "Content-Type": "application/json"},
            json={"filter": {"conjunction": "and", "conditions": [{"field_name": FIELD_FEISHU, "operator": "is", "value": [oid]}]}, "page_size": 1},
            timeout=60).json()
        items = (d.get("data", {}).get("items") or [])
        if items:
            g = ftext(items[0]["fields"].get(FIELD_GENDER))
            print(f"  抽验 {oid} -> 性别={g}")
    return 0 if fail == 0 and noid == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
