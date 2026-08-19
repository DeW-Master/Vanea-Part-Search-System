# -*- coding: utf-8 -*-
"""
van.ea 车辆零件智能查询系统 - Ollama 多实例负载均衡器

功能:
    - 支持多 Ollama 实例集群: round_robin / least_conn / random
    - 主动健康检查: 定时 ping /api/tags, 自动摘除故障节点
    - 故障恢复: 节点恢复后经 backoff 时间重新加入集群
    - 失败重试: 单次请求失败时透明切换到下一个节点
    - 连接计数: least_conn 策略下跟踪每个节点活跃请求数
    - 可观测: 暴露 Prometheus metrics (通过 metrics 模块)
"""

import json
import time
import random
import threading
import urllib.request
import urllib.error
from typing import List, Dict, Optional, Callable, Tuple

from config import (
    OLLAMA_URLS, OLLAMA_LB_STRATEGY,
    OLLAMA_HEALTHCHECK_INTERVAL, OLLAMA_REQUEST_TIMEOUT,
    OLLAMA_FAILURE_THRESHOLD, OLLAMA_RECOVERY_BACKOFF,
    OLLAMA_MODEL, INSTANCE_ID,
)


class OllamaNode:
    """单个 Ollama 实例节点"""
    __slots__ = (
        'url', 'healthy', 'consecutive_failures', 'last_check',
        'last_failure', 'active_requests', 'total_requests',
        'total_failures', 'recovery_at',
    )

    def __init__(self, url: str):
        self.url = url.rstrip('/')
        self.healthy = True
        self.consecutive_failures = 0
        self.last_check = 0.0
        self.last_failure = 0.0
        self.active_requests = 0
        self.total_requests = 0
        self.total_failures = 0
        self.recovery_at = 0.0  # 可再次尝试加入集群的时间戳

    def mark_success(self):
        self.consecutive_failures = 0
        if not self.healthy:
            print(f"[Ollama-LB] 节点恢复: {self.url}")
        self.healthy = True

    def mark_failure(self, threshold: int = OLLAMA_FAILURE_THRESHOLD):
        self.consecutive_failures += 1
        self.total_failures += 1
        self.last_failure = time.time()
        if self.healthy and self.consecutive_failures >= threshold:
            self.healthy = False
            self.recovery_at = time.time() + OLLAMA_RECOVERY_BACKOFF
            print(f"[Ollama-LB] 节点摘除: {self.url} "
                  f"(连续失败 {self.consecutive_failures} 次, "
                  f"恢复冷却: {OLLAMA_RECOVERY_BACKOFF}s)")

    def can_try_recover(self) -> bool:
        return (not self.healthy) and time.time() >= self.recovery_at

    def to_dict(self) -> Dict:
        return {
            'url': self.url,
            'healthy': self.healthy,
            'consecutive_failures': self.consecutive_failures,
            'active_requests': self.active_requests,
            'total_requests': self.total_requests,
            'total_failures': self.total_failures,
            'last_check': self.last_check,
        }


