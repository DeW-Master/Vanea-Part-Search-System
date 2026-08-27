# -*- coding: utf-8 -*-
"""Part 领域模型单元测试: norm/business_column/determine_change_type/
Part.value/diff_parts/StageCatalog.delta_pairs/stats/status_distribution"""
import pytest

from config import DELTA_BUSINESS_FIELDS, DELTA_FIELD_CONFIG
from models import (Part, FieldChange, DeltaPair, StageCatalog,
                    determine_change_type, norm, business_column)


# ---------- norm / business_column ----------

@pytest.mark.parametrize("raw,expected", [
    (None, ""), ("", ""), ("  ", ""), ("PN001  ", "PN001"),
    ("  PN001", "PN001"), (123, "123"), (0, "0"),
])
def test_norm(raw, expected):
    assert norm(raw) == expected


def test_business_column():
    for key, col in DELTA_BUSINESS_FIELDS.items():
        assert business_column(key) == col
    # 未知键原样返回 (前向兼容: 未来新列无需改代码)
    assert business_column("unknown_key") == "unknown_key"


# ---------- determine_change_type ----------

@pytest.mark.parametrize("key,old,new,expected", [
    ("", "", "", "unchanged"),          # 双空
    ("", "a", "a", "persisted"),        # 同值非空
    ("", "", "x", "added"),
    ("", "x", "", "removed"),
    ("", "a", "b", "changed"),
    ("zgs", "1", "2", "upgraded"),      # ZGS int 升级
    ("zgs", "2", "1", "changed"),       # ZGS 降级不算升级
    ("zgs", "1", "1", "persisted"),
    ("zgs", "", "5", "added"),          # 单边为空走通用规则, 不做 int 比较
    ("zgs", "5", "", "removed"),
    ("zgs", "abc", "def", "changed"),   # 非数字 fall through
    ("zgs", "1a", "2", "changed"),
])
def test_determine_change_type(key, old, new, expected):
    assert determine_change_type(key, old, new) == expected


# ---------- Part ----------

def _make_part(pn="PN001", zgs="1", stage="pre-TO", extra=None, enigma=None):
    data = {DELTA_BUSINESS_FIELDS["zgs"]: zgs}
    if extra:
        data.update(extra)
    return Part.from_row(row_id=1, file_id=1, pn=pn, data=data,
                         stage=stage, enigma_values=enigma)


def test_part_value_via_business_key():
    """value() 是统一取数入口: 业务键 -> config 映射 -> 归一值"""
    p = _make_part(extra={
        DELTA_BUSINESS_FIELDS["ec"]: "EC-1  ",   # 带尾部空格
        DELTA_BUSINESS_FIELDS["fav"]: "FAV-1",
    })
    assert p.pn == "PN001"
    assert p.zgs == "1"
    assert p.ec == "EC-1"
    assert p.fav == "FAV-1"
    assert p.kem == ""           # 缺失字段 -> ''
    assert p.has_ec is True
    assert p.in_enigma is False  # enigma_values=None


def test_part_pn_normalized():
    """from_row 应归一 PN (首尾空格), 与 catalog/ENIGMA key 对齐"""
    p = _make_part(pn="PN005  ")
    assert p.pn == "PN005"


def test_part_enigma_value_sets():
    enigma = {"ec": {"EC-4", "EC-4B"}, "kem": set(), "fav": {"FAV-4"}, "soma": {"ja"}}
    p = _make_part(enigma=enigma)
    assert p.in_enigma is True
    assert p.ec_values == {"EC-4", "EC-4B"}
    assert p.kem_values == set()
    assert p.fav_values == {"FAV-4"}
    assert p.soma_ja is True
    p2 = _make_part(enigma={"ec": set(), "kem": set(), "fav": set(), "soma": {"nein"}})
    assert p2.soma_ja is False


def test_part_to_dict_shape():
    d = _make_part().to_dict()
    assert d["part_number"] == "PN001" and d["zgs"] == "1"
    assert d["stage"] == "pre-TO" and d["in_enigma"] is False


# ---------- Part.diff_parts ----------

def test_diff_parts_zgs_upgrade():
    from_p = _make_part(zgs="1")
    to_p = _make_part(zgs="2")
    changes = Part.diff_parts(from_p, to_p, DELTA_FIELD_CONFIG)
    by_business = {c.business: c for c in changes}
    assert by_business["ZGS"].change_type == "upgraded"
    assert by_business["ZGS"].old_value == "1"
    assert by_business["ZGS"].new_value == "2"


