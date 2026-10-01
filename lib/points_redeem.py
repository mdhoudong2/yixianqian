"""兑换：扣穗、开单、以及事后的退还。

## 一笔兑换必须同时落两张表

`ledger` 记「扣了多少穗」，`redemptions` 记「换来了什么」。两张表在同一个事务里
写——分开写的话，中间崩一次就会留下「扣了穗没有单」（用户白花钱）或者
「有单没扣穗」（白拿），两种都不好补，而且没人知道发生过。

所以对外只有 `redeem_*` 这一组入口，内部一律走 `_create`，不要在别处直接
`points.add_entry(-cost, KIND_REDEEM)`。

## 幂等键里带着业务约束

兑换项的幂等键不是随手生成的 UUID，而是把「这件事只该发生一次」写进键里：

    redeem:priority_signup:<oid>:<活动ID>     一人一场只能兑一次
    redeem:wish:<oid>:<活动ID>                同上
    redeem:real_like:<oid>:<序号|请求号>      没有天然唯一性，靠调用方给的请求号
    redeem:matchmaker:<oid>:<序号|请求号>     同上

前两个不依赖调用方自觉：两次请求撞在同一个键上，数据库直接拒掉第二次，
不需要「先查再写」那种在并发下必然漏检的检查。

后两个没有天然的唯一性（同一个人可以兑第二次额外实名喜欢），所以键里放一个
**请求号**：H5 每次点击生成一个随机串带上，网络重试时带的是同一个串，于是
重试不会重复扣分。不传请求号时退化成按序号生成——那样键仍然唯一，但**重试
会变成第二笔**，因为重试拿到的是下一个序号。内部调用方（机器人指令）要么
给请求号，要么接受这个语义。

## lib 不认识「活动」

活动、名额、费用、开始时间都在多维表格里，而本模块（像 points_invite 一样）
不碰飞书。所有活动相关的事实由调用方查好后传进来，形状见 `_activity()`。
这样规则的判定逻辑可以脱离飞书单测，也让「活动数据从哪来」这件事只有一处。
"""
import json
from datetime import datetime, timedelta

from lib import points, points_config, points_db

# 兑换项。字符串会进数据库，改动等于改数据，只能加不能改。
ITEM_REAL_LIKE = "real_like"
ITEM_PRIORITY = "priority_signup"
ITEM_WISH = "wish"
ITEM_MATCHMAKER = "matchmaker"

ITEM_LABELS = {
    ITEM_REAL_LIKE: "额外实名喜欢",
    ITEM_PRIORITY: "活动优先报名",
    ITEM_WISH: "心愿名额",
    ITEM_MATCHMAKER: "红娘人工推荐",
}

# 兑换项 → points_config 里的价格键
ITEM_PRICE_KEYS = {
    ITEM_REAL_LIKE: "redeem_real_like",
    ITEM_PRIORITY: "redeem_priority_signup",
    ITEM_WISH: "redeem_wish",
    ITEM_MATCHMAKER: "redeem_matchmaker",
}

# 兑换单状态。active 是「已兑换、还没了结」，其余都是终态。
ST_ACTIVE = "active"          # 生效中（实名喜欢额度已入账 / 优先名额已占 / 心愿等待中…）
ST_REFUNDED = "refunded"      # 已退穗
ST_ARRANGED = "arranged"      # 心愿已安排（不退穗）
ST_PENDING = "pending"        # 红娘推荐单待处理
ST_RECOMMENDED = "recommended"  # 红娘已推荐，等见面/等完成
ST_DONE = "done"              # 红娘推荐已完成
ST_CANCELLED = "cancelled"    # 用户自己取消（48 小时前，已退穗）

STATUS_LABELS = {
    ST_ACTIVE: "生效中",
    ST_REFUNDED: "已退穗",
    ST_ARRANGED: "已安排",
    ST_PENDING: "待处理",
    ST_RECOMMENDED: "已推荐",
    ST_DONE: "已完成",
    ST_CANCELLED: "已取消",
}

# 还没了结、会占着「同时只能有一张」这类约束的状态。
OPEN_MATCHMAKER_STATUSES = (ST_PENDING, ST_RECOMMENDED)


