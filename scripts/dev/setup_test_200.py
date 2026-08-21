#!/usr/bin/env python3
#!/usr/bin/env python3
import os, sys
_D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _D)
sys.path.insert(0, os.path.dirname(_D))
from _prod_guard import guard
guard(os.path.basename(__file__))
# -*- coding: utf-8 -*-
"""200人大规模分组测试 - 数据准备脚本

按已批准方案：
- 200 = 7个真号 + 193个假号（97男 + 96女）
- 真号：U-0001/2/7(男) + U-0003/5/6/20(女)
- 假号 用户ID 从 U-0100 起，open_id = ou_fake_{male|female}_NNN
- 活动 A-0005，每组 4男4女 = 25 个满编组

执行步骤：
1. 清理 A-0005 历史数据（报名 / 分组选择 / 分组结果，重置分组状态为未开始）
2. 创建 193 个假用户
3. 200 人报名 A-0005
4. 预填 193 个假号的志愿（每个挑 7 位异性，互不重复）
   —— 真号 7 个不走预填，由真实流程提交

用法：
  python3 setup_test_200.py plan       # 只打印计划，不改数据
  python3 setup_test_200.py run        # 真实执行
"""
import sys
import random
import time
import requests

import local_config as _cfg
APP_ID = _cfg.APP_ID
APP_SECRET = _cfg.APP_SECRET
BASE_TOKEN = _cfg.BASE_TOKEN

USER_TABLE_ID = "tblsecbZZv0thaPe"          # 用户表
SIGNUP_TABLE_ID = "tblNVJCnohVaWf8t"        # 报名表
GROUP_SELECT_TABLE = "tblYo86Vd7dmzRQJ"     # 分组选择表（志愿）
GROUP_RESULT_TABLE = "tbl3xxAYhyTDGWAB"     # 分组结果表
ACTIVITY_TABLE_ID = "tblHLltReY8xHTfu"      # 活动表

ACTIVITY_ID = "A-0005"
M_PER = 4
F_PER = 4

# ---- 7 个真号（已从用户表确认真实 open_id/性别）----
REAL_ACCOUNTS = [
    {"uid": "U-0001", "nickname": "test 侯昵称", "gender": "男性", "open_id": "ou_xxx1"},
    {"uid": "U-0002", "nickname": "test3 昵称",  "gender": "男性", "open_id": "ou_xxx2"},
    {"uid": "U-0007", "nickname": "test5 昵称",  "gender": "男性", "open_id": "ou_xxx3"},
    {"uid": "U-0003", "nickname": "test1 昵称",  "gender": "女性", "open_id": "ou_xxx4"},
    {"uid": "U-0005", "nickname": "平安叩昵称",    "gender": "女性", "open_id": "ou_xxx5"},
    {"uid": "U-0006", "nickname": "侯登山昵称",    "gender": "女性", "open_id": "ou_xxx6"},
    {"uid": "U-0020", "nickname": "刘局",       "gender": "女性", "open_id": "ou_xxx7"},
]

# ---- 假号生成参数 ----
FAKE_MALE_NUM = 97
FAKE_FEMALE_NUM = 96
UID_START = 100  # 从 U-0100 起

_tok = {"t": None, "exp": 0}


def get_token():
    now = time.time()
    if _tok["t"] and _tok["exp"] > now + 60:
        return _tok["t"]
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=10)
    res = r.json()
    _tok["t"] = res["tenant_access_token"]
    _tok["exp"] = now + res.get("expire", 7200)
    return _tok["t"]


def search(table_id, conditions=None, page_size=100):
    """镜像机器人 search_records：按 filter 分页拉全，翻页"""
    tok = get_token()
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/search"
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    data = {"page_size": page_size}
    if conditions:
        data["filter"] = {"conjunction": "and", "conditions": conditions}
    out = []
    page_token = None
    while True:
        params = {"page_token": page_token} if page_token else {}
        r = requests.post(url, headers=headers, json=data, params=params, timeout=15)
        res = r.json()
        if res.get("code") != 0:
            print(f"  [search] {table_id} 出错: {res.get('msg')}")
            break
        d = res.get("data", {})
        out.extend(d.get("items", []))
        if not d.get("has_more"):
            break
        page_token = d.get("page_token")
        if not page_token:
            break
    return out


def search_by_field(table_id, field, value):
    """按某文本字段精确过滤查找所有匹配记录"""
    return search(table_id, [{"field_name": field, "operator": "is", "value": [value]}])


def search_user_by_uid(uid):
    """用户表按用户ID查（自动编号字段需传数字部分，如 U-0100 -> 0100）"""
    num = uid.split("-")[1]  # "0100"
    return search_by_field(USER_TABLE_ID, "用户ID", num)


