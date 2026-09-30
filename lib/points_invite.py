"""邀请关系：被邀请人注册时带「邀请人ID」→ 7 天确认期 → 到账 / 收回。

## 为什么奖励要等满 7 天

需求：「被邀请人完成资料后 7 天内，如果账号被封禁、注销或被认定为虚假账号，
已发放的邀请奖励自动收回」。与其先发再收，不如**确认期里根本不入账**：
奖励只在 finalized 那一刻才写流水。好处是「待确认的麦穗不能用于兑换」不需要
任何额外拦截——余额里压根没有它；而「收回」也退化成改一个状态，不用去追
一笔可能已经被花掉的流水（余额不够时 reverse_entry 是会失败的，见 lib/points.py）。

## 确认期从哪一刻起算

从被邀请人的**账号状态变为「单身」**那一刻。这条是跟产品确认过的：
- 「完成注册」不够——表单交上来时人还是「待审核」，可能被判「审核不通过」；
- 「单身」意味着资料填完了、审核也过了，是真的一个人来了。
与需求原文「完成注册并填完资料必填项，只完成注册不算」一致。

`confirm_due_at` 在开始倒计时那一刻就写死，之后改配置不影响已经在跑的确认期
（否则运营把 7 天改成 3 天，会有一批人凭空提前到期）。

## 收回只有「封禁」一个触发点

需求写的是「封禁、注销或被认定为虚假账号」，但现有账号状态里
（待审核 / 单身 / 已脱单 / 村情六处 / 审核不通过 / 已退出）**没有封禁也没有注销**，
而字面上最像的「已退出」在产品里的意思是「你已暂时退出相亲市场，如需恢复请
联系管理员」——自愿暂停、可恢复的正常状态。拿它当注销会误伤：一个只是暂别
相亲市场的人，他邀请好友拿到的麦穗会被凭空收回，而且不报错。

所以这里只认一个明确的值 `STATUS_BANNED`（新增的「封禁」选项，管理员手动标记）。
要扩大触发面就改 `CLAWBACK_STATUSES`，别在别处另立一套判断。
"""
import re
from datetime import timedelta

from lib import points, points_config, points_db

# 邀请关系状态
STATUS_PENDING = "pending"            # 手机号已登记，人还没到「单身」
STATUS_CONFIRMED = "confirmed"        # 已「单身」，确认期倒计时中（**不发穗**）
STATUS_FINALIZED = "finalized"        # 期满且账号正常，麦穗已到账
STATUS_CLAWED_BACK = "clawed_back"    # 期内被封禁，不发了
STATUS_REJECTED = "rejected"          # 登记时就判定了不成立（自己邀请自己等）

STATUS_LABELS = {
    STATUS_PENDING: "待确认",
    STATUS_CONFIRMED: "待确认",
    STATUS_FINALIZED: "已到账",
    STATUS_CLAWED_BACK: "已收回",
    STATUS_REJECTED: "无效邀请",
}

# 「封禁」——账号状态里的一个新选项，必须与多维表格的选项名逐字一致。
# 改表里的选项文案就要改这里，两处不一致会让收回**静默失效**（永远判不出来）。
STATUS_BANNED = "封禁"
# 触发收回的账号状态。故意做成元组而不是单个值：以后要加「已注销」只改这一行。
CLAWBACK_STATUSES = (STATUS_BANNED,)

GENDER_MALE = "男性"
GENDER_FEMALE = "女性"

# 邀请奖励的幂等键。一条邀请记录只该发一次，ID 就是那件事。
def _reward_key(invite_id):
    return f"invite_reward:{invite_id}"


class InviteError(Exception):
    """邀请关系相关的错误基类。"""


def normalize_phone(raw):
    """手机号归一化：去掉空格/横杠/国家码，只留 11 位。

    匹配「这条邀请登记的是不是眼前这个注册的人」全靠它。不归一化的话，
    登记时的 `138 0000 0000` 和表单里的 `13800000000` 会被当成两个号码，
    邀请关系永远关联不上，而且不报任何错。

    不是 11 位手机号的一律返回空串（含座机）。严格是故意的：这个字段的
    主要失败模式是**打错一位**，宽松匹配只会让错号码静默匹配到另一个人的
    号码上——那比丢掉一条邀请难查得多。
    """
    s = re.sub(r"\D", "", str(raw or ""))
    if not s:
        return ""
    if s.startswith("0086"):
        s = s[4:]
    elif s.startswith("86") and len(s) > 11:
        s = s[2:]
    if len(s) != 11 or not s.startswith("1"):
        return ""
    return s


