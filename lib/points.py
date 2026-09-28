"""麦穗账本核心：写流水、算余额、反向退还/收回。

## 一条规矩

**余额永远由流水算出来，不落库。** `ledger.balance_after` 只是写那条流水时的
审计快照，给人看「当时是多少」用的，任何判断都不许读它。判断一律走
`balance()`，也就是 `SUM(delta)`。这样账本天生自洽：退还、收回都是新增一条
反向流水，原流水一个字不改不删，任何时点的余额都能用「从头加一遍」复现。

## 幂等

「同一奖励不能重复发」不能靠「先查有没有发过、没有再发」——两个进程同时查
会同时查到「没有」，然后各发一次。真正的保证是 `ledger.idempotency_key` 上的
唯一索引：`INSERT … ON CONFLICT DO NOTHING` 之后看 `rowcount`，写进去了才算
真发出去。发奖的调用方都该给出确定的幂等键（比如 `invite_reward:<邀请ID>`），
键里那个 ID 就是「这件事只该发生一次」的那件事。

## 余额不足

在账本这一层就拦住（`InsufficientBalance`），而不是只在兑换接口里判一次。
兑换接口那层判是为了给用户一句人话，这层判是为了不管谁调、怎么调都不会把
余额写成负数——包括以后新加的兑换项、管理员指令、或者补数据的脚本。
"""
import contextlib

from lib import points_db

# 流水类型。写进 ledger.kind，管理员查账和 H5 明细按它取词。
KIND_INVITE = "invite"        # 邀请好友注册（满 7 天确认期后到账）
KIND_ASSIST = "assist"        # 协助组织活动
KIND_ADMIN = "admin"          # 管理员手动加减
KIND_REDEEM = "redeem"        # 兑换扣减
KIND_REFUND = "refund"        # 退还（活动取消、不可抗力、心愿没法安排…）
KIND_CLAWBACK = "clawback"    # 收回（确认期内封禁/注销/虚假账号）

KIND_LABELS = {
    KIND_INVITE: "邀请好友",
    KIND_ASSIST: "协助组织",
    KIND_ADMIN: "管理员调整",
    KIND_REDEEM: "兑换",
    KIND_REFUND: "退还",
    KIND_CLAWBACK: "收回",
}

# 退款/收回的流水，ref_type 指向被反向的那条原始流水。
REF_TYPE_LEDGER = "ledger"


class PointsError(Exception):
    """账本相关的错误基类。调用方要给人话时按子类分别处理。"""


class DuplicateEntry(PointsError):
    """幂等键已存在——这笔奖励/扣减之前已经记过账了。"""


class InsufficientBalance(PointsError):
    """余额不足。"""


@contextlib.contextmanager
def _tx(conn=None):
    with points_db.transaction(conn) as c:
        yield c


def _sum(conn, user_oid):
    row = conn.execute(
        "SELECT COALESCE(SUM(delta), 0) AS bal FROM ledger WHERE user_oid=?",
        (user_oid,)).fetchone()
    return int(row["bal"])


def balance(user_oid, conn=None):
    """余额 = 该用户所有流水的 delta 之和。空账为 0。"""
    if not user_oid:
        raise ValueError("user_oid 不能为空")
    with _tx(conn) as c:
        return _sum(c, user_oid)


def add_entry(user_oid, delta, kind, reason="", ref_type="", ref_id="",
              operator_oid="", idempotency_key="", conn=None):
    """写一条流水，返回 (流水 ID, 写入后的余额)。

    重复的幂等键抛 `DuplicateEntry` 并回滚，余额分毫不动。
    delta 为负且余额不够时抛 `InsufficientBalance`。
    """
    if not user_oid:
        raise ValueError("user_oid 不能为空")
    if kind not in KIND_LABELS:
        raise ValueError(f"未知流水类型：{kind}")
    delta = int(delta)
    # 空串与 NULL 是两回事：唯一索引把 NULL 视为互不相同，所以「没有幂等键」
    # 必须传 NULL，否则第二条无键流水会被当成重复而拒掉。
    key = idempotency_key or None

    with _tx(conn) as c:
        # 幂等键必须在余额之前判。反过来的话，「这笔已经退过了」会被报成
        # 「余额不足」——因为钱早退走并花掉了，余额当然不够再退一次。
        # 后者会把人引去查余额，而真正的问题是调用方重复调用。
        # 这一读在 BEGIN IMMEDIATE 之后，看到的必然是最新状态；
        # 下面 INSERT 的 ON CONFLICT 才是并发下的真正防线，这里只是把
        # 错误信息说准。
        if key is not None:
            dup = c.execute("SELECT 1 FROM ledger WHERE idempotency_key=?",
                            (key,)).fetchone()
            if dup is not None:
                raise DuplicateEntry(f"这笔已经记过账了（幂等键 {idempotency_key}）")
        # BEGIN IMMEDIATE 已经把写锁拿在手里，这里读到的一定是最新余额，
        # 不会出现两个进程各读各的旧值、各扣一次。
        current = _sum(c, user_oid)
        after = current + delta
        if after < 0:
            raise InsufficientBalance(f"余额不足：当前 {current} 穗，本次需要 {-delta} 穗")
        cur = c.execute(
            "INSERT INTO ledger(user_oid, delta, balance_after, kind, reason,"
            " ref_type, ref_id, operator_oid, created_at, idempotency_key)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(idempotency_key) DO NOTHING",
            (user_oid, delta, after, kind, reason, ref_type, ref_id,
             operator_oid, points_db.now_str(), key))
        if cur.rowcount == 0:
            raise DuplicateEntry(
                f"这笔已经记过账了（幂等键 {idempotency_key}），不重复发放")
        return int(cur.lastrowid), after