class RedeemError(Exception):
    """兑换相关的错误基类。"""


class ItemUnavailable(RedeemError):
    """不符合兑换条件（活动没名额、不是收费活动、已经有一张未完成单…）。

    与「余额不足」区分开：这个是「不能兑」，那个是「兑不起」。
    """


class AlreadyRedeemed(RedeemError):
    """这张兑换单已经开过了（幂等键撞上）。"""


def cost_of(item):
    """兑换项的当前价格（穗）。"""
    key = ITEM_PRICE_KEYS.get(item)
    if key is None:
        raise RedeemError(f"未知的兑换项：{item}")
    return int(points_config.get(key))


# ---------------------------------------------------------------- 活动信息

class _activity:
    """调用方传进来的活动事实。

    只读它需要的那几个键，缺了就给一句人话的错——这些值来自多维表格，
    字段读串了（比如把「名额」读成文本）时，报「这场活动没有名额」比
    TypeError 好查得多。
    """

    def __init__(self, raw):
        raw = raw or {}
        self.id = str(raw.get("id") or "").strip()
        self.title = str(raw.get("title") or self.id).strip()
        self.quota = _as_int(raw.get("quota"))
        self.start_at = str(raw.get("start_at") or "").strip()
        self.open = bool(raw.get("open"))
        if not self.id:
            raise ItemUnavailable("没读到这是哪一场活动")


def _as_int(value, default=0):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 核心

def _new_key(c, item, user_oid, ref, request_key):
    """构造幂等键。ref 有值时（活动类）用它，否则靠请求号。

    活动类的键固定成 `redeem:<项>:<人>:<活动>`，这就是「每人每场一次」的落点：
    重复提交撞上同一把键，报「已经开过单」。但**退过穗的不算**——报名写库失败
    会退穗（见 H5 报名路由），那笔退款留着的话，用户在同一场活动上再也用不了
    这一项了，而他的问题只是一次网络抖动。所以按已退款单数往下顺延一个序号。
    """
    if ref:
        base = f"redeem:{item}:{user_oid}:{ref}"
        n = c.execute(
            "SELECT count(*) AS n FROM redemptions WHERE user_oid=? AND item=?"
            " AND ref_id=? AND refund_key IS NOT NULL",
            (user_oid, item, str(ref))).fetchone()["n"]
        return base if not n else f"{base}#{int(n) + 1}"
    token = str(request_key or "").strip()
    if not token:
        # 没有请求号：按 (用户, 兑换项) 下已有单数往下排。键仍然唯一（数只增
        # 不减），但**重试会变成第二笔**——见模块开头那段。
        n = c.execute("SELECT count(*) AS n FROM redemptions WHERE user_oid=? AND item=?",
                      (user_oid, item)).fetchone()["n"]
        token = f"seq{int(n) + 1}"
    return f"redeem:{item}:{user_oid}:{token}"


def _require_balance(c, user_oid, item):
    """余额不够就报「兑不起」。每个兑换项在**业务校验之前**先调它。

    这一读和随后的扣减在同一个 `BEGIN IMMEDIATE` 事务里，中间没有别人能插进来
    改余额。`points.add_entry` 自己也会再拦一次（那是最后一道闸，不依赖调用方
    自觉），这里提前判纯粹是为了把话说准：余额和活动名额同时不满足时，
    「这场活动名额满了」会把用户支去换一场活动，而真正该做的是去攒穗。
    """
    cost = cost_of(item)
    if points.balance(user_oid, conn=c) < cost:
        raise points.InsufficientBalance(
            f"余额不足：{ITEM_LABELS[item]}需要 {cost} 穗")


def _create(c, user_oid, item, *, cost, ref_type, ref_id, params, key, reason, status):
    """在**调用方已经开好的事务里**扣穗 + 开单。"""
    entry_id, _ = points.add_entry(
        user_oid, -cost, points.KIND_REDEEM,
        reason=reason, ref_type=ref_type, ref_id=ref_id,
        operator_oid=user_oid, idempotency_key=key, conn=c)
    cur = c.execute(
        "INSERT INTO redemptions(user_oid, item, cost, status, params, ref_id,"
        " idempotency_key, ledger_id, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (user_oid, item, cost, status, json.dumps(params, ensure_ascii=False),
         str(ref_id or ""), key, entry_id, points_db.now_str()))
    return _row(c, int(cur.lastrowid))


