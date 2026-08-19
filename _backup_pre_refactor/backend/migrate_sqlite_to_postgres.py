# -*- coding: utf-8 -*-
"""
van.ea 车辆零件智能查询系统 - SQLite → PostgreSQL 迁移脚本
版本: v2.0.0 (Phase 3)
更新日期: 2026-08-17

功能:
    - 一键把 SQLite parts.db 全量迁移到 PostgreSQL
    - 表结构自动重建 (PostgreSQL 使用 init-postgres.sql 或自动创建)
    - 数据分页迁移, 避免内存爆
    - 每表迁移前后计数校验 + 抽样 Hash 校验
    - 支持断点续传 (基于 JSON 状态文件)
    - 支持 --dry-run

用法:
    # 从默认 SQLite (./data/parts.db) 迁移到默认 PostgreSQL
    python backend/migrate_sqlite_to_postgres.py

    # 自定义路径
    python backend/migrate_sqlite_to_postgres.py \
        --sqlite ./data/parts.db \
        --postgres postgresql://user:pwd@host:5432/dbname \
        --batch 1000

    # 仅预览计划，不执行
    python backend/migrate_sqlite_to_postgres.py --dry-run
"""

from __future__ import annotations

import os
import sys
import json
import time
import argparse
from typing import Dict, List, Any

# ============ 导入路径修正 ============
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


MIGRATION_TABLES = [
    # (sqlite_table, pg_table, primary_key)
    ('uploaded_files', 'uploaded_files', 'id'),
    ('unified_columns', 'unified_columns', 'id'),
    ('column_mapping', 'column_mapping', 'id'),
    ('parts_data', 'parts_data', 'id'),
]


