"""麦穗积分的 SQLite 数据层：连接、事务、版本化迁移（bot 与 H5 共用）。

## 为什么这里才引入 SQLite

一线牵此前的唯一存储是飞书多维表格，它**没有事务**：一次 `update_record` 要么
整笔被拒、要么逐字段落库，没法把「读余额 → 判够不够 → 扣分 → 记流水」锁在一起。
积分是账本，必须满足「余额 = 流水求和」且并发下不重复扣分——这是 SQL 的形状。
所以只把**积分账本**放进 SQLite；用户/活动/报名仍在多维表格，两边靠 open_id
关联，谁都不写对方的数据。

## 并发模型

写入者只有两个进程：bot（单进程多线程）与 H5（gunicorn workers=1, threads=16），
同机访问同一个 .db 文件。靠 WAL + `busy_timeout` 跨进程串行化：

- 读（WAL）不阻塞写：H5 的明细页不会卡住 bot 的对账；
- 写一律 `BEGIN IMMEDIATE`，**在开始读余额之前**就拿到写锁。

`BEGIN IMMEDIATE` 与 `BEGIN` 的区别不是性能而是正确性：SQLite 的 `BEGIN`
（deferred）要到第一条写语句才升级成写锁，而那时余额早读过了——两个进程会各自
读到同一个余额、各扣一次，这就是经典的 check-then-write 丢更新。

## 连接为什么是线程本地的

sqlite3 连接对象默认禁止跨线程使用，而 bot 的对账线程和 H5 的 16 个 worker
线程都会碰积分。用 thread-local 连接，而不是 `check_same_thread=False` + 一把
全局锁：前者读写能并行（WAL 下读不互斥），后者会把所有读也串起来。
"""
import contextlib
import os
import sqlite3
import threading

from lib import quota

DEFAULT_DB_BASENAME = "yixianqian_points.db"
MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
BUSY_TIMEOUT_MS = 5000