def _row(c, redemption_id):
    row = c.execute("SELECT * FROM redemptions WHERE id=?", (int(redemption_id),)).fetchone()
    return _decorate(dict(row))


def _decorate(d):
    d["item_label"] = ITEM_LABELS.get(d["item"], d["item"])
    d["status_label"] = STATUS_LABELS.get(d["status"], d["status"])
    try:
        d["params"] = json.loads(d.get("params") or "{}")
    except ValueError:
        d["params"] = {}
    return d


def _precheck(c, key, item):
    """幂等键撞上就报「已经兑过了」，并指出上一单长什么样。

    这条要在余额之前判，理由同 lib/points.py 里 add_entry 那段注释：反过来
    的话，重复提交会被报成「余额不足」（钱早扣走了，余额当然不够再扣一次），
    把人支去充值，而真正的问题是重复提交。
    """
    row = c.execute("SELECT * FROM redemptions WHERE idempotency_key=?",
                    (key,)).fetchone()
    if row is not None:
        raise AlreadyRedeemed(
            f"这笔兑换已经开过单了（{ITEM_LABELS.get(item, item)}，"
            f"{STATUS_LABELS.get(row['status'], row['status'])}）")


# ---------------------------------------------------------------- 兑换项

def redeem_real_like(user_oid, *, request_key="", conn=None):
    """额外实名喜欢（20 穗）。不过期，用掉不退穗。

    换来的是一次**永久**实名名额，进入 `lib/quota.real_left` 的永久名额池——
    `real_left` 天生就是「先用每月免费的、再用永久名额」。
    （v7 起邀请奖励改发麦穗、「管理员加赠」字段也已删除，池子只剩兑换这一条来源。）
    """
    with points_db.transaction(conn) as c:
        key = _new_key(c, ITEM_REAL_LIKE, user_oid, "", request_key)
        _precheck(c, key, ITEM_REAL_LIKE)
        _require_balance(c, user_oid, ITEM_REAL_LIKE)
        return _create(c, user_oid, ITEM_REAL_LIKE, cost=cost_of(ITEM_REAL_LIKE),
                       ref_type=ITEM_REAL_LIKE, ref_id="",
                       params={}, key=key, status=ST_ACTIVE,
                       reason="兑换额外实名喜欢")


def redeem_priority(user_oid, activity, *, request_key="", conn=None):
    """优先报名名额（30 穗）。选定一场活动，报名开放时自动报上。

    活动必须「已发布、且（还没开始报名 或 还有名额）」——判定在多维表格那侧
    做完，以 `open` 传进来（lib 不认识活动状态）。

    名额上限按**整场**算：这场活动所有优先名额加起来不超过
    `总名额 × priority_ratio`。默认 30%，所以 10 人的活动只有 3 个优先位。
    """
    act = _activity(activity)
    with points_db.transaction(conn) as c:
        key = _new_key(c, ITEM_PRIORITY, user_oid, act.id, request_key)
        _precheck(c, key, ITEM_PRIORITY)
        _require_balance(c, user_oid, ITEM_PRIORITY)

        if not act.open:
            raise ItemUnavailable(f"「{act.title}」现在不能使用优先名额")
        if act.quota <= 0:
            raise ItemUnavailable(f"「{act.title}」没有名额，优先名额用不了")
        cap = int(act.quota * float(points_config.get("priority_ratio")))
        if cap < 1:
            raise ItemUnavailable(
                f"「{act.title}」共 {act.quota} 个名额，优先名额上限不足 1 个")
        taken = c.execute(
            "SELECT count(*) AS n FROM redemptions WHERE item=? AND ref_id=?"
            " AND status IN (?,?)",
            (ITEM_PRIORITY, act.id, ST_ACTIVE, ST_DONE)).fetchone()["n"]
        if int(taken) >= cap:
            raise ItemUnavailable(
                f"「{act.title}」的优先名额已经满了（{taken}/{cap}）")
        return _create(c, user_oid, ITEM_PRIORITY, cost=cost_of(ITEM_PRIORITY),
                       ref_type=ITEM_PRIORITY, ref_id=act.id,
                       params={"activity_id": act.id, "title": act.title},
                       key=key, status=ST_ACTIVE,
                       reason=f"兑换优先报名名额：{act.title}")


