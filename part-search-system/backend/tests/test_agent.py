# -*- coding: utf-8 -*-
"""agent 模块测试: 云端 API URL 校验(SSRF) / 配置存取与掩码"""
import pytest

from agent import validate_cloud_api_url, set_cloud_config, get_cloud_config


# ---------- validate_cloud_api_url (SSRF 防护) ----------

def test_valid_public_urls():
    assert validate_cloud_api_url("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1"
    assert validate_cloud_api_url("http://10.0.0.5:8000/v1") == "http://10.0.0.5:8000/v1"  # 内网 LLM 网关合法


@pytest.mark.parametrize("bad", [
    "ftp://api.example.com/v1",        # 非 http/https
    "not-a-url",                        # 无 scheme
    "http://169.254.169.254/latest",    # AWS 元数据
    "http://100.100.100.200",           # 阿里云元数据
    "http://metadata.google.internal",  # GCP
    "http://localhost:8000/v1",         # 回环
    "http://127.0.0.1:8000/v1",         # 回环
    "http://[::1]:8000/v1",             # IPv6 回环
    "",                                 # 空
])
def test_invalid_urls_rejected(bad):
    with pytest.raises(ValueError):
        validate_cloud_api_url(bad)


# ---------- 云端配置保存 / 掩码 ----------

def test_set_and_get_cloud_config_masked(monkeypatch, tmp_path):
    from agent import CLOUD_CONFIG_PATH
    # 隔离配置文件路径
    cfg = tmp_path / "cloud_config.json"
    monkeypatch.setattr("agent.CLOUD_CONFIG_PATH", str(cfg))

    set_cloud_config("https://api.deepseek.com/v1", "sk-0123456789abcdef", "deepseek-chat")
    conf = get_cloud_config()
    assert conf["api_url"] == "https://api.deepseek.com/v1"
    assert conf["model"] == "deepseek-chat"
    assert conf["api_key_masked"] == "sk-0***********cdef"
    assert "api_key" in conf  # 内部仍含完整 key


def test_set_cloud_config_rejects_ssrf(monkeypatch, tmp_path):
    from agent import CLOUD_CONFIG_PATH
    monkeypatch.setattr("agent.CLOUD_CONFIG_PATH", str(tmp_path / "cloud_config.json"))
    with pytest.raises(ValueError):
        set_cloud_config("http://169.254.169.254", "sk-x", "gpt")
