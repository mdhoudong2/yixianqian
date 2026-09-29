#!/usr/bin/env python3
"""点赞端到端验证（在测试服跑，会往真表格里写记录，跑完清干净）。

跑法（要用 H5 的 venv：签 session cookie 依赖 itsdangerous，bot 的 venv 里没有）：
    cd /opt/yixianqian-test && YIXIANQIAN_ENV=test \\
        /opt/yixianqian-test/web/backend/venv/bin/python \\
        /opt/yixianqian-test/scripts/dev/e2e_praise.py

单测（tests/test_praise*.py）里飞书全是桩。桩不会告诉你字段名打错了、选项名对不上、
也发现不了「表 ID 没配上」。这里考的就是那一层：H5 的接口 → 真的写进测试服的点赞表
→ 机器人侧按真表读出来的东西对不对。

会改动测试服的东西（跑完都复原）：
  · 往点赞表写几条记录（开头和结尾各清一次 ACTOR 名下的有效赞）
  · 临时把 daily_praise_limit 改小验额度上限，跑完按原样恢复
**不会真的发飞书私聊**：周汇总那一步把发送和占位都换成内存版，只验「该发给谁、
内容对不对」——真发的话，跑一次 E2E 就给测试服的真实账号推一条消息，那不该由测试决定。

退出码：0=全部通过；1=有失败。
"""
import os
import sys
import time
from datetime import timedelta

# 顺序有讲究：bot/ 必须最先被找到。bot 和 web/backend 各有一份 local_config.py，
# H5 那份没有 PRAISE_TABLE_ID——先找到它，整份脚本都会以为「还没配置」。
for _p in ("/opt/yixianqian-test", "/opt/yixianqian-test/scripts/dev",
           "/opt/yixianqian-test/bot"):
    sys.path.insert(0, _p)
del _p

import local_config as lc  # noqa: E402
import requests  # noqa: E402
from _prod_guard import guard  # noqa: E402
from clients import bitable, get_field_text, get_select_value  # noqa: E402
from constants import (  # noqa: E402
    FIELD_ACCOUNT_STATUS,
    FIELD_FEISHU_ID,
    FIELD_GENDER,
    FIELD_PRAISE_DAY,
    FIELD_PRAISE_INITIATOR_OPENID,
    FIELD_PRAISE_OBJECT_TYPE,
    FIELD_PRAISE_STATUS,
    FIELD_PRAISE_TARGET_OPENID,
    FIELD_PRAISE_WEEK,
    PRAISE_TABLE_ID,
    USER_TABLE_ID,
)
from itsdangerous import URLSafeTimedSerializer  # noqa: E402

import praise_admin  # noqa: E402

from lib import points_config, praise, quota  # noqa: E402

guard(os.path.basename(__file__))

HOST = "https://testapp.nantou.love"
# 测试服里一个正常的单身账号（和 e2e_points / e2e_hearts 用的同一个）。
ACTOR = "ou_ec5d70f07daf238e81ac466a1c553aae"

WRITE_HEADERS = {"X-Requested-With": "XMLHttpRequest"}
FAILURES = []
# H5 是**另一个进程**，配置在它那边有 30 秒内存缓存（lib/points_config.CACHE_TTL_SECONDS）。
# 所以改完配置不能立刻打接口——那是脚本没等，不是接口坏了。
CONFIG_WAIT = 45


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


def delete(c, path):
    return requests.delete(f"{HOST}{path}", cookies=c, timeout=20,
                           headers=WRITE_HEADERS)


# ---------------------------------------------------------------- 表格直读

def _query(table_id, conditions=None):
    """查表，**失败要炸**，不能当成「没查到」。

    clients.search_records 把查询失败转成 []，那是给线上用的（宁可少显示也别报错）。
    在 E2E 里这个转换会让「跑完不留有效赞」这类断言因为一次网络抖动而**假通过**，
    所以这里直接走底层，拿 None 当失败。"""
    rows = bitable.search_records(table_id, conditions)
    if rows is None:
        raise RuntimeError(f"查表失败（table={table_id}）——飞书接口报错或凭证失效，"
                           "下面的结论一律不可信")
    return rows


def my_rows():
    """ACTOR 在点赞表里的全部记录（原始，不筛状态）。"""
    return _query(PRAISE_TABLE_ID, [
        {"field_name": FIELD_PRAISE_INITIATOR_OPENID, "operator": "is",
         "value": [ACTOR]}])