def reward_amount(gender):
    """邀请奖励穗数：女生 20、男生 15。

    性别读不出来时按**低的**那个发（男生档）。宁可少发 5 穗也不要多发：
    多发的穗可能已经被兑换掉，之后想追回时余额不足，reverse_entry 会失败。
    正常情况下读不出来不会发生——「性别」是注册表单的必填项。
    """
    key = "invite_reward_male" if gender != GENDER_FEMALE else "invite_reward_female"
    return int(points_config.get(key))


# ---------------------------------------------------------------- 历史参与名单

def is_historical_participant(phone, conn=None):
    """这个手机号是不是参加过以往活动的人。"""
    phone = normalize_phone(phone)
    if not phone:
        return False
    with points_db.transaction(conn) as c:
        row = c.execute("SELECT 1 FROM historical_participants WHERE phone=?",
                        (phone,)).fetchone()
    return row is not None


def add_historical_participants(rows, source="import", conn=None):
    """导入历史参与名单。rows 是 (手机号, 姓名) 的可迭代。

    返回 (新增数, 跳过数)。重跑幂等：同一个手机号不会重复计。
    号码不合法的记进跳过数而不是抛错——导入几千行时中途炸掉，人不知道该从
    哪一行接着来。
    """
    added = skipped = 0
    stamp = points_db.now_str()
    with points_db.transaction(conn) as c:
        for row in rows:
            phone_raw, name = (list(row) + ["", ""])[:2]
            phone = normalize_phone(phone_raw)
            if not phone:
                skipped += 1
                continue
            cur = c.execute(
                "INSERT INTO historical_participants(phone, name, source, added_at)"
                " VALUES(?,?,?,?) ON CONFLICT(phone) DO NOTHING",
                (phone, str(name or "").strip(), source, stamp))
            if cur.rowcount:
                added += 1
            else:
                skipped += 1
    return added, skipped


def historical_count(conn=None):
    with points_db.transaction(conn) as c:
        return int(c.execute("SELECT count(*) AS n FROM historical_participants").fetchone()["n"])


# ---------------------------------------------------------------- 登记

def record_from_form(invitee_oid, phone, gender, inviter_oid, source="form", conn=None):
    """被邀请人是**自己带着「邀请人ID」注册进来**的（注册表单那条路径）。

    这条路径不做新人判定：人正站在注册表单里，之前当然不是用户。但「一人只能
    有一个邀请人」仍然要守——如果这个手机号已经绑定了别的邀请人，以**先绑定的
    那位**为准（那个人才是真的把他拉来的人），并在 note 里留一笔备查。

    返回 (邀请 ID, 状态)；手机号不合法或邀请人ID 缺失时 (None, None)。
    """
    phone = normalize_phone(phone)
    if not phone or not inviter_oid or not invitee_oid:
        return None, None
    stamp = points_db.now_str()
    with points_db.transaction(conn) as c:
        row = c.execute("SELECT * FROM invites WHERE invitee_phone=?", (phone,)).fetchone()
        if row is not None:
            if row["inviter_oid"] == inviter_oid or row["status"] != STATUS_PENDING:
                return int(row["id"]), row["status"]
            c.execute("UPDATE invites SET note=?, updated_at=? WHERE id=?",
                      (f"注册时填的邀请人 {inviter_oid} 与已绑定的邀请人不一致，以先绑定的为准",
                       stamp, int(row["id"])))
            return int(row["id"]), row["status"]
        cur = c.execute(
            "INSERT INTO invites(inviter_oid, invitee_phone, invitee_oid, status, gender,"
            " source, created_at, updated_at, bound_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (inviter_oid, phone, invitee_oid, STATUS_PENDING, gender or "",
             source or "form", stamp, stamp, stamp))
        return int(cur.lastrowid), STATUS_PENDING


# ---------------------------------------------------------------- 确认期