def grant(user_oid, amount, kind, reason="", ref_type="", ref_id="",
          operator_oid="", idempotency_key="", conn=None):
    """发一笔奖励。已经发过时返回 None，而不是抛异常。

    周期性任务（邀请确认、对账）会一轮轮重扫同一批记录，对它们来说「上次
    已经发过了」是正常路径而不是错误，所以给一个不用写 try/except 的入口。
    """
    try:
        return add_entry(user_oid, abs(int(amount)), kind, reason=reason,
                         ref_type=ref_type, ref_id=ref_id,
                         operator_oid=operator_oid,
                         idempotency_key=idempotency_key, conn=conn)
    except DuplicateEntry:
        return None


def reverse_entry(entry_id, kind, reason="", operator_oid="",
                  idempotency_key="", conn=None):
    """按原始流水新增一条反向流水（退还 / 收回）。

    原流水不改不删。`ref_type='ledger'`、`ref_id=<原始流水 ID>`，这样从任何
    一条反向流水都能追回它抵的是哪一笔。
    """
    with _tx(conn) as c:
        row = c.execute("SELECT * FROM ledger WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            raise PointsError(f"流水不存在：{entry_id}")
        if int(row["delta"]) == 0:
            raise PointsError(f"流水 {entry_id} 金额为 0，无需反向")
        return add_entry(
            row["user_oid"], -int(row["delta"]), kind, reason=reason,
            ref_type=REF_TYPE_LEDGER, ref_id=str(entry_id),
            operator_oid=operator_oid, idempotency_key=idempotency_key, conn=c)


def entries(user_oid, limit=50, offset=0, kinds=None, conn=None):
    """明细列表，新的在前。供 H5「我的麦穗」和 `查穗` 指令用。"""
    if not user_oid:
        raise ValueError("user_oid 不能为空")
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    sql = ("SELECT id, delta, balance_after, kind, reason, ref_type, ref_id,"
           " operator_oid, created_at FROM ledger WHERE user_oid=?")
    args = [user_oid]
    if kinds:
        sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        args.extend(kinds)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    args.extend([limit, offset])
    with _tx(conn) as c:
        rows = c.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["kind_label"] = KIND_LABELS.get(d["kind"], d["kind"])
        out.append(d)
    return out


def summary(user_oid, conn=None):
    """余额 + 累计获得/消耗。用于「我的麦穗」头部三块数字。"""
    with _tx(conn) as c:
        bal = _sum(c, user_oid)
        row = c.execute(
            "SELECT COALESCE(SUM(CASE WHEN delta > 0 THEN delta ELSE 0 END), 0) AS earned,"
            " COALESCE(SUM(CASE WHEN delta < 0 THEN -delta ELSE 0 END), 0) AS spent"
            " FROM ledger WHERE user_oid=?", (user_oid,)).fetchone()
    return {"balance": bal, "earned": int(row["earned"]), "spent": int(row["spent"])}


def has_key(idempotency_key, conn=None):
    """这个幂等键记过账没有。给「先问一句」的展示逻辑用，不作为并发防线。"""
    return entry_id_for(idempotency_key, conn=conn) is not None


def entry_id_for(idempotency_key, conn=None):
    """幂等键对应的流水 ID（没有则 None）。

    周期任务里 `grant()` 返回 None 说明「上次发过了」，但往往还要把那个流水 ID
    记到业务表上（比如 invites.reward_ledger_id）。补一次查比重发一次安全。
    """
    if not idempotency_key:
        return None
    with _tx(conn) as c:
        row = c.execute("SELECT id FROM ledger WHERE idempotency_key=?",
                        (idempotency_key,)).fetchone()
    return int(row["id"]) if row is not None else None