def active_targets():
    out = set()
    for r in my_rows():
        f = r.get("fields", {})
        if praise.is_praise_active(get_select_value(f, FIELD_PRAISE_STATUS)):
            oid = get_field_text(f, FIELD_PRAISE_TARGET_OPENID)
            if oid:
                out.add(oid)
    return sorted(out)


def clear_my_praises(label):
    """取消 ACTOR 名下所有有效赞。

    开头和结尾各来一次：开头是为了让「今日剩余 = 满额」这个前提成立（跑第二遍时
    上一遍留下的赞会占额度，断言就时灵时不灵），结尾是为了不给测试服留垃圾。
    """
    n = 0
    for r in my_rows():
        f = r.get("fields", {})
        if not praise.is_praise_active(get_select_value(f, FIELD_PRAISE_STATUS)):
            continue
        if bitable.update_record(PRAISE_TABLE_ID, r.get("record_id"),
                                 {FIELD_PRAISE_STATUS: praise.PRAISE_STATUS_CANCELLED}):
            n += 1
    if n:
        print(f"    （{label}：取消了 {n} 条遗留的赞）")
    return n


def _users():
    return _query(USER_TABLE_ID)


def _gender_of(rows, oid):
    for r in rows:
        f = r.get("fields", {})
        if get_field_text(f, FIELD_FEISHU_ID) == oid:
            return get_select_value(f, FIELD_GENDER)
    return ""


def _candidates(rows, gender):
    """账号正常、填了指定性别的用户 open_id（不含 ACTOR）。"""
    out = []
    for r in rows:
        f = r.get("fields", {})
        oid = get_field_text(f, FIELD_FEISHU_ID)
        if (oid and oid != ACTOR
                and get_field_text(f, FIELD_ACCOUNT_STATUS) == "单身"
                and get_select_value(f, FIELD_GENDER) == gender):
            out.append(oid)
    return out


# ---------------------------------------------------------------- A 鉴权

def test_auth():
    anon = requests.get(f"{HOST}/api/praise/me", timeout=20)
    check("A1 未登录读点赞 → 401", anon.status_code == 401, f"实际 {anon.status_code}")

    # 写接口必须带自定义头（跨站表单发不出这个头）。前端一直带着，脚本也得带。
    r = requests.post(f"{HOST}/api/praise", cookies=cookies(), timeout=20,
                      json={"target_openid": "ou_x"})
    check("A2 缺 CSRF 头的写接口 → 403", r.status_code == 403, f"实际 {r.status_code}")


# ---------------------------------------------------------------- B 形状

def test_shape(c):
    r = get(c, "/api/praise/me")
    if not check("B1 /api/praise/me 能读", r.status_code == 200,
                 f"实际 {r.status_code} {r.text[:120]}"):
        return
    j = r.json()
    check("B2 形状齐全",
          all(k in j for k in ("limit", "left", "used", "praised", "received_week")),
          str(sorted(j))[:120])
    check("B3 praised 是「我点过谁」的列表",
          isinstance(j["praised"], list), repr(j["praised"])[:80])
    check("B4 起始是满额度", j["left"] == j["limit"], f"left={j['left']} limit={j['limit']}")


# ---------------------------------------------------------------- C 点赞落表

def test_praise_lands_in_the_table(c, targets):
    before = get(c, "/api/praise/me").json()
    r = post(c, "/api/praise", {"target_openid": targets[0]})
    if not check("C1 点赞 → 200", r.status_code == 200, f"实际 {r.status_code} {r.text[:160]}"):
        return
    j = r.json()
    check("C2 剩余次数减一", j["left"] == before["left"] - 1,
          f"{before['left']} → {j['left']}")
    check("C3 praised 里出现了对方", targets[0] in j["praised"], str(j["praised"])[:80])

    # 这一步才是这个脚本存在的意义：接口说成功了，表里真的有一条吗？
    rows = [x for x in my_rows()
            if get_field_text(x.get("fields", {}), FIELD_PRAISE_TARGET_OPENID) == targets[0]
            and praise.is_praise_active(
                get_select_value(x.get("fields", {}), FIELD_PRAISE_STATUS))]
    if not check("C4 表里真的多了一条有效赞", len(rows) == 1, f"查到 {len(rows)} 条"):
        return
    f = rows[0]["fields"]
    check("C5 状态 = 有效",
          get_select_value(f, FIELD_PRAISE_STATUS) == praise.PRAISE_STATUS_ACTIVE,
          repr(get_select_value(f, FIELD_PRAISE_STATUS)))
    # 归属日期/周是每日额度和周汇总的唯一依据，空了下游全是静默错
    check("C6 归属日期 = 今天", get_field_text(f, FIELD_PRAISE_DAY) == praise.day_key(),
          f"实际 {get_field_text(f, FIELD_PRAISE_DAY)!r}")
    check("C7 归属周 = 本周", get_field_text(f, FIELD_PRAISE_WEEK) == praise.week_key(),
          f"实际 {get_field_text(f, FIELD_PRAISE_WEEK)!r}")
    check("C8 点赞对象类型 = 用户资料",
          get_select_value(f, FIELD_PRAISE_OBJECT_TYPE) == praise.OBJECT_USER_PROFILE,
          repr(get_select_value(f, FIELD_PRAISE_OBJECT_TYPE)))


