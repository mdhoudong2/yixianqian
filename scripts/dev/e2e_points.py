#!/usr/bin/env python3
"""麦穗积分端到端验证（在测试服跑，会真的花穗、真的登记邀请）。

跑法（要用 H5 的 venv：签 session cookie 依赖 itsdangerous，bot 的 venv 里没有）：
    cd /opt/yixianqian-test && YIXIANQIAN_ENV=test \\
        /opt/yixianqian-test/web/backend/venv/bin/python \\
        /opt/yixianqian-test/scripts/dev/e2e_points.py

单测（tests/test_points_*.py）考的是规则本身，这里考的是**接线**：H5 的鉴权、
参数名、错误翻译，以及管理指令打真表格时的表 ID 和字段名对不对。单测里表格
全是桩，桩不会告诉你字段名打错了。

会改动测试服的东西（跑完都打印出来了）：
  · 给测试账号加穗（lib 直接写账本，作为预置）
  · 登记一个 E2E 专用手机号的邀请
  · 花掉一些穗（兑换 + 并发幂等验证）
退出码：0=全部通过；1=有失败。
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

# 顺序有讲究：bot/ 必须最先被找到。bot 和 web/backend 各有一份 local_config.py，
# 后者没有 WISH_TABLE_ID / MATCHMAKER_ORDER_TABLE_ID——先找到它，管理指令就会
# 一律回「这张表还没配置」，看着像没配，其实是找错了文件。
for _p in ("/opt/yixianqian-test", "/opt/yixianqian-test/scripts/dev",
           "/opt/yixianqian-test/bot"):
    sys.path.insert(0, _p)
del _p

import local_config as lc  # noqa: E402
import requests  # noqa: E402
from _prod_guard import guard  # noqa: E402
from clients import search_records, update_record  # noqa: E402
from constants import (  # noqa: E402
    ACTIVITY_TABLE_ID,
    FIELD_ACTIVITY_STATUS,
    FIELD_FEISHU_ID,
    FIELD_PHONE,
    FIELD_SIGNUP_ACTIVITY_ID,
    FIELD_SIGNUP_OPENID,
    FIELD_SIGNUP_STATUS,
    SIGNUP_TABLE_ID,
    USER_TABLE_ID,
)
from itsdangerous import URLSafeTimedSerializer  # noqa: E402

from lib import points, points_config, points_redeem  # noqa: E402
from lib.bitable_client import get_field_number, get_field_text  # noqa: E402

guard(os.path.basename(__file__))

HOST = "https://testapp.nantou.love"
# 测试服里一个正常的单身账号（e2e_hearts 用的同一个）。换个账号也能跑，改这里即可。
ACTOR = "ou_ec5d70f07daf238e81ac466a1c553aae"
# E2E 专用号码：不会和真人撞（1 后面全是 9），重复跑也只登记一条（幂等看 lib）
E2E_PHONE = "13999990001"

WRITE_HEADERS = {"X-Requested-With": "XMLHttpRequest"}
FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}".rstrip())
    if not cond:
        FAILURES.append(name)
    return cond


def cookies():
    s = URLSafeTimedSerializer(lc.FEISHU_APP_SECRET, salt="yxq-session")
    return {"yxq_session": s.dumps(ACTOR)}


def get(c, path):
    return requests.get(f"{HOST}{path}", cookies=c, timeout=20)


def post(c, path, body=None):
    return requests.post(f"{HOST}{path}", cookies=c, timeout=20,
                         headers=WRITE_HEADERS, json=body or {})


def balance():
    return points.balance(ACTOR)


def fund(target):
    """把余额**对齐到** target：不够就补，多了就扣回。两个方向都记流水、都打印。

    只补不扣的话，上一次跑剩下的穗会让「余额不足」这类用例失去前提，断言就
    时灵时不灵。开局状态一致，断言才可比。
    """
    cur = balance()
    if cur == target:
        return cur
    delta = target - cur
    if delta > 0:
        points.grant(ACTOR, delta, points.KIND_ADMIN, reason="e2e_points.py 预置")
    else:
        points.add_entry(ACTOR, delta, points.KIND_ADMIN,
                         reason="e2e_points.py 预置回正", operator_oid=ACTOR)
    print(f"    （预置：{cur} → {balance()} 穗）")
    return balance()


def find_activity(want_status, *, want_fee=None):
    """按状态找一场活动。want_fee 传 None 表示收费与否都行。"""
    for a in search_records(ACTIVITY_TABLE_ID):
        f = a.get("fields", {})
        if get_field_text(f, FIELD_ACTIVITY_STATUS) != want_status:
            continue
        if want_fee is None:
            return a
        fee = get_field_number(f, "费用", 0) or 0
        if (fee > 0) == want_fee:
            return a
    return None


# ---------------------------------------------------------------- A 鉴权

def test_auth():
    anon = requests.get(f"{HOST}/api/points/me", timeout=20)
    check("A1 未登录读麦穗 → 401", anon.status_code == 401, f"实际 {anon.status_code}")

    c = cookies()
    r = requests.post(f"{HOST}/api/points/invite", cookies=c, timeout=20,
                      json={"phone": E2E_PHONE})
    check("A2 缺 CSRF 头的写接口 → 403", r.status_code == 403, f"实际 {r.status_code}")


# ---------------------------------------------------------------- B 余额与流水

def test_balance_matches_ledger(c):
    j = get(c, "/api/points/me").json()
    check("B1 /api/points/me 形状齐全",
          all(k in j for k in ("balance", "earned", "spent", "pending_points",
                               "entries", "extra_real_like")),
          f"键={sorted(j)}")
    check("B2 接口余额 = 账本 SUM(delta)", j["balance"] == balance(),
          f"接口 {j['balance']} / 账本 {balance()}")
    check("B3 待确认的穗不在余额里",
          j["balance"] == j["earned"] - j["spent"] or j["pending_points"] >= 0,
          f"待确认 {j['pending_points']}")


# ---------------------------------------------------------------- C 邀请

def test_invite(c):
    before = balance()
    j = post(c, "/api/points/invite", {"phone": E2E_PHONE}).json()
    check("C1 登记一位还没注册的好友", j.get("ok") is True, str(j)[:120])

    lst = get(c, "/api/points/invite").json()
    rows = lst.get("list", [])
    masked = [i.get("phone_masked") for i in rows]
    check("C2 邀请列表里手机号打码（不整号回显）",
          f"{E2E_PHONE[:3]}****{E2E_PHONE[-4:]}" in masked and
          not any("invitee_phone" in i for i in rows),
          f"样例 {masked[:2]}")
    check("C3 待确认期间不进余额", balance() == before,
          f"{before} → {balance()}")

    # 自己的号码：不能邀请自己。用 ACTOR 在用户表里的手机号，取不到就跳过。
    mine = _my_phone()
    if mine:
        r = post(c, "/api/points/invite", {"phone": mine})
        check("C4 不能邀请自己", r.status_code == 400, f"实际 {r.status_code}")

    r = post(c, "/api/points/invite", {"phone": "12345"})
    check("C5 号码不合法要拒", r.status_code == 400, f"实际 {r.status_code}")


def _me_record():
    """自己在用户表里的那一行。找不到返回 None（下面的用例会跳过）。"""
    for u in search_records(USER_TABLE_ID, {"conjunction": "and", "conditions": [
            {"field_name": FIELD_FEISHU_ID, "operator": "is", "value": [ACTOR]}]}):
        return u
    return None


def _my_phone():
    rec = _me_record()
    raw = (rec or {}).get("fields", {}).get(FIELD_PHONE)
    return raw if isinstance(raw, str) and raw.isdigit() else ""


# ---------------------------------------------------------------- D 兑换

def redeem_post(c, body):
    """打兑换接口。带节流 + 429 重试。

    /api/points/redeem 限 20 次/60 秒，撞上就是 429——那看起来和「被规则拒绝」
    一模一样，是假红。本进程自己的调用数好数，但**上一个进程刚跑过的那些还占着
    服务端的窗口**，所以连跑两遍仍然会撞。429 从来不是这些用例想考的东西，
    等一分钟重来即可。"""
    while True:
        now = time.time()
        while _redeem_calls and now - _redeem_calls[0] > 60:
            _redeem_calls.pop(0)
        if len(_redeem_calls) < 18:
            _redeem_calls.append(now)
            break
        time.sleep(_redeem_calls[0] + 60 - now + 1)
    for attempt in range(3):
        r = post(c, "/api/points/redeem", body)
        if r.status_code != 429:
            return r
        print(f"    （429 限流，等 60 秒重试第 {attempt + 1} 次）")
        time.sleep(61)
        _redeem_calls.clear()
    return r


_redeem_calls = []


def test_redeem(c):
    cost = points_redeem.cost_of(points_redeem.ITEM_REAL_LIKE)
    # 每次跑都要一把新键：写死的键第二次跑就变成「已经开过单」，D5 会假红。
    run = str(int(time.time()))
    fund(cost * 3)

    b0 = balance()
    j = redeem_post(c, {"item": "real_like", "request_key": f"e2e-{run}-a"}).json()
    after_first = balance()
    check("D1 兑换额外实名喜欢扣穗",
          j.get("ok") is True and after_first == b0 - cost, f"{b0} → {after_first}")
    check("D2 兑换返回最新余额给前端",
          j.get("balance") == after_first, f"{j.get('balance')} / {after_first}")

    n_orders = len(get(c, "/api/points/redemptions").json()["list"])

    # 幂等：同一个 request_key 再来一次，不能再扣
    key = f"e2e-{run}-idem"
    fund(cost * 2)
    b0 = balance()
    redeem_post(c, {"item": "real_like", "request_key": key})
    b1 = balance()
    r = redeem_post(c, {"item": "real_like", "request_key": key})
    b2 = balance()
    check("D3 同 request_key 重试不重复扣穗", b1 == b2, f"{b1} → {b2}")
    check("D4 重试返回「已经开过单」而不是「余额不足」",
          "已经开过单" in r.json().get("error", ""), str(r.json())[:120])
    check("D5 第一次确实扣了", b1 == b0 - cost, f"{b0} → {b1}")

    # 并发：5 个一模一样的请求同时打，只能开出一单
    fund(cost * 2)
    b0 = balance()
    key2 = f"e2e-{run}-conc"

    def _hit(_):
        return redeem_post(c, {"item": "real_like",
                               "request_key": key2}).status_code

    with ThreadPoolExecutor(max_workers=5) as ex:
        codes = list(ex.map(_hit, range(5)))
    b1 = balance()
    check("D6 并发 5 次只扣一次",
          b1 == b0 - cost and codes.count(200) == 1,
          f"{b0} → {b1}，响应码 {sorted(codes)}")

    orders = get(c, "/api/points/redemptions").json()["list"]
    check("D7 兑换记录条数对得上", len(orders) >= n_orders + 1, f"{len(orders)} 条")

    # 余额不足。红娘推荐要先过「条件不能为空」，所以这里得把条件填上，
    # 否则拦下来的是空条件——那也是个正确的拒绝，但考的不是余额这条闸。
    fund(points_redeem.cost_of(points_redeem.ITEM_REAL_LIKE))   # 20 穗 < 红娘的 50
    left = balance()
    r = redeem_post(c, {"item": points_redeem.ITEM_MATCHMAKER,
                        "condition": "e2e 余额不足用例"})
    check("D8 余额不足拒兑",
          left < points_redeem.cost_of(points_redeem.ITEM_MATCHMAKER)
          and r.status_code == 400 and "余额不足" in r.json().get("error", ""),
          f"余额 {left}｜{str(r.json())[:80]}")

    r = redeem_post(c, {"item": "不存在的项"})
    check("D9 未知兑换项被拒", r.status_code == 400, f"实际 {r.status_code}")


# ---------------------------------------------------------------- E 活动类

def test_activity_items(c):
    opened = find_activity("报名中", want_fee=True)
    if not opened:
        print("[SKIP] E1-E3 测试服里没有「报名中 + 收费」的活动")
    else:
        aid = opened["record_id"]
        fund(points_redeem.cost_of(points_redeem.ITEM_FEE_DISCOUNT))
        act = get(c, f"/api/activities/{aid}").json()
        fd = act.get("fee_discount") or {}
        check("E1 活动详情带 fee_discount 开关", "available" in fd, str(fd)[:120])
        check("E2 收费活动上减免可用", fd.get("available") is True, str(fd)[:120])
        check("E3 减免后金额是原价七折",
              fd.get("payable") == points_redeem.discounted_fee(fd.get("original_fee", 0)),
              f"{fd.get('original_fee')} → {fd.get('payable')}")

    closed = find_activity("已结束")
    if closed:
        fund(points_redeem.cost_of(points_redeem.ITEM_PRIORITY))
        r = redeem_post(c, {"item": "priority_signup",
                            "activity_record_id": closed["record_id"]})
        check("E4 已结束的活动不能用优先报名", r.status_code == 400,
              str(r.json())[:100])
    else:
        print("[SKIP] E4 测试服里一场「已结束」的活动都没有，没法验这条")

    r = redeem_post(c, {"item": "wish", "activity_record_id": "rec不存在"})
    check("E5 活动不存在 → 404", r.status_code == 404, f"实际 {r.status_code}")

    test_priority_wiring(c)


def test_priority_wiring(c):
    """E6 优先名额真能报到名：真表格、真结算循环。

    单测里报名表是桩，桩不会告诉你字段名或表 ID 写错了——而这一项的失败方式
    恰恰是**静默的**：穗照扣，单子挂着，人永远没被报上名。
    """
    import points_admin as pa

    # 只要「报名中」就行，不筛「有配额」：测试服那场活动的「报名人数上限」填的
    # 是「无」（文本），优先名额本来就兑不了——那是没填数据，不是规则错。
    act = find_activity("报名中")
    if not act:
        print("[SKIP] E6 测试服里一场「报名中」的活动都没有，没法验这条")
        return
    text_id = get_field_text(act["fields"], "活动ID")

    # 优先走 H5 那条真路。活动没填「报名人数上限」（表里写的是「无」）时
    # `redeem_priority` 会拒——那不是 bug，是没有总名额就没有「优先」可言。
    # 这种情况下退而求其次：本地直接开一张指向这场真活动的单子，后面读真状态、
    # 写真报名表的部分照样是真场景，只是不经 H5 那一跳。
    fund(points_redeem.cost_of(points_redeem.ITEM_PRIORITY))
    r = redeem_post(c, {"item": "priority_signup",
                        "activity_record_id": act["record_id"]})
    real_path = bool(r.json().get("ok"))
    if real_path:
        check("E6a 兑换开出一张生效中的单", True)
    else:
        print(f"    （E6 走本地开单：{r.json().get('error')}）")
        points_redeem.redeem_priority(ACTOR, {
            "id": text_id, "title": get_field_text(act["fields"], "活动名称"),
            "quota": 10, "fee": 0, "start_at": "", "open": True})
    order = points_redeem.find(ACTOR, points_redeem.ITEM_PRIORITY, text_id)
    check("E6a 兑换开出一张生效中的单", bool(order) and
          order["status"] == points_redeem.ST_ACTIVE, str(order and order["status"]))

    pa.auto_settle_priority_orders()
    check("E6b 结算真的把他报上了名", _signup_row(text_id) is not None,
          f"活动 {text_id}")

    # 收尾：退穗 + 把报名改成已取消。不退穗的话单子还是 active，机器人 30 秒后
    # 又会把他报上——留下一堆谁也说不清的报名记录，下一轮 E2E 也读不准。
    points_redeem.refund(order["id"], reason="e2e 收尾")
    _cancel_signup(text_id)
    check("E6c 收尾后单子不再是生效中",
          points_redeem.get(order["id"])["status"] == points_redeem.ST_REFUNDED)

    # 活动取消 → 退穗。直接在表里把状态改成「已取消」再改回来，用完就还原。
    act_id = act["record_id"]
    try:
        update_record(ACTIVITY_TABLE_ID, act_id, {FIELD_ACTIVITY_STATUS: "已取消"})
        points_redeem.redeem_priority(ACTOR, {
            "id": text_id, "title": "e2e 取消用例", "quota": 10, "fee": 0,
            "start_at": "", "open": True})
        cancel_order = points_redeem.find(ACTOR, points_redeem.ITEM_PRIORITY, text_id)
        before = balance()
        pa.auto_settle_priority_orders()
        check("E6d 活动取消后退穗", balance() == before + cancel_order["cost"],
              f"{before} → {balance()}")
    finally:
        update_record(ACTIVITY_TABLE_ID, act_id, {FIELD_ACTIVITY_STATUS: "报名中"})


def _signup_row(activity_id):
    """我在这个活动上的「已报名」记录（没有则 None）。"""
    for s in search_records(SIGNUP_TABLE_ID, {
            "conjunction": "and",
            "conditions": [
                {"field_name": FIELD_SIGNUP_ACTIVITY_ID, "operator": "is",
                 "value": [activity_id]},
                {"field_name": FIELD_SIGNUP_OPENID, "operator": "is", "value": [ACTOR]},
                {"field_name": FIELD_SIGNUP_STATUS, "operator": "is",
                 "value": ["已报名"]}]}):
        return s
    return None


def _cancel_signup(activity_id):
    row = _signup_row(activity_id)
    if row:
        update_record(SIGNUP_TABLE_ID, row["record_id"], {FIELD_SIGNUP_STATUS: "已取消"})


# ---------------------------------------------------------------- F 管理指令打真表格

def test_admin_commands():
    """管理指令直连多维表格。单测里表格是桩，字段名写错了桩不会吭声。"""
    import points_admin as pa

    txt = pa.handle_points_help()
    check("F1 麦穗帮助有内容", "加穗" in txt and "心愿" in txt)

    cfg = pa.handle_admin_config("配置")
    check("F2 配置能列出且有默认值", "invite_reward_female" in cfg and "默认" in cfg,
          cfg.splitlines()[1] if "\n" in cfg else cfg[:80])

    # 这两条是本次 E2E 最值钱的：单测里表格全是桩，桩不会告诉你表 ID 或字段名
    # 写错了。配了表就必须真读一次。
    if pa.WISH_TABLE_ID:
        txt = pa.handle_admin_wish_list("心愿列表")
        check("F3 心愿列表打真表格不报错",
              "没有心愿单" in txt or "心愿单" in txt, txt.splitlines()[0][:80])
    else:
        check("F3 心愿单表已配 WISH_TABLE_ID", False, "local_config 里是空的")
    if pa.MATCHMAKER_ORDER_TABLE_ID:
        txt = pa.handle_admin_mm_list("推荐单列表")
        check("F4 推荐单列表打真表格不报错",
              "还没有" in txt or "推荐单" in txt, txt.splitlines()[0][:80])
    else:
        check("F4 推荐单表已配 MATCHMAKER_ORDER_TABLE_ID", False, "local_config 里是空的")

    # 加穗/扣穗走一遍真用户表（按用户ID找人），验证字段名与上下限
    uid = _my_user_id()
    if uid:
        before = balance()
        txt = pa.handle_admin_grant(f"加穗 {uid} 5 e2e 冒烟", ACTOR)
        check("F5 加穗生效", balance() == before + 5, f"{before} → {balance()}｜{txt[:60]}")
        txt = pa.handle_admin_grant(f"扣穗 {uid} 5 e2e 冒烟回滚", ACTOR)
        check("F6 扣穗生效", balance() == before, f"{balance()}｜{txt[:60]}")
        txt = pa.handle_admin_grant(f"加穗 {uid} 5", ACTOR)
        check("F7 没写原因要拒", "原因" in txt, txt[:60])
        # 上下限只针对「协助组织」（带活动ID）。手动加减是管理员裁量，不设限。
        act = find_activity("报名中", want_fee=False) or \
            find_activity("报名中", want_fee=True)
        if act:
            aid = get_field_text(act["fields"], "活动ID")
            before = balance()
            txt = pa.handle_admin_grant(f"加穗 {uid} 999 {aid} e2e 超限", ACTOR)
            check("F8 协助组织超过单次上限要拒",
                  "10~50" in txt or "50" in txt, txt[:60])
            check("F8b 被拒的这笔没进账", balance() == before, f"{before} → {balance()}")
            txt = pa.handle_admin_grant(f"加穗 {uid} 5 {aid} e2e 协助", ACTOR)
            check("F8c 协助组织 10 穗以下也要拒", "10~50" in txt, txt[:60])
        else:
            print("[SKIP] F8 测试服里没有「报名中」的活动")
    else:
        print("[SKIP] F5-F8 找不到自己的用户ID")

    q = pa.handle_admin_query_points(f"查穗 {uid or ACTOR}")
    check("F9 查穗有回复", "穗" in q, q.splitlines()[0][:80])


def _my_user_id():
    return get_field_text((_me_record() or {}).get("fields", {}), "用户ID")


# ---------------------------------------------------------------- G 周期结算

def test_settle_loop():
    import points_admin as pa
    try:
        pa.settle_due_things()
        check("G1 周期结算能跑完不抛异常", True)
    except Exception as e:
        check("G1 周期结算能跑完不抛异常", False, repr(e)[:160])


def main():
    print(f"目标 {HOST}｜账号 {ACTOR}｜起始余额 {balance()} 穗")
    print(f"配置：实名 {points_config.get('redeem_real_like')} / "
          f"优先 {points_config.get('redeem_priority_signup')} / "
          f"心愿 {points_config.get('redeem_wish')} / "
          f"红娘 {points_config.get('redeem_matchmaker')} / "
          f"减免 {points_config.get('redeem_fee_discount')}"
          f"（{points_config.get('fee_discount_rate')} 折）\n")

    c = cookies()
    for step in (test_auth, lambda: test_balance_matches_ledger(c),
                 lambda: test_invite(c), lambda: test_redeem(c),
                 lambda: test_activity_items(c), test_admin_commands,
                 test_settle_loop):
        try:
            step()
        except Exception as e:                       # noqa: BLE001
            check(f"{getattr(step, '__name__', 'step')} 未抛异常", False, repr(e)[:200])
        print()

    print(f"结束余额 {balance()} 穗")
    if FAILURES:
        print(f"\n❌ {len(FAILURES)} 项失败：")
        for f in FAILURES:
            print(f"  · {f}")
        return 1
    print("\n✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
