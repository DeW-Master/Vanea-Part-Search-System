# -*- coding: utf-8 -*-
"""数据层核心测试: 导入/搜索/复杂搜索(含注入防护)/统计/Delta/单元格更新

数据模型 (与生产一致):
- BOM 文件按阶段分文件导入 (file_type='BOM', stage=pre-TO/TO1/TO2),
  只含 Part Number / ZGS / Part Name, 不含 EC 等业务字段
- ENIGMA 主表单独导入 (file_type='supplementary', 无 stage),
  提供 EC(Bundle Number)/FAV/KEM/SOMA/状态字段, 按 PN 富化到各阶段

场景设计:
- PN001: 三阶段都在, ZGS 1->2->2 (delta1 升级, delta2 跳过), ENIGMA 有 EC+FAV+KEM+SOMA=ja
- PN002: 三阶段都在, ZGS 1->1->3 (delta1 跳过, delta2 升级), ENIGMA 有 EC 无 FAV
- PN003: 仅 pre-TO (delta1 停用), ENIGMA 有 EC+FAV
- PN004: 仅 TO1/TO2 (delta1 新增), ENIGMA 两行 (多 EC 值 EC-4+EC-4B) + FAV + SOMA=ja
- PN005: 三阶段都在但原始 PN 带尾部空格 (数据质量场景),
  norm 后同 PN 同 ZGS -> 不产生幻影 delta; 不在 ENIGMA
"""
import os
import pytest

import database
from conftest import make_xlsx

BOM_HEADERS = ["Part Number", "ZGS", "Part Name"]
BOM_MAPPING = [
    {"original_header": h, "unified_name": h, "action": "mapped"} for h in BOM_HEADERS
]

ENIGMA_HEADERS = ["Part Number", "Bundle Number", "FAV", "KEM Number",
                  "SOMA in ZEUS", "ProzessStatusDetail", "FAV Status Short"]
ENIGMA_MAPPING = [
    {"original_header": h, "unified_name": h, "action": "mapped"} for h in ENIGMA_HEADERS
]

BOM_ROWS = {
    "pre-TO": [
        ["PN001", "1", "Part One"],
        ["PN002", "1", "Part Two"],
        ["PN003", "1", "Part Three"],
        ["PN005  ", "1", "Part Five"],   # 尾部空格 (BOM 导出数据质量)
    ],
    "TO1": [
        ["PN001", "2", "Part One"],
        ["PN002", "1", "Part Two"],
        ["PN004", "1", "Part Four"],
        ["PN005", "1", "Part Five"],
    ],
    "TO2": [
        ["PN001", "2", "Part One"],
        ["PN002", "3", "Part Two"],
        ["PN004", "2", "Part Four"],
        ["PN005  ", "1", "Part Five"],
    ],
}

ENIGMA_ROWS = [
    ["PN001", "EC-1", "FAV-1", "KEM-1", "ja",   "released", "active"],
    ["PN002", "EC-2", "",      "",      "nein", "open",     ""],
    ["PN003", "EC-0", "FAV-3", "",      "nein", "closed",   "done"],
    ["PN004", "EC-4", "FAV-4", "",      "ja",   "open",     "active"],
    ["PN004", "EC-4B", "",     "",      "",     "",         ""],  # 同 PN 多 EC 值
]


def _import_stage(db, tmp_path, stage):
    """导入单个阶段的 BOM 文件"""
    xlsx = make_xlsx(str(tmp_path / f"bom_{stage}.xlsx"), {
        stage: {"headers": BOM_HEADERS, "rows": BOM_ROWS[stage]},
    })
    results = db.import_excel_data(xlsx, f"bom_{stage}.xlsx", {stage: BOM_MAPPING},
                                   file_type="BOM", stage=stage)
    assert len(results) == 1, f"{stage} 导入失败: {results}"


def _import_enigma(db, tmp_path):
    """导入 ENIGMA 主表 (supplementary)"""
    xlsx = make_xlsx(str(tmp_path / "enigma.xlsx"), {
        "ENIGMA": {"headers": ENIGMA_HEADERS, "rows": ENIGMA_ROWS},
    })
    results = db.import_excel_data(xlsx, "enigma.xlsx", {"ENIGMA": ENIGMA_MAPPING},
                                   file_type="supplementary")
    assert len(results) == 1, f"ENIGMA 导入失败: {results}"