def start_confirm_window(invitee_oid, phone="", gender="", conn=None):
    """被邀请人的账号状态变成「单身」了：开始 7 天确认期。

    返回 (邀请 ID, 状态)。没有邀请关系、或状态已经不是 pending 时返回现有状态。
    """
    if not invitee_oid:
        return None, None
    phone = normalize_phone(phone)
    with points_db.transaction(conn) as c:
        row = None
        if phone:
            row = c.execute("SELECT * FROM invites WHERE invitee_phone=?", (phone,)).fetchone()
        if row is None:
            row = c.execute("SELECT * FROM invites WHERE invitee_oid=?",
                            (invitee_oid,)).fetchone()
        if row is None:
            return None, None
        invite_id = int(row["id"])
        stamp = points_db.now_str()
        if row["inviter_oid"] == invitee_oid:
            # 「不能邀请自己」的最后一道闸：手机号可能注册前后换过，
            # 只有到这一步才确定这个 open_id 到底是不是邀请人本人。
            c.execute("UPDATE invites SET status=?, invitee_oid=?, note=?, updated_at=?"
                      " WHERE id=?",
                      (STATUS_REJECTED, invitee_oid, "自己邀请自己", stamp, invite_id))
            return invite_id, STATUS_REJECTED
        if row["status"] != STATUS_PENDING:
            return invite_id, row["status"]
        due = _stamp(points_db.now_dt()
                     + timedelta(days=int(points_config.get("invite_confirm_days"))))
        c.execute(
            "UPDATE invites SET status=?, invitee_oid=?, gender=?, confirmed_at=?,"
            " confirm_due_at=?, updated_at=? WHERE id=?",
            (STATUS_CONFIRMED, invitee_oid, gender or row["gender"] or "", stamp,
             due, stamp, invite_id))
        return invite_id, STATUS_CONFIRMED


def _stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def due_invites(now_stamp=None, limit=200, conn=None):
    """确认期已满、等着结算的邀请（状态 confirmed 且 confirm_due_at 已过）。"""
    now_stamp = now_stamp or points_db.now_str()
    with points_db.transaction(conn) as c:
        rows = c.execute(
            "SELECT * FROM invites WHERE status=? AND confirm_due_at <> ''"
            " AND confirm_due_at <= ? ORDER BY id LIMIT ?",
            (STATUS_CONFIRMED, now_stamp, int(limit))).fetchall()
    return [dict(r) for r in rows]


def settle(invite_id, *, banned=False, conn=None):
    """确认期满，结算一条邀请。

    banned=True（账号已被封禁）→ 转 clawed_back，不发穗。
    否则 → 发穗（幂等键 `invite_reward:<id>`，重复调用不会发第二次）并转 finalized。

    发穗与改状态在**同一个事务**里：分开做的话，中间崩一次就会出现
    「穗发了、状态还是 confirmed」，下一轮再结算时靠幂等键不会重发，
    但详情页会一直显示「待确认」。
    返回 (新状态, 流水 ID 或 None)。
    """
    with points_db.transaction(conn) as c:
        row = c.execute("SELECT * FROM invites WHERE id=?", (int(invite_id),)).fetchone()
        if row is None:
            raise InviteError(f"邀请记录不存在：{invite_id}")
        if row["status"] != STATUS_CONFIRMED:
            return row["status"], None       # 已结算过（或已作废），不重复处理
        stamp = points_db.now_str()
        if not banned and row["confirm_due_at"] and row["confirm_due_at"] > stamp:
            # 确认期没满就先别发。这条闸门只拦「发穗」那一侧——早发出去的穗
            # 是收不回来的（可能已经被兑换掉），而提前作废一个注定要作废的
            # 邀请没有任何代价。
            raise InviteError(
                f"邀请 {invite_id} 的确认期到 {row['confirm_due_at']} 才满，现在还不能发")
        if banned:
            c.execute("UPDATE invites SET status=?, note=?, updated_at=? WHERE id=?",
                      (STATUS_CLAWED_BACK, "确认期内被封禁", stamp, int(invite_id)))
            return STATUS_CLAWED_BACK, None
        amount = reward_amount(row["gender"])
        entry = points.grant(
            row["inviter_oid"], amount, points.KIND_INVITE,
            reason=f"邀请好友注册（{row['invitee_phone'][-4:]}）",
            ref_type="invite", ref_id=str(invite_id),
            idempotency_key=_reward_key(invite_id), conn=c)
        entry_id = entry[0] if entry else points.entry_id_for(_reward_key(invite_id), conn=c)
        c.execute("UPDATE invites SET status=?, reward_ledger_id=?, updated_at=? WHERE id=?",
                  (STATUS_FINALIZED, entry_id, stamp, int(invite_id)))
        return STATUS_FINALIZED, entry_id