def test_duplicate_is_idempotent(c, targets):
    before = len(my_rows())
    r = post(c, "/api/praise", {"target_openid": targets[0]})
    check("C9 重复点赞 → 成功且 already",
          r.status_code == 200 and r.json().get("already") is True,
          f"实际 {r.status_code} {r.text[:120]}")
    check("C10 重复点赞没有多写一条", len(my_rows()) == before,
          f"{before} → {len(my_rows())}")


# ---------------------------------------------------------------- D 门禁

def test_gates(c, rows, my_gender):
    r = post(c, "/api/praise", {"target_openid": ACTOR})
    check("D1 不能给自己点赞",
          r.status_code == 400 and "自己" in r.json().get("error", ""),
          f"实际 {r.status_code} {r.text[:100]}")

    same = _candidates(rows, my_gender)
    if same:
        r = post(c, "/api/praise", {"target_openid": same[0]})
        check("D2 不能给同性点赞",
              r.status_code == 400 and "异性" in r.json().get("error", ""),
              f"实际 {r.status_code} {r.text[:100]}")
    else:
        print("[SKIP] D2 测试服里没有可用的同性账号")

    r = post(c, "/api/praise", {"target_openid": "ou_没这个人"})
    check("D3 不存在的用户 → 404", r.status_code == 404,
          f"实际 {r.status_code} {r.text[:100]}")


# ---------------------------------------------------------------- E 取消

def test_cancel(c, targets):
    r = delete(c, f"/api/praise/{targets[0]}")
    if not check("E1 取消点赞 → 200", r.status_code == 200,
                 f"实际 {r.status_code} {r.text[:160]}"):
        return
    j = r.json()
    check("E2 取消后剩余次数退回", j["left"] == j["limit"],
          f"left={j['left']} limit={j['limit']}")
    check("E3 取消后 praised 里没有了", targets[0] not in j["praised"], str(j["praised"])[:80])

    # 取消是改状态，不是删记录——删了就没有「点过又反悔」这件事了
    rows = [x for x in my_rows()
            if get_field_text(x.get("fields", {}), FIELD_PRAISE_TARGET_OPENID) == targets[0]]
    check("E4 记录还在，状态变成已取消",
          bool(rows) and get_select_value(rows[-1]["fields"], FIELD_PRAISE_STATUS)
          == praise.PRAISE_STATUS_CANCELLED,
          f"查到 {len(rows)} 条")

    r = delete(c, f"/api/praise/{targets[0]}")
    check("E5 重复取消也是成功（不弹红提示）",
          r.status_code == 200 and r.json().get("already") is True,
          f"实际 {r.status_code} {r.text[:100]}")


# ---------------------------------------------------------------- F 每日上限

def test_daily_limit(c, targets):
    """上限是配置项，临时改小验一次，**跑完按原样恢复**。

    恢复要分清「本来就是覆盖值」和「本来用默认」：原来有覆盖就写回原值，
    原来没有就删掉覆盖行。一律 reset 会把管理员改过的值抹掉。
    """
    old = points_config.get("daily_praise_limit")
    was_overridden = points_config.is_overridden("daily_praise_limit")
    try:
        points_config.set_value("daily_praise_limit", 1)
        limit, waited = None, 0
        while waited < CONFIG_WAIT:
            limit = get(c, "/api/praise/me").json().get("limit")
            if limit == 1:
                break
            time.sleep(3)
            waited += 3
        # 等到就等于顺带验了「改配置 → H5 生效」这条链路；一直不等就是缓存没过去，
        # 或者 H5 读配置的路径断了。两种情况都该红。
        check("F1 改配置后 H5 会生效", limit == 1, f"等了 {waited}s，limit={limit}")

        r1 = post(c, "/api/praise", {"target_openid": targets[0]})
        r2 = post(c, "/api/praise", {"target_openid": targets[1]})
        check("F2 上限内的赞成功", r1.status_code == 200,
              f"实际 {r1.status_code} {r1.text[:100]}")
        check("F3 超过上限的被拒",
              r2.status_code == 400 and "点满" in r2.json().get("error", ""),
              f"实际 {r2.status_code} {r2.text[:120]}")
        # 拒了就是拒了：返回 400 还偷偷写了一条进去，是最坏的一种错
        check("F4 被拒的那个赞确实没落表",
              targets[1] not in active_targets(),
              str([t[-6:] for t in active_targets()]))
    finally:
        if was_overridden:
            points_config.set_value("daily_praise_limit", old)
        else:
            points_config.reset("daily_praise_limit")
        check("F6 上限已恢复原值",
              points_config.get("daily_praise_limit") == old,
              f"{points_config.get('daily_praise_limit')} != {old}")
    clear_my_praises("F 收尾")


