"""存储层：本地/免费实例用 SQLite，设置 DATABASE_URL 时自动切到 Postgres（持久化）。

对外只暴露几个简单方法，SQL 方言差异都收敛在这里。
"""
import os
import sqlite3
from pathlib import Path

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "health.db"
        self.kind = "postgres" if USE_PG else "sqlite"
        self.ph = "%s" if USE_PG else "?"

    # ---------------------------------------------------------------- 连接
    def connect(self):
        if self.kind == "postgres":
            import psycopg
            url = DATABASE_URL
            # Supabase 给的是 postgres://，psycopg 只认 postgresql://
            if url.startswith("postgres://"):
                url = "postgresql://" + url[len("postgres://"):]
            # prepare_threshold=None：兼容 PgBouncer（Supabase 的 6543 连接池）
            return psycopg.connect(url, prepare_threshold=None)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _rows(cur):
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ---------------------------------------------------------------- 建表
    def init(self):
        conn = self.connect()
        cur = conn.cursor()
        if self.kind == "postgres":
            cur.execute("""CREATE TABLE IF NOT EXISTS samples(
                id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                metric  TEXT NOT NULL,
                day     TEXT NOT NULL,
                ts      TEXT NOT NULL,
                value   DOUBLE PRECISION,
                extra   TEXT,
                recv    DOUBLE PRECISION)""")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_sample ON samples(metric, ts)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_day ON samples(metric, day)")
            conn.commit()
        else:
            cur.execute("""CREATE TABLE IF NOT EXISTS samples(
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                metric TEXT NOT NULL,
                day    TEXT NOT NULL,
                ts     TEXT NOT NULL,
                value  REAL,
                extra  TEXT,
                recv   REAL)""")
            ver = cur.execute("PRAGMA user_version").fetchone()[0]
            if ver < 2:
                cur.execute("DROP INDEX IF EXISTS uq_sample")
                cur.execute("DELETE FROM samples WHERE id NOT IN "
                            "(SELECT MAX(id) FROM samples GROUP BY metric, ts)")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_sample ON samples(metric, ts)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_day ON samples(metric, day)")
            if ver < 2:
                cur.execute("PRAGMA user_version = 2")
            conn.commit()
        conn.close()

    # ---------------------------------------------------------------- 写入
    def upsert(self, rows):
        """rows: [(metric, day, ts, value, extra, recv), ...]；同一 (metric, ts) 覆盖。"""
        if not rows:
            return 0
        conn = self.connect()
        cur = conn.cursor()
        if self.kind == "postgres":
            cur.executemany(
                "INSERT INTO samples(metric,day,ts,value,extra,recv) VALUES(%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (metric, ts) DO UPDATE SET day=excluded.day, value=excluded.value, "
                "extra=excluded.extra, recv=excluded.recv", rows)
        else:
            cur.executemany(
                "INSERT OR REPLACE INTO samples(metric,day,ts,value,extra,recv) VALUES(?,?,?,?,?,?)",
                rows)
        conn.commit()
        conn.close()
        return len(rows)

    def delete_day(self, day):
        conn = self.connect()
        cur = conn.cursor()
        cur.execute(f"DELETE FROM samples WHERE day = {self.ph}", (day,))
        conn.commit()
        conn.close()

    # ---------------------------------------------------------------- 读取
    def range_rows(self, start: str, end: str):
        conn = self.connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT metric, day, ts, value, extra, recv FROM samples "
            f"WHERE day >= {self.ph} AND day <= {self.ph}", (start, end))
        rows = self._rows(cur)
        conn.close()
        return rows

    def recent(self, limit: int = 50):
        conn = self.connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT metric, day, ts, value, extra FROM samples "
            f"ORDER BY recv DESC, id DESC LIMIT {self.ph}", (int(limit),))
        rows = self._rows(cur)
        conn.close()
        return rows

    def all_rows(self):
        conn = self.connect()
        cur = conn.cursor()
        cur.execute("SELECT metric, day, ts, value, extra, recv FROM samples ORDER BY day, metric, ts")
        rows = self._rows(cur)
        conn.close()
        return rows

    def count(self):
        conn = self.connect()
        cur = conn.cursor()
        n = cur.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        conn.close()
        return n


