# -*- coding: utf-8 -*-
"""
van.ea 车辆零件智能查询系统 - 数据库抽象层 (DBAL)
版本: build20260817
更新日期: 2026-08-17
"""

from __future__ import annotations

import os
import json as _json
import threading
import time
from contextlib import contextmanager
from typing import List, Dict, Any, Optional, Iterable, Tuple

from config import DB_TYPE, DB_PATH, get_database_url


# ==============================================================
# 1. SQL 方言适配器 - 处理语法差异
# ==============================================================
class SQLDialect:
    """SQL 方言适配器基类"""

    name: str = "base"

    # ---- JSON 提取 ----
    @staticmethod
    def json_extract_text(col: str, json_path: str) -> str:
        """提取 JSON 字段中的文本值"""
        raise NotImplementedError

    @staticmethod
    def json_extract_raw(col: str, json_path: str) -> str:
        """提取 JSON 字段中的 JSON 对象 (保留类型)"""
        raise NotImplementedError

    # ---- 模糊匹配 ----
    @staticmethod
    def ilike_expr(column_expr: str, pattern: str) -> str:
        """大小写不敏感匹配表达式"""
        raise NotImplementedError

    @staticmethod
    def json_text_like(col: str, json_path: str, pattern: str, case_insensitive: bool = True) -> str:
        """
        构造 WHERE 条件: json 字段的某个 key LIKE pattern
        例如: 查找 Part Number 以 A000 开头的记录
              json_text_like('data', 'Part Number', 'A000%')
        """
        raise NotImplementedError

    # ---- 聚合 ----
    @staticmethod
    def group_concat(expr: str, sep: str = "', '") -> str:
        """字符串聚合"""
        raise NotImplementedError

    # ---- UPSERT ----
    @staticmethod
    def on_conflict_ignore(pk_cols: List[str]) -> str:
        """INSERT ... ON CONFLICT DO NOTHING"""
        raise NotImplementedError

    # ---- 分页 ----
    @staticmethod
    def limit_offset(limit: int, offset: int = 0) -> str:
        return f"LIMIT {int(limit)} OFFSET {int(offset)}"

    # ---- 其他 ----
    @staticmethod
    def cast_to_text(expr: str) -> str:
        raise NotImplementedError

    @staticmethod
    def random_function() -> str:
        """随机排序函数"""
        raise NotImplementedError


class SQLiteDialect(SQLDialect):
    name = "sqlite"

    @staticmethod
    def json_extract_text(col: str, json_path: str) -> str:
        # json_path 统一转为 $.key 形式
        if not json_path.startswith('$'):
            json_path = '$.' + json_path
        return f"json_extract({col}, '{json_path}')"

    @staticmethod
    def json_extract_raw(col: str, json_path: str) -> str:
        return SQLiteDialect.json_extract_text(col, json_path)

    @staticmethod
    def ilike_expr(column_expr: str, pattern: str) -> str:
        # SQLite 的 LIKE 默认大小写不敏感 (取决于 collation)
        # 使用 LIKE + LOWER 保证行为一致
        safe_p = "'" + pattern.replace("'", "''") + "'"
        return f"LOWER({column_expr}) LIKE LOWER({safe_p})"

    @staticmethod
    def json_text_like(col: str, json_path: str, pattern: str, case_insensitive: bool = True) -> str:
        extracted = SQLiteDialect.json_extract_text(col, json_path)
        safe_p = "'" + pattern.replace("'", "''") + "'"
        if case_insensitive:
            return f"LOWER(COALESCE({extracted}, '')) LIKE LOWER({safe_p})"
        return f"COALESCE({extracted}, '') LIKE {safe_p}"

    @staticmethod
    def group_concat(expr: str, sep: str = "', '") -> str:
        return f"group_concat({expr}, {sep})"

    @staticmethod
    def on_conflict_ignore(pk_cols: List[str]) -> str:
        cols = ", ".join(pk_cols)
        return f"ON CONFLICT ({cols}) DO NOTHING"

    @staticmethod
    def cast_to_text(expr: str) -> str:
        return f"CAST({expr} AS TEXT)"

    @staticmethod
    def random_function() -> str:
        return "RANDOM()"


class PostgreSQLDialect(SQLDialect):
    name = "postgresql"

    @staticmethod
    def json_extract_text(col: str, json_path: str) -> str:
        # json_path 若是 $.key 形式，去掉 $.
        key = json_path
        if key.startswith('$.'):
            key = key[2:]
        # 使用 ->> 返回 text
        # 如果 key 含空格或特殊字符, 需要双引号
        if ' ' in key or '-' in key or '.' in key or '"' in key:
            safe = key.replace('"', '\"')
            return f"({col}->>'{safe}')"
        return f"({col}->>'{key}')"

    @staticmethod
    def json_extract_raw(col: str, json_path: str) -> str:
        key = json_path
        if key.startswith('$.'):
            key = key[2:]
        if ' ' in key or '-' in key or '.' in key or '"' in key:
            safe = key.replace('"', '\"')
            return f"({col}->'{safe}')"
        return f"({col}->'{key}')"

    @staticmethod
    def ilike_expr(column_expr: str, pattern: str) -> str:
        safe_p = f"'{pattern.replace("'", "''")}'"
        return f"{column_expr} ILIKE {safe_p}"

    @staticmethod
    def json_text_like(col: str, json_path: str, pattern: str, case_insensitive: bool = True) -> str:
        extracted = PostgreSQLDialect.json_extract_text(col, json_path)
        safe_p = f"'{pattern.replace("'", "''")}'"
        op = "ILIKE" if case_insensitive else "LIKE"
        return f"COALESCE({extracted}, '') {op} {safe_p}"

    @staticmethod
    def group_concat(expr: str, sep: str = "', '") -> str:
        return f"string_agg({expr}, {sep})"

    @staticmethod
    def on_conflict_ignore(pk_cols: List[str]) -> str:
        cols = ", ".join(pk_cols)
        return f"ON CONFLICT ({cols}) DO NOTHING"

    @staticmethod
    def cast_to_text(expr: str) -> str:
        return f"{expr}::TEXT"

    @staticmethod
    def random_function() -> str:
        return "RANDOM()"