def test_diff_parts_new_part_added():
    to_p = _make_part(zgs="1", extra={DELTA_BUSINESS_FIELDS["ec"]: "EC-1"})
    changes = Part.diff_parts(None, to_p, DELTA_FIELD_CONFIG)
    by_business = {c.business: c for c in changes}
    assert by_business["EC"].change_type == "added"
    assert by_business["EC"].old_value == ""
    assert by_business["EC"].new_value == "EC-1"


def test_diff_parts_unavailable_when_column_missing():
    """tracked 字段不在表列集合中 -> change_type='unavailable'"""
    changes = Part.diff_parts(_make_part(), _make_part(zgs="2"),
                              DELTA_FIELD_CONFIG, col_names={"Part Number", "ZGS"})
    by_business = {c.business: c for c in changes}
    assert by_business["EC"].change_type == "unavailable"
    assert by_business["ZGS"].change_type == "upgraded"  # 在列集合中, 正常比较


def test_diff_parts_sorts_changed_first():
    changes = Part.diff_parts(_make_part(zgs="1"), _make_part(zgs="2"),
                              DELTA_FIELD_CONFIG)
    unchanged_seen = False
    for c in changes:
        if c.change_type == "unchanged":
            unchanged_seen = True
        elif unchanged_seen:
            pytest.fail("unchanged 应排在有变化的字段之后")


# ---------- StageCatalog ----------

def _catalog(stage, spec):
    """spec: {pn: zgs}"""
    cat = StageCatalog(stage)
    for pn, zgs in spec.items():
        cat.add(_make_part(pn=pn, zgs=zgs, stage=stage))
    return cat


def test_catalog_basics():
    cat = _catalog("pre-TO", {"PN001": "1", "PN002": "1"})
    assert len(cat) == 2 and bool(cat)
    assert "PN001" in cat and "PN999" not in cat
    assert cat.pns == {"PN001", "PN002"}
    assert cat.get("PN001").zgs == "1"
    assert {p.pn for p in cat} == {"PN001", "PN002"}


def test_delta_pairs_match_types():
    pre = _catalog("pre-TO", {"PN001": "1", "PN002": "1", "PN003": "1"})
    to1 = _catalog("TO1", {"PN001": "2", "PN002": "1", "PN004": "1"})
    pairs = {p.pn: p for p in pre.delta_pairs(to1)}
    # PN002 同 PN 同 ZGS -> 跳过
    assert set(pairs) == {"PN001", "PN003", "PN004"}
    assert pairs["PN001"].match_type == "zgs_upgraded"
    assert pairs["PN001"].from_part.zgs == "1" and pairs["PN001"].to_part.zgs == "2"
    assert pairs["PN003"].match_type == "discontinued_part"
    assert pairs["PN003"].to_part is None
    assert pairs["PN004"].match_type == "new_part"
    assert pairs["PN004"].from_part is None


def test_catalog_stats_api_contract():
    cat = StageCatalog("TO1")
    cat.add(_make_part(pn="PN001", enigma={
        "ec": {"EC-1"}, "kem": {"KEM-1"}, "fav": {"FAV-1"}, "soma": {"ja"}}))
    cat.add(_make_part(pn="PN002", enigma={
        "ec": {"EC-2"}, "kem": set(), "fav": set(), "soma": {"nein"}}))
    cat.add(_make_part(pn="PN003", enigma=None))  # 不在 ENIGMA
    s = cat.stats()
    assert s == {
        "total_records": 3, "unique_pn": 3,
        "ec_count": 2, "ec_pn": 2,
        "fav_count": 1, "fav_pn": 1,
        "kem_count": 1, "soma_ja": 1,
    }


def test_status_distribution_sorted_skip_empty():
    cat = StageCatalog("TO1")
    status_col = DELTA_BUSINESS_FIELDS["ec_status"]
    cat.add(_make_part(pn="PN001", extra={status_col: "open"}))
    cat.add(_make_part(pn="PN002", extra={status_col: "open"}))
    cat.add(_make_part(pn="PN003", extra={status_col: "released"}))
    cat.add(_make_part(pn="PN004"))  # 无状态 -> 跳过
    assert cat.status_distribution("ec_status") == [
        {"name": "open", "value": 2}, {"name": "released", "value": 1}]
