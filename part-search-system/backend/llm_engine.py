# -*- coding: utf-8 -*-
"""
van.ea 车辆零件智能查询系统 - LLM 双引擎抽象层

设计目标:
    - 统一封装 Ollama 与 vLLM 两种推理后端, 对上层 (agent.py) 暴露一致的
      chat(messages, system_prompt, temperature) 接口
    - 一键无缝切换: 切换期间新请求等待, 在途请求排空, 保证请求不丢失
    - 自动故障转移: 首选引擎不可用时自动降级到备用引擎, 不中断服务
    - 看门狗: 后台线程定期健康检查, vLLM 卡死/宕机时自动重启并重新分配流量
    - 模型注册表: 统一记录各引擎可用模型, 供前端展示与切换
    - 全量日志与 Prometheus 指标 (请求量/耗时/在途数/切换/重启)

引擎说明:
    - OllamaEngine: 复用现有 OllamaAgent (含多实例负载均衡 LB)
    - VllmEngine:   vLLM OpenAI 兼容 API (/v1/chat/completions), 跑在 WSL2
"""

import os
import re
import json
import time
import shutil
import threading
import subprocess
import urllib.request
import urllib.error
from datetime import datetime

from config import (
    DATA_DIR,
    OLLAMA_MODEL,
    VLLM_URL, VLLM_MODEL, VLLM_API_KEY, VLLM_HEALTH_URL,
    VLLM_REQUEST_TIMEOUT,
    LLM_ENGINE_PREFERRED,
    LLM_WATCHDOG_INTERVAL, LLM_WATCHDOG_FAILURE_THRESHOLD,
    VLLM_RESTART_ENABLED, VLLM_SERVICE_SCRIPT,
    LLM_SWITCH_DRAIN_TIMEOUT,
)

from log_config import get_logger

logger = get_logger("llm_engine")

try:
    from metrics import MetricsManager
    _metrics = MetricsManager()
except Exception:  # metrics 为可选依赖, 缺失时降级为 no-op
    _metrics = None


# 引擎状态持久化文件 (记录用户选择的首选引擎, 重启后端后保持)
_ENGINE_STATE_FILE = os.path.join(DATA_DIR, 'llm_engine.json')

# 切换中/引擎状态枚举
ENGINE_OLLAMA = 'ollama'
ENGINE_VLLM = 'vllm'
_VALID_ENGINES = (ENGINE_OLLAMA, ENGINE_VLLM)


def _log(msg, level="info"):
    """统一引擎日志前缀, 便于后台日志检索。"""
    getattr(logger, level, logger.info)("[LLMEngine] %s", msg)


# GPU 指标缓存 (nvidia-smi 查询代价小, 但前端轮询频繁, 缓存 2s)
_GPU_CACHE = {'ts': 0.0, 'data': None}
_GPU_CACHE_LOCK = threading.Lock()