def redeem_wish(user_oid, activity, target_oid, *, signed_up=True, request_key="",
                conn=None):
    """心愿名额（30 穗）。**报名成功后**指定 1 位 App 用户。

    自己报没报名由调用方查好、以 `signed_up` 传进来（本模块不碰飞书）。不在
    这里查是因为它是个业务资格，得排在「表单没填完」「余额不够」后面——顺序
    错了，用户会先被告知「你还没报名」，填好对方名字再点一次才发现钱也不够。

    「对方不被告知」是产品规则，这里只保证不往 `target_oid` 那边写任何东西——
    通知一律不发，跟这里无关。对方到活动开始还没报名就退穗，见
    `wishes_waiting_on` + `refund`。
    """
    act = _activity(activity)
    with points_db.transaction(conn) as c:
        # 表单没填完是**输入问题**，排在余额之前：这不是「兑不起」，是「还没说清
        # 楚要兑什么」，重试一百次也是同一句话。
        target_oid = str(target_oid or "").strip()
        if not target_oid:
            raise ItemUnavailable("请指定一位想认识的人")
        if target_oid == user_oid:
            raise ItemUnavailable("不能把心愿指定给自己")

        key = _new_key(c, ITEM_WISH, user_oid, act.id, request_key)
        _precheck(c, key, ITEM_WISH)
        _require_balance(c, user_oid, ITEM_WISH)
        # 自己不在场的活动，心愿没人能安排，30 穗会白花（对方报了名就不退穗）
        if not signed_up:
            raise ItemUnavailable(f"先报名「{act.title}」，才能在这场活动上许愿")
        return _create(c, user_oid, ITEM_WISH, cost=cost_of(ITEM_WISH),
                       ref_type=ITEM_WISH, ref_id=act.id,
                       params={"activity_id": act.id, "title": act.title,
                               "target_oid": target_oid},
                       key=key, status=ST_ACTIVE,
                       reason=f"兑换心愿名额：{act.title}")


def redeem_matchmaker(user_oid, condition, *, request_key="", conn=None):
    """红娘人工推荐（50 穗）。填一段择偶条件，红娘 14 天内推荐 1–3 人。

    「每人同时只能有 1 张未完成单」——是**同时**，不是一生一次：上一张结单
    （已推荐→已完成）之后可以再兑。
    """
    with points_db.transaction(conn) as c:
        condition = str(condition or "").strip()
        if not condition:
            raise ItemUnavailable("请先写下你的条件和期望")
        limit = int(points_config.get("matchmaker_condition_max_len"))
        if len(condition) > limit:
            raise ItemUnavailable(f"条件最多 {limit} 个字，现在 {len(condition)} 个")

        key = _new_key(c, ITEM_MATCHMAKER, user_oid, "", request_key)
        _precheck(c, key, ITEM_MATCHMAKER)
        _require_balance(c, user_oid, ITEM_MATCHMAKER)
        open_orders = c.execute(
            "SELECT count(*) AS n FROM redemptions WHERE user_oid=? AND item=?"
            " AND status IN (?,?)",
            (user_oid, ITEM_MATCHMAKER, ST_PENDING, ST_RECOMMENDED)).fetchone()["n"]
        if int(open_orders):
            raise ItemUnavailable("你还有一张推荐单没完成，等处理完再兑")
        return _create(c, user_oid, ITEM_MATCHMAKER, cost=cost_of(ITEM_MATCHMAKER),
                       ref_type=ITEM_MATCHMAKER, ref_id="",
                       params={"condition": condition}, key=key, status=ST_PENDING,
                       reason="兑换红娘人工推荐")


# ---------------------------------------------------------------- 退还