def settle_all(*, is_banned=None, limit=200, conn=None):
    """扫一轮到期未结算的邀请。

    `is_banned(open_id) -> bool` 由调用方提供（要查多维表格的账号状态，
    lib 不碰飞书）。不传就一律视为正常账号——那样至少不会漏发。
    返回 {"finalized": n, "clawed_back": n}。
    """
    out = {"finalized": 0, "clawed_back": 0}
    for row in due_invites(limit=limit, conn=conn):
        banned = bool(is_banned and row["invitee_oid"] and is_banned(row["invitee_oid"]))
        status, _ = settle(int(row["id"]), banned=banned, conn=conn)
        if status == STATUS_FINALIZED:
            out["finalized"] += 1
        elif status == STATUS_CLAWED_BACK:
            out["clawed_back"] += 1
    return out


def claw_back_for_invitee(invitee_oid, reason="确认期内被封禁", conn=None):
    """被邀请人被标记封禁时**立刻**调用：作废他名下还在确认期内的邀请。

    为什么不等 7 天到期再看一眼：需求说的「7 天内被封禁」算的是**发生过**，
    不是「到期那一刻还在封」。第 3 天封、第 5 天解封也该收回，等结算那天
    去看就正好看不到。

    已到账（finalized）的不动——需求的收回窗口就是这 7 天，出了窗口就是终局。
    要事后追回用管理员「扣穗」，那条路径会留下操作人和原因。
    返回作废条数。
    """
    if not invitee_oid:
        return 0
    with points_db.transaction(conn) as c:
        cur = c.execute(
            "UPDATE invites SET status=?, note=?, updated_at=?"
            " WHERE invitee_oid=? AND status=?",
            (STATUS_CLAWED_BACK, reason, points_db.now_str(),
             invitee_oid, STATUS_CONFIRMED))
        return cur.rowcount or 0


# ---------------------------------------------------------------- 查询（H5 / 机器人）

def open_invites_for(invitee_oid, conn=None):
    """这个人名下还在确认期内的邀请（谁邀请的他、能拿多少穗）。

    给「被邀请人封禁 → 通知邀请人奖励没了」用：光知道作废了几条不够，
    还得知道该去告诉谁。`claw_back_for_invitee` 只回收、不回报，所以单列一个查询。
    """
    if not invitee_oid:
        return []
    with points_db.transaction(conn) as c:
        rows = c.execute("SELECT id, inviter_oid, gender, invitee_phone FROM invites"
                         " WHERE invitee_oid=? AND status=?",
                         (invitee_oid, STATUS_CONFIRMED)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["reward"] = reward_amount(d["gender"])
        out.append(d)
    return out


def progress(inviter_oid, conn=None):
    """邀请进度。`pending_points` 只算已经知道性别的（已「单身」的那些）——
    还没注册的人不知道性别，硬按某一档估会让数字来回跳。"""
    with points_db.transaction(conn) as c:
        rows = c.execute("SELECT status, gender, count(*) AS n FROM invites"
                         " WHERE inviter_oid=? GROUP BY status, gender",
                         (inviter_oid,)).fetchall()
    out = {"total": 0, "pending": 0, "confirmed": 0, "finalized": 0,
           "clawed_back": 0, "rejected": 0, "pending_points": 0, "finalized_points": 0}
    for r in rows:
        status, n = r["status"], int(r["n"])
        out["total"] += n
        if status in out:
            out[status] += n
        if status == STATUS_CONFIRMED:
            out["pending_points"] += reward_amount(r["gender"]) * n
        elif status == STATUS_FINALIZED:
            out["finalized_points"] += reward_amount(r["gender"]) * n
    return out


def list_for(inviter_oid, limit=100, conn=None):
    """我邀请的人（脱敏后的手机号），新的在前。"""
    with points_db.transaction(conn) as c:
        rows = c.execute("SELECT * FROM invites WHERE inviter_oid=? ORDER BY id DESC LIMIT ?",
                         (inviter_oid, int(limit))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["phone_masked"] = _mask(d["invitee_phone"])
        d["status_label"] = STATUS_LABELS.get(d["status"], d["status"])
        d["reward"] = reward_amount(d["gender"]) if d["status"] in (
            STATUS_CONFIRMED, STATUS_FINALIZED) else 0
        d.pop("invitee_phone", None)
        out.append(d)
    return out


def _mask(phone):
    phone = str(phone or "")
    return f"{phone[:3]}****{phone[-4:]}" if len(phone) == 11 else ""
