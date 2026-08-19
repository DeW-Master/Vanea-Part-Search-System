# -*- coding: utf-8 -*-
"""
van.ea 车辆零件智能查询系统 - Prometheus Metrics 模块

功能:
    - 暴露 /metrics 端点供 Prometheus 抓取
    - Flask 请求中间件: 自动记录请求数/延迟/状态码
    - 20+ 自定义业务指标: Redis 命中率, Delta 预计算耗时, Ollama 集群状态, DB 统计
    - 可选 Basic Auth 保护
"""

import time
import base64
from typing import Dict, Any, Optional

from flask import request, Response, g

from config import (
    METRICS_ENABLED, METRICS_ENDPOINT,
    METRICS_BASIC_AUTH_USER, METRICS_BASIC_AUTH_PASS,
    APP_VERSION, APP_CODENAME, INSTANCE_ID,
)


class MetricsManager:
    """
    Prometheus 指标管理器。
    封装 prometheus_client，提供:
        - Counter / Gauge / Histogram / Summary 的便捷工厂
        - Flask before/after 请求钩子
        - /metrics 端点 Basic Auth
        - 业务指标方法 (redis_hit、delta_compute 等)
    """

    _instance = None
    _lock = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            import threading
            cls._lock = threading.Lock()
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, '_initialized', False):
            return
        self._initialized = True
        self.enabled = METRICS_ENABLED
        self._pc = None  # prometheus_client 模块

        # 指标注册表
        self._registry = None
        self._counters: Dict[str, Any] = {}
        self._gauges: Dict[str, Any] = {}
        self._histograms: Dict[str, Any] = {}
        self._summaries: Dict[str, Any] = {}

        if self.enabled:
            self._init_client()
            self._declare_metrics()

    # ============== 初始化 ==============
    def _init_client(self):
        try:
            import prometheus_client as pc
            self._pc = pc
            # 禁用默认进程指标中的 GC 收集器 (减少噪音)
            self._registry = pc.CollectorRegistry()
            pc.PROCESS_COLLECTOR = None
            pc.PLATFORM_COLLECTOR = None
            pc.GC_COLLECTOR = None
            # 启用基础指标 (进程 + Python)
            pc.ProcessCollector(registry=self._registry)
        except ImportError:
            print("[Metrics] prometheus_client 未安装，指标已禁用")
            self.enabled = False

    def _declare_metrics(self):
        pc = self._pc
        reg = self._registry
        common_labels = ['instance_id', 'app', 'version']
        common_values = [INSTANCE_ID, APP_CODENAME, APP_VERSION]

        # ---------- HTTP ----------
        self._counters['http_requests_total'] = pc.Counter(
            'http_requests_total',
            'Total HTTP requests',
            ['method', 'endpoint', 'status'] + common_labels,
            registry=reg,
        )
        self._histograms['http_request_duration_seconds'] = pc.Histogram(
            'http_request_duration_seconds',
            'HTTP request duration in seconds',
            ['method', 'endpoint'] + common_labels,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
            registry=reg,
        )
        self._counters['http_request_size_bytes'] = pc.Counter(
            'http_request_size_bytes_total',
            'Total HTTP request body size in bytes',
            common_labels,
            registry=reg,
        )
        self._counters['http_response_size_bytes'] = pc.Counter(
            'http_response_size_bytes_total',
            'Total HTTP response body size in bytes',
            common_labels,
            registry=reg,
        )
        self._counters['http_exceptions_total'] = pc.Counter(
            'http_exceptions_total',
            'Unhandled HTTP exceptions',
            ['endpoint', 'exception_type'] + common_labels,
            registry=reg,
        )

        # ---------- Redis ----------
        self._counters['redis_cache_hits'] = pc.Counter(
            'redis_cache_hits_total',
            'Redis cache hits',
            common_labels,
            registry=reg,
        )
        self._counters['redis_cache_misses'] = pc.Counter(
            'redis_cache_misses_total',
            'Redis cache misses',
            common_labels,
            registry=reg,
        )
        self._counters['redis_cache_errors'] = pc.Counter(
            'redis_cache_errors_total',
            'Redis cache errors',
            ['operation'] + common_labels,
            registry=reg,
        )
        self._gauges['redis_cache_enabled'] = pc.Gauge(
            'redis_cache_enabled',
            'Whether Redis cache is enabled',
            common_labels,
            registry=reg,
        )

        # ---------- Delta ----------
        self._histograms['delta_compute_duration'] = pc.Histogram(
            'delta_compute_duration_seconds',
            'Delta compute duration in seconds',
            ['stage_comparison'] + common_labels,
            buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
            registry=reg,
        )
        self._counters['delta_compute_runs'] = pc.Counter(
            'delta_compute_runs_total',
            'Delta background compute runs',
            ['result'] + common_labels,
            registry=reg,
        )
        self._counters['delta_compute_errors'] = pc.Counter(
            'delta_compute_errors_total',
            'Delta compute errors',
            ['error_type'] + common_labels,
            registry=reg,
        )
        self._gauges['delta_last_compute_timestamp'] = pc.Gauge(
            'delta_last_compute_timestamp_seconds',
            'Unix timestamp of last successful Delta compute',
            common_labels,
            registry=reg,
        )

        # ---------- Ollama ----------
        self._counters['ollama_requests'] = pc.Counter(
            'ollama_requests_total',
            'Ollama requests',
            ['node', 'model', 'success'] + common_labels,
            registry=reg,
        )
        self._histograms['ollama_request_duration'] = pc.Histogram(
            'ollama_request_duration_seconds',
            'Ollama request duration',
            ['node', 'model'] + common_labels,
            buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
            registry=reg,
        )
        self._gauges['ollama_nodes_total'] = pc.Gauge(
            'ollama_nodes_total',
            'Total Ollama nodes configured',
            common_labels,
            registry=reg,
        )
        self._gauges['ollama_healthy_nodes'] = pc.Gauge(
            'ollama_healthy_nodes',
            'Healthy Ollama nodes',
            common_labels,
            registry=reg,
        )
        self._gauges['ollama_pending_requests'] = pc.Gauge(
            'ollama_pending_requests',
            'Pending Ollama requests across cluster',
            common_labels,
            registry=reg,
        )

        # ---------- Database ----------
        self._gauges['db_total_records'] = pc.Gauge(
            'db_total_records',
            'Total records in parts_data table',
            ['db_type'] + common_labels,
            registry=reg,
        )
        self._gauges['db_total_parts'] = pc.Gauge(
            'db_total_parts',
            'Unique part numbers in DB',
            ['db_type'] + common_labels,
            registry=reg,
        )
        self._gauges['db_total_files'] = pc.Gauge(
            'db_total_files',
            'Number of uploaded files',
            ['db_type'] + common_labels,
            registry=reg,
        )
        self._histograms['db_query_duration'] = pc.Histogram(
            'db_query_duration_seconds',
            'DB query duration',
            ['operation'] + common_labels,
            buckets=(0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 30.0),
            registry=reg,
        )

        # ---------- Leader / Instance ----------
        self._gauges['is_leader'] = pc.Gauge(
            'van_ea_is_leader',
            'Whether this instance is the Delta compute leader (1=leader, 0=follower)',
            common_labels,
            registry=reg,
        )
        self._gauges['concurrent_search_slots'] = pc.Gauge(
            'concurrent_search_slots',
            'Current active concurrent search slots',
            common_labels,
            registry=reg,
        )

        # ---------- Info ----------
        self._gauges['app_info'] = pc.Gauge(
            'van_ea_app_info',
            'Application info (always 1, labels contain metadata)',
            common_labels,
            registry=reg,
        )
        self._gauges['app_info'].labels(*common_values).set(1)

        # 保存 label 默认值用于后续无标签调用
        self._default_labels = dict(zip(common_labels, common_values))

    def _common(self, extra=None):
        """返回公共 label 值列表"""
        vals = [
            self._default_labels['instance_id'],
            self._default_labels['app'],
            self._default_labels['version'],
        ]
        if extra:
            return list(extra) + vals
        return vals

    # ============== 对外 API: HTTP 中间件 ==============
    def before_request(self):
        if not self.enabled:
            return
        g._metrics_start = time.perf_counter()
        g._metrics_endpoint = self._normalize_endpoint(request.path)
        g._metrics_method = request.method

    def after_request(self, response: Response) -> Response:
        if not self.enabled:
            return response
        try:
            start = getattr(g, '_metrics_start', None)
            if start is not None:
                elapsed = time.perf_counter() - start
                endpoint = getattr(g, '_metrics_endpoint', request.path)
                method = getattr(g, '_metrics_method', request.method)

                self._histograms['http_request_duration_seconds'] \
                    .labels(*self._common([method, endpoint])) \
                    .observe(elapsed)

                status = str(response.status_code)
                self._counters['http_requests_total'] \
                    .labels(*self._common([method, endpoint, status])) \
                    .inc()

                # 响应大小
                clen = response.calculate_content_length() or 0
                if clen > 0:
                    try:
                        self._counters['http_response_size_bytes'] \
                            .labels(*self._common()) \
                            .inc(clen)
                    except Exception:
                        pass

            # 请求大小
            try:
                cl = request.content_length or 0
                if cl > 0:
                    self._counters['http_request_size_bytes'] \
                        .labels(*self._common()) \
                        .inc(cl)
            except Exception:
                pass
        except Exception as e:
            print(f"[Metrics] after_request 异常: {e}")
        return response

    def teardown_request(self, exc):
        if exc is None or not self.enabled:
            return
        try:
            endpoint = getattr(g, '_metrics_endpoint', request.path)
            self._counters['http_exceptions_total'] \
                .labels(*self._common([endpoint, type(exc).__name__])) \
                .inc()
        except Exception:
            pass

    def register_flask(self, app):
        """把中间件挂到 Flask app 上，并添加 /metrics 路由"""
        if not self.enabled:
            return
        app.before_request(self.before_request)
        app.after_request(self.after_request)
        app.teardown_request(self.teardown_request)

        endpoint_path = METRICS_ENDPOINT or '/metrics'

        # 可选: Basic Auth 保护
        def _check_auth():
            if not (METRICS_BASIC_AUTH_USER and METRICS_BASIC_AUTH_PASS):
                return True
            hdr = request.headers.get('Authorization', '')
            if not hdr.startswith('Basic '):
                return False
            try:
                token = base64.b64decode(hdr[6:]).decode('utf-8')
                user, _, pwd = token.partition(':')
                return user == METRICS_BASIC_AUTH_USER and pwd == METRICS_BASIC_AUTH_PASS
            except Exception:
                return False

        @app.route(endpoint_path, methods=['GET'])
        def metrics_expose():
            if not _check_auth():
                return Response(
                    'Unauthorized',
                    status=401,
                    headers={'WWW-Authenticate': 'Basic realm="Prometheus"'},
                )
            # 抓取时刷新动态 gauge
            self._refresh_dynamic_gauges()
            data = self._pc.generate_latest(self._registry)
            return Response(data, mimetype=self._pc.CONTENT_TYPE_LATEST)

        print(f"[Metrics] 已注册端点: {endpoint_path}")

    # ============== 对外 API: 业务指标 ==============
    def observe_redis(self, hit: bool):
        if not self.enabled:
            return
        try:
            if hit:
                self._counters['redis_cache_hits'].labels(*self._common()).inc()
            else:
                self._counters['redis_cache_misses'].labels(*self._common()).inc()
        except Exception:
            pass

    def observe_redis_error(self, op: str):
        if not self.enabled:
            return
        try:
            self._counters['redis_cache_errors'].labels(*self._common([op])).inc()
        except Exception:
            pass

    def set_redis_enabled(self, enabled: bool):
        if not self.enabled:
            return
        try:
            self._gauges['redis_cache_enabled'].labels(*self._common()).set(1 if enabled else 0)
        except Exception:
            pass

    def observe_delta_compute(self, duration_sec: float, stage_comparison: str = 'all'):
        if not self.enabled:
            return
        try:
            self._histograms['delta_compute_duration'] \
                .labels(*self._common([stage_comparison])) \
                .observe(duration_sec)
        except Exception:
            pass

    def inc_delta_compute_run(self, result: str = 'success'):
        if not self.enabled:
            return
        try:
            self._counters['delta_compute_runs'].labels(*self._common([result])).inc()
            if result == 'success':
                import time as _t
                self._gauges['delta_last_compute_timestamp'] \
                    .labels(*self._common()) \
                    .set(_t.time())
        except Exception:
            pass

    def inc_delta_compute_error(self, error_type: str = 'unknown'):
        if not self.enabled:
            return
        try:
            self._counters['delta_compute_errors'] \
                .labels(*self._common([error_type])) \
                .inc()
        except Exception:
            pass

    def observe_ollama_request(self, node: str, model: str, success: bool, duration_sec: float = 0.0):
        if not self.enabled:
            return
        try:
            self._counters['ollama_requests'] \
                .labels(*self._common([node, model, 'true' if success else 'false'])) \
                .inc()
            self._histograms['ollama_request_duration'] \
                .labels(*self._common([node, model])) \
                .observe(duration_sec)
        except Exception:
            pass

    def observe_db_query(self, duration_sec: float, operation: str = 'query'):
        if not self.enabled:
            return
        try:
            self._histograms['db_query_duration'] \
                .labels(*self._common([operation])) \
                .observe(duration_sec)
        except Exception:
            pass

    def set_db_stats(self, db_type: str, total_records: int, total_parts: int, total_files: int):
        if not self.enabled:
            return
        try:
            self._gauges['db_total_records'].labels(*self._common([db_type])).set(total_records)
            self._gauges['db_total_parts'].labels(*self._common([db_type])).set(total_parts)
            self._gauges['db_total_files'].labels(*self._common([db_type])).set(total_files)
        except Exception:
            pass

    def set_is_leader(self, is_leader: bool):
        if not self.enabled:
            return
        try:
            self._gauges['is_leader'].labels(*self._common()).set(1 if is_leader else 0)
        except Exception:
            pass

    def set_concurrent_slots(self, active: int):
        if not self.enabled:
            return
        try:
            self._gauges['concurrent_search_slots'].labels(*self._common()).set(active)
        except Exception:
            pass

    # ============== 内部工具 ==============
    def _refresh_dynamic_gauges(self):
        """抓取时刷新动态 gauge (Ollama / Leader)"""
        try:
            from ollama_lb import get_ollama_lb
            lb = get_ollama_lb()
            self._gauges['ollama_nodes_total'].labels(*self._common()).set(lb.total_nodes())
            self._gauges['ollama_healthy_nodes'].labels(*self._common()).set(lb.healthy_count())
            pending = sum(max(0, n.active_requests) for n in lb._nodes)
            self._gauges['ollama_pending_requests'].labels(*self._common()).set(pending)
        except Exception:
            pass

    @staticmethod
    def _normalize_endpoint(path: str) -> str:
        """把 URL 参数归一化，避免 metrics 基数爆炸"""
        if path.startswith('/api/search'):
            return '/api/search'
        if path.startswith('/api/delta') and path != '/api/delta/dashboard':
            return '/api/delta'
        if path.startswith('/api/admin/import'):
            return '/api/admin/import'
        if path.startswith('/api/agent/query'):
            return '/api/agent/query'
        # 静态文件合并
        if '.' in path.split('/')[-1] and not path.startswith('/api'):
            return '/static/*'
        return path


# 全局单例
def get_metrics() -> MetricsManager:
    return MetricsManager()
