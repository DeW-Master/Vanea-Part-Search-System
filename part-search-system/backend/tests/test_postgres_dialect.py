# -*- coding: utf-8 -*-
"""PostgreSQL 方言单元测试 (不依赖真实 PG 实例)"""
import pytest

import database


# ---------- _pg_sql: SQLite -> PostgreSQL 语法转换 ----------

def test_pg_sql_placeholder_and_percent():
    assert database._pg_sql("SELECT * FROM t WHERE a = ?") == "SELECT * FROM t WHERE a = %s"
    assert database._pg_sql("SELECT * FROM t WHERE a LIKE '%PRO1%'") == \
        "SELECT * FROM t WHERE a LIKE '%%PRO1%%'"
    assert database._pg_sql("WHERE id IN (?, ?, ?)") == "WHERE id IN (%s, %s, %s)"
    # 混合: 字面 % + 占位符
    assert database._pg_sql("UPPER(x) LIKE '%A%' AND y = ?") == \
        "UPPER(x) LIKE '%%A%%' AND y = %s"


# ---------- _json_field: PG 方言分支 ----------

def test_json_field_postgres_branch(monkeypatch):
    monkeypatch.setattr(database, "DB_TYPE", "postgresql")
    assert database._json_field("Part Number") == "(data->>'Part Number')"
    assert database._json_field("Baulos_aggr") == "(data->>'Baulos_aggr')"
    with pytest.raises(ValueError):
        database._json_field("x' OR 1=1 --")


def test_json_field_sqlite_branch_unchanged(monkeypatch):
    monkeypatch.setattr(database, "DB_TYPE", "sqlite")
    assert database._json_field("Part Number") == 'json_extract(data, \'$."Part Number"\')'


# ---------- _PgCursor: INSERT ... RETURNING id ----------

class _FakePgCur:
    """模拟 psycopg2 游标"""
    def __init__(self):
        self.executed = None
        self.params = None
        self.description = None
        self._return_row = None

    def execute(self, sql, params=None):
        self.executed = sql
        self.params = params
        self.description = [("id",)] if "RETURNING" in sql else None
        self._return_row = [42] if "RETURNING" in sql else None

    def fetchone(self):
        return self._return_row


def test_pg_cursor_insert_appends_returning():
    cur = database._PgCursor(None, _FakePgCur())
    cur.execute("INSERT INTO t (a) VALUES (?)", ("x",))
    assert "RETURNING id" in cur._cur.executed
    assert cur._cur.executed.replace("%s", "?").replace("%%", "%") == \
        "INSERT INTO t (a) VALUES (?) RETURNING id"
    assert cur.lastrowid == 42


def test_pg_cursor_select_no_returning():
    cur = database._PgCursor(None, _FakePgCur())
    cur.execute("SELECT * FROM t WHERE a = ?", ("x",))
    assert "RETURNING" not in cur._cur.executed
    assert cur.lastrowid is None


# ---------- _PgConn 事务语义 ----------

class _FakePgConn:
    def __init__(self):
        self.committed = False
        self.rolled_back = False
        self._closed = False

    @property
    def closed(self):
        return self._closed

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self._closed = True


def test_pg_conn_close_rolls_back_uncommitted():
    fake = _FakePgConn()
    conn = database._PgConn(fake)
    conn.close()
    assert fake.rolled_back is True
    assert fake.closed is True
