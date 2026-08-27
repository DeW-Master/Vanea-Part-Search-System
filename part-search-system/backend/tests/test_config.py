# -*- coding: utf-8 -*-
"""config 模块冒烟: 关键常量存在且类型正确"""
import config


def test_core_constants():
    assert isinstance(config.APP_VERSION, str) and config.APP_VERSION
    assert isinstance(config.DB_TYPE, str)
    assert config.DB_TYPE in ("sqlite", "postgresql")
    assert isinstance(config.CACHE_TTL, int) and config.CACHE_TTL > 0
    assert isinstance(config.FLASK_PORT, int)
    assert isinstance(config.SESSION_TYPE, str)
    assert isinstance(config.CORS_ORIGINS, list)
    assert isinstance(config.ALLOWED_EXTENSIONS, set)
    assert ".xlsx" in config.ALLOWED_EXTENSIONS


def test_delta_config():
    # 业务键 -> 统一列名映射: 任何语言表头经导入管线统一后, 比较层只认业务键
    for key in ("zgs", "ec", "ec_status", "fav", "fav_status", "kem", "soma"):
        assert key in config.DELTA_BUSINESS_FIELDS
    assert config.DELTA_BUSINESS_FIELDS["zgs"] == "ZGS"
    assert config.DELTA_BUSINESS_FIELDS["ec"] == "Bundle Number"
    # Delta 字段配置: zgs/ec 列名单点引用同一映射 (消重), 每项带业务键
    assert config.DELTA_FIELD_CONFIG, "Delta 字段配置不能为空"
    by_business = {c["business"]: c for c in config.DELTA_FIELD_CONFIG}
    assert by_business["ZGS"]["field"] == config.DELTA_BUSINESS_FIELDS["zgs"]
    assert by_business["EC"]["field"] == config.DELTA_BUSINESS_FIELDS["ec"]
    assert by_business["ZGS"]["key"] == "zgs"
    assert by_business["EC"]["key"] == "ec"
    assert any(c["field"] == "Part Number" for c in config.DELTA_FIELD_CONFIG)
