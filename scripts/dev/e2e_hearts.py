#!/usr/bin/env python3
"""爱心体系端到端自动验证（v6 事件溯源模型）。

在测试环境运行：
    cd /opt/yixianqian-test/bot && ./venv/bin/python ../web/backend/../scripts/dev/e2e_hearts.py
    （实际路径：./venv/bin/python /opt/yixianqian-test/scripts/dev/e2e_hearts.py）

断言清单：
  A. 初始余额与「初始+邀请−有效喜欢」计算值一致
  B. 喜欢成功：响应 hearts 立即 -1（0 秒精确）
  C. 重复喜欢：400
  D. 连点第二人：hearts 再 -1（含在途意图，防双花）
  E. 取消：hearts 立即 +1
  F. 爱心归零后喜欢被拒：400
  G. 对账收敛：30 秒后表内余额 = 计算真值（无抖动）
退出码：0=全部通过；1=存在失败。
"""
import os
import sys
import time

sys.path.insert(0, "/opt/yixianqian-test")
sys.path.insert(0, "/opt/yixianqian-test/bot")
sys.path.insert(0, "/opt/yixianqian-test/web/backend")
sys.path.insert(0, "/opt/yixianqian-test/scripts/dev")

from _prod_guard import guard  # noqa: E402
guard(os.path.basename(__file__))

import requests  # noqa: E402
from itsdangerous import URLSafeTimedSerializer  # noqa: E402

from constants import INITIAL_HEARTS, USER_TABLE_ID, LIKE_TABLE_ID  # noqa: E402
from clients import search_records, create_record, delete_record  # noqa: E402
from lib.bitable_client import get_field_text, get_select_value  # noqa: E402
import local_config as lc  # noqa: E402

HOST = "https://testapp.nantou.love"
HIS_OID = "ou_ec5d70f07daf238e81ac466a1c553aae"  # 猴哥猴哥
T1, T2, T3 = "ou_e2e_t1", "ou_e2e_t2", "ou_e2e_t3"

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        FAILURES.append(name)


def sess_cookie():
    s = URLSafeTimedSerializer(lc.FEISHU_APP_SECRET, salt="yxq-session")
    return {"yxq_session": s.dumps(HIS_OID)}


def get_me(cookies):
    r = requests.get(f"{HOST}/api/user/me", cookies=cookies, timeout=20)
    return r.json().get("hearts")


def like(cookies, target, note=""):
    r = requests.post(f"{HOST}/api/like", cookies=cookies, timeout=20,
                      json={"target_openid": target, "message": note})
    return r.status_code, r.json().get("hearts"), r.json()


def unlike(cookies, target):
    r = requests.delete(f"{HOST}/api/like/{target}", cookies=cookies, timeout=20)
    return r.status_code, r.json().get("hearts")


def active_like_count(oid):
    n = 0
    for l in search_records(LIKE_TABLE_ID):
        f = l.get("fields", {})
        if get_field_text(f, "发起用户open_id") == oid and get_select_value(f, "状态") != "已取消":
            n += 1
    return n


def cleanup():
    for l in search_records(LIKE_TABLE_ID):
        f = l.get("fields", {})
        if "ou_e2e_t" in str(f.get("目标用户open_id", "")) or "ou_e2e_t" in str(f.get("发起用户open_id", "")):
            delete_record(LIKE_TABLE_ID, l["record_id"])
    for oid in (T1, T2, T3):
        for u in search_records(USER_TABLE_ID, {"conjunction": "and", "conditions": [
                {"field_name": "飞书用户ID", "operator": "is", "value": [oid]}]}):
            delete_record(USER_TABLE_ID, u["record_id"])


def main():
    cleanup()
    for oid, nick in ((T1, "E2E目标1"), (T2, "E2E目标2"), (T3, "E2E目标3")):
        create_record(USER_TABLE_ID, {"昵称": nick, "飞书用户ID": oid,
                                      "账号状态": "活跃", "性别": "女性"})
    time.sleep(16)  # 等 users/likes 快照刷新
    ck = sess_cookie()

    # A. 初始一致性
    me0 = get_me(ck)
    expect0 = INITIAL_HEARTS - active_like_count(HIS_OID)
    check("A.初始余额=计算值", me0 == expect0, f"(me={me0}, 计算={expect0})")

    # B. 喜欢第一人：立即-1
    st, h1, _ = like(ck, T1)
    check("B.喜欢成功且hearts立即-1", st == 200 and h1 == expect0 - 1, f"(status={st}, hearts={h1})")

    # C. 重复喜欢 400
    st, _, body = like(ck, T1)
    check("C.重复喜欢被拒", st == 400, f"(status={st}, msg={body.get('error')})")

    # D. 连点第二人：含在途意图仍正确-1（防双花）
    st, h2, _ = like(ck, T2)
    check("D.第二人hearts再-1", st == 200 and h2 == expect0 - 2, f"(status={st}, hearts={h2})")

    # 若 expect0-2 == 0 则继续验证归零拒绝，否则先取消一个再测
    if h2 > 0:
        st, hx = unlike(ck, T2)
        check("E1.取消立即+1", st == 200 and hx == h2 + 1, f"(status={st}, hearts={hx})")
        like(ck, T2)  # 重新喜欢回去
    # E. 取消立即+1（当前余额应为 expect0-2）
    st, h3 = unlike(ck, T1)
    check("E.取消立即+1", st == 200 and h3 == expect0 - 1, f"(status={st}, hearts={h3})")

    # F. 耗尽余额后喜欢被拒（每点一次 200 即少一颗，直至 400）
    guard_budget = 6
    last_st = None
    while guard_budget > 0 and get_me(ck) > 0:
        last_st, _, _ = like(ck, T3)
        if last_st != 200:
            break
        guard_budget -= 1
    me = get_me(ck)
    st, _, body = like(ck, T3)
    check("F.爱心耗尽后被拒", st == 400, f"(me={me}, status={st}, msg={body.get('error')})")

    # G. 对账收敛（30s后表值与计算值一致）
    time.sleep(32)
    real = None
    for u in search_records(USER_TABLE_ID, {"conjunction": "and", "conditions": [
            {"field_name": "飞书用户ID", "operator": "is", "value": [HIS_OID]}]}):
        real = u["fields"].get("爱心剩余")
    expect_final = max(0, INITIAL_HEARTS - active_like_count(HIS_OID))
    check("G.对账收敛且无抖动", real == expect_final, f"(表内={real}, 计算={expect_final})")

    cleanup()
    print("\n结果:", "全部通过 ✅" if not FAILURES else f"失败 {len(FAILURES)} 项: {FAILURES}")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
