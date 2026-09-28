"""麦穗数据层单测：迁移机制、库结构、连接生命周期（不联网）。"""
import os
import sqlite3
import sys

import pytest

from lib import points_db

_WEB_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
if _WEB_BACKEND not in sys.path:
    sys.path.insert(0, _WEB_BACKEND)


def _write(dirpath, name, sql):
    (dirpath / name).write_text(sql, encoding="utf-8")


def test_a_fresh_database_gets_every_table(points_db):
    conn = points_db.connection()
    got = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"ledger", "redemptions", "invites",
            "historical_participants", "config"} <= got
    assert points_db.applied_versions() == [1]


def test_migrate_is_idempotent(points_db):
    assert points_db.migrate() == 0            # 进程内已迁移过
    assert points_db.close_all() is None
    assert points_db.migrate() == 0            # 换一条连接再看一遍库里也记着
    assert points_db.applied_versions() == [1]


def test_only_one_schema_migrations_row_per_version(points_db):
    for _ in range(3):
        points_db.close_all()
        points_db.migrate()
    rows = points_db.connection().execute(
        "SELECT version FROM schema_migrations").fetchall()
    assert [r["version"] for r in rows] == [1]


def test_migrations_run_in_version_order_and_only_once(tmp_path, monkeypatch):
    """两条迁移按版本号顺序跑；重跑时一条都不再执行。

    顺序错了会出事：002 引用 001 建的列，反了就建不出来。
    """
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    _write(mdir, "001_a.sql", "CREATE TABLE a(x INTEGER);\n")
    _write(mdir, "002_b.sql",
           "CREATE TABLE b(x INTEGER);\nINSERT INTO b(x) VALUES(1);\n")
    monkeypatch.setattr(points_db, "MIGRATIONS_DIR", str(mdir))
    points_db.set_db_path(str(tmp_path / "order.db"))

    assert points_db.migrate() == 2
    assert points_db.applied_versions() == [1, 2]
    assert points_db.connection().execute("SELECT x FROM b").fetchone()["x"] == 1
    points_db.close_all()
    assert points_db.migrate() == 0


def test_a_migration_file_with_several_statements_runs_all_of_them(tmp_path, monkeypatch):
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    _write(mdir, "001_many.sql",
           "-- 建表\nCREATE TABLE a(x INTEGER);\n"
           "INSERT INTO a(x) VALUES(1);\n"
           "\n"
           "-- 分号在字符串里，不能按 ; 硬切\n"
           "INSERT INTO a(x) VALUES(';不是分隔符');\n"
           "\n"
           "-- 结尾注释，没有语句\n")
    monkeypatch.setattr(points_db, "MIGRATIONS_DIR", str(mdir))
    points_db.set_db_path(str(tmp_path / "many.db"))
    points_db.migrate()

    rows = points_db.connection().execute("SELECT x FROM a ORDER BY rowid").fetchall()
    assert [r["x"] for r in rows] == [1, ";不是分隔符"]
    points_db.close_all()


def test_duplicate_migration_versions_fail_loudly(tmp_path, monkeypatch):
    """两个文件同版本号时按文件名顺序跑其中一个，schema 会随机器而异——
    这是那种「本地全过、线上全错」的根源，必须启动就炸。"""
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    _write(mdir, "001_a.sql", "CREATE TABLE a(x INTEGER);\n")
    _write(mdir, "001_b.sql", "CREATE TABLE b(x INTEGER);\n")
    monkeypatch.setattr(points_db, "MIGRATIONS_DIR", str(mdir))
    points_db.set_db_path(str(tmp_path / "dup.db"))

    with pytest.raises(RuntimeError, match="重复"):
        points_db.migrate()
    points_db.close_all()


def test_a_failed_migration_is_not_recorded(tmp_path, monkeypatch):
    """迁移中途失败不能留下版本记录，否则下次启动会跳过它，schema 永远缺一块。"""
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    _write(mdir, "001_ok.sql", "CREATE TABLE ok(x INTEGER);\n")
    _write(mdir, "002_bad.sql", "CREATE TABLE bad(x INTEGER);\n这不是 SQL;\n")
    monkeypatch.setattr(points_db, "MIGRATIONS_DIR", str(mdir))
    points_db.set_db_path(str(tmp_path / "bad.db"))

    with pytest.raises(Exception):
        points_db.migrate()
    points_db.close_all()
    assert points_db.applied_versions() == [1]   # 002 没被记上


def test_switching_database_does_not_reuse_the_old_connection(tmp_path, monkeypatch):
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    _write(mdir, "001_a.sql", "CREATE TABLE a(x INTEGER);\n")
    monkeypatch.setattr(points_db, "MIGRATIONS_DIR", str(mdir))

    points_db.set_db_path(str(tmp_path / "one.db"))
    points_db.migrate()
    points_db.connection().execute("INSERT INTO a(x) VALUES(1)")

    points_db.set_db_path(str(tmp_path / "two.db"))
    points_db.migrate()
    got = points_db.connection().execute("SELECT count(*) AS n FROM a").fetchone()["n"]
    assert got == 0                              # 看的是新库，不是上一个句柄
    points_db.close_all()


def test_connection_is_reused_within_a_thread(points_db):
    assert points_db.connection() is points_db.connection()


def test_connections_are_per_thread(points_db):
    """sqlite3 的连接不能跨线程用，所以连接必须是线程本地的——
    共用一条连接会在并发下抛「SQLite objects created in a thread can only be
    used in that same thread」。"""
    import threading

    box = {}
    worker = threading.Thread(target=lambda: box.setdefault("conn", points_db.connection()))
    worker.start()
    worker.join(timeout=5)
    assert box["conn"] is not points_db.connection()


def test_wal_and_foreign_keys_are_on(points_db):
    conn = points_db.connection()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_a_phone_can_only_have_one_inviter(points_db):
    """「一人只能有一个邀请人」由唯一索引保证，而不是靠先查后写。"""
    conn = points_db.connection()
    conn.execute("INSERT INTO invites(inviter_oid, invitee_phone, status, created_at)"
                 " VALUES('ou_a','13800000000','pending','2026-09-28 10:00:00')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO invites(inviter_oid, invitee_phone, status, created_at)"
                     " VALUES('ou_b','13800000000','pending','2026-09-28 10:00:01')")


def test_bot_and_h5_point_at_the_same_database_file():
    """bot 与 H5 必须写同一个库。

    两边各写各的文件是这套设计里最安静的灾难：余额会「一半在 bot 那边、
    一半在 H5 那边」，两边各自看都自洽，只有用户对不上账时才被发现。
    """
    import config as h5_config
    import constants as bot_constants

    assert bot_constants.POINTS_DB_FILE == h5_config.POINTS_DB_FILE
    assert (os.path.abspath(points_db.default_db_path())
            == os.path.abspath(bot_constants.POINTS_DB_FILE))


def test_one_registered_account_can_only_be_bound_once(points_db):
    conn = points_db.connection()
    conn.execute("INSERT INTO invites(inviter_oid, invitee_phone, invitee_oid, status,"
                 " created_at) VALUES('ou_a','13800000001','ou_x','confirmed','t')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO invites(inviter_oid, invitee_phone, invitee_oid, status,"
                     " created_at) VALUES('ou_b','13800000002','ou_x','confirmed','t')")
