"""DB層。DATABASE_URL があれば PostgreSQL、無ければ SQLite(ローカル開発用)。"""
import os
import sqlite3
from contextlib import contextmanager

DATABASE_URL = os.environ.get("DATABASE_URL", "")
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "app.db"))

if IS_PG:
    import psycopg2
    import psycopg2.extras


def _connect():
    if IS_PG:
        return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


class Tx:
    def __init__(self, conn):
        self.c = conn

    def _prep(self, sql):
        return sql.replace("?", "%s") if IS_PG else sql

    def query(self, sql, args=(), lock=False):
        if lock and IS_PG:
            sql += " FOR UPDATE"
        cur = self.c.cursor()
        cur.execute(self._prep(sql), tuple(args))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows

    def one(self, sql, args=(), lock=False):
        rows = self.query(sql, args, lock)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        cur = self.c.cursor()
        cur.execute(self._prep(sql), tuple(args))
        n = cur.rowcount
        cur.close()
        return n

    def insert(self, sql, args=()):
        cur = self.c.cursor()
        if IS_PG:
            cur.execute(self._prep(sql) + " RETURNING id", tuple(args))
            rid = cur.fetchone()["id"]
        else:
            cur.execute(sql, tuple(args))
            rid = cur.lastrowid
        cur.close()
        return rid


@contextmanager
def transaction():
    conn = _connect()
    try:
        if not IS_PG:
            conn.execute("BEGIN IMMEDIATE")
        yield Tx(conn)
        conn.commit() if IS_PG else conn.execute("COMMIT")
    except BaseException:
        try:
            conn.rollback() if IS_PG else conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


@contextmanager
def reader():
    conn = _connect()
    try:
        yield Tx(conn)
    finally:
        if IS_PG:
            conn.rollback()
        conn.close()


def init_db():
    pk = "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    stmts = [
        f"""CREATE TABLE IF NOT EXISTS users (
            id {pk}, name TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user', created_at TEXT NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS reservations (
            id {pk}, user_id INTEGER NOT NULL REFERENCES users(id),
            scheduled_at TEXT NOT NULL, address TEXT NOT NULL,
            lat DOUBLE PRECISION NOT NULL, lon DOUBLE PRECISION NOT NULL,
            item_desc TEXT NOT NULL, forgotten_items TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'reserved', hold_reason TEXT,
            cart_lat DOUBLE PRECISION, cart_lon DOUBLE PRECISION,
            progress_m DOUBLE PRECISION NOT NULL DEFAULT 0, route TEXT,
            locked INTEGER NOT NULL DEFAULT 1, unlock_expires_at TEXT,
            obstacle INTEGER NOT NULL DEFAULT 0, comms_ok INTEGER NOT NULL DEFAULT 1,
            sensor_ok INTEGER NOT NULL DEFAULT 1, signal_state TEXT NOT NULL DEFAULT 'red',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS events (
            id {pk}, ts TEXT NOT NULL, reservation_id INTEGER, actor TEXT NOT NULL,
            kind TEXT NOT NULL, severity TEXT NOT NULL DEFAULT 'info', detail TEXT NOT NULL DEFAULT '')""",
        "CREATE INDEX IF NOT EXISTS idx_res_user ON reservations(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_events_res ON events(reservation_id)",
    ]
    with transaction() as tx:
        if IS_PG:
            tx.execute("SELECT pg_advisory_xact_lock(7123001)")
        for s in stmts:
            tx.execute(s)