def create_record(table_id, fields):
    tok = get_token()
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records"
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    try:
        r = requests.post(url, headers=headers, json={"fields": fields}, timeout=15)
        res = r.json()
        if res.get("code") == 0:
            return res.get("data", {}).get("record", {}).get("record_id")
        print(f"  [create] {table_id} 失败: {res.get('msg')}")
        return None
    except Exception as e:
        print(f"  [create] {table_id} 异常: {e}")
        return None


def delete_record(table_id, record_id):
    tok = get_token()
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/{record_id}"
    headers = {"Authorization": f"Bearer {tok}"}
    r = requests.delete(url, headers=headers, timeout=15)
    return r.json().get("code") == 0


def update_record(table_id, record_id, fields):
    tok = get_token()
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/{record_id}"
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    r = requests.put(url, headers=headers, json={"fields": fields}, timeout=15)
    return r.json().get("code") == 0


def ftext(v):
    """把字段原始值转成展示文本"""
    if isinstance(v, dict):
        if "value" in v and isinstance(v["value"], list) and v["value"]:
            return ftext(v["value"][0])
        return str(v.get("text", "") or v.get("name", "") or "")
    if isinstance(v, list):
        return ftext(v[0]) if v else ""
    return str(v)


def build_fake_users():
    """生成 193 个假号（97男 + 96女），用户ID 从 U-0100 起，open_id 唯一。"""
    males, females = [], []
    seq = UID_START
    for i in range(FAKE_MALE_NUM):
        males.append({
            "uid": f"U-{seq:04d}", "gender": "男性",
            "nickname": f"测试男{i+1:03d}昵称",
            "name": f"测试男{i+1:03d}",
            "open_id": f"ou_fake_male_{i+1:03d}",
        })
        seq += 1
    for i in range(FAKE_FEMALE_NUM):
        females.append({
            "uid": f"U-{seq:04d}", "gender": "女性",
            "nickname": f"测试女{i+1:03d}昵称",
            "name": f"测试女{i+1:03d}",
            "open_id": f"ou_fake_female_{i+1:03d}",
        })
        seq += 1
    return males, females


def gen_profile_fields(u):
    return {
        "用户ID": u["uid"],
        "昵称": u["nickname"],
        "姓名": u["name"],
        "性别": u["gender"],
        "飞书用户ID": u["open_id"],
        "账号状态": "活跃",
        "注册时间": int(time.time() * 1000),
        "爱心剩余": 30,
        "年龄": random.randint(22, 35),
        "学历": random.choice(["本科", "硕士", "博士"]),
        "现居/工作城市": "深圳",
        "经常去的教堂": "深圳南头堂",
        "职位": random.choice(["工程师", "教师", "医生", "设计师", "公务员", "金融分析师"]),
        "身高（cm）": random.randint(160, 185),
        "你结过婚吗？": "没结过婚",
        "您替子女注册吗？": "不是，我为自己报名",
    }


def plan_summary():
    males, females = build_fake_users()
    fake = males + females
    total = len(REAL_ACCOUNTS) + len(fake)
    print("=" * 60)
    print("200 人大规模分组测试 - 数据准备计划")
    print("=" * 60)
    print(f"真号: {len(REAL_ACCOUNTS)} 个")
    for r in REAL_ACCOUNTS:
        print(f"  {r['uid']}  {r['nickname']}  {r['gender']}  {r['open_id']}")
    print(f"假号: {len(fake)} 个 = {FAKE_MALE_NUM}男 + {FAKE_FEMALE_NUM}女")
    print(f"  男用户ID: {males[0]['uid']}~{males[-1]['uid']}，open_id ou_fake_male_001~{FAKE_MALE_NUM:03d}")
    print(f"  女用户ID: {females[0]['uid']}~{females[-1]['uid']}，open_id ou_fake_female_001~{FAKE_FEMALE_NUM:03d}")
    print(f"总参与: {total} 人（男 {3 + FAKE_MALE_NUM}，女 {4 + FAKE_FEMALE_NUM}）")
    print(f"活动: {ACTIVITY_ID}，每组 {M_PER}男{F_PER}女 = {M_PER + F_PER}人/组")
    print(f"满编组数: {min((3 + FAKE_MALE_NUM) // M_PER, (4 + FAKE_FEMALE_NUM) // F_PER)} 组")
    print()
    print("真实流程（不预填）的 7 个真号将在「开始填志愿」~「执行分组」间由机器人真实操作提交志愿。")
    print("=" * 60)


