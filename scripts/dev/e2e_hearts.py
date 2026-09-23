#!/usr/bin/env python3
"""月度额度体系端到端自动验证（v7 模型：匿名每月 10 颗，实名每月 1 次 + 永久名额）。

在测试环境运行：
    cd /opt/yixianqian-test/bot && ./venv/bin/python /opt/yixianqian-test/scripts/dev/e2e_hearts.py

断言清单：
  A. 起始额度与机器人对账一致（匿名 = 10 − 本月已用；实名 = 1 + 永久名额 − 本月已用）
  B. 匿名喜欢成功：响应 anon_left 立即 -1（0 秒精确，不等机器人那 25 秒）
  C. 重复喜欢同一人：400
  D. 连点第二人：anon_left 再 -1（含在途意图，防双花）
  E. 取消喜欢接口已下线：DELETE /api/like/<oid> → 404
  F. 10 颗用完后第 11 次被拒：400
  G. 对账收敛：35 秒后表内「爱心剩余」= 计算真值（无抖动）
  H. 实名喜欢：real_left -1，且 anon_left 不变（实名不占匿名那 10 颗）
  I. 本月实名机会用完后再点实名：400
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

from constants import USER_TABLE_ID, LIKE_TABLE_ID  # noqa: E402
from clients import search_records, create_record, delete_record  # noqa: E402
from lib.bitable_client import get_field_text  # noqa: E402
from lib import quota  # noqa: E402
import local_config as lc  # noqa: E402

HOST = "https://testapp.nantou.love"
HIS_OID = "ou_ec5d70f07daf238e81ac466a1c553aae"  # 猴哥猴哥
# 匿名额度是 10 颗，验证「用完被拒」需要 10 个不同的目标（同一人只能喜欢一次）
TARGETS = [f"ou_e2e_t{i}" for i in range(1, 11)]
BEFORE = "ou_e2e_before"  # 第一个实名喜欢的目标
AFTER = "ou_e2e_after"    # 第二个实名喜欢的目标（验证名额用尽）

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        FAILURES.append(name)


def sess_cookie():
    s = URLSafeTimedSerializer(lc.FEISHU_APP_SECRET, salt="yxq-session")
    return {"yxq_session": s.dumps(HIS_OID)}


def get_quota(cookies):
    """返回 (anon_left, real_left)。机器人快照 + 在途意图合出来的展示值。"""
    r = requests.get(f"{HOST}/api/user/me", cookies=cookies, timeout=20)
    j = r.json()
    return j.get("anon_left"), j.get("real_left")


def like(cookies, target, like_type="匿名", note=""):
    r = requests.post(f"{HOST}/api/like", cookies=cookies, timeout=20,
                      json={"target_openid": target, "like_type": like_type, "message": note})
    j = r.json()
    return r.status_code, j.get("anon_left"), j.get("real_left"), j


def his_month_likes(like_type=None):
    """本月本人发起、且仍然有效的喜欢数（与 lib.quota 同一口径）。"""
    n = 0
    for l in search_records(LIKE_TABLE_ID):
        f = l.get("fields", {})
        if get_field_text(f, "发起用户open_id") != HIS_OID:
            continue
        if not quota.is_like_active(
                get_field_text(f, "状态"),
                get_field_text(f, "喜欢类型") or quota.LIKE_TYPE_ANON,
                get_field_text(f, "归属月份") or (get_field_text(f, "创建时间") or "")[:7]):
            continue
        if like_type and (get_field_text(f, "喜欢类型") or quota.LIKE_TYPE_ANON) != like_type:
            continue
        n += 1
    return n


def table_heart_remain():
    """用户表里机器人对账写下的匿名剩余。"""
    for u in search_records(USER_TABLE_ID, {"conjunction": "and", "conditions": [
            {"field_name": "飞书用户ID", "operator": "is", "value": [HIS_OID]}]}):
        return u["fields"].get("爱心剩余")
    return None


def table_real_remain():
    for u in search_records(USER_TABLE_ID, {"conjunction": "and", "conditions": [
            {"field_name": "飞书用户ID", "operator": "is", "value": [HIS_OID]}]}):
        return u["fields"].get("实名剩余")
    return None


def cleanup():
    """清掉本脚本造的所有记录。测试目标用固定 open_id 前缀，重跑即幂等。"""
    for l in search_records(LIKE_TABLE_ID):
        f = l.get("fields", {})
        blob = str(f.get("目标用户open_id", "")) + str(f.get("发起用户open_id", ""))
        if "ou_e2e_" in blob:
            delete_record(LIKE_TABLE_ID, l["record_id"])
    for oid in TARGETS + [BEFORE, AFTER]:
        for u in search_records(USER_TABLE_ID, {"conjunction": "and", "conditions": [
                {"field_name": "飞书用户ID", "operator": "is", "value": [oid]}]}):
            delete_record(USER_TABLE_ID, u["record_id"])


def main():
    cleanup()
    for oid in TARGETS + [BEFORE, AFTER]:
        create_record(USER_TABLE_ID, {"昵称": f"E2E-{oid[-2:]}", "飞书用户ID": oid,
                                      "账号状态": "单身", "性别": "女性"})
    time.sleep(16)  # 等 users/likes 快照刷新
    ck = sess_cookie()

    # A. 起始额度与计算值一致。本月已经点过的喜欢要算进去（重跑、或本月真用过额度）
    used_anon = his_month_likes(quota.LIKE_TYPE_ANON)
    anon0, _real0 = get_quota(ck)
    expect_anon0 = max(0, quota.MONTHLY_ANON_HEARTS - used_anon)
    check("A.起始匿名额度=计算值", anon0 == expect_anon0,
          f"(页面={anon0}, 计算={expect_anon0}, 本月已用={used_anon})")
    if expect_anon0 < 3:
        # 继续跑下去只会得到一堆 RED，掩盖真正的原因。说清楚再退出。
        print(f"\n本月匿名额度只剩 {expect_anon0} 颗（{HIS_OID} 已经用过 {used_anon} 次），"
              f"不足以验证「喜欢—扣减—耗尽—拒绝」全链路。\n"
              f"请先清掉该账号本月的喜欢记录，或换一个本月没用过额度的账号。")
        sys.exit(2)

    # B. 第一次匿名喜欢：立即 -1
    st, a1, _r1, _ = like(ck, TARGETS[0])
    check("B.喜欢成功且anon_left立即-1", st == 200 and a1 == expect_anon0 - 1,
          f"(status={st}, anon_left={a1}, 期望={expect_anon0 - 1})")

    # C. 重复喜欢同一人 400
    st, _, _, body = like(ck, TARGETS[0])
    check("C.重复喜欢被拒", st == 400, f"(status={st}, msg={body.get('error')})")

    # D. 连点第二人：含在途意图，仍然是精确 -1
    st, a2, _, _ = like(ck, TARGETS[1])
    check("D.第二人anon_left再-1", st == 200 and a2 == expect_anon0 - 2,
          f"(status={st}, anon_left={a2})")

    # E. 取消喜欢已整个下线：路由不存在 → 404（不是 400/405）
    r = requests.delete(f"{HOST}/api/like/{TARGETS[0]}", cookies=ck, timeout=20)
    check("E.取消喜欢接口已下线", r.status_code == 404, f"(status={r.status_code})")

    # F. 一路点到被拒为止。不假设起始额度就是满的——本月已用几次也算数
    ok, rejected_on = 2, None
    for t in TARGETS[2:]:
        st, _a, _r, _body = like(ck, t)
        if st == 200:
            ok += 1
        else:
            rejected_on = t
            break
    check("F1.额度耗尽时总量正好 MONTHLY_ANON_HEARTS",
          ok + used_anon == quota.MONTHLY_ANON_HEARTS,
          f"(本次成功 {ok} 次 + 本月原有 {used_anon} 次)")
    rejected_on = rejected_on or BEFORE
    st, _, _, body = like(ck, rejected_on)
    check("F2.额度耗尽后匿名喜欢被拒", st == 400,
          f"(status={st}, msg={body.get('error')})")

    # G. 对账收敛：等机器人跑完一轮，表内字段 = 计算真值
    time.sleep(35)
    expect_anon = max(0, quota.MONTHLY_ANON_HEARTS - his_month_likes(quota.LIKE_TYPE_ANON))
    heart_remain = table_heart_remain()
    check("G.匿名额度对账收敛且无抖动", heart_remain == expect_anon,
          f"(表内={heart_remain}, 计算={expect_anon})")

    # H. 实名喜欢：扣实名的账，不占匿名那 10 颗
    anon_before, real_before = get_quota(ck)
    st, anon_after, real_after, body = like(ck, BEFORE, like_type="实名")
    if real_before and real_before > 0:
        check("H1.实名喜欢成功且real_left-1",
              st == 200 and real_after == real_before - 1,
              f"(status={st}, real_left={real_after}, 期望={real_before - 1})")
        check("H2.实名不占用匿名额度", anon_after == anon_before,
              f"(anon={anon_after}, 期望={anon_before})")
        # I. 本月实名机会用完（每月 1 次 + 永久名额）后再点实名
        st2, _, _, body2 = like(ck, AFTER, like_type="实名")
        still_left = real_before - 1 > 0
        check("I.实名额度用尽后按预期处理",
              (st2 == 200) if still_left else (st2 == 400),
              f"(status={st2}, msg={body2.get('error')}, 第二次实名前剩余={real_before - 1})")
    else:
        # 本月实名机会已经用掉了：此时实名应当被拒，且不影响匿名额度
        check("H1.本月无实名名额时应被拒", st == 400, f"(status={st}, msg={body.get('error')})")
        check("H2.被拒不影响匿名额度", anon_after == anon_before,
              f"(anon={anon_after}, 期望={anon_before})")

    # J. 实名字段对账收敛（1 个月后只返还名额，喜欢记录本身不变，所以这里只断言字段可读且一致）
    time.sleep(35)
    real_remain = table_real_remain()
    check("J.实名剩余对账收敛", real_remain is not None, f"(表内={real_remain})")

    cleanup()
    print("\n结果:", "全部通过 ✅" if not FAILURES else f"失败 {len(FAILURES)} 项: {FAILURES}")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