@pytest.fixture
def full_db(db, tmp_path):
    """三阶段 BOM + ENIGMA 全部导入后的 db_manager"""
    for stage in ("pre-TO", "TO1", "TO2"):
        _import_stage(db, tmp_path, stage)
    _import_enigma(db, tmp_path)
    return db


# ---------- _json_field ----------

def test_json_field_valid():
    assert database._json_field("Part Number") == 'json_extract(data, \'$."Part Number"\')'
    assert database._json_field("ZGS") == 'json_extract(data, \'$."ZGS"\')'


@pytest.mark.parametrize("bad", ["x' OR 1=1 --", 'a"b', "a]b", "", None, "a\\b", "a[b"])
def test_json_field_rejects_illegal(bad):
    with pytest.raises(ValueError):
        database._json_field(bad)


# ---------- 导入与搜索 ----------

def test_import_and_search_by_pn(full_db):
    rows = full_db.search_by_part_number("PN001", exact=True)
    assert rows, "应能按精确 PN 搜索到记录"
    assert all(r.get("Part Number") == "PN001" for r in rows)


def test_search_by_field(full_db):
    rows = full_db.search_by_field("Part Name", "Part One")
    assert rows and rows[0].get("Part Number") == "PN001"


def test_search_by_field_invalid_field_name(full_db):
    """非法字段名应返回空列表而非抛错"""
    assert full_db.search_by_field('x" OR 1=1 --', "a") == []


def test_compare_records(full_db):
    res = full_db.compare_records("Part Number", "PN001", "PN002")
    assert res["success"] is True
    assert res["diff_count"] >= 1  # 名称等有差异


def test_compare_records_invalid_field(full_db):
    res = full_db.compare_records('x" OR 1=1 --', "a", "b")
    assert res["success"] is False


def test_search_complex_legal(full_db):
    conds = [{"field": "Part Number", "value": "PN", "operator": "like"}]
    rows = full_db.search_complex(conds)
    assert len(rows) >= 9  # 12 行 BOM + 5 行 ENIGMA 均含 PN


def test_search_complex_ignores_injection_field(full_db):
    """非法字段名应被跳过而不是注入 SQL 或抛错"""
    conds = [
        {"field": 'x" OR 1=1 --', "value": "a", "operator": "like"},
        {"field": "Part Number", "value": "PN001", "operator": "like"},
    ]
    rows = full_db.search_complex(conds)
    assert rows, "合法条件应正常执行"
    assert all(r.get("Part Number") == "PN001" for r in rows)


# ---------- 统计 / 列 ----------

def test_get_stats(full_db):
    stats = full_db.get_stats()
    assert stats.get("total_records", 0) >= 12  # 12 行 BOM + 5 行 ENIGMA


def test_get_all_columns(full_db):
    cols = full_db.get_all_columns()
    names = [c["english_name"] for c in cols]
    assert "Part Number" in names and "ZGS" in names
    assert "Bundle Number" in names  # ENIGMA 列也注册为统一列


# ---------- 单元格更新 ----------

def test_update_cell(full_db):
    row = full_db.search_by_part_number("PN001", exact=True)[0]
    ok = full_db.update_cell(row["_record_id"], "Part Name", "Renamed Part")
    assert ok
    updated = full_db.search_by_part_number("PN001", exact=True)[0]
    assert updated.get("Part Name") == "Renamed Part"


# ---------- Delta 计算: 匹配类型 ----------

def test_delta_pre_to_to1(full_db):
    res = full_db.calculate_delta("pre-TO", "TO1")
    assert res.get("success", True), res
    by_pn = {d["part_number"]: d["match_type"] for d in res["deltas"]}
    # PN001: ZGS 1->2 升级; PN002: 相同跳过; PN003: 停用; PN004: 新增
    assert by_pn == {"PN001": "zgs_upgraded", "PN003": "discontinued_part",
                     "PN004": "new_part"}


def test_delta_to1_to2(full_db):
    res = full_db.calculate_delta("TO1", "TO2")
    by_pn = {d["part_number"]: d["match_type"] for d in res["deltas"]}
    # PN002: ZGS 1->3, PN004: 1->2; PN001 相同跳过
    assert by_pn == {"PN002": "zgs_upgraded", "PN004": "zgs_upgraded"}


