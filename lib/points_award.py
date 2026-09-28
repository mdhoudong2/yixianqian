"""人手动发的麦穗：协助组织活动、管理员加减。

两者都是「管理员说了算」，区别只有一条：协助必须挂到某场活动上、且有条数上限。
管理员加减不需要挂活动，但要留操作人和原因——需求原文「只有管理员角色可以
手动加减麦穗，每次操作都记录操作人和时间」。操作人和时间由账本自己记
（`ledger.operator_oid` / `created_at`），扣穗是**新增一条负流水**而不是改原来那条。
"""
from lib import points, points_config, points_db

REF_TYPE_ACTIVITY = "activity"
REF_TYPE_MANUAL = "manual"


class AwardError(Exception):
    """手动发穗相关的错误。"""


def activity_assist_total(user_oid, activity_id, conn=None):
    """这个人在这场比赛里已经拿到的协助穗数（已退还的不算）。

    只统计**没被反向过**的协助流水：一笔协助如果后来退还了，占用过的额度就该
    释放出来，否则「本场已满」会一直挡着，而账上其实一分钱没给。
    """
    with points_db.transaction(conn) as c:
        row = c.execute(
            "SELECT COALESCE(SUM(l.delta), 0) AS n FROM ledger l"
            " WHERE l.user_oid=? AND l.kind=? AND l.ref_type=? AND l.ref_id=?"
            "   AND NOT EXISTS (SELECT 1 FROM ledger r"
            "                   WHERE r.ref_type=? AND r.ref_id=CAST(l.id AS TEXT))",
            (user_oid, points.KIND_ASSIST, REF_TYPE_ACTIVITY, str(activity_id),
             points.REF_TYPE_LEDGER)).fetchone()
    return int(row["n"])


def grant_assist(user_oid, activity_id, amount, reason, operator_oid="",
                 idempotency_key="", conn=None):
    """协助组织活动（帮忙签到、带小组、筹划等）加穗。

    两条上限都来自需求原文，缺一不可：
    - 单次 10~50 穗（`assist_min` / `assist_max`）；
    - 同一个人在同一场活动里累计不超过 50 穗（`assist_activity_cap`）。

    第二条不能靠第一条兜底：连着加两次 30 就是 60，单次都没超。
    """
    if not user_oid:
        raise AwardError("用户不能为空")
    if not str(activity_id or "").strip():
        raise AwardError("协助组织必须关联到某一场活动")
    if not str(reason or "").strip():
        raise AwardError("必须填写加穗原因")

    amount = int(amount)
    low = int(points_config.get("assist_min"))
    high = int(points_config.get("assist_max"))
    if amount < low or amount > high:
        raise AwardError(f"协助组织单次应为 {low}~{high} 穗，收到 {amount}")

    cap = int(points_config.get("assist_activity_cap"))
    with points_db.transaction(conn) as c:
        if idempotency_key and points.entry_id_for(idempotency_key, conn=c) is not None:
            # 上限要排在幂等之后判，理由同 lib/points.py 里 add_entry 的那段：
            # 重放一条已经发过的协助时，额度当然已经占着了，先判上限会把
            # 「这笔已经记过账了」报成「超过上限」——把人支去查额度，
            # 而真正的问题是调用方在重放。
            raise points.DuplicateEntry(
                f"这笔已经记过账了（幂等键 {idempotency_key}）")
        used = activity_assist_total(user_oid, activity_id, conn=c)
        if used + amount > cap:
            raise AwardError(
                f"这场活动已经给过 {used} 穗，再加 {amount} 穗会超过上限 {cap}")

        return points.add_entry(
            user_oid, amount, points.KIND_ASSIST,
            reason=f"协助组织活动 {activity_id}：{reason}".strip("："),
            ref_type=REF_TYPE_ACTIVITY, ref_id=str(activity_id),
            operator_oid=operator_oid, idempotency_key=idempotency_key, conn=c)


def adjust(user_oid, amount, reason, operator_oid="", idempotency_key="", conn=None):
    """管理员手动加/扣麦穗。amount 为正=加，为负=扣。

    扣穗不是撤销原来那条奖励，而是新增一条负流水——原流水一个字不改，
    「这笔麦穗是怎么来的、又是怎么没的」在明细里都查得到。
    余额不够扣时账本会拒绝（lib/points.InsufficientBalance），这里不兜。
    """
    if not user_oid:
        raise AwardError("用户不能为空")
    if not str(reason or "").strip():
        raise AwardError("必须填写原因")
    amount = int(amount)
    if amount == 0:
        raise AwardError("数量不能为 0")

    return points.add_entry(
        user_oid, amount, points.KIND_ADMIN,
        reason=reason.strip(), ref_type=REF_TYPE_MANUAL, ref_id="",
        operator_oid=operator_oid, idempotency_key=idempotency_key, conn=conn)
