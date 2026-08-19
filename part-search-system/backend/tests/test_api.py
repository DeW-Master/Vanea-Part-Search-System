# -*- coding: utf-8 -*-
"""API 层测试: 健康检查/搜索/认证/SSRF 鉴权/metrics"""


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "healthy"
    assert body["database"]["type"] == "sqlite"


def test_version(client):
    r = client.get("/api/version")
    assert r.status_code == 200
    assert r.get_json()["version"]


def test_columns(client):
    r = client.get("/api/columns")
    assert r.status_code == 200
    assert r.get_json()["success"] is True


def test_search_requires_pn(client):
    r = client.get("/api/search")
    assert r.status_code == 400


def test_search_complex_empty(client):
    r = client.post("/api/search_complex", json={"conditions": []})
    assert r.status_code == 400


def test_search_complex_injection_field(client):
    """非法字段应被跳过, 接口正常返回"""
    r = client.post("/api/search_complex", json={
        "conditions": [{"field": 'x" OR 1=1 --', "value": "a", "operator": "like"}]
    })
    assert r.status_code == 200
    assert r.get_json()["success"] is True


# ---------- 认证 ----------

def test_login_wrong_password(client):
    r = client.post("/api/auth/login", json={"password": "wrong-password"})
    assert r.status_code == 401


def test_login_correct_password(client):
    r = client.post("/api/auth/login", json={"password": "test-admin-pw"})
    assert r.status_code == 200
    assert r.get_json()["success"] is True


def test_admin_api_requires_auth(client):
    r = client.get("/api/admin/files")
    assert r.status_code == 403


# ---------- SSRF 防护 (cloud-config 必须鉴权) ----------

def test_cloud_config_requires_auth(client):
    """未登录时配置云端 API 应被拒绝 (SSRF 防护)"""
    r = client.post("/api/agent/cloud-config", json={
        "api_url": "http://169.254.169.254/latest/meta-data", "model": "gpt"
    })
    assert r.status_code == 403


def test_cloud_test_requires_auth(client):
    r = client.post("/api/agent/cloud-test", json={
        "api_url": "http://169.254.169.254", "api_key": "sk-x", "model": "gpt"
    })
    assert r.status_code == 403


def test_cloud_config_rejects_ssrf_target(client):
    """已登录后配置元数据地址仍应被拒绝"""
    client.post("/api/auth/login", json={"password": "test-admin-pw"})
    r = client.post("/api/agent/cloud-config", json={
        "api_url": "http://169.254.169.254/latest/meta-data", "model": "gpt"
    })
    assert r.status_code == 500  # ValueError -> 500, 且不发出任何请求
    body = r.get_json()
    assert "169.254.169.254" in body.get("error", "")


# ---------- 监控 ----------

def test_metrics_endpoint(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert b"http_requests_total" in r.data
