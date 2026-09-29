"""点赞的机器人侧：每周汇总私聊 + 管理员「点赞统计」。

表的读写都在这一层；规则（有效性、日期桶、周桶、各种占比口径）全在
`lib/praise.py`，这里只做「拉表 → 交给 lib 算 → 写成人话」。分界和
`points_admin.py` 一样：lib 不认识飞书，这里不做算术。

## 为什么点赞没有 H5 那边的 spool

点赞是同步写表的（见 web/backend/app.py）。机器人这边只**读**表，不写——
唯一会改表的地方是每周汇总把「这周发过了」记进自己的小文件，那也不碰飞书。
"""
import time

from clients import get_field_text, get_select_value, log, search_records, send_text_message
from constants import (
    FIELD_ACCOUNT_STATUS,
    FIELD_FEISHU_ID,
    FIELD_GENDER,
    FIELD_LIKE_INITIATOR_OPENID,
    FIELD_LIKE_TARGET_OPENID,
    FIELD_PRAISE_DAY,
    FIELD_PRAISE_INITIATOR_OPENID,
    FIELD_PRAISE_STATUS,
    FIELD_PRAISE_TARGET_OPENID,
    FIELD_PRAISE_WEEK,
    LIKE_TABLE_ID,
    PRAISE_TABLE_ID,
    USER_TABLE_ID,
)
from store import reserve_praise_week, unreserve_praise_week

from lib import praise

# 「账号正常」的判定，与 get_all_users / 用户统计 / 喜欢门禁保持一致：
# 单身 = 在用的账号。已脱单、已退出、待审核、审核不通过都不进分母——
# 把他们算进去，男女两个比例都会偏低，而管理员看不出为什么。
ACTIVE_STATUS = "单身"

STATS_DAYS = 7          # 「每天的点赞总数」看最近几天


def _fu(fields, name):
    """多行/单行文本读出来可能是富文本数组，统一成纯文本。"""
    return get_field_text(fields, name)


def _ready():
    return bool(PRAISE_TABLE_ID)


def _all_praise_rows():
    """点赞表全量。表不大（一人一天最多 10 条），统计和汇总都在本地算。

    量真涨起来再改成分批拉：飞书的 search 有 500 条一页的上限，这里的
    search_records 已经在内部翻页了，但一次全量仍然会随历史线性变慢。
    """
    return search_records(PRAISE_TABLE_ID) or []


# ==================== 每周汇总 ====================

def send_weekly_summaries(now=None):
    """给本周收到过赞的人各发一条私聊。返回 (发了几条, 汇总的是哪个周)。

    **当周没收到赞的人不发**（需求第 6 条），也不逐条推送「谁赞了你」——
    点赞是匿名的，说不出是谁点的，说得出就破了第 3 条。

    发送权靠 reserve_praise_week 按「周 + 人」占位，占到了才发：循环半小时
    一轮、窗口又开着一整周，不占位就会把人刷屏。发失败回滚占位，下轮重试，
    不会漏。
    """
    if not _ready():
        return 0, ""
    week = praise.summary_week(now)
    rows = _all_praise_rows()
    records = [(get_select_value(r.get("fields", {}), FIELD_PRAISE_STATUS),
                _fu(r.get("fields", {}), FIELD_PRAISE_WEEK),
                _fu(r.get("fields", {}), FIELD_PRAISE_TARGET_OPENID))
               for r in rows]
    got = praise.received_by_week(records, week)
    if not got:
        return 0, week

    sent = 0
    for oid, count in got.items():
        if not oid or not reserve_praise_week(week, oid):
            continue
        if send_text_message(oid, praise.weekly_message(count)):
            sent += 1
        else:
            # 发失败就退回占位。退回而不是「下次算了」——用户不会知道
            # 自己本该收到一条，漏了就是永远漏了。
            unreserve_praise_week(week, oid)
    if sent:
        log(f"点赞周汇总（{week}）已发 {sent} 条")
    return sent, week


def praise_weekly_loop(interval=1800):
    """每周汇总循环。

    间隔 30 分钟：要发的窗口是周日 20:00 到下一周，粒度是「周」，跑再勤
    也不会更早——所有人都得等那一周过完。占位文件保证只发一次。
    """
    log(f"点赞周汇总已启动，检查间隔 {interval} 秒")
    while True:
        try:
            if praise.weekly_window_open():
                send_weekly_summaries()
        except Exception as e:
            log(f"点赞周汇总异常: {e}")
        time.sleep(interval)


# ==================== 管理员统计 ====================