def refund(redemption_id, *, reason="", kind=points.KIND_REFUND, operator_oid="",
           status=ST_REFUNDED, conn=None):
    """把一张兑换单的穗退回去。返回 (退穗流水 ID, 余额)；不该退/退过了返回 None。

    退的是**当初扣掉的那个数**（走 `reverse_entry` 反向原始流水），不是按当前
    价格重算——价格后来被运营调过的话，重算会让用户多退或少退。

    幂等靠 `redemptions.refund_key` 的唯一索引 + 账本的幂等键，不是「先看状态
    再退」：后者在并发下会退两次（活动取消和红娘标记无法安排可能同时发生）。
    """
    reason = str(reason or "").strip()
    with points_db.transaction(conn) as c:
        row = c.execute("SELECT * FROM redemptions WHERE id=?",
                        (int(redemption_id),)).fetchone()
        if row is None:
            raise RedeemError(f"兑换单不存在：{redemption_id}")
        if row["status"] not in (ST_ACTIVE, ST_PENDING, ST_RECOMMENDED):
            return None                      # 已经是终态（退过/已完成），不重复处理
        if not row["ledger_id"]:
            raise RedeemError(f"兑换单 {redemption_id} 没有对应的扣穗流水，不能自动退")

        key = f"refund:{int(redemption_id)}"
        entry_id, after = points.reverse_entry(
            int(row["ledger_id"]), kind,
            reason=reason or f"退还{ITEM_LABELS.get(row['item'], row['item'])}",
            operator_oid=operator_oid, idempotency_key=key, conn=c)
        c.execute("UPDATE redemptions SET status=?, refund_key=?, refund_ledger_id=?,"
                  " resolved_at=?, note=? WHERE id=?",
                  (status, key, entry_id, points_db.now_str(), reason,
                   int(redemption_id)))
        return entry_id, after


def mark(redemption_id, status, *, note="", operator_oid="", conn=None):
    """改一张兑换单的状态（不涉及穗）。红娘勾「已推荐/已完成」、心愿「已安排」用。

    想退穗请用 `refund()`——这里只改状态，故意不提供「改状态顺带退穗」的
    便利入口，免得两条路径各自退一次。
    """
    if status not in STATUS_LABELS:
        raise RedeemError(f"未知的兑换单状态：{status}")
    with points_db.transaction(conn) as c:
        row = c.execute("SELECT * FROM redemptions WHERE id=?",
                        (int(redemption_id),)).fetchone()
        if row is None:
            raise RedeemError(f"兑换单不存在：{redemption_id}")
        c.execute("UPDATE redemptions SET status=?, note=?, resolved_at=? WHERE id=?",
                  (status, str(note or row["note"]), points_db.now_str(),
                   int(redemption_id)))
        return _row(c, redemption_id)


# ---------------------------------------------------------------- 查询

def get(redemption_id, conn=None):
    with points_db.transaction(conn) as c:
        row = c.execute("SELECT * FROM redemptions WHERE id=?",
                        (int(redemption_id),)).fetchone()
    return _decorate(dict(row)) if row else None


def list_for(user_oid, limit=100, item=None, conn=None):
    """我的兑换记录，新的在前。"""
    sql = "SELECT * FROM redemptions WHERE user_oid=?"
    args = [user_oid]
    if item:
        sql += " AND item=?"
        args.append(item)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with points_db.transaction(conn) as c:
        rows = c.execute(sql, args).fetchall()
    return [_decorate(dict(r)) for r in rows]


def find(user_oid, item, ref_id, conn=None):
    """这个人在这场活动上兑过这一项没有（H5 画按钮状态用）。"""
    with points_db.transaction(conn) as c:
        row = c.execute("SELECT * FROM redemptions WHERE user_oid=? AND item=?"
                        " AND ref_id=? ORDER BY id DESC LIMIT 1",
                        (user_oid, item, str(ref_id))).fetchone()
    return _decorate(dict(row)) if row else None


def by_activity(activity_id, item=None, statuses=None, conn=None):
    """某场活动名下的兑换单（结算活动取消、心愿到期时用）。"""
    sql = "SELECT * FROM redemptions WHERE ref_id=?"
    args = [str(activity_id)]
    if item:
        sql += " AND item=?"
        args.append(item)
    if statuses:
        sql += " AND status IN (%s)" % ",".join("?" * len(statuses))
        args.extend(statuses)
    sql += " ORDER BY id"
    with points_db.transaction(conn) as c:
        rows = c.execute(sql, args).fetchall()
    return [_decorate(dict(r)) for r in rows]


