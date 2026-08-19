# -*- coding: utf-8 -*-
"""数据层核心测试: 导入/搜索/复杂搜索(含注入防护)/统计/Delta/单元格更新"""
import os
import pytest

import database
from conftest import make_xlsx

HEADERS = ["Part Number", "ZGS DiaP", "Baulos_aggr", "BuendelNr", "Part Name"]
MAPPING = [
    {"original_header": "Part Number", "unified_name": "Part Number", "action": "mapped"},
    {"original_header": "ZGS DiaP", "unified_name": "ZGS DiaP", "action": "mapped"},
    {"original_header": "Baulos_aggr", "unified_name": "Baulos_aggr", "action": "mapped"},
    {"original_header": "BuendelNr", "unified_name": "BuendelNr", "action": "mapped"},
    {"original_header": "Part Name", "unified_name": "Part Name", "action": "mapped"},
]


def _three_stage_xlsx(path):
    """生成 pre-TO / TO1 / TO2 三阶段测试数据 (含 ZGS 升级/新增/停用场景)"""
    return make_xlsx(path, {
        "pre-TO": {
            "headers": HEADERS,
            "rows": [
                ["PN001", "1", "Pre TEO", "EC-1", "Part One"],
                ["PN002", "1", "Pre TEO", "EC-1", "Part Two"],
                ["PN003", "1", "Pre TEO", "EC-1", "Part Three"],
            ],
        },
        "TO1": {
            "headers": HEADERS,
            "rows": [
                ["PN001", "2", "TO1 PRO1", "EC-2", "Part One"],
                ["PN002", "1", "TO1 PRO1", "EC-1", "Part Two"],
                ["PN004", "1", "TO1 PRO1", "EC-3", "Part Four"],
            ],
        },
        "TO2": {
            "headers": HEADERS,
            "rows": [
                ["PN001", "2", "TO2 PRO2", "EC-2", "Part One"],
                ["PN002", "3", "TO2 PRO2", "EC-4", "Part Two"],
                ["PN004", "2", "TO2 PRO2", "EC-5", "Part Four"],
            ],
        },
    })


@pytest.fixture
def imported_db(db, tmp_path):
    """导入三阶段数据后的 db_manager"""
    xlsx = _three_stage_xlsx(str(tmp_path / "bom.xlsx"))
    sheets = {name: MAPPING for name in ["pre-TO", "TO1", "TO2"]}
    results = db.import_excel_data(xlsx, "bom.xlsx", sheets,
                                   file_type="BOM", stage="pre-TO")
    assert len(results) == 3, f"三阶段导入失败: {results}"
    return db


# ---------- _json_field ----------

def test_json_field_valid():
    assert database._json_field("Part Number") == 'json_extract(data, \'$."Part Number"\')'
    assert database._json_field("Baulos_aggr") == 'json_extract(data, \'$."Baulos_aggr"\')'


@pytest.mark.parametrize("bad", ["x' OR 1=1 --", 'a"b', "a]b", "", None, "a\\b", "a[b"])
def test_json_field_rejects_illegal(bad):
    with pytest.raises(ValueError):
        database._json_field(bad)


# ---------- 导入与搜索 ----------

def test_import_and_search_by_pn(imported_db):
    rows = imported_db.search_by_part_number("PN001", exact=True)
    assert rows, "应能按精确 PN 搜索到记录"
    assert all(r.get("Part Number") == "PN001" for r in rows)


def test_search_by_field(imported_db):
    rows = imported_db.search_by_field("Part Name", "Part One")
    assert rows and rows[0].get("Part Number") == "PN001"


def test_search_by_field_invalid_field_name(imported_db):
    """非法字段名应返回空列表而非抛错"""
    assert imported_db.search_by_field('x" OR 1=1 --', "a") == []


def test_compare_records(imported_db):
    res = imported_db.compare_records("Part Number", "PN001", "PN002")
    assert res["success"] is True
    assert res["diff_count"] >= 1  # ZGS/EC/名称有差异


def test_compare_records_invalid_field(imported_db):
    res = imported_db.compare_records('x" OR 1=1 --', "a", "b")
    assert res["success"] is False


def test_search_complex_legal(imported_db):
    conds = [{"field": "Part Number", "value": "PN", "operator": "like"}]
    rows = imported_db.search_complex(conds)
    assert len(rows) >= 3


def test_search_complex_ignores_injection_field(imported_db):
    """非法字段名应被跳过而不是注入 SQL 或抛错"""
    conds = [
        {"field": 'x" OR 1=1 --', "value": "a", "operator": "like"},
        {"field": "Part Number", "value": "PN001", "operator": "like"},
    ]
    rows = imported_db.search_complex(conds)
    assert rows, "合法条件应正常执行"
    assert all(r.get("Part Number") == "PN001" for r in rows)


# ---------- 统计 / 列 ----------

def test_get_stats(imported_db):
    stats = imported_db.get_stats()
    assert stats.get("total_records", 0) >= 8  # 3+3+3 行 (跳过空行)


def test_get_all_columns(imported_db):
    cols = imported_db.get_all_columns()
    names = [c["english_name"] for c in cols]
    assert "Part Number" in names and "Baulos_aggr" in names


# ---------- 单元格更新 ----------

def test_update_cell(imported_db):
    row = imported_db.search_by_part_number("PN001", exact=True)[0]
    ok = imported_db.update_cell(row["_record_id"], "Part Name", "Renamed Part")
    assert ok
    updated = imported_db.search_by_part_number("PN001", exact=True)[0]
    assert updated.get("Part Name") == "Renamed Part"


# ---------- Delta 计算 ----------

def test_delta_pre_to_to1(imported_db):
    res = imported_db.calculate_delta("pre-TO", "TO1")
    assert res.get("success", True), res
    types = {d["match_type"] for d in res["deltas"]}
    # PN001: ZGS 1->2 升级; PN002: 相同跳过; PN003: 停用; PN004: 新增
    assert "zgs_upgraded" in types
    assert "discontinued_part" in types
    assert "new_part" in types
    assert "PN003" in {d["part_number"] for d in res["deltas"]}
    assert "PN004" in {d["part_number"] for d in res["deltas"]}


def test_delta_to1_to2(imported_db):
    res = imported_db.calculate_delta("TO1", "TO2")
    types = {d["match_type"] for d in res["deltas"]}
    # PN002: ZGS 1->3 升级; PN001 相同跳过; PN004: 1->2 升级
    assert "zgs_upgraded" in types
    assert "PN002" in {d["part_number"] for d in res["deltas"]}


def test_delta_dashboard_data(imported_db):
    data = imported_db.get_delta_dashboard_data()
    assert "stages" in data and "delta1" in data and "delta2" in data
    assert len(data["stages"]) >= 3
