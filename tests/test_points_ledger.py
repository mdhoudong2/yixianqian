"""麦穗账本核心单测（SQLite 临时库，不联网）。

锁的是三条不变量，它们各自对应一类线上事故：
  1. 余额 = SUM(流水)。有任何一处拿 `balance_after` 当权威值就会漂。
  2. 不删不改已有流水，退还/收回只新增反向流水。否则账再也对不上。
  3. 幂等键唯一索引兜底，而不是「先查后写」。后者并发必漏。
"""
import multiprocessing
import os
import threading

import pytest

from lib import points


def _spend_worker(db_path, out_dir, index, tries):
    """子进程：反复扣 10 穗直到余额不够，把自己成功的次数写进文件。

    这里 import 出来的是一个全新的 points_db 模块状态，跟父进程没有任何共享
    内存——这正是 bot 与 H5 两个进程的真实样子。
    """
    from lib import points as child_points
    from lib import points_db

    points_db.set_db_path(db_path)
    ok, err = 0, ""
    for _ in range(tries):
        try:
            child_points.add_entry("ou_a", -10, child_points.KIND_REDEEM)
            ok += 1
        except child_points.InsufficientBalance:
            break
        except Exception as e:      # 锁等不到之类的意外，别让它变成一句「少扣了」
            err = f"{type(e).__name__}: {e}"
            break
    with open(os.path.join(out_dir, f"{index}.txt"), "w", encoding="utf-8") as f:
        f.write(f"{ok}\n{err}")