# ---------------------------------------------------------------- G 公开资料页

def test_public_profile(c, targets):
    r = get(c, f"/api/users/{targets[0]}/public")
    if not check("G1 能读他人资料页", r.status_code == 200, f"实际 {r.status_code}"):
        return
    j = r.json()
    check("G2 没点过赞时 praised=false", j.get("praised") is False, repr(j.get("praised")))

    if check("G3 先点上赞", post(c, "/api/praise",
                                {"target_openid": targets[0]}).status_code == 200):
        j = get(c, f"/api/users/{targets[0]}/public").json()
        check("G4 点过赞之后 praised=true", j.get("praised") is True, repr(j.get("praised")))
        # 需求第 4 条：收到几个赞只有本人可见。公开接口里一个数字都不能有。
        leaked = [k for k in ("received", "received_week", "praise_count", "praised_count")
                  if k in j]
        check("G5 公开资料页不含任何收到数", not leaked, str(leaked))


# ---------------------------------------------------------------- H 周汇总（不发消息）

class _dry_run:
    """把发送和占位都换成内存版跑一段：不真的发私聊，也不碰占位文件。

    真发的话，跑一次 E2E 就给测试服的真实账号推一条消息；占位文件还要真写，
    把人家这周的汇总资格占掉。两样都不该由测试决定。
    """
    def __enter__(self):
        self.captured, self.reserved = [], set()
        self.real = (praise_admin.send_text_message, praise_admin.reserve_praise_week,
                     praise_admin.unreserve_praise_week)
        praise_admin.send_text_message = \
            lambda oid, text: self.captured.append((oid, text)) or True
        praise_admin.reserve_praise_week = \
            lambda wk, oid: (wk, oid) not in self.reserved and not self.reserved.add((wk, oid))
        praise_admin.unreserve_praise_week = lambda wk, oid: self.reserved.discard((wk, oid))
        return self

    def __exit__(self, *exc):
        (praise_admin.send_text_message, praise_admin.reserve_praise_week,
         praise_admin.unreserve_praise_week) = self.real
        return False


def test_weekly_dry_run(targets):
    """验「该发给谁、个数对不对」，以及**同一个周不会发第二遍**。

    时刻要显式传：`summary_week()` 在周日 20:00 之前算的是**上一个周**，直接
    `send_weekly_summaries()` 的话（表里那条赞记在本周）周一跑发 0 条、周日夜跑
    发 1 条——测试会不会过取决于今天星期几。传本周日 21:00 进去，两种日子都一样。
    """
    now = quota.now()
    at = (now + timedelta(days=6 - now.weekday())).replace(
        hour=21, minute=0, second=0, microsecond=0)
    if not check("H0 构造的时刻仍在本周内", praise.week_key(at) == praise.week_key(now),
                 f"{praise.week_key(at)} vs {praise.week_key(now)}"):
        return

    # 期望个数从**真表**算，不写死 1：测试服上别人可能也赞过这个账号，
    # 写死的话这条断言会随别人手滑而红。
    rows = _query(PRAISE_TABLE_ID)
    want = praise.received_by_week(
        [(get_select_value(r.get("fields", {}), FIELD_PRAISE_STATUS),
          get_field_text(r.get("fields", {}), FIELD_PRAISE_WEEK),
          get_field_text(r.get("fields", {}), FIELD_PRAISE_TARGET_OPENID))
         for r in rows], praise.week_key(now)).get(targets[0], 0)

    with _dry_run() as run:
        praise_admin.send_weekly_summaries(at)
        hit = [t for oid, t in run.captured if oid == targets[0]]
        check("H1 被赞的人会收到周汇总", bool(hit),
              f"共 {len(run.captured)} 条；{targets[0][-6:]} 收到 {len(hit)} 条")
        if hit:
            check("H2 正文报的个数和真表一致", f"收到 {want} 个赞" in hit[0],
                  f"表里 {want} 个，消息「{hit[0][:40].replace(chr(10), ' ')}」")
            check("H3 正文不出现是谁点的", targets[0] not in hit[0], hit[0][:60])

        # 循环半小时一轮，占位没生效就会把人刷屏——这条比 H1 还要紧
        before = len(run.captured)
        praise_admin.send_weekly_summaries(at)
        check("H4 同一个周不会发第二遍", len(run.captured) == before,
              f"{before} → {len(run.captured)}")