def test_delta_no_phantom_from_whitespace(full_db):
    """带尾部空格的 PN005 三阶段都在且 ZGS 相同 -> 不产生幻影 新增/停用 delta"""
    for frm, to in (("pre-TO", "TO1"), ("TO1", "TO2")):
        res = full_db.calculate_delta(frm, to)
        assert "PN005" not in {d["part_number"] for d in res["deltas"]}


def test_delta_part_number_filter(full_db):
    """part_number 模糊过滤应只返回匹配的 delta"""
    res = full_db.calculate_delta("pre-TO", "TO1", part_number="PN001")
    assert res["total"] == 1
    assert res["deltas"][0]["match_type"] == "zgs_upgraded"


def test_delta_part_number_filter_no_from_match(full_db):
    """过滤后前阶段无匹配 (PN004 不在 pre-TO) -> 沿用阶段校验, 返回 success=False"""
    res = full_db.calculate_delta("pre-TO", "TO1", part_number="PN004")
    assert res.get("success") is False
    assert "pre-TO" in res.get("error", "")


# ---------- Delta 数据校验（阶段非空） ----------

def test_delta_fails_when_from_stage_empty(db, tmp_path):
    """前阶段无数据时 calculate_delta 应返回 success=False 错误"""
    _import_stage(db, tmp_path, "TO1")
    res = db.calculate_delta("pre-TO", "TO1")
    assert res.get("success") is False
    assert "pre-TO" in res.get("error", "")


def test_delta_fails_when_to_stage_empty(db, tmp_path):
    """后阶段无数据时 calculate_delta 应返回 success=False"""
    _import_stage(db, tmp_path, "pre-TO")
    res = db.calculate_delta("pre-TO", "TO1")
    assert res.get("success") is False
    assert "TO1" in res.get("error", "")


# ---------- Delta summary 统计 ----------

def test_delta_summary_pre_to_to1(full_db):
    s = full_db.calculate_delta("pre-TO", "TO1")["summary"]
    assert s["total_records"] == 3
    assert s["zgs_upgraded"] == 1
    assert s["new_parts"] == 1
    assert s["discontinued_parts"] == 1
    assert s["ec_added"] == 1      # PN004 (新增) ENIGMA 有 EC
    assert s["has_ec"] == 2        # PN001 + PN004
    assert s["zeus_updated"] == 2  # PN001 (FAV-1) + PN004 (FAV-4)


def test_delta_summary_to1_to2(full_db):
    s = full_db.calculate_delta("TO1", "TO2")["summary"]
    assert s["total_records"] == 2
    assert s["zgs_upgraded"] == 2
    assert s["new_parts"] == 0
    assert s["discontinued_parts"] == 0
    assert s["ec_added"] == 0      # 两阶段 EC 均来自同一 ENIGMA, 无"新增"
    assert s["has_ec"] == 2        # PN002 (EC-2) + PN004
    assert s["zeus_updated"] == 1  # 仅 PN004 有 FAV


# ---------- EC / ZEUS(FAV) 标记 (值来自 ENIGMA 富化) ----------

def test_delta_ec_and_zeus_flags(full_db):
    by_pn = {d["part_number"]: d for d in full_db.calculate_delta("pre-TO", "TO1")["deltas"]}
    # PN001: ENIGMA 有 EC-1 + FAV-1
    assert by_pn["PN001"]["has_ec"] is True
    assert by_pn["PN001"]["ec_value"] == "EC-1"
    assert by_pn["PN001"]["zeus_updated"] is True
    assert by_pn["PN001"]["fav_value"] == "FAV-1"
    # PN004: ENIGMA 多 EC 值 (合并后取后导入行), 有 FAV-4
    assert by_pn["PN004"]["has_ec"] is True
    assert by_pn["PN004"]["zeus_updated"] is True
    # PN003: 停用 -> 无后阶段记录, 无 EC
    assert by_pn["PN003"]["has_ec"] is False
    assert by_pn["PN003"]["zeus_updated"] is False