def get_dialect() -> SQLDialect:
    if DB_TYPE == "postgresql":
        return PostgreSQLDialect
    return SQLiteDialect


# ==============================================================
# 2. 连接管理 - SQLite / PostgreSQL 统一接口
# ==============================================================
class DatabaseConnectionManager:
    """
    统一的连接管理器.
    - SQLite: 使用 sqlite3 模块, WAL 模式
    - PostgreSQL: 使用 psycopg2, 连接池
    对外暴露:
        - get_conn() -> 连接对象 (支持 with 语句)
        - execute(sql, params, fetch) -> 结果列表 / lastrowid
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, '_initialized', False):
            return
        self._initialized = True
        self.db_type = DB_TYPE
        self._pg_pool = None
        self._sqlite_lock = threading.Lock()

        if self.db_type == "postgresql":
            self._init_postgresql_pool()
        else:
            self._ensure_sqlite_dirs()
            self._init_sqlite()

        print(f"[DBAL] 数据库类型: {self.db_type}")

    # ---- SQLite 初始化 ----
    def _ensure_sqlite_dirs(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    def _init_sqlite(self):
        # 预热连接 + 设置 PRAGMA
        conn = self._sqlite_connect()
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA cache_size=-65536")  # 64MB
            conn.commit()
        finally:
            conn.close()

    def _sqlite_connect(self):
        import sqlite3
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # 注册 json_extract 等函数 (默认已内置)
        return conn

    # ---- PostgreSQL 初始化 ----
    def _init_postgresql_pool(self):
        try:
            import psycopg2.pool
            from config import (
                POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER,
                POSTGRES_PASSWORD, POSTGRES_DB, POSTGRES_SSLMODE,
            )
            self._pg_pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=2,
                maxconn=20,
                host=POSTGRES_HOST,
                port=POSTGRES_PORT,
                user=POSTGRES_USER,
                password=POSTGRES_PASSWORD,
                dbname=POSTGRES_DB,
                sslmode=POSTGRES_SSLMODE,
                connect_timeout=10,
            )
            # 验证连接
            c = self._pg_pool.getconn()
            try:
                with c.cursor() as cur:
                    cur.execute("SELECT 1")
            finally:
                self._pg_pool.putconn(c)
        except ImportError:
            raise RuntimeError(
                "DB_TYPE=postgresql 需要安装 psycopg2-binary, "
                "请执行: pip install psycopg2-binary"
            )
        except Exception as e:
            print(f"[DBAL] PostgreSQL 连接失败，应用将尝试降级: {e}")
            raise

    # ============== 对外: 连接上下文 ==============
    @contextmanager
    def get_conn(self):
        """
        获取连接上下文管理器.
        SQLite: 每次新建, 锁避免并发写入
        PostgreSQL: 从连接池取
        """
        if self.db_type == "postgresql":
            conn = self._pg_pool.getconn()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                self._pg_pool.putconn(conn)
        else:
            # SQLite: 只读操作不加锁，写操作串行化
            with self._sqlite_lock:
                conn = self._sqlite_connect()
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()

    # ============== 对外: 方便的执行方法 ==============
    def execute(self, sql: str, params: Tuple = None, *, fetch: str = None):
        """
        执行 SQL 并返回结果.
        fetch:
            - None: 返回 lastrowid (DML) 或 None
            - 'all': 返回 list[dict]
            - 'one': 返回 dict 或 None
            - 'val': 返回第一列第一行值
        """
        params = params or ()
        with self.get_conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)

            if fetch is None:
                try:
                    return cur.lastrowid
                except Exception:
                    return None

            rows = cur.fetchall()
            if fetch == 'val':
                if not rows:
                    return None
                return rows[0][0]

            # 转 dict
            if self.db_type == "postgresql":
                from psycopg2.extras import RealDictRow
                result = []
                for r in rows:
                    if isinstance(r, RealDictRow):
                        result.append(dict(r))
                    else:
                        result.append({cur.description[i][0]: r[i] for i in range(len(r))})
            else:
                result = [dict(r) for r in rows]

            if fetch == 'one':
                return result[0] if result else None
            return result

    def executemany(self, sql: str, seq_of_params: Iterable[Tuple]):
        """批量 DML"""
        with self.get_conn() as conn:
            cur = conn.cursor()
            cur.executemany(sql, list(seq_of_params))
            return cur.rowcount or 0

    # ============== 生命周期 ==============
    def shutdown(self):
        if self._pg_pool is not None:
            try:
                self._pg_pool.closeall()
                self._pg_pool = None
            except Exception:
                pass


# 便捷函数: 返回当前 dialect + connection manager
_dbal: Optional[DatabaseConnectionManager] = None


def get_db_manager() -> DatabaseConnectionManager:
    global _dbal
    if _dbal is None:
        with threading.Lock():
            if _dbal is None:
                _dbal = DatabaseConnectionManager()
    return _dbal


def dialect() -> SQLDialect:
    return get_dialect()