def _run_threads(fn, n):
    """n 个线程尽量同时起跑。用 Barrier 而不是 sleep，否则第一条线程可能
    在最后一条还没启动时就已经跑完了，并发根本没被测到。"""
    barrier = threading.Barrier(n)

    def wrapper():
        barrier.wait()
        fn()

    threads = [threading.Thread(target=wrapper) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


# ---------------------------------------------------------------- 余额

def test_empty_balance_is_zero(points_db):
    assert points.balance("ou_a") == 0


def test_balance_is_the_sum_of_entries(points_db):
    points.add_entry("ou_a", 20, points.KIND_INVITE, reason="邀请好友")
    points.add_entry("ou_a", 15, points.KIND_ASSIST, reason="协助签到")
    points.add_entry("ou_a", -20, points.KIND_REDEEM, reason="兑换实名喜欢")
    assert points.balance("ou_a") == 15


def test_balance_only_counts_its_own_user(points_db):
    points.add_entry("ou_a", 20, points.KIND_INVITE)
    points.add_entry("ou_b", 50, points.KIND_ASSIST)
    assert points.balance("ou_a") == 20
    assert points.balance("ou_b") == 50


def test_balance_after_is_a_snapshot_not_the_source_of_truth(points_db):
    """写下的 balance_after 要等于当时的权威余额——它只是审计用的快照。"""
    points.add_entry("ou_a", 20, points.KIND_INVITE)
    points.add_entry("ou_a", 15, points.KIND_ASSIST)
    points.add_entry("ou_a", -5, points.KIND_ADMIN)
    rows = sorted(points.entries("ou_a"), key=lambda r: r["id"])
    assert [r["balance_after"] for r in rows] == [20, 35, 30]
    assert points.balance("ou_a") == rows[-1]["balance_after"]


def test_summary_splits_earned_and_spent(points_db):
    points.add_entry("ou_a", 20, points.KIND_INVITE)
    points.add_entry("ou_a", 30, points.KIND_ASSIST)
    points.add_entry("ou_a", -20, points.KIND_REDEEM)
    assert points.summary("ou_a") == {"balance": 30, "earned": 50, "spent": 20}


# ---------------------------------------------------------------- 余额不足

def test_spending_more_than_the_balance_is_refused_and_writes_nothing(points_db):
    points.add_entry("ou_a", 10, points.KIND_ADMIN)
    with pytest.raises(points.InsufficientBalance):
        points.add_entry("ou_a", -11, points.KIND_REDEEM, reason="兑换")
    assert points.balance("ou_a") == 10
    assert len(points.entries("ou_a")) == 1  # 被拒的那笔一个字都没落


def test_spending_exactly_the_balance_is_allowed(points_db):
    points.add_entry("ou_a", 20, points.KIND_INVITE)
    points.add_entry("ou_a", -20, points.KIND_REDEEM)
    assert points.balance("ou_a") == 0


def test_zero_balance_cannot_redeem_at_all(points_db):
    with pytest.raises(points.InsufficientBalance):
        points.add_entry("ou_a", -1, points.KIND_REDEEM)


# ---------------------------------------------------------------- 幂等

def test_the_same_idempotency_key_cannot_be_paid_twice(points_db):
    points.add_entry("ou_a", 20, points.KIND_INVITE, idempotency_key="invite:1")
    with pytest.raises(points.DuplicateEntry):
        points.add_entry("ou_a", 20, points.KIND_INVITE, idempotency_key="invite:1")
    assert points.balance("ou_a") == 20


def test_grant_returns_none_instead_of_raising_on_a_repeat(points_db):
    """周期性任务会一轮轮重扫同一批记录，「上次发过了」是正常路径不是错误。"""
    first = points.grant("ou_a", 20, points.KIND_INVITE, idempotency_key="invite:1")
    again = points.grant("ou_a", 20, points.KIND_INVITE, idempotency_key="invite:1")
    assert first is not None
    assert again is None
    assert points.balance("ou_a") == 20


def test_different_keys_pay_twice(points_db):
    points.grant("ou_a", 20, points.KIND_INVITE, idempotency_key="invite:1")
    points.grant("ou_a", 15, points.KIND_INVITE, idempotency_key="invite:2")
    assert points.balance("ou_a") == 35


def test_entries_without_a_key_do_not_count_as_duplicates(points_db):
    """管理员可以两次加同样的分。空幂等键必须存成 NULL——SQLite 的唯一索引
    把多个 NULL 视为互不相同，存成空串的话第二条会被当成重复而拒掉。"""
    points.add_entry("ou_a", 5, points.KIND_ADMIN, reason="帮忙搬东西")
    points.add_entry("ou_a", 5, points.KIND_ADMIN, reason="帮忙搬东西")
    assert points.balance("ou_a") == 10


def test_concurrent_grants_with_the_same_key_pay_out_exactly_once(points_db):
    """16 个线程抢同一笔奖励，只能发出去一次。

    「先查有没有、没有再发」在这里必挂：16 个线程会同时查到「没有」。
    只有让唯一索引来拒才拦得住。
    """
    paid = []
    lock = threading.Lock()

    def worker():
        got = points.grant("ou_a", 20, points.KIND_INVITE, idempotency_key="invite:1")
        with lock:
            paid.append(got)

    _run_threads(worker, 16)

    assert sum(1 for p in paid if p is not None) == 1
    assert points.balance("ou_a") == 20


def test_has_key_reports_what_was_paid(points_db):
    assert points.has_key("invite:1") is False
    points.grant("ou_a", 20, points.KIND_INVITE, idempotency_key="invite:1")
    assert points.has_key("invite:1") is True
    assert points.has_key("") is False


# ---------------------------------------------------------------- 反向流水

def test_reverse_adds_an_opposite_entry_and_keeps_the_original(points_db):
    entry_id, _ = points.add_entry("ou_a", 20, points.KIND_INVITE,
                                   reason="邀请好友", idempotency_key="invite:1")
    points.reverse_entry(entry_id, points.KIND_CLAWBACK, reason="确认期内注销",
                         idempotency_key="clawback:1")

    rows = points.entries("ou_a")
    assert len(rows) == 2                                # 原流水还在
    assert {r["delta"] for r in rows} == {20, -20}
    assert points.balance("ou_a") == 0

    clawback = rows[0]                                   # 明细新的在前
    assert clawback["kind"] == points.KIND_CLAWBACK
    assert clawback["ref_type"] == points.REF_TYPE_LEDGER
    assert clawback["ref_id"] == str(entry_id)           # 追得回抵的是哪一笔


def test_reversing_twice_does_not_refund_twice(points_db):
    entry_id, _ = points.add_entry("ou_a", 20, points.KIND_INVITE,
                                   idempotency_key="invite:1")
    points.reverse_entry(entry_id, points.KIND_REFUND, idempotency_key="refund:1")
    with pytest.raises(points.DuplicateEntry):
        points.reverse_entry(entry_id, points.KIND_REFUND, idempotency_key="refund:1")
    assert points.balance("ou_a") == 0


def test_reversing_an_unknown_entry_is_refused(points_db):
    with pytest.raises(points.PointsError):
        points.reverse_entry(999, points.KIND_REFUND)


def test_reverse_is_refused_when_the_points_were_already_spent(points_db):
    """奖励发出去、被花掉了，之后又要收回——余额不够，收回整笔失败。

    这里刻意选「拒绝」而不是「允许负余额」：负余额会让「余额不足不能兑换」
    这句保证变得没法解释。真遇到了会抛错（进日志），需要人工处理。
    """
    entry_id, _ = points.add_entry("ou_a", 20, points.KIND_INVITE,
                                   idempotency_key="invite:1")
    points.add_entry("ou_a", -20, points.KIND_REDEEM, reason="兑换实名喜欢")
    with pytest.raises(points.InsufficientBalance):
        points.reverse_entry(entry_id, points.KIND_CLAWBACK)
    assert points.balance("ou_a") == 0


def test_reverse_only_touches_the_owner(points_db):
    entry_id, _ = points.add_entry("ou_a", 20, points.KIND_INVITE)
    points.add_entry("ou_b", 30, points.KIND_ASSIST)
    points.reverse_entry(entry_id, points.KIND_REFUND)
    assert points.balance("ou_a") == 0
    assert points.balance("ou_b") == 30


# ---------------------------------------------------------------- 事务

def test_an_error_inside_a_transaction_rolls_back_the_whole_thing(points_db):
    points.add_entry("ou_a", 100, points.KIND_ADMIN)
    with pytest.raises(points.InsufficientBalance):
        with points_db.transaction() as conn:
            points.add_entry("ou_a", -10, points.KIND_REDEEM, conn=conn)
            points.add_entry("ou_a", -200, points.KIND_REDEEM, conn=conn)  # 这笔不行
    assert points.balance("ou_a") == 100     # 前半笔也没留下来
    assert len(points.entries("ou_a")) == 1


def test_nested_transaction_joins_the_outer_one(points_db):
    """reverse_entry 内部会调 add_entry，两者都在事务里——内层必须并入外层，
    而不是再 BEGIN 一次（那会直接抛 cannot start a transaction within a
    transaction）。"""
    entry_id, _ = points.add_entry("ou_a", 20, points.KIND_INVITE)
    with points_db.transaction() as conn:
        points.reverse_entry(entry_id, points.KIND_REFUND, conn=conn)
        assert points.balance("ou_a", conn=conn) == 0
    assert points.balance("ou_a") == 0
    assert points_db.connection().in_transaction is False


def test_committed_entries_survive_a_reconnect(points_db):
    points.add_entry("ou_a", 20, points.KIND_INVITE)
    points_db.close_all()
    assert points.balance("ou_a") == 20


# ---------------------------------------------------------------- 并发扣分

def test_concurrent_spending_never_overdraws(points_db):
    """账上 100 穗，20 个线程各扣 10——恰好 10 个成功，余额 0，不会变负。

    这条锁的是 `BEGIN IMMEDIATE`：如果事务要到第一条写语句才升级成写锁，
    多个线程会各自读到「还有 100」，于是都扣成功，余额跑成负数。
    """
    points.add_entry("ou_a", 100, points.KIND_ADMIN)
    ok, refused = [], []
    lock = threading.Lock()

    def worker():
        try:
            points.add_entry("ou_a", -10, points.KIND_REDEEM)
            with lock:
                ok.append(1)
        except points.InsufficientBalance:
            with lock:
                refused.append(1)

    _run_threads(worker, 20)

    assert len(ok) == 10
    assert len(refused) == 10
    assert points.balance("ou_a") == 0
    assert len(points.entries("ou_a")) == 11   # 初始那笔 + 10 笔扣减


def test_two_processes_never_overdraw_the_same_account(tmp_path, points_db):
    """bot 与 H5 是两个进程、写同一个 .db 文件——真正要防的是这一层。

    上面那条多线程测试证明不了它：线程共享 GIL、共享同一份模块状态，而
    `BEGIN IMMEDIATE` 拿的是**文件级**写锁，跨进程才见真章。写锁要是退化成
    deferred，四个进程会各自读到「还有 100」，然后各扣各的。
    """
    db = str(tmp_path / "cross_process.db")
    points_db.set_db_path(db)
    points_db.migrate()
    points.add_entry("ou_a", 100, points.KIND_ADMIN)
    # fork 之前先断开：父进程的连接是打开状态，复制给四个子进程后，
    # 谁先关掉都会影响其他进程看到的文件描述符状态。
    points_db.close_all()

    out = tmp_path / "out"
    out.mkdir()
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_spend_worker, args=(db, str(out), i, 50))
             for i in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0

    spent, errors = 0, []
    for i in range(4):
        lines = (out / f"{i}.txt").read_text(encoding="utf-8").splitlines()
        spent += int(lines[0])
        if len(lines) > 1 and lines[1]:
            errors.append(lines[1])
    assert not errors, f"子进程报错：{errors}"
    assert spent == 10                        # 100 穗 ÷ 10，一笔不多一笔不少
    points_db.close_all()
    assert points.balance("ou_a") == 0


# ---------------------------------------------------------------- 入参

def test_unknown_kind_is_refused(points_db):
    with pytest.raises(ValueError):
        points.add_entry("ou_a", 10, "不知道什么类型")


def test_empty_user_is_refused(points_db):
    with pytest.raises(ValueError):
        points.add_entry("", 10, points.KIND_ADMIN)
    with pytest.raises(ValueError):
        points.balance("")