def test_delta_zeus_not_updated_without_fav(full_db):
    """PN002 在 ENIGMA 有 EC 但无 FAV -> has_ec=True 且 zeus_updated=False"""
    by_pn = {d["part_number"]: d for d in full_db.calculate_delta("TO1", "TO2")["deltas"]}
    assert by_pn["PN002"]["has_ec"] is True
    assert by_pn["PN002"]["ec_value"] == "EC-2"
    assert by_pn["PN002"]["has_zeus"] is False
    assert by_pn["PN002"]["zeus_updated"] is False


# ---------- Dashboard ----------

def test_dashboard_valid_flag(full_db):
    """三阶段数据齐全时 dashboard valid 应为 True"""
    data = full_db.get_delta_dashboard_data()
    assert data.get("valid") is True


def test_dashboard_kpi_accuracy(full_db):
    """Dashboard KPI: new_ec 统计新增 PN 的 ENIGMA EC 多值集合, soma_ja 统计 ''->ja 转换"""
    data = full_db.get_delta_dashboard_data()
    assert data["delta1"]["kpi"] == {
        "new_pn": 1,           # PN004
        "discontinued_pn": 1,  # PN003
        "zgs_changed": 1,      # PN001
        "new_ec": 2,           # PN004 的 {EC-4, EC-4B}
        "new_kem": 0,          # PN004 无 KEM
        "soma_ja": 1,          # PN004 SOMA '' -> ja
        "ec_with_zeus": 1,     # PN004 有 EC 且有 FAV
    }
    assert data["delta2"]["kpi"] == {
        "new_pn": 0,
        "discontinued_pn": 0,
        "zgs_changed": 2,      # PN002 + PN004
        "new_ec": 0,
        "new_kem": 0,
        "soma_ja": 0,
        "ec_with_zeus": 0,
    }


def test_dashboard_stage_stats(full_db):
    """阶段统计: PN 带空格已归一 (4 个唯一 PN), EC 计数走 ENIGMA 多值集合"""
    data = full_db.get_delta_dashboard_data()
    pre, to1 = data["stages"]["pre-TO"], data["stages"]["TO1"]
    assert pre["total_records"] == 4 and pre["unique_pn"] == 4
    assert pre["ec_pn"] == 3 and pre["ec_count"] == 3   # PN001/PN002/PN003 在 ENIGMA
    assert pre["fav_pn"] == 2 and pre["kem_count"] == 1 and pre["soma_ja"] == 1
    assert to1["total_records"] == 4
    assert to1["ec_count"] == 4   # PN004 两个 EC 值 (EC-4 + EC-4B)
    assert to1["ec_pn"] == 3 and to1["soma_ja"] == 2    # PN001 + PN004 SOMA=ja


def test_dashboard_bar_line_stages(full_db):
    """bar_line 应包含全部阶段 (pre-TO→TO3) EC/FAV 计数; 本夹具未导 TO3, 其计数为 0"""
    data = full_db.get_delta_dashboard_data()
    assert data["bar_line"]["stages"] == ["pre-TO", "TO1", "TO2", "TO3"]
    # PN004 在 ENIGMA 有两个 EC 值 (EC-4 + EC-4B), TO1/TO2 各计 4 (与 test_dashboard_stage_stats 口径一致)
    assert data["bar_line"]["ec_counts"] == [3, 4, 4, 0]
    assert data["bar_line"]["fav_counts"] == [2, 2, 2, 0]


def test_dashboard_status_pies(full_db):
    """状态饼图: 按值计数降序, 空值跳过"""
    data = full_db.get_delta_dashboard_data()
    assert data["delta1"]["ec_pie"] == [
        {"name": "open", "value": 2}, {"name": "released", "value": 1}]
    assert data["delta1"]["fav_pie"] == [{"name": "active", "value": 2}]


# ---------- /api/delta 端点：校验失败透传 ----------

def test_api_delta_returns_400_on_empty_stage(client, db, tmp_path):
    """阶段数据为空时 /api/delta 应返回 HTTP 400 与错误信息"""
    _import_stage(db, tmp_path, "pre-TO")
    resp = client.get("/api/delta?from=pre-TO&to=TO1")
    assert resp.status_code == 400
    body = resp.get_json()
    assert body.get("success") is False
    assert "TO1" in body.get("error", "")


def test_api_delta_success(client, full_db):
    """/api/delta 正常返回 success=True 与 deltas/summary"""
    resp = client.get("/api/delta?from=pre-TO&to=TO1")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert "deltas" in body["data"]
    assert "summary" in body["data"]
