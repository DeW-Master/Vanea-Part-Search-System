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
    assert config.DELTA_STAGE_FIELD == "Baulos_aggr"
    assert set(config.DELTA_STAGE_PATTERNS) >= {"pre-TO", "TO1", "TO2"}
    assert config.DELTA_FIELD_CONFIG, "Delta 字段配置不能为空"
    assert any(c["field"] == "Part Number" for c in config.DELTA_FIELD_CONFIG)