def query_gpu_stats():
    """查询显卡资源占用 (经 nvidia-smi)。

    Windows 后端可直接调用 nvidia-smi (WSL2 共享同一块 GPU, 占用一并可见)。
    返回 {available, name, utilization_percent, memory_used_mb, memory_total_mb,
    memory_percent, temperature_c}; 无 GPU / 命令缺失时 available=False。
    """
    with _GPU_CACHE_LOCK:
        if time.time() - _GPU_CACHE['ts'] < 2.0 and _GPU_CACHE['data'] is not None:
            return _GPU_CACHE['data']

    stats = {'available': False}
    try:
        if not shutil.which('nvidia-smi'):
            raise RuntimeError('nvidia-smi 不在 PATH 中')
        out = subprocess.run(
            ['nvidia-smi',
             '--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=8,
        )
        line = (out.stdout or '').strip().splitlines()[0]
        parts = [p.strip() for p in line.split(',')]
        name, util, mem_used, mem_total, temp = parts[:5]
        mem_used_mb = float(mem_used)
        mem_total_mb = float(mem_total)
        stats = {
            'available': True,
            'name': name,
            'utilization_percent': float(util),
            'memory_used_mb': mem_used_mb,
            'memory_total_mb': mem_total_mb,
            'memory_percent': round(100.0 * mem_used_mb / mem_total_mb, 1)
            if mem_total_mb else 0.0,
            'temperature_c': float(temp),
        }
    except Exception as e:
        stats = {'available': False, 'error': str(e)[:120]}

    with _GPU_CACHE_LOCK:
        _GPU_CACHE['ts'] = time.time()
        _GPU_CACHE['data'] = stats
    return stats


# ============ 请求级追踪 (多用户并发监控) ============
# 调用方 (agent.py) 在处理一次用户搜索时, 通过 set_request_trace() 登记
# 会话标识/查询摘要; chat() 内部每次 LLM 调用再叠加阶段标签 (意图/SQL/答案),
# 由 _call_engine 写入在途请求登记表, 供监控面板实时展示。
_TRACE_LOCAL = threading.local()


def set_request_trace(session_id=None, query=None, ip=None):
    """登记当前线程正在处理的用户请求 (会话短ID + 查询摘要 + 客户端IP)。传 None 清除。"""
    _TRACE_LOCAL.trace = {'session_id': session_id or '',
                          'query': (query or '')[:40],
                          'ip': ip or ''} if (session_id or query or ip) else None


def get_request_trace():
    return getattr(_TRACE_LOCAL, 'trace', None)


def _estimate_prompt_tokens(messages):
    """粗略估算 prompt token 数 (中文按字符计, 用于算力份额加权, 非精确值)。"""
    try:
        total = sum(len(m.get('content') or '') for m in messages)
    except Exception:
        return 0
    return total


# vLLM 服务端指标缓存 (/metrics Prometheus 文本, 抓取廉价, 缓存 3s)
_VLLM_METRICS_CACHE = {'ts': 0.0, 'data': None}
_VLLM_METRICS_LOCK = threading.Lock()


def query_vllm_server_stats():
    """抓取 vLLM /metrics 中的服务端队列与 KV cache 指标。

    返回 {available, running, waiting, kv_used_percent, kv_total_gb}。
    vLLM 不可达 / 非 vLLM 部署时 available=False (监控面板相应区块隐藏)。
    """
    with _VLLM_METRICS_LOCK:
        if time.time() - _VLLM_METRICS_CACHE['ts'] < 3.0 and _VLLM_METRICS_CACHE['data'] is not None:
            return _VLLM_METRICS_CACHE['data']

    result = {'available': False}
    try:
        metrics_url = VLLM_HEALTH_URL.rstrip('/health').rstrip('/') + '/metrics'
        req = urllib.request.Request(metrics_url)
        if VLLM_API_KEY:
            req.add_header('Authorization', f'Bearer {VLLM_API_KEY}')
        with urllib.request.urlopen(req, timeout=3) as resp:
            text = resp.read().decode('utf-8', errors='ignore')

        def _grab(pattern):
            m = re.search(pattern, text, re.M)
            return float(m.group(1)) if m else None

        running = _grab(r'^vllm:num_requests_running\{[^}]*\}\s+([\d.]+)\s*$')
        waiting = _grab(r'^vllm:num_requests_waiting\{[^}]*\}\s+([\d.]+)\s*$')
        # vLLM 0.6.x 起指标名为 vllm:kv_cache_usage_perc; 旧版为 vllm:gpu_cache_usage_perc
        kv_used = _grab(r'^vllm:kv_cache_usage_perc\{[^}]*\}\s+([\d.eE+-]+)\s*$')
        if kv_used is None:
            kv_used = _grab(r'^vllm:gpu_cache_usage_perc\{[^}]*\}\s+([\d.eE+-]+)\s*$')
        # KV cache 总 token 容量 (cache_config_info 标签内), 用于面板展示可容纳并发规模
        kv_tokens = None
        m_info = re.search(r'vllm:cache_config_info\{([^}]*)\}\s+[\d.]+\s*$', text, re.M)
        if m_info:
            m_tok = re.search(r'kv_cache_size_tokens="(\d+)"', m_info.group(1))
            if m_tok:
                kv_tokens = int(m_tok.group(1))
        # 新版 vLLM 指标名可能不带冒号前缀, 兜底
        if running is None:
            running = _grab(r'^vllm_num_requests_running\{[^}]*\}\s+([\d.]+)\s*$')
        if waiting is None:
            waiting = _grab(r'^vllm_num_requests_waiting\{[^}]*\}\s+([\d.]+)\s*$')

        result = {
            'available': True,
            'running': int(running or 0),
            'waiting': int(waiting or 0),
            'kv_cache_used_percent': round(float(kv_used) * 100, 1) if kv_used is not None else None,
            'kv_cache_total_tokens': kv_tokens,
        }
    except Exception as e:
        result = {'available': False, 'error': str(e)[:120]}

    with _VLLM_METRICS_LOCK:
        _VLLM_METRICS_CACHE['ts'] = time.time()
        _VLLM_METRICS_CACHE['data'] = result
    return result


class BaseEngine:
    """引擎基类: 子类需实现 name / health_check / chat / list_models。"""

    name = 'base'

    def health_check(self):
        """返回 True/False。应快速返回 (短超时), 供看门狗调用。"""
        raise NotImplementedError

    def chat(self, messages, system_prompt=None, temperature=0.3):
        """多轮对话。messages: [{'role','content'}]; 返回 assistant 文本。"""
        raise NotImplementedError

    def chat_stream(self, messages, system_prompt=None, temperature=0.3):
        """流式多轮对话, 逐段 yield (kind, delta):
        kind='thinking' 为思考增量, kind='answer' 为正文增量。
        基类默认实现: 退化为非流式, 一次性返回全部正文 (无思考)。
        """
        content = self.chat(messages, system_prompt=system_prompt,
                            temperature=temperature)
        if content:
            yield 'answer', content

    def list_models(self):
        """返回该引擎已注册可用模型名列表。"""
        return []


class OllamaEngine(BaseEngine):
    """Ollama 引擎: 包装现有 OllamaAgent (内部已含 LB 负载均衡 + 重试)。"""

    name = ENGINE_OLLAMA

    def __init__(self):
        self._agent = None
        self._lock = threading.Lock()

    def _get_agent(self):
        """懒加载 OllamaAgent (避免模块导入期即探测服务)。"""
        if self._agent is None:
            with self._lock:
                if self._agent is None:
                    from agent import OllamaAgent
                    self._agent = OllamaAgent()
        return self._agent

    def health_check(self):
        try:
            return bool(self._get_agent().available)
        except Exception:
            return False

    def chat(self, messages, system_prompt=None, temperature=0.3):
        return self._get_agent().chat(
            messages, system_prompt=system_prompt, temperature=temperature
        )

    def chat_stream(self, messages, system_prompt=None, temperature=0.3):
        return self._get_agent().chat_stream(
            messages, system_prompt=system_prompt, temperature=temperature
        )

    def list_models(self):
        """通过 /api/tags 获取已安装模型。"""
        try:
            import json as _json
            req = urllib.request.Request(
                "http://localhost:11434/api/tags", method='GET'
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = _json.loads(resp.read())
            return [m.get('name', '') for m in data.get('models', []) if m.get('name')]
        except Exception:
            return []

    def current_model(self):
        try:
            return self._get_agent().model or OLLAMA_MODEL
        except Exception:
            return OLLAMA_MODEL

    def loaded_models(self):
        """Ollama 当前已加载(驻留显存)的模型列表 (via /api/ps)。

        Ollama 服务本身不占显存, 模型 runner 加载后才占; keep_alive 到期或
        显式卸载后 runner 退出、显存归还。
        """
        try:
            req = urllib.request.Request(
                "http://localhost:11434/api/ps", method='GET'
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
            return [m.get('name', '') for m in (data.get('models') or [])
                    if m.get('name')]
        except Exception:
            return []

    def unload_models(self):
        """卸载 Ollama 全部驻留模型 (keep_alive=0), 立即释放显存。

        用于 vLLM 承载流量时: 8GB 共享显卡上 vLLM(~6.6G) 与 Ollama 模型(~5G)
        无法同时驻留。Ollama 服务保持在线, 故障转移时模型按需重新加载
        (首次请求多几秒加载时间)。返回成功卸载的模型名列表。
        """
        unloaded = []
        for name in self.loaded_models():
            try:
                payload = json.dumps({"model": name, "keep_alive": 0}).encode('utf-8')
                req = urllib.request.Request(
                    "http://localhost:11434/api/generate",
                    data=payload, method='POST',
                )
                req.add_header('Content-Type', 'application/json')
                with urllib.request.urlopen(req, timeout=15) as resp:
                    resp.read()
                unloaded.append(name)
                _log(f"已卸载 Ollama 模型释放显存: {name}")
            except Exception as e:
                _log(f"卸载 Ollama 模型 {name} 失败: {e}", level="warning")
        return unloaded


class VllmEngine(BaseEngine):
    """vLLM 引擎: 调用 OpenAI 兼容 API (/v1/chat/completions)。"""

    name = ENGINE_VLLM

    def __init__(self, base_url=None, model=None, api_key=None):
        self.base_url = (base_url or VLLM_URL).rstrip('/')
        self.model = model or VLLM_MODEL
        self.api_key = api_key or VLLM_API_KEY

    def _request(self, path, payload=None, timeout=10, method=None):
        url = f"{self.base_url}{path}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode('utf-8')
            method = method or 'POST'
        req = urllib.request.Request(url, data=data, method=method or 'GET')
        req.add_header('Content-Type', 'application/json')
        if self.api_key:
            req.add_header('Authorization', f'Bearer {self.api_key}')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw.decode('utf-8')) if raw else {}

    def health_check(self):
        """vLLM /health: 模型加载完成返回 200。短超时 (3s)。"""
        try:
            health = VLLM_HEALTH_URL.rstrip('/')
            req = urllib.request.Request(health, method='GET')
            with urllib.request.urlopen(req, timeout=3) as resp:
                return 200 <= resp.status < 300
        except Exception:
            return False

    def chat(self, messages, system_prompt=None, temperature=0.3):
        msgs = list(messages or [])
        if system_prompt:
            msgs.insert(0, {'role': 'system', 'content': system_prompt})
        payload = {
            'model': self.model,
            'messages': msgs,
            'temperature': temperature,
            'stream': False,
            # 非思考模式 (服务端 chat-template 已注入 /no_think; 双保险显式关闭)
            'chat_template_kwargs': {'enable_thinking': False},
        }
        data = self._request('/chat/completions', payload,
                             timeout=VLLM_REQUEST_TIMEOUT)
        choices = data.get('choices') or []
        if not choices:
            raise RuntimeError(f"vLLM 返回空 choices: {str(data)[:200]}")
        content = (choices[0].get('message') or {}).get('content', '')
        return self._strip_think(content)

    def chat_stream(self, messages, system_prompt=None, temperature=0.3):
        """流式对话 (OpenAI 兼容 SSE), 开启思考。

        逐段 yield ('thinking', delta) / ('answer', delta)。
        服务端模板可能为 nonthinking 版, 此时无 reasoning_content, 仅有正文。
        """
        msgs = list(messages or [])
        if system_prompt:
            msgs.insert(0, {'role': 'system', 'content': system_prompt})
        payload = {
            'model': self.model,
            'messages': msgs,
            'temperature': temperature,
            'stream': True,
            # 流式问答开启思考 (非流式 NL2SQL 仍显式关闭, 互不影响)
            'chat_template_kwargs': {'enable_thinking': True},
            'stream_options': {'include_usage': False},
        }
        url = f"{self.base_url}/chat/completions"
        body = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        if self.api_key:
            req.add_header('Authorization', f'Bearer {self.api_key}')
        # 流式响应整体可能持续数分钟, 用较长读超时
        with urllib.request.urlopen(req, timeout=max(VLLM_REQUEST_TIMEOUT, 180)) as resp:
            yielded = False
            for raw_line in resp:
                if not raw_line:
                    continue
                line = raw_line.decode('utf-8', errors='ignore').strip()
                if not line or not line.startswith('data:'):
                    continue
                data_str = line[5:].strip()
                if data_str == '[DONE]':
                    break
                try:
                    chunk = json.loads(data_str)
                except Exception:
                    continue
                choices = chunk.get('choices') or []
                if not choices:
                    continue
                delta = choices[0].get('delta') or {}
                reasoning = delta.get('reasoning_content')
                if reasoning:
                    yielded = True
                    yield 'thinking', reasoning
                content = delta.get('content')
                if content:
                    yielded = True
                    yield 'answer', content
            if not yielded:
                raise RuntimeError("vLLM 流式返回为空")

    @staticmethod
    def _strip_think(text):
        """剥离 Qwen3 非思考模式下残留的空 <think></think> 外壳。

        /no_think 生效后模型不再产出推理内容, 但仍可能返回空思考块;
        去掉它避免污染 NL2SQL/JSON 结构化输出。仅移除空块, 非空思考内容保留。
        """
        if not text:
            return text
        import re
        # 去掉 <think> 与 </think> 之间只有空白的块 (含标签)
        cleaned = re.sub(r'<think>\s*</think>\s*', '', text, flags=re.DOTALL)
        return cleaned.strip()

    def list_models(self):
        try:
            data = self._request('/models', timeout=5)
            return [m.get('id', '') for m in data.get('data', []) if m.get('id')]
        except Exception:
            # vLLM 未启动时至少返回注册的服务名
            return [self.model] if self.health_check() else []


class EngineManager:
    """
    引擎管理器 (单例)。

    职责:
        - 维护 Ollama / vLLM 两个引擎实例
        - 管理首选引擎 (preferred) 与当前实际服务引擎 (active)
        - 无缝切换: 切换时等待在途请求排空 (drain), 新请求在锁/条件变量上排队
        - 故障自动转移: chat 调用首选引擎失败 -> 自动尝试备用引擎
        - 看门狗线程: 周期健康检查, vLLM 连续失败则触发重启
    """

    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, '_initialized', False):
            return
        self._initialized = True

        self.engines = {
            ENGINE_OLLAMA: OllamaEngine(),
            ENGINE_VLLM: VllmEngine(),
        }

        # 首选引擎 (用户期望使用); active 为当前实际承载流量的引擎
        self.preferred = self._load_preferred()
        if self.preferred not in _VALID_ENGINES:
            self.preferred = ENGINE_OLLAMA
        self.active = self.preferred

        # 在途请求计数 (按引擎)
        self._inflight = {ENGINE_OLLAMA: 0, ENGINE_VLLM: 0}
        self._inflight_lock = threading.Lock()

        # 在途请求登记表 (多用户并发监控): rid -> 请求快照
        # {rid, engine, start_time, session_id, query, stage, prompt_tokens}
        self._inflight_requests = {}
        self._inflight_seq = 0

        # 切换锁: 切换期间为写锁定, 所有 chat 请求阻塞等待 (请求不丢失)
        self._switch_lock = threading.RLock()
        self._switching = False
        self._switch_condition = threading.Condition()

        # 引擎健康状态缓存
        self._healthy = {ENGINE_OLLAMA: False, ENGINE_VLLM: False}
        self._fail_count = {ENGINE_OLLAMA: 0, ENGINE_VLLM: 0}

        # 模型注册表: {engine: [model_names]}
        self._registry = {ENGINE_OLLAMA: [], ENGINE_VLLM: []}

        # 看门狗
        self._watchdog_thread = None
        self._watchdog_stop = threading.Event()
        self._restart_cooldown = 0  # 重启冷却 (看门狗 tick 计数)

        # 切换历史 (供前端/日志查看)
        self.switch_history = []

        # 调用计数 (进程内累计, 供前端实时展示大模型调用情况)
        self._request_total = {ENGINE_OLLAMA: 0, ENGINE_VLLM: 0}
        self._request_failed = {ENGINE_OLLAMA: 0, ENGINE_VLLM: 0}

        # vLLM 恢复状态机: 'idle' | 'restarting' (自动重启/恢复中)
        self._vllm_recovery = {
            'state': 'idle',
            'started_at': None,
            'message': '',
            'attempts': 0,
        }
        self._recovery_lock = threading.Lock()

        self._refresh_registry()
        self._init_health()
        self._start_watchdog()
        _log(f"初始化完成, 首选引擎={self.preferred}, 当前引擎={self.active}")

    # ---------- 持久化 ----------
    def _load_preferred(self):
        try:
            if os.path.exists(_ENGINE_STATE_FILE):
                with open(_ENGINE_STATE_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f).get('preferred', LLM_ENGINE_PREFERRED)
        except Exception as e:
            _log(f"读取引擎状态文件失败: {e}", level="warning")
        return LLM_ENGINE_PREFERRED

    def _save_preferred(self):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(_ENGINE_STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump({'preferred': self.preferred,
                           'updated_at': datetime.now().isoformat()}, f,
                          ensure_ascii=False, indent=2)
        except Exception as e:
            _log(f"写入引擎状态文件失败: {e}", level="warning")

    # ---------- 健康 ----------
    def _init_health(self):
        for name in _VALID_ENGINES:
            self._healthy[name] = self.engines[name].health_check()
            if _metrics:
                _metrics.set_llm_healthy(name, self._healthy[name])

    def is_healthy(self, engine=None):
        """查询引擎健康 (默认查当前 active)。"""
        return self._healthy.get(engine or self.active, False)

    def _refresh_registry(self):
        """刷新模型注册表。"""
        for name, eng in self.engines.items():
            try:
                models = eng.list_models()
                if models:
                    self._registry[name] = models
            except Exception:
                pass

    def list_registered_models(self):
        """返回统一模型注册表: [{engine, model, active}]。"""
        self._refresh_registry()
        result = []
        for eng_name in _VALID_ENGINES:
            for m in self._registry.get(eng_name, []):
                result.append({
                    'engine': eng_name,
                    'model': m,
                    'active': eng_name == self.active,
                    'healthy': self._healthy.get(eng_name, False),
                })
        return result

    # ---------- 核心: 对话调用 (含故障转移) ----------
    def chat(self, messages, system_prompt=None, temperature=0.3, stage=None):
        """
        对外统一对话接口。
        - 切换期间: 在条件变量上等待, 直到切换完成 (请求不丢失)
        - 首选引擎: 优先调用; 失败则自动故障转移到备用引擎
        - 全程记录在途计数 / 指标 / 日志
        - stage: 调用阶段标签 (如 '意图识别'/'SQL 生成'/'答案合成'), 仅用于监控展示;
          会话标识与查询摘要由 set_request_trace() 经线程上下文透传
        """
        # 1. 等待切换完成 (新请求排队, 不拒绝不丢失)
        with self._switch_condition:
            while self._switching:
                self._switch_condition.wait(timeout=LLM_SWITCH_DRAIN_TIMEOUT + 5)
                if self._switching:
                    _log("等待引擎切换排空超时, 继续尝试当前引擎", level="warning")
                    break

        # 2. 确定调用顺序: 首选优先, 备用兜底
        order = [self.preferred]
        for name in _VALID_ENGINES:
            if name != self.preferred:
                order.append(name)

        last_err = None
        for eng_name in order:
            eng = self.engines.get(eng_name)
            if eng is None:
                continue
            # 备用引擎仅在健康时才尝试 (首选引擎即使健康缓存为否也允许试, 由异常触发转移)
            if eng_name != self.preferred and not self._healthy.get(eng_name, False):
                continue
            try:
                return self._call_engine(eng, messages, system_prompt, temperature,
                                         stage=stage)
            except Exception as e:
                last_err = e
                _log(f"引擎 {eng_name} 调用失败: {e}", level="warning")
                if _metrics:
                    _metrics.set_llm_healthy(eng_name, False)
                self._healthy[eng_name] = False
                self._fail_count[eng_name] = self._fail_count.get(eng_name, 0) + 1
                # 首选失败 -> 标记并尝试下一个 (故障转移)
                if eng_name == self.preferred:
                    self._handle_preferred_failure()
                continue

        raise RuntimeError(f"所有 LLM 引擎均不可用: {last_err}")

    def chat_stream(self, messages, system_prompt=None, temperature=0.3, stage=None):
        """流式对话接口 (与 chat 同语义): 逐段 yield (kind, delta)。

        - 切换期间同样在条件变量排队等待
        - 故障转移仅允许在"首字节返回前": 一旦已有内容发给上层,
          中途断流不能重放 (会造成重复输出), 直接向上抛出
        """
        with self._switch_condition:
            while self._switching:
                self._switch_condition.wait(timeout=LLM_SWITCH_DRAIN_TIMEOUT + 5)
                if self._switching:
                    _log("流式调用: 等待引擎切换排空超时, 继续尝试当前引擎",
                         level="warning")
                    break

        order = [self.preferred]
        for name in _VALID_ENGINES:
            if name != self.preferred:
                order.append(name)

        last_err = None
        for eng_name in order:
            eng = self.engines.get(eng_name)
            if eng is None:
                continue
            if eng_name != self.preferred and not self._healthy.get(eng_name, False):
                continue
            gen = self._call_engine_stream(
                eng, messages, system_prompt, temperature, stage=stage)
            started = False
            try:
                for item in gen:
                    started = True
                    yield item
                return
            except Exception as e:
                last_err = e
                if started:
                    # 已有内容输出, 无法安全重放, 直接上抛
                    raise
                _log(f"引擎 {eng_name} 流式连接失败: {e}", level="warning")
                if _metrics:
                    _metrics.set_llm_healthy(eng_name, False)
                self._healthy[eng_name] = False
                self._fail_count[eng_name] = self._fail_count.get(eng_name, 0) + 1
                if eng_name == self.preferred:
                    self._handle_preferred_failure()
                continue

        raise RuntimeError(f"所有 LLM 引擎均不可用(流式): {last_err}")

    def _call_engine_stream(self, engine, messages, system_prompt, temperature,
                            stage=None):
        """在指定引擎上执行流式调用, 维护在途计数/登记表/指标 (生成器)。"""
        eng_name = engine.name
        trace = get_request_trace() or {}
        rid = None
        with self._inflight_lock:
            self._inflight[eng_name] += 1
            self._inflight_seq += 1
            rid = self._inflight_seq
            self._inflight_requests[rid] = {
                'rid': rid,
                'engine': eng_name,
                'stage': stage or 'LLM 流式调用',
                'session_id': trace.get('session_id', ''),
                'ip': trace.get('ip', ''),
                'query': trace.get('query', ''),
                'prompt_chars': _estimate_prompt_tokens(messages),
                'start_time': time.time(),
            }
            if _metrics:
                _metrics.set_llm_inflight(eng_name, self._inflight[eng_name])
        t0 = time.time()
        success = False
        try:
            first = True
            for kind, delta in engine.chat_stream(
                    messages, system_prompt=system_prompt,
                    temperature=temperature):
                if first:
                    # 首字节到达 = 连接与生成正常, 恢复健康标记
                    first = False
                    success = True
                    self._healthy[eng_name] = True
                    self._fail_count[eng_name] = 0
                    self._request_total[eng_name] = \
                        self._request_total.get(eng_name, 0) + 1
                    if _metrics:
                        _metrics.set_llm_healthy(eng_name, True)
                yield kind, delta
        except Exception:
            self._request_total[eng_name] = self._request_total.get(eng_name, 0) + 1
            self._request_failed[eng_name] = \
                self._request_failed.get(eng_name, 0) + 1
            raise
        finally:
            dur = time.time() - t0
            with self._inflight_lock:
                self._inflight[eng_name] = max(0, self._inflight[eng_name] - 1)
                self._inflight_requests.pop(rid, None)
                if _metrics:
                    _metrics.set_llm_inflight(eng_name, self._inflight[eng_name])
            model = getattr(engine, 'model', None) or OLLAMA_MODEL
            if _metrics:
                _metrics.observe_llm_request(eng_name, str(model), success, dur)

    def _call_engine(self, engine, messages, system_prompt, temperature, stage=None):
        """在指定引擎上执行一次调用, 维护在途计数、请求登记表与指标。"""
        eng_name = engine.name
        trace = get_request_trace() or {}
        rid = None
        with self._inflight_lock:
            self._inflight[eng_name] += 1
            self._inflight_seq += 1
            rid = self._inflight_seq
            self._inflight_requests[rid] = {
                'rid': rid,
                'engine': eng_name,
                'stage': stage or 'LLM 调用',
                'session_id': trace.get('session_id', ''),
                'ip': trace.get('ip', ''),
                'query': trace.get('query', ''),
                'prompt_chars': _estimate_prompt_tokens(messages),
                'start_time': time.time(),
            }
            if _metrics:
                _metrics.set_llm_inflight(eng_name, self._inflight[eng_name])
        t0 = time.time()
        success = False
        try:
            content = engine.chat(messages, system_prompt=system_prompt,
                                  temperature=temperature)
            success = True
            # 调用成功: 恢复健康
            self._healthy[eng_name] = True
            self._fail_count[eng_name] = 0
            self._request_total[eng_name] = self._request_total.get(eng_name, 0) + 1
            if _metrics:
                _metrics.set_llm_healthy(eng_name, True)
            return content
        except Exception:
            self._request_total[eng_name] = self._request_total.get(eng_name, 0) + 1
            self._request_failed[eng_name] = self._request_failed.get(eng_name, 0) + 1
            raise
        finally:
            dur = time.time() - t0
            with self._inflight_lock:
                self._inflight[eng_name] = max(0, self._inflight[eng_name] - 1)
                self._inflight_requests.pop(rid, None)
                if _metrics:
                    _metrics.set_llm_inflight(eng_name, self._inflight[eng_name])
            model = getattr(engine, 'model', None) or OLLAMA_MODEL
            if _metrics:
                _metrics.observe_llm_request(eng_name, str(model), success, dur)

    def _handle_preferred_failure(self):
        """首选引擎调用失败: 若备用健康则自动转移 active。"""
        backup = ENGINE_VLLM if self.preferred == ENGINE_OLLAMA else ENGINE_OLLAMA
        if self._healthy.get(backup, False):
            _log(f"首选引擎 {self.preferred} 故障, 自动转移到 {backup}", level="warning")
            self._set_active(backup, reason='auto_failover')

    # ---------- 无缝切换 ----------
    def switch_engine(self, target, reason='manual', drain_timeout=None):
        """
        一键无缝切换引擎。
        流程:
            1. 置 switching 标志 -> 新 chat 请求在条件变量排队
            2. 等待目标引擎健康 (不健康则拒绝切换, 避免切到死引擎)
            3. 等待当前引擎在途请求排空 (drain), 最长 drain_timeout
            4. 切换 active, 持久化 preferred, 唤醒所有排队请求
        返回 (success: bool, message: str)。
        """
        if target not in _VALID_ENGINES:
            return False, f"非法引擎: {target} (可选: {_VALID_ENGINES})"
        if target == self.active and target == self.preferred:
            return True, f"引擎已是 {target}"

        drain_timeout = drain_timeout or LLM_SWITCH_DRAIN_TIMEOUT

        # 切换前排他 (多个切换请求串行)
        with self._switch_condition:
            self._switching = True

        try:
            # 1. 检查目标引擎健康
            target_healthy = self.engines[target].health_check()
            self._healthy[target] = target_healthy
            if _metrics:
                _metrics.set_llm_healthy(target, target_healthy)
            if not target_healthy:
                return False, f"目标引擎 {target} 未就绪/不健康, 已取消切换"

            # 2. 等待在途请求排空 (仅等待当前 active 引擎)
            old = self.active
            waited = 0.0
            interval = 0.5
            while self._inflight.get(old, 0) > 0 and waited < drain_timeout:
                time.sleep(interval)
                waited += interval
            remaining = self._inflight.get(old, 0)
            if remaining > 0:
                _log(f"排空等待 {waited:.1f}s 后仍有 {remaining} 个在途请求, 强制切换", level="warning")

            # 3. 执行切换
            self.preferred = target
            self._set_active(target, reason=reason)
            self._save_preferred()
            _log(f"引擎切换完成: {old} -> {target} (reason={reason})")
            return True, f"已切换到 {target} 引擎"
        finally:
            with self._switch_condition:
                self._switching = False
                self._switch_condition.notify_all()

    def _set_active(self, engine, reason='manual'):
        """内部: 设置 active 引擎并记录历史/指标。"""
        old = self.active
        self.active = engine
        self.switch_history.append({
            'from': old, 'to': engine,
            'reason': reason,
            'at': datetime.now().isoformat(timespec='seconds'),
        })
        self.switch_history = self.switch_history[-20:]
        if _metrics:
            _metrics.set_llm_active(ENGINE_OLLAMA, engine == ENGINE_OLLAMA)
            _metrics.set_llm_active(ENGINE_VLLM, engine == ENGINE_VLLM)
            if old != engine:
                _metrics.inc_llm_switch(old, engine, reason)
        _log(f"active 引擎: {old} -> {engine} ({reason})")
        # 流量切到 vLLM 后, 后台卸载 Ollama 驻留模型释放显存 (小显存卡两引擎模型无法共存)
        if engine == ENGINE_VLLM and old != ENGINE_VLLM:
            threading.Thread(
                target=self._release_ollama_vram, name='ollama-vram-release',
                daemon=True,
            ).start()

    def _release_ollama_vram(self):
        """卸载 Ollama 全部驻留模型, 把显存让给 vLLM (Ollama 服务保持在线)。"""
        try:
            ollama_eng = self.engines.get(ENGINE_OLLAMA)
            loaded = ollama_eng.loaded_models() if ollama_eng else []
            if not loaded:
                return
            _log(f"vLLM 承载流量, 后台释放 Ollama 显存: {loaded}")
            unloaded = ollama_eng.unload_models()
            if unloaded:
                _log(f"Ollama 显存释放完成, 卸载模型: {unloaded}")
        except Exception as e:
            _log(f"释放 Ollama 显存异常 (不影响切换): {e}", level="warning")

    def _free_vram_before_restart(self):
        """看门狗重启 vLLM 前: 同步卸载 Ollama 模型并等待显存归还。

        故障转移后 Ollama 已加载模型(~5G)驻留显存; 不释放则 vLLM 启动会因
        KV cache 无显存可分配而失败 (No available memory for the cache blocks)。
        Ollama 卸载是异步归还显存的, 卸载后轮询空闲显存回升再返回。
        """
        ollama_eng = self.engines.get(ENGINE_OLLAMA)
        if ollama_eng is None:
            return
        try:
            loaded = ollama_eng.loaded_models()
            if loaded:
                _log(f"重启 vLLM 前先卸载 Ollama 驻留模型释放显存: {loaded}")
                ollama_eng.unload_models()

            # 等待显存归还: 卸载请求返回后 runner 退出、显存释放有短暂延迟。
            # vLLM 启动需 util(0.45~0.90)*总显存; 8GB 卡需 ~6.6G, 这里要求空闲
            # 显存 >= 总显存 * 0.85 - 安全余量, 最多等 ~20s。
            for i in range(10):
                time.sleep(2)
                g = query_gpu_stats()
                if g.get('available'):
                    free_mb = g['memory_total_mb'] - g['memory_used_mb']
                    _log(f"等待显存归还 ({i+1}/10): 空闲 {free_mb:.0f}/"
                         f"{g['memory_total_mb']:.0f} MiB")
                    if free_mb >= g['memory_total_mb'] * 0.80:
                        _log(f"显存已归还 (空闲 {free_mb:.0f} MiB), 开始重启 vLLM")
                        return
                else:
                    # 无 GPU 指标 (nvidia-smi 不可用), 卸载后固定等 5s 即可
                    if i >= 2:
                        return
            _log("等待显存归还超时, 仍尝试重启 vLLM (脚本会按实际空闲显存动态计算 util)", level="warning")
        except Exception as e:
            _log(f"重启前释放 Ollama 显存异常 (继续重启): {e}", level="warning")

    # ---------- 看门狗 ----------
    def _start_watchdog(self):
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, name='llm-watchdog', daemon=True
        )
        self._watchdog_thread.start()
        _log(f"看门狗已启动 (间隔 {LLM_WATCHDOG_INTERVAL}s)")

    def _watchdog_loop(self):
        """后台健康检查 + vLLM 卡死自动重启。"""
        while not self._watchdog_stop.is_set():
            try:
                for name in _VALID_ENGINES:
                    eng = self.engines[name]
                    healthy = False
                    try:
                        healthy = eng.health_check()
                    except Exception:
                        healthy = False

                    prev = self._healthy.get(name, False)
                    self._healthy[name] = healthy
                    if _metrics:
                        _metrics.set_llm_healthy(name, healthy)

                    if healthy:
                        if not prev:
                            _log(f"引擎 {name} 恢复健康")
                        self._fail_count[name] = 0
                        # vLLM 恢复后若它是首选, 切回
                        if (name == ENGINE_VLLM and self.preferred == ENGINE_VLLM
                                and self.active != ENGINE_VLLM):
                            _log("vLLM 恢复, 切回首选 vLLM 引擎")
                            self._set_active(ENGINE_VLLM, reason='watchdog_recovery')
                    else:
                        self._fail_count[name] = self._fail_count.get(name, 0) + 1
                        fails = self._fail_count[name]
                        _log(f"引擎 {name} 健康检查失败 ({fails}/{LLM_WATCHDOG_FAILURE_THRESHOLD})", level="warning")

                        # 连续失败达阈值:
                        #  - 若该引擎正承载流量 -> 故障转移到备用
                        #  - 若为 vLLM -> 触发自动重启
                        if fails >= LLM_WATCHDOG_FAILURE_THRESHOLD:
                            if self.active == name:
                                backup = ENGINE_VLLM if name == ENGINE_OLLAMA else ENGINE_OLLAMA
                                if self.engines[backup].health_check():
                                    self._set_active(backup, reason='watchdog_failover')
                                    _log(f"看门狗: {name} 故障, 转移到 {backup}", level="warning")
                            if name == ENGINE_VLLM and VLLM_RESTART_ENABLED:
                                with self._recovery_lock:
                                    self._vllm_recovery['state'] = 'restarting'
                                    self._vllm_recovery['started_at'] = datetime.now().isoformat(timespec='seconds')
                                    self._vllm_recovery['attempts'] += 1
                                    self._vllm_recovery['message'] = 'vLLM 服务异常, 正在自动重启并恢复 ...'
                                self._restart_vllm()
                                # 重启后重置失败计数 (给加载时间)
                                self._fail_count[name] = 0
                    # vLLM 恢复健康且恢复流程进行中 -> 结束恢复状态
                    if (name == ENGINE_VLLM and healthy
                            and self._vllm_recovery['state'] == 'restarting'):
                        with self._recovery_lock:
                            self._vllm_recovery['state'] = 'idle'
                            self._vllm_recovery['message'] = 'vLLM 已恢复'
                        _log("vLLM 恢复完成, 恢复状态结束")
            except Exception as e:
                _log(f"看门狗循环异常: {e}", level="warning")

            self._watchdog_stop.wait(LLM_WATCHDOG_INTERVAL)

    def _restart_vllm(self):
        """经 WSL 调用 vllm_service.sh restart 重启 vLLM 服务。"""
        if self._restart_cooldown > 0:
            self._restart_cooldown -= 1
            return
        self._restart_cooldown = 4  # 冷却约 4 个检查周期, 避免频繁重启
        _log("看门狗触发 vLLM 自动重启 ...")
        if _metrics:
            _metrics.inc_llm_restart(ENGINE_VLLM, 'attempted')
        try:
            import subprocess
            # 重启前必须先释放显存: 故障转移后 Ollama 已加载模型(~5G)驻留显存,
            # vLLM 启动需 ~6.6G, 8GB 卡二者无法共存。WSL 脚本访问不到 Windows 侧
            # Ollama, 必须由 Windows 后端可靠卸载 (keep_alive=0), 并等待显存归还。
            self._free_vram_before_restart()
            # Windows 后端 -> wsl 执行服务脚本 restart
            # 脚本路径作 argv 直传 (路径无空格, 已用 /root/vllm_scripts 软链接),
            # 避免 bash -c "..." 经 PowerShell/WSL 传递层引号被破坏
            cmd = [
                'wsl', '-d', 'Ubuntu', '--',
                'bash', VLLM_SERVICE_SCRIPT, 'restart',
            ]
            # 重启脚本内含健康等待, 这里异步执行不阻塞看门狗
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
            _log(f"vLLM 重启进程已发起 (pid={proc.pid}), 等待服务恢复 ...")

            def _wait():
                try:
                    out, _ = proc.communicate(timeout=240)
                    ok = proc.returncode == 0
                    _log(f"vLLM 重启完成 rc={proc.returncode}: "
                         f"{(out or b'')[-300:].decode('utf-8', 'ignore')}")
                    if _metrics:
                        _metrics.inc_llm_restart(
                            ENGINE_VLLM, 'success' if ok else 'failed')
                    # 重启后主动健康探测
                    self._healthy[ENGINE_VLLM] = self.engines[ENGINE_VLLM].health_check()
                    if _metrics:
                        _metrics.set_llm_healthy(ENGINE_VLLM, self._healthy[ENGINE_VLLM])
                except Exception as e:
                    _log(f"vLLM 重启等待异常: {e}", level="warning")
                    if _metrics:
                        _metrics.inc_llm_restart(ENGINE_VLLM, 'failed')

            threading.Thread(target=_wait, name='vllm-restart', daemon=True).start()
        except Exception as e:
            _log(f"vLLM 重启失败: {e}", level="warning")
            if _metrics:
                _metrics.inc_llm_restart(ENGINE_VLLM, 'failed')

    # ---------- 状态汇总 ----------
    def status(self):
        """返回引擎状态快照 (供 API/前端)。"""
        engines = {}
        for name in _VALID_ENGINES:
            eng_info = {
                'healthy': self._healthy.get(name, False),
                'inflight': self._inflight.get(name, 0),
                'fail_count': self._fail_count.get(name, 0),
                'models': self._registry.get(name, []),
                'request_total': self._request_total.get(name, 0),
                'request_failed': self._request_failed.get(name, 0),
            }
            # Ollama: 当前驻留显存的模型 (空列表 = 模型已卸载/不占显存, 可让 vLLM 用满显存)
            if name == ENGINE_OLLAMA:
                try:
                    eng_info['loaded_models'] = self.engines[name].loaded_models()
                except Exception:
                    eng_info['loaded_models'] = []
            engines[name] = eng_info
        any_healthy = any(e['healthy'] for e in engines.values())
        with self._recovery_lock:
            recovery = dict(self._vllm_recovery)

        # 在途请求快照 (多用户并发监控): 附已耗时与算力份额估算
        now = time.time()
        with self._inflight_lock:
            inflight = list(self._inflight_requests.values())
        total_chars = sum(r.get('prompt_chars', 0) for r in inflight) or 1
        requests = []
        for r in sorted(inflight, key=lambda x: x['start_time']):
            chars = r.get('prompt_chars', 0)
            requests.append({
                'rid': r['rid'],
                'engine': r['engine'],
                'stage': r['stage'],
                'session_id': r.get('session_id', ''),
                'ip': r.get('ip', ''),
                'query': r.get('query', ''),
                'prompt_chars': chars,
                'elapsed_seconds': round(now - r['start_time'], 1),
                # 算力份额估算: vLLM 连续批处理下算力由在途请求共享, 无法精确拆分,
                # 按各请求 prompt 规模加权给出参考占比
                'compute_share_percent': round(100.0 * chars / total_chars, 1)
                if inflight else 0.0,
            })

        return {
            'preferred': self.preferred,
            'active': self.active if any_healthy else 'rule',
            'switching': self._switching,
            # rule: 两个引擎都不可用, Agent 回退规则模式
            'fallback_mode': 'rule' if not any_healthy else 'llm',
            'engines': engines,
            'gpu': query_gpu_stats(),
            # vLLM 服务端队列/KV cache 指标 (不可用时 available=False)
            'vllm_server': query_vllm_server_stats(),
            # 在途 LLM 调用实时清单
            'inflight_requests': requests,
            'inflight_total': len(requests),
            'vllm_recovery': recovery,
            'registered_models': self.list_registered_models(),
            'switch_history': list(reversed(self.switch_history[:10])),
        }

    def stop(self):
        self._watchdog_stop.set()


# 模块级单例访问
def get_engine_manager():
    return EngineManager()