# schema_migrations 不放在 lib/migrations/001_init.sql 里，是它自己要在迁移
# 之前就存在——否则迁移器连「哪些版本已应用」都没处查。
SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    applied_at TEXT NOT NULL
)
"""

_lock = threading.Lock()
_conn_state = threading.local()
_all_conns = []
_migrated = set()
_path_override = None
_generation = 0
_migrate_lock = threading.Lock()


def default_db_path():
    """默认库位置：环境变量 → 仓库根 data/ 下。与 bot/constants.py、
    web/backend/config.py 的 `POINTS_DB_FILE` 算出来是同一个文件。"""
    env = os.environ.get("YIXIANQIAN_POINTS_DB")
    if env:
        return env
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", DEFAULT_DB_BASENAME)


def db_path():
    with _lock:
        return _path_override or default_db_path()


def set_db_path(path):
    """换库（测试，或入口显式注入 config.POINTS_DB_FILE）。

    会把本进程已有的连接和「已迁移」标记一起作废——否则换成新库之后，
    还在用旧库的连接，或者以为 schema 已经建好了。
    """
    global _path_override
    close_all()
    with _lock:
        _path_override = path


def close_all():
    """关掉本进程的所有连接，并让各线程的本地缓存失效。"""
    global _generation
    with _lock:
        _generation += 1
        conns, _all_conns[:] = list(_all_conns), []
        _migrated.clear()
    for c in conns:
        try:
            c.close()
        except Exception:
            pass
    _conn_state.state = None


def now_dt():
    """当前时刻。用 lib.quota 的时区，不跟着系统时区漂。

    账本里所有「现在」都从这里来，别处不要再调 `quota.now()`——
    单测要拨动时间（比如验证 7 天确认期的边界）时只钉这一个点就够了。
    """
    return quota.now()


def now_str():
    """流水时间戳。定宽 `YYYY-mm-dd HH:MM:SS`，所以字符串比较就是时间比较——
    `confirm_due_at <= now` 这类判断全靠这个格式，改格式会静默改变比较结果。"""
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")


def _open(path):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    # isolation_level=None 关掉 sqlite3 的隐式事务，BEGIN/COMMIT 全部由
    # transaction() 显式控制——隐式事务恰恰是拿不到写锁的那种。
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    # WAL 的常规搭档是 NORMAL（抗进程崩溃，不抗断电）。账本按钱对待，
    # 用 FULL：每条提交都落盘。写入量是「每天几十条」，这点 fsync 不值一提。
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def _connect(path):
    """取本线程的连接（不触发迁移）。"""
    state = getattr(_conn_state, "state", None)
    if state is not None:
        gen, cached_path, conn = state
        if gen == _generation and cached_path == path:
            return conn
        try:
            conn.close()
        except Exception:
            pass
    conn = _open(path)
    _conn_state.state = (_generation, path, conn)
    with _lock:
        _all_conns.append(conn)
    return conn


def connection():
    """取本线程的连接（必要时先把 schema 补齐）。"""
    path = db_path()
    if os.path.abspath(path) not in _migrated:
        with _migrate_lock:
            _do_migrate()
    return _connect(path)


@contextlib.contextmanager
def transaction(conn=None):
    """写事务：`BEGIN IMMEDIATE` … `COMMIT` / `ROLLBACK`。

    传入 conn 表示「并入调用方已经在开的那个事务」，不再 BEGIN。这样
    helper 之间可以互相调用（`reverse_entry` 内部调 `add_entry`）而不会撞上
    "cannot start a transaction within a transaction"。代价是内层失败会连
    外层一起回滚——账本场景里这恰恰是想要的：要么整笔成立，要么整笔没有。
    """
    if conn is None:
        conn = connection()
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        # 回滚失败时抛出的异常会盖掉原始异常。宁可这样也不吞：回滚都失败了，
        # 原始那个错误已经不是当下最要紧的信息。
        conn.execute("ROLLBACK")
        raise


def _split_statements(sql):
    """把 .sql 文件切成一条条语句。

    不用 `executescript()`：它在 Python 里会先把调用方已开的事务 COMMIT 掉，
    于是「一个迁移文件要么全成功要么全回滚」就没了。用
    `sqlite3.complete_statement` 逐条切分，它认得字符串字面量和触发器里的
    分号——按 `;` 硬切会在这两种情况下切坏。
    """
    stmts, buf = [], []
    for line in sql.splitlines(keepends=True):
        buf.append(line)
        text = "".join(buf)
        if sqlite3.complete_statement(text):
            stmts.append(text)
            buf = []
    tail = "".join(buf)
    if _has_sql(tail):
        stmts.append(tail)
    return stmts


def _has_sql(text):
    """这段文本里有真语句吗（排除纯注释/空白尾巴）。"""
    return any(line.strip() and not line.strip().startswith("--")
               for line in text.splitlines())


def _discover():
    """lib/migrations/ 下的迁移文件，按版本号升序。返回 [(版本, 文件名, 路径)]。"""
    try:
        names = sorted(os.listdir(MIGRATIONS_DIR))
    except OSError:
        return []
    out, seen = [], {}
    for name in names:
        if not name.endswith(".sql"):
            continue
        head = name[:-4].split("_", 1)[0]
        if not head.isdigit():
            continue
        version = int(head)
        if version in seen:
            # 两个文件同版本号时，先应用哪个取决于文件名的字典序——静默按
            # 其中一个跑，schema 会随机器而异地长出来。宁可启动失败。
            raise RuntimeError(
                f"迁移版本号重复：{seen[version]} 与 {name} 都是 {version}")
        seen[version] = name
        out.append((version, name, os.path.join(MIGRATIONS_DIR, name)))
    out.sort(key=lambda x: x[0])
    return out


def migrate():
    """应用所有未应用的迁移，返回本次应用条数。进程内每个库只真正跑一次。"""
    with _migrate_lock:
        return _do_migrate()


def _do_migrate():
    path = db_path()
    key = os.path.abspath(path)
    if key in _migrated:
        return 0
    conn = _connect(path)
    conn.execute(SCHEMA_MIGRATIONS_DDL)
    applied = 0
    for version, name, fpath in _discover():
        with transaction(conn) as c:
            row = c.execute("SELECT 1 FROM schema_migrations WHERE version=?",
                            (version,)).fetchone()
            if row is not None:
                # 另一个进程（bot 与 H5 同时启动）刚跑过同一个版本。BEGIN
                # IMMEDIATE 让我们读到的一定是它提交后的结果，跳过即可。
                continue
            with open(fpath, encoding="utf-8") as f:
                sql = f.read()
            for stmt in _split_statements(sql):
                c.execute(stmt)
            c.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES(?,?,?)",
                (version, name, now_str()))
            applied += 1
    with _lock:
        _migrated.add(key)
    return applied


def applied_versions():
    """已应用的迁移版本号（升序）。

    刻意走 `_connect` 而不是 `connection()`：这是个只读的查看接口，不能顺手
    触发迁移——否则「查一下现在到哪一版了」会去跑迁移，而迁移可能正要失败。
    """
    conn = _connect(db_path())
    try:
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version").fetchall()
    except sqlite3.OperationalError:
        return []   # schema_migrations 还没建，等于一版都没应用
    return [r["version"] for r in rows]
