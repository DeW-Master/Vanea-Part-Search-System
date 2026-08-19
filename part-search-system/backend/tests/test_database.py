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

# 完整字段表头（含 FAV/ZEUS、KEM、SOMA，用于 EC/ZEUS 验证测试）
FULL_HEADERS = HEADERS + ["FAV_fav", "KEM Number", "SOMA in ZEUS"]
FULL_MAPPING = MAPPING + [
    {"original_header": "FAV_fav", "unified_name": "FAV_fav", "action": "mapped"},
    {"original_header": "KEM Number", "unified_name": "KEM Number", "action": "mapped"},
    {"original_header": "SOMA in ZEUS", "unified_name": "SOMA in ZEUS", "action": "mapped"},
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


def _full_stage_xlsx(path):
    """生成含 EC/FAV/KEM/SOMA 完整字段的三阶段数据。

    场景设计:
    - PN001: pre-TO(无EC,ZGS1) -> TO1(有EC+FAV+KEM+SOMA=ja,ZGS2) -> TO2(同TO1)
      Delta1: new EC + ZEUS updated + KEM + SOMA + ZGS升级；Delta2: 无变化跳过
    - PN002: pre-TO(有EC无FAV,ZGS1) -> TO1(有EC无FAV,ZGS2) -> TO2(有EC+FAV,ZGS3)
      Delta1: ZGS升级 + has_ec=True 但 zeus_updated=False (EC存在但FAV缺失)；Delta2: ZGS升级+ZEUS更新
    - PN003: pre-TO 独有（停用）
    - PN004: TO1 新增（有EC+FAV），TO2 ZGS升级
    """
    return make_xlsx(path, {
        "pre-TO": {
            "headers": FULL_HEADERS,
            "rows": [
                ["PN001", "1", "Pre TEO", "",      "Part One",   "",      "",    "nein"],
                ["PN002", "1", "Pre TEO", "EC-0",  "Part Two",   "",      "",    "nein"],
                ["PN003", "1", "Pre TEO", "EC-0",  "Part Three", "FAV-3", "",    "nein"],
            ],
        },
        "TO1": {
            "headers": FULL_HEADERS,
            "rows": [
                ["PN001", "2", "TO1 PRO1", "EC-1", "Part One",   "FAV-1", "KEM-1", "ja"],
                ["PN002", "2", "TO1 PRO1", "EC-1", "Part Two",   "",      "",      "nein"],
                ["PN004", "1", "TO1 PRO1", "EC-4", "Part Four",  "FAV-4", "",      "nein"],
            ],
        },
        "TO2": {
            "headers": FULL_HEADERS,
            "rows": [
                ["PN001", "2", "TO2 PRO2", "EC-1", "Part One",   "FAV-1", "KEM-1", "ja"],
                ["PN002", "3", "TO2 PRO2", "EC-2", "Part Two",   "FAV-2", "KEM-2", "ja"],
                ["PN004", "2", "TO2 PRO2", "EC-4", "Part Four",  "FAV-4", "",      "nein"],
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


@pytest.fixture
def full_db(db, tmp_path):
    """导入含 EC/FAV/KEM/SOMA 完整字段三阶段数据后的 db_manager"""
    xlsx = _full_stage_xlsx(str(tmp_path / "full_bom.xlsx"))
    sheets = {name: FULL_MAPPING for name in ["pre-TO", "TO1", "TO2"]}
    results = db.import_excel_data(xlsx, "full_bom.xlsx", sheets,
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


# ---------- Delta 数据校验（阶段非空） ----------

def test_delta_fails_when_from_stage_empty(db, tmp_path):
    """前阶段无数据时 calculate_delta 应返回 success=False 错误"""
    # 仅导入 TO1，pre-TO 为空
    xlsx = make_xlsx(str(tmp_path / "only_to1.xlsx"), {
        "TO1": {"headers": HEADERS, "rows": [["PN001", "1", "TO1 PRO1", "EC-1", "P1"]]},
    })
    db.import_excel_data(xlsx, "only_to1.xlsx", {"TO1": MAPPING},
                         file_type="BOM", stage="pre-TO")
    res = db.calculate_delta("pre-TO", "TO1")
    assert res.get("success") is False
    assert "pre-TO" in res.get("error", "")


def test_delta_fails_when_to_stage_empty(db, tmp_path):
    """后阶段无数据时 calculate_delta 应返回 success=False"""
    # 仅导入 pre-TO，TO1 为空
    xlsx = make_xlsx(str(tmp_path / "only_pre.xlsx"), {
        "pre-TO": {"headers": HEADERS, "rows": [["PN001", "1", "Pre TEO", "EC-1", "P1"]]},
    })
    db.import_excel_data(xlsx, "only_pre.xlsx", {"pre-TO": MAPPING},
                         file_type="BOM", stage="pre-TO")
    res = db.calculate_delta("pre-TO", "TO1")
    assert res.get("success") is False
    assert "TO1" in res.get("error", "")


def test_dashboard_valid_flag(imported_db):
    """三阶段数据齐全时 dashboard valid 应为 True"""
    data = imported_db.get_delta_dashboard_data()
    assert data.get("valid") is True


# ---------- PN 差异检测（新增 / 停用） ----------

def test_delta_new_part_detection(full_db):
    """PN004 仅存在于 TO1（后阶段），应被识别为 new_part"""
    res = full_db.calculate_delta("pre-TO", "TO1")
    new_parts = {d["part_number"] for d in res["deltas"] if d["match_type"] == "new_part"}
    assert "PN004" in new_parts


def test_delta_discontinued_part_detection(full_db):
    """PN003 仅存在于 pre-TO（前阶段），应被识别为 discontinued_part"""
    res = full_db.calculate_delta("pre-TO", "TO1")
    disc = {d["part_number"] for d in res["deltas"] if d["match_type"] == "discontinued_part"}
    assert "PN003" in disc


# ---------- ZGS 变更识别 ----------

def test_delta_zgs_upgrade(full_db):
    """同PN不同ZGS应被识别为 zgs_upgraded；同PN同ZGS应跳过"""
    res = full_db.calculate_delta("pre-TO", "TO1")
    zgs = {d["part_number"] for d in res["deltas"] if d["match_type"] == "zgs_upgraded"}
    assert "PN001" in zgs          # ZGS 1 -> 2
    assert "PN002" in zgs          # ZGS 1 -> 2


def test_delta_to1_to2_zgs_upgrade(full_db):
    """连续试装阶段 to1->to2 规则同样适用：PN002 ZGS1->3, PN004 ZGS1->2"""
    res = full_db.calculate_delta("TO1", "TO2")
    zgs = {d["part_number"] for d in res["deltas"] if d["match_type"] == "zgs_upgraded"}
    assert "PN002" in zgs
    assert "PN004" in zgs
    # PN001 两阶段完全相同，应被跳过（不出现）
    assert "PN001" not in {d["part_number"] for d in res["deltas"]}


# ---------- EC 检测（后阶段存在 EC 即为变更，非仅从无到有转换） ----------

def test_delta_ec_presence_detection(full_db):
    """EC检测规则：后阶段PN在ENIGMA记录中存在EC(BuendelNr)即为EC变更。

    PN002 在 pre-TO 已有 EC(EC-0)，TO1 仍有 EC(EC-1) —— 应被标记 has_ec=True，
    而非要求"从无到有转换"。
    """
    res = full_db.calculate_delta("pre-TO", "TO1")
    by_pn = {d["part_number"]: d for d in res["deltas"]}
    assert by_pn["PN002"]["has_ec"] is True
    assert by_pn["PN002"]["ec_value"] == "EC-1"
    # PN001 TO1 有EC
    assert by_pn["PN001"]["has_ec"] is True
    # PN004 新增且有EC
    assert by_pn["PN004"]["has_ec"] is True


def test_delta_kpi_new_ec_counts_presence(full_db):
    """KPI new_ec 应统计所有后阶段存在EC的delta（PN001/PN002/PN004 = 3）"""
    res = full_db.calculate_delta("pre-TO", "TO1")
    assert res["summary"]["has_ec"] == 3


# ---------- ZEUS / FAV 信息更新验证 ----------

def test_delta_zeus_updated(full_db):
    """有EC且FAV(ZEUS ID)已填写 -> zeus_updated=True；有EC无FAV -> False"""
    res = full_db.calculate_delta("pre-TO", "TO1")
    by_pn = {d["part_number"]: d for d in res["deltas"]}
    # PN001: 有EC + 有FAV-1
    assert by_pn["PN001"]["has_zeus"] is True
    assert by_pn["PN001"]["zeus_updated"] is True
    assert by_pn["PN001"]["fav_value"] == "FAV-1"
    # PN002: 有EC 但无FAV -> ZEUS 未更新
    assert by_pn["PN002"]["has_zeus"] is False
    assert by_pn["PN002"]["zeus_updated"] is False


def test_delta_kpi_ec_with_zeus(full_db):
    """KPI 应统计有EC且已更新ZEUS的PN数量（PN001 + PN004 = 2，PN002缺FAV不计）"""
    res = full_db.calculate_delta("pre-TO", "TO1")
    assert res["summary"]["zeus_updated"] == 2


def test_delta_zeus_update_on_to1_to2(full_db):
    """to1->to2: PN002 从无FAV变为有FAV-2，应识别为ZEUS已更新"""
    res = full_db.calculate_delta("TO1", "TO2")
    by_pn = {d["part_number"]: d for d in res["deltas"]}
    assert by_pn["PN002"]["zeus_updated"] is True
    assert by_pn["PN002"]["fav_value"] == "FAV-2"


# ---------- 连续试装阶段 to1->to2 规则一致性 ----------

def test_delta_to1_to2_full(full_db):
    """to1->to2 综合验证: KPI new_parts=0, discontinued=0, zgs_upgraded=2(PN002,PN004)"""
    res = full_db.calculate_delta("TO1", "TO2")
    summary = res["summary"]
    assert summary["new_parts"] == 0
    assert summary["discontinued_parts"] == 0
    assert summary["zgs_upgraded"] == 2


# ---------- Dashboard KPI 准确性 ----------

def test_dashboard_kpi_accuracy(full_db):
    """Dashboard KPI 应与 calculate_delta 结果一致"""
    data = full_db.get_delta_dashboard_data()
    d1 = data["delta1"]["kpi"]
    # Delta1: new_pn=1(PN004), discontinued=1(PN003), zgs_changed=2(PN001,PN002),
    # new_ec=3, ec_with_zeus=2(PN001,PN004), soma_ja=1(PN001)
    assert d1["new_pn"] == 1
    assert d1["discontinued_pn"] == 1
    assert d1["zgs_changed"] == 2
    assert d1["new_ec"] == 3
    assert d1["ec_with_zeus"] == 2
    assert d1["soma_ja"] == 1


def test_dashboard_bar_line_stages(full_db):
    """bar_line 应包含三阶段 EC/FAV 计数"""
    data = full_db.get_delta_dashboard_data()
    assert data["bar_line"]["stages"] == ["pre-TO", "TO1", "TO2"]
    assert len(data["bar_line"]["ec_counts"]) == 3
    assert len(data["bar_line"]["fav_counts"]) == 3


# ---------- /api/delta 端点：校验失败透传 ----------

def test_api_delta_returns_400_on_empty_stage(client, db, tmp_path):
    """阶段数据为空时 /api/delta 应返回 HTTP 400 与错误信息"""
    # 仅导入 pre-TO
    xlsx = make_xlsx(str(tmp_path / "p.xlsx"), {
        "pre-TO": {"headers": HEADERS, "rows": [["PN001", "1", "Pre TEO", "EC-1", "P1"]]},
    })
    db.import_excel_data(xlsx, "p.xlsx", {"pre-TO": MAPPING},
                         file_type="BOM", stage="pre-TO")
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