def _active_pairs(rows):
    """有效的 (点赞人, 被点赞人)，喂给 lib.praise.converted_ratio。"""
    out = []
    for r in rows:
        f = r.get("fields", {})
        if not praise.is_praise_active(get_select_value(f, FIELD_PRAISE_STATUS)):
            continue
        a = _fu(f, FIELD_PRAISE_INITIATOR_OPENID)
        b = _fu(f, FIELD_PRAISE_TARGET_OPENID)
        if a and b:
            out.append((a, b))
    return out


def _like_pairs():
    """喜欢表里**有过**喜欢的 (发起人, 目标) 集合，不看现在还成不成立。

    「点完赞去表白了，后来取消/被驳回」——那次转化确实发生过，回头抹掉它
    等于把转化率往低了算。所以这里不按状态过滤。
    """
    out = set()
    for r in search_records(LIKE_TABLE_ID) or []:
        f = r.get("fields", {})
        a = _fu(f, FIELD_LIKE_INITIATOR_OPENID)
        b = _fu(f, FIELD_LIKE_TARGET_OPENID)
        if a and b:
            out.add((a, b))
    return out


def _roster():
    """分母：账号正常且填了性别的用户 → [(open_id, 性别)]。"""
    out = []
    for r in search_records(USER_TABLE_ID) or []:
        f = r.get("fields", {})
        if _fu(f, FIELD_ACCOUNT_STATUS) != ACTIVE_STATUS:
            continue
        oid = _fu(f, FIELD_FEISHU_ID)
        if oid:
            out.append((oid, _fu(f, FIELD_GENDER)))
    return out


def _daily_counts(rows, days=None):
    """最近几天的点赞总数 → [(日期, 条数)]，新的在前。

    days 默认在**调用时**取 STATS_DAYS，不写成默认参数：默认参数在函数定义时
    就求值了，那条标题行「最近 N 天」是运行时读的——两处会各说各话。
    """
    days = STATS_DAYS if days is None else days
    tally = {}
    for r in rows:
        f = r.get("fields", {})
        if not praise.is_praise_active(get_select_value(f, FIELD_PRAISE_STATUS)):
            continue
        d = _fu(f, FIELD_PRAISE_DAY)
        if d:
            tally[d] = tally.get(d, 0) + 1
    return sorted(tally.items(), reverse=True)[:days]


def praise_stats_text():
    """管理员发「点赞统计」看到的东西。

    三类数据对应需求第 12 条：每天的总数、点赞转喜欢的比例、收到过赞的
    用户占比（男女分开）。**这份东西只发给管理员**——它逐条列出了谁赞了谁
    的转化，普通人看不到（收到数只有本人可见，见 /api/praise/me）。
    """
    if not _ready():
        return ("点赞统计：\n\n还没配置 PRAISE_TABLE_ID。\n"
                "在服务器跑 python scripts/dev/create_praise_table.py apply，\n"
                "把表 ID 填进 local_config.py 再重启机器人。")

    rows = _all_praise_rows()
    pairs = _active_pairs(rows)
    cancelled = sum(
        1 for r in rows
        if not praise.is_praise_active(
            get_select_value(r.get("fields", {}), FIELD_PRAISE_STATUS)))

    lines = [f"点赞统计：\n\n有效点赞 {len(pairs)} 条，已取消 {cancelled} 条\n"]

    lines.append(f"\n最近 {STATS_DAYS} 天：")
    daily = _daily_counts(rows)
    if not daily:
        lines.append("  还没有点赞")
    for d, n in daily:
        lines.append(f"  {d}  {n} 个")

    hit, total, ratio = praise.converted_ratio(pairs, _like_pairs())
    lines.append(f"\n点赞后转喜欢：{hit}/{total}（{ratio * 100:.1f}%）")
    if not total:
        lines.append("  （还没有点赞，算不出比例）")

    share = praise.received_share(_roster(), {b for _a, b in pairs})
    lines.append("\n收到过赞的用户：")
    for gender in praise.GENDERS:
        s = share.get(gender)
        if not s:
            lines.append(f"  {gender}：没有账号正常的{gender}用户")
            continue
        lines.append(f"  {gender}：{s['received']}/{s['total']}"
                     f"（{s['ratio'] * 100:.1f}%）")
    for gender, s in share.items():
        if gender not in praise.GENDERS:
            lines.append(f"  性别写成「{gender}」的：{s['received']}/{s['total']}"
                         "（这个值不该出现，去用户表看看）")
    return "\n".join(lines)
