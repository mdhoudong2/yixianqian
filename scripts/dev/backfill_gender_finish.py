#!/usr/bin/env python3
#!/usr/bin/env python3
import os, sys
_D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _D)
sys.path.insert(0, os.path.dirname(_D))
from _prod_guard import guard
guard(os.path.basename(__file__))
# -*- coding: utf-8 -*-
"""继续/复验性别回填。

backfill_gender.py 因 300s 超时在 160/209 处被截断，剩余用户性别可能仍为空。
本脚本：
1) 读取全部用户，统计「性别」为空 / 已回填 的情况（用男性/女性约定判断）
2) 只对仍缺失的用户用身份证号第17位奇偶回填（奇→男性，偶→女性）
3) 抽样验证
幂等：已有有效性别的用户会被跳过。
"""
import os, sys, time, requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yixianqian_bot_ws as bot

BASE_TOKEN = bot.BASE_TOKEN
USER_TABLE = bot.USER_TABLE_ID
FIELD_GENDER = bot.FIELD_GENDER        # "性别"
FIELD_ID_CARD = "身份证号"
FIELD_FEISHU = bot.FIELD_FEISHU_ID     # "飞书用户ID"
VALID = ("男性", "女性")

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

def derive_gender(idc):
    s = str(idc)
    if len(s) < 18: return None
    c = s[16]
    if not c.isdigit(): return None
    return "男性" if int(c) % 2 == 1 else "女性"

def main():
    token = get_token()
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    records = fetch_all_records()
    total = len(records)

    to_fill = []
    for it in records:
        f = it["fields"]
        cur = ftext(f.get(FIELD_GENDER))
        if cur in VALID:
            continue
        idc = ftext(f.get(FIELD_ID_CARD))
        g = derive_gender(idc) if idc else None
        if g is None:
            print(f"  [跳过-无有效身份证] {it['record_id']} cur={cur!r} idc={idc!r}")
            continue
        to_fill.append((it["record_id"], g))

    print(f"总用户={total}  仍需回填={len(to_fill)}")

    filled = 0; fail = 0
    for rid, g in to_fill:
        d = requests.put(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{USER_TABLE}/records/{rid}",
            headers=h, json={"fields": {FIELD_GENDER: g}}, timeout=30).json()
        if d.get("code") == 0:
            filled += 1
        else:
            fail += 1
            print(f"  更新失败 {rid}: {d.get('msg')}")
        if filled % 40 == 0 and filled:
            print(f"  ...已回填 {filled}/{len(to_fill)}  失败={fail}")
        time.sleep(0.02)

    print(f"本次回填完成: 成功={filled}  失败={fail}")

    # 复验：统计整表性别分布
    time.sleep(2)
    records = fetch_all_records()
    from collections import Counter
    stats = Counter((ftext(r["fields"].get(FIELD_GENDER)) or "<空>" ) for r in records)
    print("整表性别分布:", dict(stats))

    # 抽样验证
    for oid in ["ou_fake_male_001", "ou_fake_female_001", "ou_xxx_admin"]:
        d = requests.post(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{USER_TABLE}/records/search",
            headers=h, json={"filter": {"conjunction": "and",
                "conditions": [{"field_name": FIELD_FEISHU, "operator": "is", "value": [oid]}]},
                "page_size": 1}, timeout=60).json()
        items = (d.get("data", {}).get("items") or [])
        if items:
            print(f"  抽验 {oid} -> 性别={ftext(items[0]['fields'].get(FIELD_GENDER))!r}")
    return 0 if fail == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
