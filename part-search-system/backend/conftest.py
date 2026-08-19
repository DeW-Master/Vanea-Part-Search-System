# -*- coding: utf-8 -*-
"""
pytest 全局配置:
- 在导入 app/database 之前设置隔离环境(临时 DB、禁用 Redis/后台线程)
- 提供 Flask test_client / 数据库访问 fixtures
- 每个测试后清理数据表, 保证用例间隔离
"""
import os
import sys
import tempfile

# ---- 必须在 import app/database 之前设置环境变量 ----
_TMP = tempfile.mkdtemp(prefix="van_ea_test_")
os.environ["PARTS_DB_PATH"] = os.path.join(_TMP, "test_parts.db")
os.environ["PARTS_UPLOAD_DIR"] = os.path.join(_TMP, "uploads")
os.environ["SESSION_TYPE"] = "null"          # 内存 Session, 避免文件/Redis 依赖
os.environ["CACHE_ENABLED"] = "false"        # 关闭 Redis 缓存(自动降级)
os.environ["DELTA_REFRESH_INTERVAL"] = "0"   # 关闭 Delta 预计算后台线程
os.environ["LEADER_ELECTION_ENABLED"] = "false"
os.environ["OLLAMA_HEALTHCHECK_INTERVAL"] = "0"  # 关闭 Ollama 健康检查线程
os.environ["OLLAMA_URL"] = "http://127.0.0.1:1"  # 指向无效地址, 避免误连
os.environ["ADMIN_PASSWORD"] = "test-admin-pw"

# 确保 backend 目录在 sys.path, 以便 import app/database/config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import database
from app import app as flask_app

os.makedirs(os.environ["PARTS_UPLOAD_DIR"], exist_ok=True)


@pytest.fixture(scope="session")
def app():
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture(scope="function")
def client(app):
    """每测试新建 test_client, 保证 Session/Cookie 隔离"""
    return app.test_client()


@pytest.fixture
def db():
    """返回 DatabaseManager 单例"""
    return database.db_manager


@pytest.fixture(autouse=True)
def clean_tables():
    """每个测试后清空数据表, 保证用例隔离"""
    yield
    conn = database.get_db()
    try:
        conn.execute("DELETE FROM parts_data")
        conn.execute("DELETE FROM column_mapping")
        conn.execute("DELETE FROM uploaded_files")
        conn.execute("DELETE FROM unified_columns")
        conn.commit()
    finally:
        conn.close()


def make_xlsx(path, sheets):
    """
    生成测试用 Excel 文件。
    sheets: {sheet_name: {"headers": [...], "rows": [[...], ...]}}
    """
    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, spec in sheets.items():
        ws = wb.create_sheet(name)
        ws.append(spec["headers"])
        for row in spec["rows"]:
            ws.append(row)
    wb.save(path)
    return path