def all_orders(item, statuses=None, limit=200, conn=None):
    """全站某个兑换项的单子（不分人）。给机器人往多维表格补单据用。

    和 `list_for` 的区别就是不分人——同一件事按人查要遍历全表用户，
    按项查一条 SQL 就够。
    """
    sql = "SELECT * FROM redemptions WHERE item=?"
    args = [item]
    if statuses:
        sql += " AND status IN (%s)" % ",".join("?" * len(statuses))
        args.extend(statuses)
    sql += " ORDER BY id LIMIT ?"
    args.append(int(limit))
    with points_db.transaction(conn) as c:
        rows = c.execute(sql, args).fetchall()
    return [_decorate(dict(r)) for r in rows]


def open_matchmaker_orders(conn=None):
    """还没结单的红娘推荐单（超期自动退穗用）。"""
    with points_db.transaction(conn) as c:
        rows = c.execute(
            "SELECT * FROM redemptions WHERE item=? AND status IN (?,?) ORDER BY id",
            (ITEM_MATCHMAKER, ST_PENDING, ST_RECOMMENDED)).fetchall()
    return [_decorate(dict(r)) for r in rows]


def matchmaker_overdue(now_stamp=None, conn=None):
    """超过处理期限还没推荐出去的红娘推荐单。

    只算 `pending`（红娘一次都没推荐）：`recommended` 说明人已经推过去了，
    14 天的期限管的是「红娘有没有干活」，不是「用户有没有脱单」。
    """
    now_stamp = now_stamp or points_db.now_str()
    days = int(points_config.get("matchmaker_deadline_days"))
    cutoff = _stamp_ago(now_stamp, days)
    with points_db.transaction(conn) as c:
        rows = c.execute(
            "SELECT * FROM redemptions WHERE item=? AND status=? AND created_at <= ?"
            " ORDER BY id", (ITEM_MATCHMAKER, ST_PENDING, cutoff)).fetchall()
    return [_decorate(dict(r)) for r in rows]


def _stamp_ago(now_stamp, days):
    """now_stamp 往前 days 天的时刻。字符串格式与 now_str 一致。"""
    try:
        dt = datetime.strptime(now_stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""
    return (dt - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def extra_real_like_quota(user_oid, conn=None):
    """这个人靠兑换攒下的实名名额数（还没退掉的）。

    接进额度计算的方式：这个数直接加到 `lib/quota.real_left(records, permanent)`
    的 permanent 上。`real_left` 是 `max(0, 1 + permanent − 本月已用)`，所以
    「先用每月免费的、再用兑换来的」是天然成立的，不需要额外的消费顺序逻辑。
    """
    with points_db.transaction(conn) as c:
        row = c.execute("SELECT count(*) AS n FROM redemptions WHERE user_oid=?"
                        " AND item=? AND status=?",
                        (user_oid, ITEM_REAL_LIKE, ST_ACTIVE)).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------- 报名侧

def priority_available(activity, conn=None):
    """这场活动还剩几个优先名额（H5 判断能不能显示兑换按钮）。"""
    act = _activity(activity)
    ratio = float(points_config.get("priority_ratio"))
    cap = int(act.quota * ratio)
    with points_db.transaction(conn) as c:
        taken = c.execute(
            "SELECT count(*) AS n FROM redemptions WHERE item=? AND ref_id=?"
            " AND status IN (?,?)",
            (ITEM_PRIORITY, act.id, ST_ACTIVE, ST_DONE)).fetchone()["n"]
    return max(0, cap - int(taken))


def can_cancel_priority(refund_hours=None, hours_to_start=None):
    """现在取消优先报名还退不退穗。「开始前 48 小时之前主动取消 → 退」。"""
    if hours_to_start is None:
        return False
    limit = (int(points_config.get("priority_cancel_hours"))
             if refund_hours is None else int(refund_hours))
    return float(hours_to_start) >= limit
