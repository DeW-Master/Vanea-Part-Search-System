# -*- coding: utf-8 -*-
"""
PostgreSQL 端到端集成测试 (默认跳过)。

启用方式 (需先创建测试数据库, 例如):
    docker run -d --name van-ea-pg-test -e POSTGRES_PASSWORD=van_ea_2026 \
        -e POSTGRES_DB=van_ea_parts_test -p 5433:5432 postgres:16-alpine

    POSTGRES_TEST_DSN="postgresql://van_ea:van_ea_2026@localhost:5433/van_ea_parts_test" \
        python -m pytest tests/test_postgres_integration.py -v

注意: 测试会在该库中建表并写入数据 (TRUNCATE 清空), 请勿指向生产库。
"""
import os
import subprocess
import sys

import pytest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DSN = os.environ.get("POSTGRES_TEST_DSN", "")

pytestmark = pytest.mark.skipif(
    not DSN,
    reason="未设置 POSTGRES_TEST_DSN, 跳过 PostgreSQL 集成测试 (方言单元测试仍会执行)",
)

# 子进程脚本: 在独立进程中以 DB_TYPE=postgresql 跑完整业务流
_SUB_SCRIPT = r"""
import os, sys, json
sys.path.insert(0, sys.argv[1])

import database

def _clear():
    conn = database.get_db()
    try:
        for t in ("parts_data", "column_mapping", "uploaded_files", "unified_columns"):
            conn.execute("TRUNCATE {0} RESTART IDENTITY CASCADE".format(t))
        conn.commit()
    finally:
        conn.close()

def _make_xlsx(path):
    import openpyxl
    headers = ["Part Number", "ZGS DiaP", "Baulos_aggr", "BuendelNr", "Part Name"]
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    spec = {
        "pre-TO": [["PN001","1","Pre TEO","EC-1","One"],["PN002","1","Pre TEO","EC-1","Two"],["PN003","1","Pre TEO","EC-1","Three"]],
        "TO1":    [["PN001","2","TO1 PRO1","EC-2","One"],["PN002","1","TO1 PRO1","EC-1","Two"],["PN004","1","TO1 PRO1","EC-3","Four"]],
        "TO2":    [["PN001","2","TO2 PRO2","EC-2","One"],["PN002","3","TO2 PRO2","EC-4","Two"],["PN004","2","TO2 PRO2","EC-5","Four"]],
    }
    for name, rows in spec.items():
        ws = wb.create_sheet(name)
        ws.append(headers)
        for r in rows:
            ws.append(r)
    wb.save(path)

def main():
    _clear()
    mapping = [{"original_header": h, "unified_name": h, "action": "mapped"} for h in
               ["Part Number", "ZGS DiaP", "Baulos_aggr", "BuendelNr", "Part Name"]]
    xlsx = os.path.join(sys.argv[2], "bom.xlsx")
    _make_xlsx(xlsx)
    results = database.db_manager.import_excel_data(
        xlsx, "bom.xlsx", {n: mapping for n in ["pre-TO", "TO1", "TO2"]},
        file_type="BOM", stage="pre-TO")
    assert len(results) == 3, results

    rows = database.db_manager.search_by_part_number("PN001", exact=True)
    assert rows and rows[0].get("Part Number") == "PN001", rows

    # 复杂搜索 + 注入防护
    r2 = database.db_manager.search_complex([{"field": "Part Number", "value": "PN", "operator": "like"}])
    assert len(r2) >= 3
    r3 = database.db_manager.search_complex([
        {"field": 'x" OR 1=1 --', "value": "a", "operator": "like"},
        {"field": "Part Number", "value": "PN001", "operator": "like"},
    ])
    assert r3 and all(x.get("Part Number") == "PN001" for x in r3), r3

    # Delta
    delta = database.db_manager.calculate_delta("pre-TO", "TO1")
    types = {d["match_type"] for d in delta["deltas"]}
    assert {"zgs_upgraded", "new_part", "discontinued_part"} <= types, delta
    dash = database.db_manager.get_delta_dashboard_data()
    assert "stages" in dash and "delta1" in dash and "delta2" in dash

    # 单元格更新
    rec = rows[0]
    assert database.db_manager.update_cell(rec["_record_id"], "Part Name", "Renamed")
    up = database.db_manager.search_by_part_number("PN001", exact=True)[0]
    assert up.get("Part Name") == "Renamed", up

    print("PG_INTEGRATION_OK")

main()
"""


def test_postgres_full_flow(tmp_path):
    env = dict(os.environ)
    env.update({
        "DB_TYPE": "postgresql",
        "CACHE_ENABLED": "false",
        "DELTA_REFRESH_INTERVAL": "0",
        "SESSION_TYPE": "null",
    })
    # 从 DSN 解析 POSTGRES_* 环境变量
    from urllib.parse import urlparse
    u = urlparse(DSN)
    env.update({
        "POSTGRES_HOST": u.hostname or "localhost",
        "POSTGRES_PORT": str(u.port or 5432),
        "POSTGRES_USER": u.username or "van_ea",
        "POSTGRES_PASSWORD": u.password or "van_ea_2026",
        "POSTGRES_DB": (u.path or "/van_ea_parts").lstrip("/"),
        "POSTGRES_SSLMODE": "prefer",
    })
    proc = subprocess.run(
        [sys.executable, "-c", _SUB_SCRIPT, BACKEND_DIR, str(tmp_path)],
        capture_output=True, text=True, timeout=180, env=env,
    )
    assert proc.returncode == 0, f"PG 集成失败:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert "PG_INTEGRATION_OK" in proc.stdout