# ---------------------------------------------------------------- I 管理统计

def test_admin_stats(targets):
    try:
        text = praise_admin.praise_stats_text()
    except Exception as e:                             # noqa: BLE001
        check("I1 点赞统计能跑完不抛异常", False, repr(e)[:200])
        return
    check("I1 点赞统计能跑完不抛异常", True)
    check("I2 三类数据都在",
          all(k in text for k in ("有效点赞", "点赞后转喜欢", "收到过赞的用户")),
          text[:160].replace("\n", " / "))
    # 「男」「女」不是用户表里的选项名。分组照抄了它们的话，正式那两行会变成
    # 「没有账号正常的X用户」，所有人都落进兜底那一类——数字看着有，其实全错。
    check("I3 男女两行用的都是真选项名",
          "性别写成" not in text,
          text[text.find("收到过赞的用户"):].replace("\n", " / ")[:160])
    check("I4 男生那行有真实的分母",
          f"{praise.GENDER_MALE}：" in text and "没有账号正常" not in text,
          text[text.find("收到过赞的用户"):].replace("\n", " / ")[:160])
    # 统计给的是汇总数，不是名单。泄露了 open_id 就等于点名谁赞了谁。
    leaked = [oid for oid in targets if oid in text]
    check("I5 统计里不出现任何 open_id", not leaked, str(leaked))


# ---------------------------------------------------------------- J 清理

def test_cleanup(c):
    clear_my_praises("收尾")
    check("J1 跑完不留有效赞", not active_targets(), str([t[-6:] for t in active_targets()]))
    j = get(c, "/api/praise/me").json()
    check("J2 额度回到满", j["left"] == j["limit"], f"left={j['left']} limit={j['limit']}")
    check("J3 praised 清空", j["praised"] == [], str(j["praised"])[:80])


def main():
    if not PRAISE_TABLE_ID:
        print("⛔ 测试服还没配 PRAISE_TABLE_ID。先跑：\n"
              "   YIXIANQIAN_ENV=test python scripts/dev/create_praise_table.py apply\n"
              "   再把表 ID 填进 /opt/yixianqian-test/{bot,web/backend}/local_config.py")
        return 1

    rows = _users()
    my_gender = _gender_of(rows, ACTOR)
    targets = _candidates(rows, praise.GENDER_FEMALE if my_gender == praise.GENDER_MALE
                          else praise.GENDER_MALE)
    print(f"目标 {HOST}｜账号 {ACTOR[-6:]}（{my_gender}）｜点赞表 {PRAISE_TABLE_ID}"
          f"｜对象 {[t[-6:] for t in targets[:3]]}")
    print(f"用户表 {len(rows)} 人\n")
    if not check("C0 找到两个异性测试对象", len(targets) >= 2, str(targets)):
        return 1

    clear_my_praises("开局")
    c = cookies()
    steps = (
        ("A 鉴权", test_auth),
        ("B 形状", lambda: test_shape(c)),
        ("C 点赞落表", lambda: test_praise_lands_in_the_table(c, targets)),
        ("C 重复点赞", lambda: test_duplicate_is_idempotent(c, targets)),
        ("D 门禁", lambda: test_gates(c, rows, my_gender)),
        ("E 取消", lambda: test_cancel(c, targets)),
        ("F 每日上限", lambda: test_daily_limit(c, targets)),
        ("G 公开资料页", lambda: test_public_profile(c, targets)),
        ("H 周汇总（不发消息）", lambda: test_weekly_dry_run(targets)),
        ("I 管理统计", lambda: test_admin_stats(targets)),
        ("J 清理", lambda: test_cleanup(c)),
    )
    for name, step in steps:
        try:
            step()
        except Exception as e:                         # noqa: BLE001
            check(f"{name} 未抛异常", False, repr(e)[:200])
        print()

    if FAILURES:
        print(f"❌ {len(FAILURES)} 项失败：")
        for f in FAILURES:
            print(f"  · {f}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