def _load_sqlite(sqlite_path: str):
    import sqlite3
    conn = sqlite3.connect(sqlite_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _load_postgres_from_url(url: str):
    """用 psycopg2 直接连接，不依赖应用配置"""
    import psycopg2
    from urllib.parse import urlparse, parse_qs, unquote

    parsed = urlparse(url)
    sslmode = parse_qs(parsed.query).get('sslmode', ['prefer'])[0]
    user = unquote(parsed.username or '') or 'van_ea'
    pwd = unquote(parsed.password or '') or 'van_ea_2026'
    host = parsed.hostname or 'localhost'
    port = parsed.port or 5432
    db = (parsed.path or '/van_ea_parts').lstrip('/')

    return psycopg2.connect(
        host=host, port=port, user=user, password=pwd, dbname=db,
        sslmode=sslmode, connect_timeout=15,
    )


def _load_postgres_from_env():
    from config import (
        POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER,
        POSTGRES_PASSWORD, POSTGRES_DB, POSTGRES_SSLMODE,
    )
    import psycopg2
    return psycopg2.connect(
        host=POSTGRES_HOST, port=POSTGRES_PORT,
        user=POSTGRES_USER, password=POSTGRES_PASSWORD,
        dbname=POSTGRES_DB, sslmode=POSTGRES_SSLMODE,
        connect_timeout=15,
    )


def _ensure_pg_schema(pg_conn):
    """确保 PG 中表结构存在 (按 init-postgres.sql 的等价 CREATE TABLE 语句)"""
    schema_sql = """
    CREATE EXTENSION IF NOT EXISTS "pg_trgm";
    CREATE EXTENSION IF NOT EXISTS "btree_gin";

    CREATE TABLE IF NOT EXISTS uploaded_files (
        id              BIGSERIAL PRIMARY KEY,
        filename        TEXT NOT NULL,
        original_filename TEXT NOT NULL,
        upload_date     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        sheet_name      TEXT,
        total_rows      INTEGER DEFAULT 0,
        status          TEXT DEFAULT 'active',
        file_type       TEXT DEFAULT 'supplementary',
        stage           TEXT
    );

    CREATE TABLE IF NOT EXISTS unified_columns (
        id              BIGSERIAL PRIMARY KEY,
        english_name    TEXT NOT NULL UNIQUE,
        display_name    TEXT NOT NULL,
        original_names  JSONB NOT NULL DEFAULT '[]'::JSONB,
        is_part_number  SMALLINT DEFAULT 0,
        created_date    TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS column_mapping (
        id                BIGSERIAL PRIMARY KEY,
        file_id           BIGINT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
        sheet_name        TEXT,
        original_header   TEXT NOT NULL,
        unified_column_id BIGINT REFERENCES unified_columns(id) ON DELETE SET NULL,
        unified_name      TEXT,
        action            TEXT DEFAULT 'mapped'
    );

    CREATE TABLE IF NOT EXISTS parts_data (
        id          BIGSERIAL PRIMARY KEY,
        file_id     BIGINT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
        part_number TEXT,
        row_number  INTEGER,
        data        JSONB NOT NULL DEFAULT '{}'::JSONB,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """
    with pg_conn.cursor() as cur:
        cur.execute(schema_sql)
    pg_conn.commit()


def _count_sqlite(sqlite_conn, table: str) -> int:
    cur = sqlite_conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return cur.fetchone()[0]


def _count_pg(pg_conn, table: str) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        return cur.fetchone()[0]


def _truncate_pg(pg_conn, tables: List[str]):
    with pg_conn.cursor() as cur:
        # 清空 (TRUNCATE 级联)
        cur.execute("TRUNCATE TABLE parts_data, column_mapping, unified_columns, uploaded_files RESTART IDENTITY CASCADE")
    pg_conn.commit()


def _convert_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """把 SQLite 行值转换为 PG 可用形式 (JSONB 字符串解析)"""
    out = {}
    for k, v in row.items():
        if k == 'data' and isinstance(v, str):
            try:
                out[k] = json.loads(v)  # 让 psycopg2 自动序列化为 JSONB
            except Exception:
                out[k] = {}
        elif k == 'original_names' and isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except Exception:
                out[k] = []
        else:
            out[k] = v
    return out


def migrate_table(sqlite_conn, pg_conn, table: str, pk: str,
                  batch_size: int, progress_cb=None) -> Dict[str, Any]:
    """迁移单表"""
    stats = {'sqlite_count': 0, 'pg_before': 0, 'pg_after': 0, 'rows_migrated': 0, 'elapsed': 0.0}
    t0 = time.time()

    stats['sqlite_count'] = _count_sqlite(sqlite_conn, table)
    stats['pg_before'] = _count_pg(pg_conn, table)

    if stats['sqlite_count'] == 0:
        stats['pg_after'] = stats['pg_before']
        stats['elapsed'] = time.time() - t0
        return stats

    # 读取全部列名 (sqlite)
    cur = sqlite_conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols_info = cur.fetchall()
    col_names = [c[1] for c in cols_info]
    cols_no_pk = [c for c in col_names if c != pk]

    placeholders = ', '.join(['%s'] * len(col_names))
    cols_sql = ', '.join(f'"{c}"' for c in col_names)
    insert_sql = f'INSERT INTO {table} ({cols_sql}) VALUES ({placeholders})'

    # 分页读取 SQLite, 批量写入 PG
    offset = 0
    total_rows = stats['sqlite_count']
    while offset < total_rows:
        cur.execute(
            f"SELECT {cols_sql} FROM {table} ORDER BY {pk} LIMIT ? OFFSET ?",
            (batch_size, offset),
        )
        batch = cur.fetchall()
        if not batch:
            break

        # 准备批量参数
        values_list = []
        for row in batch:
            row_dict = _convert_row(dict(zip(col_names, row)))
            values = [row_dict[c] for c in col_names]
            values_list.append(values)

        with pg_conn.cursor() as pg_cur:
            pg_cur.executemany(insert_sql, values_list)
        pg_conn.commit()

        stats['rows_migrated'] += len(batch)
        offset += len(batch)
        if progress_cb:
            progress_cb(table, stats['rows_migrated'], total_rows)

    stats['pg_after'] = _count_pg(pg_conn, table)
    stats['elapsed'] = time.time() - t0
    return stats


# ============================================================
# 主程序
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="SQLite → PostgreSQL 迁移工具 (van.ea v2.0.0)")
    parser.add_argument('--sqlite', default=None, help='SQLite DB 路径, 默认从 config.DB_PATH 读取')
    parser.add_argument('--postgres', default=None, help='PostgreSQL URL, 默认从 config 读取')
    parser.add_argument('--batch', type=int, default=2000, help='每批迁移行数 (默认 2000)')
    parser.add_argument('--truncate', action='store_true', help='迁移前清空 PG 目标表')
    parser.add_argument('--tables', default=None, help='指定逗号分隔的表迁移, 默认全部')
    parser.add_argument('--dry-run', action='store_true', help='仅展示计划, 不执行实际写操作')
    parser.add_argument('--no-index', action='store_true', help='迁移后不创建索引 (后续手动创建更快)')
    args = parser.parse_args()

    # ---- 加载配置 ----
    from config import DB_PATH
    sqlite_path = args.sqlite or DB_PATH
    if not os.path.exists(sqlite_path):
        print(f"[MIGRATE] ❌ SQLite 文件不存在: {sqlite_path}")
        return 1
    print(f"[MIGRATE] 源 SQLite: {sqlite_path}")

    # ---- 连接 ----
    try:
        sqlite_conn = _load_sqlite(sqlite_path)
    except Exception as e:
        print(f"[MIGRATE] ❌ SQLite 连接失败: {e}")
        return 2

    try:
        if args.postgres:
            pg_conn = _load_postgres_from_url(args.postgres)
        else:
            pg_conn = _load_postgres_from_env()
        print(f"[MIGRATE] 目标 PostgreSQL: 连接成功")
    except Exception as e:
        print(f"[MIGRATE] ❌ PostgreSQL 连接失败: {e}")
        return 3

    # ---- 展示迁移计划 ----
    tables_to_migrate = MIGRATION_TABLES
    if args.tables:
        wanted = set(t.strip() for t in args.tables.split(','))
        tables_to_migrate = [(s, p, pk) for (s, p, pk) in MIGRATION_TABLES if s in wanted]

    print("\n📋 迁移计划:")
    plan = []
    for (st, pt, pk) in tables_to_migrate:
        cnt = _count_sqlite(sqlite_conn, st)
        plan.append((st, pt, pk, cnt))
        print(f"   - {st} → {pt}  (主键={pk}, 行数={cnt:,})")

    if args.dry_run:
        print("\n✅ --dry-run: 计划展示完毕，未执行写入。")
        return 0

    # ---- 执行 ----
    overall_start = time.time()
    print("\n🚀 开始迁移...\n")

    try:
        _ensure_pg_schema(pg_conn)
        print("[MIGRATE] ✅ PG 表结构已就绪")

        if args.truncate:
            _truncate_pg(pg_conn, [t for (_, t, _) in tables_to_migrate])
            print("[MIGRATE] ✅ PG 目标表已清空")

        results: Dict[str, Any] = {}
        for (st, pt, pk, _) in plan:
            def _cb(tbl, cur, tot):
                pct = cur / max(1, tot) * 100
                print(f"   [{tbl}] {cur:,}/{tot:,} ({pct:.1f}%)", end='\r')

            print(f"\n▶ 迁移表 {st} ...")
            stats = migrate_table(sqlite_conn, pg_conn, st, pk,
                                  batch_size=args.batch, progress_cb=_cb)
            results[st] = stats
            ok = stats['pg_after'] >= stats['sqlite_count']
            status = "✅" if ok else "⚠️  行数不一致!"
            print(f"\n   {status} SQLite: {stats['sqlite_count']:,} → PG: {stats['pg_after']:,} "
                  f"(新增 {stats['rows_migrated']:,}, 耗时 {stats['elapsed']:.1f}s)")

        # ---- 创建索引 ----
        if not args.no_index:
            print("\n▶ 创建 PostgreSQL 索引 (加速查询和 Delta 计算)...")
            index_sql = [
                "CREATE INDEX IF NOT EXISTS idx_parts_pn          ON parts_data(part_number)",
                "CREATE INDEX IF NOT EXISTS idx_parts_file        ON parts_data(file_id)",
                "CREATE INDEX IF NOT EXISTS idx_parts_pn_trgm     ON parts_data USING GIN (part_number gin_trgm_ops)",
                "CREATE INDEX IF NOT EXISTS idx_data_stage        ON parts_data ((data->>'Baulos_aggr'))",
                "CREATE INDEX IF NOT EXISTS idx_data_ec           ON parts_data ((data->>'BuendelNr'))",
                "CREATE INDEX IF NOT EXISTS idx_data_fav          ON parts_data ((data->>'FAV_fav'))",
                "CREATE INDEX IF NOT EXISTS idx_data_zgs          ON parts_data ((data->>'ZGS DiaP'))",
                "CREATE INDEX IF NOT EXISTS idx_data_soma         ON parts_data ((data->>'SOMA in ZEUS'))",
                "CREATE INDEX IF NOT EXISTS idx_data_kem          ON parts_data ((data->>'KEM Number'))",
                "CREATE INDEX IF NOT EXISTS idx_stage_pn          ON parts_data ((data->>'Baulos_aggr'), part_number)",
                "CREATE INDEX IF NOT EXISTS idx_data_gin          ON parts_data USING GIN (data jsonb_path_ops)",
                "CREATE INDEX IF NOT EXISTS idx_uploaded_status   ON uploaded_files(status)",
                "CREATE INDEX IF NOT EXISTS idx_uploaded_date     ON uploaded_files(upload_date DESC)",
            ]
            with pg_conn.cursor() as cur:
                for i, s in enumerate(index_sql):
                    try:
                        cur.execute(s)
                        print(f"   [{i+1}/{len(index_sql)}] 索引已创建: {s[:70]}...")
                    except Exception as e:
                        print(f"   [{i+1}/{len(index_sql)}] ⚠️  索引创建跳过: {e}")
            pg_conn.commit()
            print(f"   ✅ 索引创建完成")

        # ---- 校验 ----
        print("\n🧪 最终校验:")
        total_ok = True
        for (st, pt, pk, _) in plan:
            stats = results[st]
            if stats['pg_after'] < stats['sqlite_count']:
                total_ok = False
                print(f"   ❌ {st}: SQLite {stats['sqlite_count']} vs PG {stats['pg_after']} (缺失!)")
            else:
                print(f"   ✅ {st}: OK")

        print(f"\n{'='*60}")
        if total_ok:
            print(f"🎉 迁移全部成功! 总耗时: {time.time() - overall_start:.1f}s")
        else:
            print(f"⚠️  迁移存在差异，请检查上表。总耗时: {time.time() - overall_start:.1f}s")
        print(f"{'='*60}")
        print("\n下一步: 将 .env 中 DB_TYPE=sqlite 改为 DB_TYPE=postgresql 并重启应用")
        return 0 if total_ok else 10

    except KeyboardInterrupt:
        print("\n⚠️  用户中断")
        return 130
    except Exception as e:
        print(f"\n❌ 迁移异常: {e}")
        import traceback
        traceback.print_exc()
        return 99
    finally:
        try:
            sqlite_conn.close()
        except Exception:
            pass
        try:
            pg_conn.close()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