class OllamaLoadBalancer:
    """
    Ollama 多实例负载均衡器
    用法:
        lb = OllamaLoadBalancer()
        # 简单请求 (自动重试)
        resp_bytes = lb.request('/api/generate', payload, stream=False)
        # 流式请求 (生成器)
        for chunk in lb.request_stream('/api/chat', payload):
            ...
    """

    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, urls: Optional[List[str]] = None, strategy: str = None):
        if getattr(self, '_initialized', False):
            return
        self._initialized = True

        self._urls = urls or OLLAMA_URLS
        self._strategy = (strategy or OLLAMA_LB_STRATEGY).lower()

        # 节点池
        self._nodes: List[OllamaNode] = [OllamaNode(u) for u in self._urls]
        self._nodes_lock = threading.RLock()

        # round_robin 指针
        self._rr_index = 0
        self._rr_lock = threading.Lock()

        # 健康检查线程
        self._stop_event = threading.Event()
        self._health_thread: Optional[threading.Thread] = None

        # metrics 回调 (可选, 由 metrics 模块注入)
        self._metrics_callbacks: Dict[str, Callable] = {}

        # 启动健康检查
        self._start_health_checker()

        print(f"[Ollama-LB] 初始化: 策略={self._strategy}, 节点数={len(self._nodes)}")
        for n in self._nodes:
            print(f"[Ollama-LB]   - {n.url}")

    # ============== 生命周期 ==============
    def _start_health_checker(self):
        if OLLAMA_HEALTHCHECK_INTERVAL <= 0:
            return
        self._health_thread = threading.Thread(
            target=self._health_loop,
            name="OllamaLB-HealthCheck",
            daemon=True,
        )
        self._health_thread.start()

    def _health_loop(self):
        # 启动时先快速检查一次
        time.sleep(3)
        while not self._stop_event.is_set():
            try:
                self._check_all_nodes()
            except Exception as e:
                print(f"[Ollama-LB] 健康检查异常: {e}")
            self._stop_event.wait(OLLAMA_HEALTHCHECK_INTERVAL)

    def _check_all_nodes(self):
        with self._nodes_lock:
            nodes = list(self._nodes)
        for node in nodes:
            if node.can_try_recover() or node.healthy:
                ok = self._ping_node(node)
                node.last_check = time.time()
                if ok:
                    node.mark_success()
                else:
                    node.mark_failure(threshold=1)  # 健康检查失败直接降级

    @staticmethod
    def _ping_node(node: OllamaNode) -> bool:
        """通过 /api/tags 接口检查节点是否可用"""
        try:
            req = urllib.request.Request(f"{node.url}/api/tags", method='GET')
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def shutdown(self):
        self._stop_event.set()
        if self._health_thread:
            self._health_thread.join(timeout=5)

    # ============== 节点选择 ==============
    def pick_node(self) -> Optional[OllamaNode]:
        """根据策略选择一个健康节点"""
        with self._nodes_lock:
            candidates = [
                n for n in self._nodes
                if n.healthy or n.can_try_recover()
            ]

        if not candidates:
            # 全部不可用，返回 None (让调用者决定是否重试或报错)
            return None

        if self._strategy == 'random':
            return random.choice(candidates)

        if self._strategy == 'least_conn':
            candidates.sort(key=lambda n: (n.active_requests, n.total_failures))
            return candidates[0]

        # round_robin (默认)
        with self._rr_lock:
            idx = self._rr_index % len(candidates)
            self._rr_index += 1
            return candidates[idx]

    def get_all_nodes_status(self) -> List[Dict]:
        with self._nodes_lock:
            return [n.to_dict() for n in self._nodes]

    def healthy_count(self) -> int:
        with self._nodes_lock:
            return sum(1 for n in self._nodes if n.healthy)

    def total_nodes(self) -> int:
        return len(self._nodes)

    # ============== 请求执行 ==============
    def request(
        self,
        path: str,
        payload: Dict,
        method: str = 'POST',
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> Tuple[bytes, OllamaNode]:
        """
        执行非流式请求，自动重试故障节点。
        返回 (response_bytes, used_node)
        """
        timeout = timeout or OLLAMA_REQUEST_TIMEOUT
        max_retries = max_retries if max_retries is not None else len(self._nodes)

        last_exc = None
        for attempt in range(max_retries):
            node = self.pick_node()
            if node is None:
                raise ConnectionError(
                    "[Ollama-LB] 所有 Ollama 节点均不可用。"
                    f"节点状态: {self.get_all_nodes_status()}"
                )
            try:
                node.active_requests += 1
                node.total_requests += 1
                body = json.dumps(payload).encode('utf-8')
                req = urllib.request.Request(
                    f"{node.url}{path}",
                    data=body,
                    headers={'Content-Type': 'application/json'},
                    method=method,
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = resp.read()
                node.mark_success()
                self._fire_metric('ollama_request_total', node=node, success=True)
                return data, node
            except Exception as e:
                node.mark_failure()
                self._fire_metric('ollama_request_total', node=node, success=False)
                last_exc = e
                print(f"[Ollama-LB] 请求 {node.url}{path} 失败 (尝试 {attempt+1}/{max_retries}): {e}")
            finally:
                node.active_requests = max(0, node.active_requests - 1)

        assert last_exc is not None
        raise last_exc

    def request_stream(
        self,
        path: str,
        payload: Dict,
        method: str = 'POST',
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
    ):
        """
        执行流式请求，返回行级生成器。
        注意: 流式请求一旦首字节返回后不跨节点重试 (语义不允许)。
        """
        timeout = timeout or OLLAMA_REQUEST_TIMEOUT
        max_retries = max_retries if max_retries is not None else max(1, len(self._nodes))

        # 只在请求建立阶段重试
        last_exc = None
        for attempt in range(max_retries):
            node = self.pick_node()
            if node is None:
                raise ConnectionError(
                    "[Ollama-LB] 所有 Ollama 节点均不可用。"
                    f"节点状态: {self.get_all_nodes_status()}"
                )
            try:
                node.active_requests += 1
                node.total_requests += 1
                body = json.dumps(payload).encode('utf-8')
                req = urllib.request.Request(
                    f"{node.url}{path}",
                    data=body,
                    headers={'Content-Type': 'application/json'},
                    method=method,
                )
                resp = urllib.request.urlopen(req, timeout=timeout)
                node.mark_success()
                self._fire_metric('ollama_request_total', node=node, success=True)
                # 进入流式输出
                try:
                    for line in resp:
                        if line:
                            yield line
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass
                    node.active_requests = max(0, node.active_requests - 1)
                return
            except Exception as e:
                node.mark_failure()
                self._fire_metric('ollama_request_total', node=node, success=False)
                node.active_requests = max(0, node.active_requests - 1)
                last_exc = e
                print(f"[Ollama-LB] 流式请求 {node.url}{path} 失败 "
                      f"(尝试 {attempt+1}/{max_retries}): {e}")
                # 非流式已建立才不重试，这里是建连阶段，可以继续重试
                continue

        assert last_exc is not None
        raise last_exc

    # ============== metrics 回调 ==============
    def register_metric_callback(self, name: str, cb: Callable):
        """metrics 模块调用以注册回调"""
        self._metrics_callbacks[name] = cb

    def _fire_metric(self, name: str, **kwargs):
        cb = self._metrics_callbacks.get(name)
        if cb:
            try:
                cb(**kwargs)
            except Exception as e:
                print(f"[Ollama-LB] metric 回调异常 {name}: {e}")


# 全局单例
_ollama_lb_instance: Optional[OllamaLoadBalancer] = None
_lb_init_lock = threading.Lock()


def get_ollama_lb() -> OllamaLoadBalancer:
    """获取负载均衡器单例"""
    global _ollama_lb_instance
    if _ollama_lb_instance is None:
        with _lb_init_lock:
            if _ollama_lb_instance is None:
                _ollama_lb_instance = OllamaLoadBalancer()
    return _ollama_lb_instance