def cleanup():
    print("\n[1/4] 清理 A-0005 历史数据...")
    # 报名
    s = search_by_field(SIGNUP_TABLE_ID, "活动ID", ACTIVITY_ID)
    print(f"  删报名记录 {len(s)} 条")
    for it in s:
        delete_record(SIGNUP_TABLE_ID, it["record_id"])
    # 分组选择
    gs = search_by_field(GROUP_SELECT_TABLE, "活动ID", ACTIVITY_ID)
    print(f"  删分组选择 {len(gs)} 条")
    for it in gs:
        delete_record(GROUP_SELECT_TABLE, it["record_id"])
    # 分组结果
    gr = search_by_field(GROUP_RESULT_TABLE, "活动ID", ACTIVITY_ID)
    print(f"  删分组结果 {len(gr)} 条")
    for it in gr:
        delete_record(GROUP_RESULT_TABLE, it["record_id"])
    # 活动状态重置（活动ID 为自动编号字段，A-0005 -> 5）
    acts = search(ACTIVITY_TABLE_ID, [{"field_name": "活动ID", "operator": "is", "value": ["5"]}])
    if acts:
        update_record(ACTIVITY_TABLE_ID, acts[0]["record_id"], {
            "分组状态": "未开始",
            "每组男生数": M_PER,
            "每组女生数": F_PER,
            # 报名人数上限 200（已确认；如需可保留 200）
        })
        print(f"  重置活动 {ACTIVITY_ID} 分组状态 -> 未开始，每组 {M_PER}男{F_PER}女")
    else:
        print("  !! 未找到活动 A-0005")


def create_fake_users():
    print("\n[2/4] 创建 193 个假用户...")
    males, females = build_fake_users()
    fake = males + females
    created = 0
    for i, u in enumerate(fake, 1):
        # 防重：用户ID 已存在则跳过
        exist = search_user_by_uid(u["uid"])
        if exist:
            u["created"] = False
            continue
        rid = create_record(USER_TABLE_ID, gen_profile_fields(u))
        if rid:
            created += 1
        if i % 20 == 0:
            print(f"  ...{i}/{len(fake)}")
        time.sleep(0.03)
    print(f"  新建成功 {created} 个，已存在跳过 {len(fake) - created} 个")
    return fake


def enroll_signups(fake_users):
    print("\n[3/4] 200 人报名 A-0005...")
    all_p = REAL_ACCOUNTS + [u for u in fake_users if u.get("created", True)]
    ok = 0
    for i, p in enumerate(all_p, 1):
        # 防重：同 open_id 已报名则跳过
        exist = search_by_field(SIGNUP_TABLE_ID, "报名人open_id", p["open_id"])
        if any(ftext(x.get("fields", {}).get("活动ID")) == ACTIVITY_ID for x in exist):
            continue
        rid = create_record(SIGNUP_TABLE_ID, {
            "活动ID": ACTIVITY_ID,
            "报名人open_id": p["open_id"],
            "报名人昵称": p["nickname"],
            "状态": "已报名",
            "报名时间": int(time.time() * 1000),
        })
        if rid:
            ok += 1
        if i % 20 == 0:
            print(f"  ...{i}/{len(all_p)}")
        time.sleep(0.03)
    print(f"  报名成功 {ok} 人")
    return all_p


def prefill_fake_selections(fake_users):
    """预填假号志愿：每个假号从对侧「假号」中随机互不重复选 7 位异性。
    真号 7 个不预填，留给真实流程。"""
    print("\n[4/4] 预填 193 个假号志愿（每个选 7 位异性）...")
    males = [u for u in fake_users if u["gender"] == "男性" and u.get("created", True)]
    females = [u for u in fake_users if u["gender"] == "女性" and u.get("created", True)]
    all_m_oids = [u["open_id"] for u in males]
    all_f_oids = [u["open_id"] for u in females]
    done = 0
    pairs = []
    # 男 -> 7 女；女 -> 7 男
    for m in males:
        picks = random.sample(all_f_oids, 7)
        pairs.append((m, picks))
        done += 1
    for fm in females:
        picks = random.sample(all_m_oids, 7)
        pairs.append((fm, picks))
        done += 1
    # 写入分组选择表
    written = 0
    choice_fields = [f"第{i}志愿" for i in range(1, 8)]
    for picker, picks in pairs:
        # 防重：已有选择则跳过（更新）
        fields = {
            "活动ID": ACTIVITY_ID,
            "选择人open_id": picker["open_id"],
            "选择人昵称": picker["nickname"],
            "选择人性别": picker["gender"],
        }
        for cf, oid in zip(choice_fields, picks):
            fields[cf] = oid
        create_record(GROUP_SELECT_TABLE, fields)
        written += 1
    print(f"  预填志愿条数 {written}")
    return written


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "plan"
    if mode == "plan":
        plan_summary()
        return
    if mode != "run":
        print("用法: python3 setup_test_200.py plan | run")
        return

    plan_summary()
    confirm = input("\n确认执行真实数据准备？输入 yes 继续: ")
    if confirm.strip().lower() != "yes":
        print("已取消。")
        return

    cleanup()
    fake_users = create_fake_users()
    enroll_signups(fake_users)
    prefill_fake_selections(fake_users)

    print("\n全部完成！下一步请在飞书真实操作：")
    print("  1) 管理员发：开始填志愿 A-0005 4 4")
    print("  2) 7 个真号发「分组」提交志愿")
    print("  3) 管理员发：执行分组 A-0005")
    print("  4) 用 check_group_result.py 校验结果")


if __name__ == "__main__":
    main()
