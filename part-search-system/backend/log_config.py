# -*- coding: utf-8 -*-
"""统一日志配置。

目标:
- 控制台保持干净: 默认只输出 ERROR (真正的异常), 正常流程/降级提醒不刷屏。
- 全量日志写入 logs/app.log, 错误单独写入 logs/error.log, 均带时间戳与操作者 IP。
- 安全基线提醒(默认凭据等)只写文件, 不在控制台展示。

日志行格式:
    2026-09-09 14:23:05 [ERROR] [10.16.94.120] app: 消息内容
后台线程(无请求上下文)的 IP 显示为 "-"。

可用环境变量:
- CONSOLE_LOG_LEVEL: 控制台级别, 默认 ERROR (调试时可设 INFO/DEBUG)。
- LOG_DIR: 日志目录, 默认 <backend>/logs。
"""

import os
import logging
import threading
from logging.handlers import RotatingFileHandler

# 请求线程本地变量: 保存当前操作者 IP
_ctx = threading.local()

_CONFIGURED = False
_LOG_DIR = os.environ.get("LOG_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

_LOG_FMT = "%(asctime)s [%(levelname)s] [%(client_ip)s] %(name)s: %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


class _IpFilter(logging.Filter):
    """给每条日志记录注入 client_ip 字段 (请求线程外为 '-')。"""

    def filter(self, record):
        record.client_ip = getattr(_ctx, "ip", "-") or "-"
        return True


def set_request_ip(ip):
    """在请求入口(before_request)调用, 记录当前操作者 IP。"""
    _ctx.ip = (ip or "-").strip() or "-"


def clear_request_ip():
    """在请求结束(teardown)调用, 避免线程复用时串号。"""
    _ctx.ip = "-"


def setup_logging():
    """幂等初始化根 logger。多次调用安全。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    os.makedirs(_LOG_DIR, exist_ok=True)

    class _Fmt(logging.Formatter):
        """兜底: 即使记录未经过 _IpFilter (如第三方 logger 的 handler),
        也不会因缺少 client_ip 字段而报错。"""

        def formatMessage(self, record):
            if not hasattr(record, "client_ip"):
                record.client_ip = "-"
            return super().formatMessage(record)

    formatter = _Fmt(_LOG_FMT, datefmt=_DATE_FMT)
    ip_filter = _IpFilter()

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    # 1) 全量文件: INFO 及以上
    app_handler = RotatingFileHandler(
        os.path.join(_LOG_DIR, "app.log"),
        maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    app_handler.setLevel(logging.INFO)
    app_handler.setFormatter(formatter)
    app_handler.addFilter(ip_filter)
    root.addHandler(app_handler)

    # 2) 错误文件: ERROR 及以上 (便于只看问题)
    err_handler = RotatingFileHandler(
        os.path.join(_LOG_DIR, "error.log"),
        maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    err_handler.setLevel(logging.ERROR)
    err_handler.setFormatter(formatter)
    err_handler.addFilter(ip_filter)
    root.addHandler(err_handler)

    # 3) 控制台: 默认只显示 ERROR; 可用环境变量放开
    console_level = os.environ.get("CONSOLE_LOG_LEVEL", "ERROR").upper()
    console = logging.StreamHandler()
    console.setLevel(getattr(logging, console_level, logging.ERROR))
    console.setFormatter(formatter)
    console.addFilter(ip_filter)
    root.addHandler(console)

    # 静音 Flask/Werkzeug 每请求访问日志 (INFO 的 "GET /... 200"),
    # 真实错误(ERROR)仍会输出
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    # 安全提醒 logger: 只写文件, 不向控制台/根 logger 传播
    sec = logging.getLogger("security")
    sec.setLevel(logging.INFO)
    sec.propagate = False
    if not sec.handlers:
        sec_handler = RotatingFileHandler(
            os.path.join(_LOG_DIR, "security.log"),
            maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        sec_handler.setLevel(logging.INFO)
        sec_handler.setFormatter(formatter)
        sec_handler.addFilter(ip_filter)
        sec.addHandler(sec_handler)


def get_logger(name):
    """统一获取 logger; 未初始化时先幂等初始化, 保证独立脚本也安全。"""
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)


# 安全基线专用 (默认凭据告警等), 只落 security.log
security_logger = get_logger("security")
