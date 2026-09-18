# -*- coding: utf-8 -*-
"""
van.ea 车辆零件智能查询系统 - 智能体模块

- 建议问题动态生成（每次刷新随机变化）
- 支持并发搜索控制集成

基于本地Ollama或云端API，支持RAG检索增强生成
适配SQLite动态数据库，支持多字段自然语言查询
"""

import re
import json
import os
import threading
import urllib.request
import urllib.error

from database import db_manager, get_db
from config import OLLAMA_URL, OLLAMA_MODEL, OLLAMA_REQUEST_TIMEOUT
from log_config import get_logger

logger = get_logger("agent")

# ============ Phase 3: Ollama 负载均衡器 (懒加载，失败回退直连 OLLAMA_URL) ============
_lb_cache = [None]
def _get_lb():
    if _lb_cache[0] is False:
        return None
    if _lb_cache[0] is not None:
        return _lb_cache[0]
    try:
        from ollama_lb import get_ollama_lb
        _lb_cache[0] = get_ollama_lb()
        return _lb_cache[0]
    except Exception:
        logger.warning("Ollama LB unavailable, falling back to direct %s",
                       OLLAMA_URL, exc_info=True)
        _lb_cache[0] = False
        return None

# ============ 双引擎管理器 (Ollama / vLLM, 懒加载) ============
_em_cache = [None]
def _get_engine_manager():
    """获取 LLM 双引擎管理器单例 (失败返回 None, 不影响 Ollama 旧路径)。"""
    if _em_cache[0] is False:
        return None
    if _em_cache[0] is not None:
        return _em_cache[0]
    try:
        from llm_engine import get_engine_manager
        _em_cache[0] = get_engine_manager()
        return _em_cache[0]
    except Exception:
        logger.warning("LLM engine manager unavailable", exc_info=True)
        _em_cache[0] = False
        return None


class EngineChatAdapter:
    """双引擎适配器: 对外暴露与 OllamaAgent 一致的 chat() 接口。

    内部委托 EngineManager.chat(), 由管理器负责:
    Ollama/vLLM 路由、无缝切换排队、故障自动转移、在途计数与监控指标。
    """

    def __init__(self):
        self.em = _get_engine_manager()

    @property
    def available(self):
        if self.em is None:
            return False
        try:
            return self.em.is_healthy('vllm') or self.em.is_healthy('ollama')
        except Exception:
            return False

    def chat(self, messages, system_prompt=None, temperature=0.3, stage=None):
        if self.em is None:
            raise RuntimeError("LLM 引擎管理器不可用")
        return self.em.chat(messages, system_prompt=system_prompt,
                            temperature=temperature, stage=stage)

    def chat_stream(self, messages, system_prompt=None, temperature=0.3,
                    stage=None):
        if self.em is None:
            raise RuntimeError("LLM 引擎管理器不可用")
        return self.em.chat_stream(messages, system_prompt=system_prompt,
                                   temperature=temperature, stage=stage)


# ============ 配置 ============

# 当前语言: 'zh' 或 'en'
_current_lang = 'zh'

# 当前使用的模型
_current_model = OLLAMA_MODEL

# 模型下载状态
_pull_status = {}  # {model_name: {'status': 'downloading'|'success'|'error', 'progress': int, 'message': str}}
_pull_lock = threading.Lock()

# ============ 算力后端配置 ============
# 算力后端: 'local'(本地Ollama) 或 'cloud'(线上API)
_compute_backend = 'local'

# 云端配置文件路径（项目根目录下的 data/）
_AGENT_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_AGENT_BASE_DIR)
CLOUD_CONFIG_PATH = os.path.join(_PROJECT_ROOT, 'data', 'cloud_config.json')

# 云端配置
_cloud_config = {
    'api_url': '',
    'api_key': '',
    'model': '',
}

# 推荐云端服务商（均兼容OpenAI API格式）
RECOMMENDED_CLOUD_PROVIDERS = [
    {'name': 'DeepSeek', 'api_url': 'https://api.deepseek.com/v1',
     'models': ['deepseek-chat', 'deepseek-reasoner'],
     'desc_zh': '深度求索（高性价比，推荐）', 'desc_en': 'DeepSeek (High cost-performance, recommended)'},
    {'name': 'Qwen', 'api_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
     'models': ['qwen-plus', 'qwen-turbo', 'qwen-max'],
     'desc_zh': '通义千问（阿里云）', 'desc_en': 'Qwen (Alibaba Cloud)'},
    {'name': 'Moonshot', 'api_url': 'https://api.moonshot.cn/v1',
     'models': ['moonshot-v1-8k', 'moonshot-v1-32k'],
     'desc_zh': '月之暗面Kimi', 'desc_en': 'Moonshot Kimi'},
    {'name': 'Zhipu', 'api_url': 'https://open.bigmodel.cn/api/paas/v4',
     'models': ['glm-4', 'glm-4-flash'],
     'desc_zh': '智谱清言GLM', 'desc_en': 'Zhipu GLM'},
    {'name': 'OpenAI', 'api_url': 'https://api.openai.com/v1',
     'models': ['gpt-4o', 'gpt-4o-mini'],
     'desc_zh': 'OpenAI GPT', 'desc_en': 'OpenAI GPT'},
    {'name': 'Custom', 'api_url': '',
     'models': [],
     'desc_zh': '自定义兼容OpenAI格式的API', 'desc_en': 'Custom OpenAI-compatible API'},
]


def _load_cloud_config():
    """从文件加载云端配置"""
    global _cloud_config
    try:
        if os.path.exists(CLOUD_CONFIG_PATH):
            with open(CLOUD_CONFIG_PATH, 'r', encoding='utf-8') as f:
                saved = json.load(f)
                _cloud_config.update(saved)
    except Exception:
        logger.warning("Failed to load cloud config", exc_info=True)


def _save_cloud_config():
    """保存云端配置到文件"""
    try:
        os.makedirs(os.path.dirname(CLOUD_CONFIG_PATH), exist_ok=True)
        with open(CLOUD_CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(_cloud_config, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.warning("Failed to save cloud config", exc_info=True)


# 启动时加载云端配置
_load_cloud_config()


def get_compute_backend():
    """获取当前算力后端: 'local' 或 'cloud'"""
    return _compute_backend


def set_compute_backend(backend):
    """设置算力后端"""
    global _compute_backend
    if backend in ('local', 'cloud'):
        _compute_backend = backend
        return True
    return False


def get_cloud_config():
    """获取云端配置（隐藏API Key中间部分）"""
    config = dict(_cloud_config)
    key = config.get('api_key', '')
    if key and len(key) > 8:
        config['api_key_masked'] = key[:4] + '*' * (len(key) - 8) + key[-4:]
    else:
        config['api_key_masked'] = '****' if key else ''
    return config


# 常见云元数据/回环地址 (SSRF 高危目标), 禁止配置为云端 API 地址
_SSRF_BLOCKED_HOSTS = {
    '169.254.169.254',            # AWS/GCP/Azure 实例元数据
    '100.100.100.200',            # 阿里云元数据
    'metadata.google.internal',   # GCP
    'metadata.tencentyun.com',    # 腾讯云
    'metadata.azure.com',         # Azure
    '127.0.0.1', 'localhost', '::1',
}


def validate_cloud_api_url(url):
    """
    校验云端 API URL, 防止 SSRF 与畸形配置。
    仅允许 http/https, 且禁止指向回环/云元数据地址。
    返回规范化后的 URL; 非法时抛出 ValueError。
    """
    from urllib.parse import urlparse
    u = urlparse(url.strip())
    if u.scheme not in ('http', 'https') or not u.hostname:
        raise ValueError("云端 API URL 必须是合法的 http/https 地址")
    host = u.hostname.lower().rstrip('.')
    if host in _SSRF_BLOCKED_HOSTS:
        raise ValueError(f"禁止配置到回环/元数据地址: {host}")
    return url.strip()


def set_cloud_config(api_url, api_key, model):
    """设置云端配置并保存 (api_url 经过 SSRF 校验)"""
    global _cloud_config
    _cloud_config['api_url'] = validate_cloud_api_url(api_url)
    # 如果传入的key是掩码格式（包含*），则保留原有key
    if api_key and '*' not in api_key:
        _cloud_config['api_key'] = api_key.strip()
    _cloud_config['model'] = model.strip()
    _save_cloud_config()
    return True


def set_language(lang):
    """设置当前响应语言"""
    global _current_lang
    _current_lang = lang or 'zh'


def get_language():
    return _current_lang


# ============ 模型管理 ============

def get_current_model():
    """获取当前使用的模型名称"""
    return _current_model


def get_available_models():
    """获取Ollama中已安装的所有模型列表 (Phase 3: 通过 LB + 自动重试)"""
    try:
        lb = _get_lb()
        if lb:
            resp_bytes, _ = lb.request('/api/tags', None, method='GET', timeout=5, max_retries=2)
            data = json.loads(resp_bytes.decode('utf-8'))
        else:
            req = urllib.request.Request(f"{OLLAMA_URL}/api/tags", method='GET')
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
        models = []
        for m in data.get('models', []):
            models.append({
                'name': m.get('name', ''),
                'size': m.get('size', 0),
                'size_gb': round(m.get('size', 0) / 1024 / 1024 / 1024, 2) if m.get('size') else 0,
                'modified': m.get('modified_at', ''),
                'is_current': m.get('name') == _current_model,
            })
        return models
    except Exception:
        logger.warning("Failed to get models", exc_info=True)
        return []


def set_model(model_name):
    """切换当前使用的模型"""
    global _current_model
    _current_model = model_name
    # 同时更新OllamaAgent实例
    if agent_manager and agent_manager.ollama_agent:
        agent_manager.ollama_agent.model = model_name
    return True


def pull_model(model_name):
    """在后台线程中下载模型 (Phase 3: LB 支持)"""
    def _pull():
        with _pull_lock:
            _pull_status[model_name] = {
                'status': 'downloading',
                'progress': 0,
                'message': 'Starting download...'
            }
        try:
            payload = {"name": model_name}
            lb = _get_lb()
            if lb:
                # Phase 3: 通过 LB 路由到任一可用节点，流式读取
                stream_iter = lb.request_stream('/api/pull', payload, method='POST')
                stream_source = stream_iter
            else:
                # 回退直连
                req = urllib.request.Request(
                    f"{OLLAMA_URL}/api/pull",
                    data=json.dumps(payload).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST'
                )
                raw_resp = urllib.request.urlopen(req, timeout=OLLAMA_REQUEST_TIMEOUT)
                def _raw_iter():
                    with raw_resp as resp:
                        for line in resp:
                            yield json.loads(line.decode('utf-8').strip())
                stream_source = _raw_iter()

            for data in stream_source:
                if not isinstance(data, dict):
                    continue
                status = data.get('status', '')
                if 'total' in data and data.get('total', 0) > 0:
                    completed = data.get('completed', 0)
                    total = data.get('total', 1)
                    progress = round(completed / total * 100, 1)
                else:
                    progress = 0

                with _pull_lock:
                    _pull_status[model_name] = {
                        'status': 'downloading' if status != 'success' else 'success',
                        'progress': progress if status != 'success' else 100,
                        'message': status
                    }

                if status == 'success':
                    with _pull_lock:
                        _pull_status[model_name] = {
                            'status': 'success',
                            'progress': 100,
                            'message': 'Download complete'
                        }
                    return
            with _pull_lock:
                _pull_status[model_name] = {
                    'status': 'success',
                    'progress': 100,
                    'message': 'Download complete'
                }
        except Exception as e:
            with _pull_lock:
                _pull_status[model_name] = {
                    'status': 'error',
                    'progress': 0,
                    'message': str(e)
                }

    thread = threading.Thread(target=_pull, daemon=True)
    thread.start()
    return True


def get_pull_status(model_name=None):
    """获取模型下载状态"""
    with _pull_lock:
        if model_name:
            return _pull_status.get(model_name, None)
        return dict(_pull_status)


def delete_model(model_name):
    """删除已安装的模型 (Phase 3: 对所有 LB 节点广播 delete)"""
    payload = {"name": model_name}
    ok_count = 0
    total_attempt = 0
    try:
        lb = _get_lb()
        if lb:
            # Phase 3: 广播到所有健康的 Ollama 节点
            nodes = [n for n in lb.get_all_nodes_status() if n.get('healthy')]
            if not nodes:
                # 无健康节点，尝试对全部节点逐个删除
                nodes = list(lb.get_all_nodes_status())
            for n in nodes:
                total_attempt += 1
                try:
                    n_req = urllib.request.Request(
                        f"{n['url'].rstrip('/')}/api/delete",
                        data=json.dumps(payload).encode('utf-8'),
                        headers={'Content-Type': 'application/json'},
                        method='DELETE'
                    )
                    with urllib.request.urlopen(n_req, timeout=10) as r:
                        if 200 <= r.status < 300:
                            ok_count += 1
                except Exception as ee:
                    logger.warning("Delete model %s on %s failed: %s",
                                   model_name, n['url'], ee, exc_info=True)
            return ok_count >= 1
        # 回退: 单节点直连
        total_attempt = 1
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/delete",
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='DELETE'
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True
    except Exception as e:
        logger.warning("Failed to delete model %s: %s (ok=%s/%s)",
                       model_name, e, ok_count, total_attempt, exc_info=True)
        return ok_count >= 1


# 推荐可下载的模型列表
RECOMMENDED_MODELS = [
    {'name': 'qwen2.5:7b', 'size_gb': 4.7, 'desc_zh': '通义千问2.5 7B（推荐，当前默认）', 'desc_en': 'Qwen2.5 7B (Recommended, current default)'},
    {'name': 'qwen2.5:3b', 'size_gb': 2.0, 'desc_zh': '通义千问2.5 3B（更小更快）', 'desc_en': 'Qwen2.5 3B (Smaller and faster)'},
    {'name': 'qwen2.5:14b', 'size_gb': 9.0, 'desc_zh': '通义千问2.5 14B（更强能力，需更多内存）', 'desc_en': 'Qwen2.5 14B (More capable, needs more RAM)'},
    {'name': 'llama3.2:3b', 'size_gb': 2.0, 'desc_zh': 'Llama 3.2 3B（Meta轻量模型）', 'desc_en': 'Llama 3.2 3B (Meta lightweight model)'},
    {'name': 'llama3.1:8b', 'size_gb': 4.9, 'desc_zh': 'Llama 3.1 8B（Meta通用模型）', 'desc_en': 'Llama 3.1 8B (Meta general model)'},
    {'name': 'phi3:3.8b', 'size_gb': 2.3, 'desc_zh': 'Phi-3 3.8B（微软轻量模型）', 'desc_en': 'Phi-3 3.8B (Microsoft lightweight model)'},
    {'name': 'gemma2:9b', 'size_gb': 5.5, 'desc_zh': 'Gemma2 9B（Google模型）', 'desc_en': 'Gemma2 9B (Google model)'},
    {'name': 'mistral:7b', 'size_gb': 4.1, 'desc_zh': 'Mistral 7B（欧洲开源模型）', 'desc_en': 'Mistral 7B (European open-source model)'},
]

# ============ 字段映射 ============
FIELD_SYNONYMS = {
    'part_number': {
        'keywords': ['part number', 'part_number', '零件号', '零件编号', 'sachnummer', '零件码', 'pn', 'p/n', '物料号', 'teilenummer', 'teil nummer'],
        'db_fields': ['Part Number', 'Part_Number', 'part_number', 'Sachnummer'],
        'description': '零件号 (Part Number / Sachnummer)'
    },
    'fav': {
        'keywords': ['fav', 'fav编号', 'fav号', 'fav nr', 'favnummer', 'fav number', 'fav_number', 'zeus', 'zeus编号', 'favs_fav', 'fav_fav', 'fav-nummer', 'fav nummer'],
        'db_fields': ['FAV Number', 'FAV Nr.', 'FAV_number', 'fav_number', 'FAV_fav'],
        'description': 'FAV编号 (FAV Number / ZEUS)'
    },
    'zeus_status': {
        'keywords': ['zeus status', 'zeus状态', 'fav status', 'fav状态', 'favstatuskurz', 'erledigt', '已解决'],
        'db_fields': ['FAVStatusKurz_fav', 'FAV Status', 'FAV_Status'],
        'description': 'ZEUS/FAV状态 (FAVStatusKurz)'
    },
    'zgs': {
        'keywords': ['zgs', 'zgs diap', '版本号', '版本状态', 'zustandsgültigkeitsschlüssel', 'version status', 'zgs版本', 'zustandsgültigkeit', 'zgs schlüssel'],
        'db_fields': ['ZGS DiaP', 'ZGS', 'ZGS_ACM', 'ZGS_KEM'],
        'description': 'ZGS版本号 (Zustandsgültigkeitsschlüssel)'
    },
    'ec': {
        'keywords': ['ec', 'ec编号', 'ec号', 'fehler', '错误号', '故障号', 'fehler nr', 'fehler编号', '缺陷号', 'ec number', 'buendelnr', 'bundle number', 'bündelnr', 'bundle nr', 'fehlercode', 'fehlernummer', 'fehler nr.'],
        'db_fields': ['Bundle Number', 'EC', 'EC Number', 'Fehler Nr.', 'Fehler_Nr', 'BuendelNr'],
        'description': 'EC编号 (EC / Bundle Number / Fehler Nr. / BuendelNr)'
    },
    'soma': {
        'keywords': ['soma', 'soma in zeus', 'soma状态', 'soma in zeus?', 'soma status'],
        'db_fields': ['SOMA in ZEUS', 'Soma in ZEUS ?', 'Soma in ZEUS?'],
        'description': 'SOMA状态 (SOMA in ZEUS)'
    },
    'sdr': {
        'keywords': ['sdr', 'sdr link', 'sdr链接', '偏差请求', 'abweichungsanfrage'],
        'db_fields': ['SDR Link', 'SDR', 'SDR_Link'],
        'description': 'SDR (Supplier Deviation Request)'
    },
    'mg': {
        'keywords': ['mg', 'mg号', 'mg编号', 'maingroup', '主组', 'hauptgruppe', 'main group'],
        'db_fields': ['MG', 'Main Group', 'Main_Group'],
        'description': 'MG主组编号 (Main Group)'
    },
    'br': {
        'keywords': ['br', 'baureihe', '系列', '车型', '车系', 'br编号', 'vehicle series'],
        'db_fields': ['Vehicle Series', 'BR', 'Baureihe', 'result.BR'],
        'description': '车型系列 (Vehicle Series / BR)'
    },
    'part_name': {
        'keywords': ['teilbenennung', '零件名', '零件名称', 'part name', 'partname', '部件名', '部件名称', '描述', 'teilbezeichnung', 'name'],
        'db_fields': ['Name', 'Part Name', 'Teilbenennung', 'result.Teilbenennung'],
        'description': '零件名称 (Name / Part Name / Teilbenennung)'
    },
    'status': {
        'keywords': ['status', '状态', 'zustand'],
        'db_fields': ['Status'],
        'description': '状态 (Status)'
    },
    'kem': {
        'keywords': ['kem', 'kem号', 'kem编号', 'kem nummer', 'kem_number'],
        'db_fields': ['KEM', 'KEM Number', 'KEM_Nummer'],
        'description': 'KEM编号 (KEM Number)'
    },
    'prio': {
        'keywords': ['prio', '优先级', 'priority', '优先度', 'fav_prio', 'priorität'],
        'db_fields': ['Priority', 'Prio', 'FAV Priority', 'FAV_Prio'],
        'description': '优先级 (Priority / Prio)'
    },
    'responsible': {
        'keywords': ['responsible', '负责人', '责任者', 'verantwortlicher', 'bnd', 'zuständig'],
        'db_fields': ['Responsible', 'BNDVerantwortlicher', 'Champion / Responsible'],
        'description': '负责人 (Responsible)'
    },
    'request_number': {
        'keywords': ['request number', '请求号', 'request编号', 'request号', '请求编号'],
        'db_fields': ['Request Number', 'Request_Number'],
        'description': '请求编号 (Request Number)'
    },
    'current_status': {
        'keywords': ['istzustand', 'ist-zustand', '现状', '当前状态', 'current status'],
        'db_fields': ['Current Status', 'Current Status Detail', 'IstZustand'],
        'description': '现状 (Current Status)'
    },
    'future_status': {
        'keywords': ['sollzustand', 'soll-zustand', '目标状态', '应达状态', '解决方案', '措施', 'future status'],
        'db_fields': ['Future Status', 'Future Status Detail', 'SollZustand'],
        'description': '目标状态 (Future Status)'
    },
}

# 对比关键词
COMPARE_KEYWORDS = [
    '对比', '比较', '区别', '差异', '不同', 'versus', 'compare', 'comparison',
    'diff', 'difference', 'between', '之间', '和...比', '跟...比', '哪个',
    'vergleichen', 'vergleich', 'gegenüberstellen', 'unterschied', 'unterschiede', 'unterschiedlich', 'zwischen', 'gegen',
]


def detect_compare_intent(query):
    """
    检测是否是对比查询
    返回: {is_compare, field, value1, value2} 或 None
    """
    query_lower = query.lower().strip()

    # 检查是否包含对比关键词
    has_compare_kw = any(kw in query_lower for kw in COMPARE_KEYWORDS)
    if not has_compare_kw:
        # 也检查"vs"模式
        if not re.search(r'\bvs\b|\bversus\b', query_lower):
            return None

    # 尝试提取两个值和字段类型
    # 模式1: "对比 A0004318001 和 A0004318002"
    # 模式2: "比较EC 0151937-001 和 0151937-002"
    # 模式3: "A0004318001 vs A0004318002"

    # 先检测字段类型
    field_key = None
    for fk, fi in FIELD_SYNONYMS.items():
        for kw in fi['keywords']:
            if kw in query_lower:
                field_key = fk
                break
        if field_key:
            break

    # 提取所有值模式
    all_values = []

    # Part Number模式 (A开头+数字)
    pn_matches = re.findall(r'(?<![A-Za-z0-9])(A\d{7,12})(?![A-Za-z0-9])', query, re.IGNORECASE)
    all_values.extend([(v, 'part_number') for v in pn_matches])

    # 数字编号 (6-8位)
    num_matches = re.findall(r'(?<![A-Za-z0-9])(\d{6,8})(?![0-9])', query)
    all_values.extend([(v, 'fav_or_fehler') for v in num_matches])

    # EC/错误号格式 (如 0151937-001)
    ec_matches = re.findall(r'(?<![A-Za-z0-9])(\d{6,}-\d{2,4})(?![0-9])', query)
    all_values.extend([(v, 'ec') for v in ec_matches])

    # VAT开头的KEM
    kem_matches = re.findall(r'(?<![A-Za-z0-9])(VAT\d+)(?![A-Za-z0-9])', query, re.IGNORECASE)
    all_values.extend([(v, 'kem') for v in kem_matches])

    # 通用标识符 (字母+数字混合，长度>3)
    general_matches = re.findall(r'(?<![A-Za-z0-9])([A-Z]\d[A-Za-z0-9\-]{3,})(?![A-Za-z0-9])', query)
    for v in general_matches:
        if v not in [x[0] for x in all_values]:
            all_values.append((v, 'part_number'))

    # 去重保持顺序
    seen = set()
    unique_values = []
    for v, t in all_values:
        if v.lower() not in seen:
            seen.add(v.lower())
            unique_values.append((v, t))

    # 去除子串：如果一个值是另一个值的子串，保留更长的（更具体的）值
    # 例如 "0151937" 是 "0151937-001" 的子串，应保留后者
    filtered = []
    for i, (vi, ti) in enumerate(unique_values):
        is_substring = False
        for j, (vj, tj) in enumerate(unique_values):
            if i != j and vi.lower() in vj.lower() and vi.lower() != vj.lower():
                is_substring = True
                break
        if not is_substring:
            filtered.append((vi, ti))
    unique_values = filtered if len(filtered) >= 2 else unique_values

    if len(unique_values) < 2:
        return None

    value1, type1 = unique_values[0]
    value2, type2 = unique_values[1]

    # 确定字段
    if field_key is None:
        # 根据值类型推断
        type_priority = {'part_number': 1, 'ec': 2, 'fav_or_fehler': 3, 'kem': 4}
        if type1 in type_priority and type2 in type_priority:
            if type_priority.get(type1, 99) <= type_priority.get(type2, 99):
                field_key = type1 if type1 != 'fav_or_fehler' else 'ec'
            else:
                field_key = type2 if type2 != 'fav_or_fehler' else 'ec'
        elif type1 != 'fav_or_fehler':
            field_key = type1
        elif type2 != 'fav_or_fehler':
            field_key = type2
        else:
            field_key = 'part_number'

    # 映射到实际数据库字段名
    field_info = FIELD_SYNONYMS.get(field_key, {})
    db_fields = field_info.get('db_fields', [])

    return {
        'is_compare': True,
        'field_key': field_key,
        'field_name': db_fields[0] if db_fields else 'Part Number',
        'value1': value1,
        'value2': value2,
        'db_fields': db_fields,
    }


def detect_complex_search_intent(query):
    """
    检测复杂条件搜索意图
    支持: "查找EC不为空且SOMA为ja的零件"
    """
    query_lower = query.lower().strip()

    conditions = []
    has_complex = False

    # 检测"不为空"/"为空"条件
    for fk, fi in FIELD_SYNONYMS.items():
        for kw in fi['keywords']:
            kw_lower = kw.lower()
            if kw_lower in query_lower:
                # 检查是否是不为空条件
                if any(neg in query_lower for neg in ['不为空', '有值', '非空', 'not null', 'not empty', 'has value', 'nicht leer', 'nicht null', 'hat wert', 'vorhanden', 'gefüllt']):
                    conditions.append({
                        'field_key': fk,
                        'field_name': fi['db_fields'][0] if fi['db_fields'] else None,
                        'operator': 'not_null',
                        'value': ''
                    })
                    has_complex = True
                    break
                # 检查是否是为空条件
                elif any(neg in query_lower for neg in ['为空', '没有', '无值', 'is null', 'is empty', 'no value', 'ist leer', 'ist null', 'kein wert', 'fehlt', 'leer']):
                    conditions.append({
                        'field_key': fk,
                        'field_name': fi['db_fields'][0] if fi['db_fields'] else None,
                        'operator': 'is_null',
                        'value': ''
                    })
                    has_complex = True
                    break
                # 检查"等于"条件
                elif '等于' in query_lower or '是' in query_lower or '=' in query or 'ist gleich' in query_lower or 'gleicht' in query_lower:
                    # 尝试提取值
                    for pattern, ptype in VALUE_PATTERNS:
                        matches = re.findall(pattern, query, re.IGNORECASE)
                        if matches:
                            conditions.append({
                                'field_key': fk,
                                'field_name': fi['db_fields'][0] if fi['db_fields'] else None,
                                'operator': 'eq',
                                'value': matches[0] if isinstance(matches[0], str) else matches[0][0]
                            })
                            has_complex = True
                            break
                    break

    if has_complex and len(conditions) > 0:
        return conditions

    return None


# 值模式匹配
VALUE_PATTERNS = [
    (r'(?<![A-Za-z0-9])(A\d{7,12})(?![A-Za-z0-9])', 'part_number'),
    (r'(?<![0-9])(\d{6,8})(?![0-9])', 'fav_or_fehler'),
    (r'(?<![A-Za-z0-9])(VAT\d+)(?![A-Za-z0-9])', 'kem'),
    (r'(?<![0-9])(\d{6,}-\d{3})(?![0-9])', 'buendel'),
    (r'(?<![A-Za-z0-9])(C\d{3,4})(?![A-Za-z0-9])', 'br'),
    (r'(?<![0-9])(\d{4}-\d{4}-\d+\.?\d*)(?![0-9])', 'request_number'),
    (r'(?<![0-9])(2026-\d{4}-\d+\.?\d*)(?![0-9])', 'sdr'),
]

OFF_TOPIC_KEYWORDS = [
    '天气', 'weather', '新闻', 'news', '股票', 'stock', '电影', 'movie',
    '游戏', 'game', '音乐', 'music', '食谱', 'recipe', '旅游', 'travel',
    '诗', 'poem', '故事', 'story', '笑话', 'joke', '翻译', 'translate',
    '编程', 'programming', 'code', '代码', '写代码', '写程序',
    '你好', 'hello', 'hi', '你是谁', 'who are you', '谢谢', 'thank',
    '再见', 'bye', '生日', 'birthday', '节日', 'holiday',
]

# ---------- 检索结果归并/提炼 ----------
# 归一化字段名: 原始行中的多种别名 -> 标准键
_CONSOLIDATED_FIELD_ALIASES = {
    'part_number': ['part_number', 'Part Number', 'Sachnummer', 'Teilenummer'],
    'part_name': ['Name', 'teilbenennung', 'Teilbenennung', 'Part Name', 'Teilbezeichnung'],
    'zgs': ['ZGS DiaP', 'ZGS', 'ZGS_ACM', 'ZGS_KEM'],
    'ec': ['BuendelNr', 'EC', 'EC Number', 'Fehler Nr.', 'Fehler_Nr', 'Bundle Number'],
    'fav': ['FAV_fav', 'FAV Number', 'FAV Nr.', 'FAV_number', 'fav_number'],
    'kem': ['KEM', 'KEM Number', 'KEM_Nummer'],
    'soma': ['SOMA in ZEUS', 'Soma in ZEUS ?', 'Soma in ZEUS?'],
    'stage': ['Build Lot Aggregate', 'Baulos_aggr', 'Build Lot', 'Stage'],
    'status': ['status', 'Status'],
    'mg': ['MG', 'Main Group'],
    'br': ['BR', 'Vehicle Series', 'Baureihe'],
    'responsible': ['bndverantwortlicher', 'Responsible'],
}
_CONSOLIDATED_LABELS = {
    'part_number': 'Part Number', 'part_name': 'Part Name', 'zgs': 'ZGS',
    'ec': 'EC/Bundle', 'fav': 'FAV', 'kem': 'KEM', 'soma': 'SOMA',
    'stage': 'Stage/Build Lot', 'status': 'Status', 'mg': 'MG',
    'br': 'BR/Series', 'responsible': 'Responsible',
}


def _split_multi(value):
    """把 'AG1_PreTO_Fuz | AG1_TO1_Fuz' / 'a,b' 之类的合并值拆成原子值集合。"""
    if value is None:
        return []
    s = str(value).strip()
    if not s or s.lower() == 'null':
        return []
    atoms = re.split(r'\s*[|,;/]\s*', s)
    return [a.strip() for a in atoms if a.strip()]


def _norm_zgs_value(value):
    """ZGS 纯数字去前导零 ('005' -> '5')，与 models.part.norm_zgs 保持一致。"""
    s = str(value).strip()
    if s.isdigit():
        return str(int(s))
    return s


def consolidate_rows(rows):
    """把同一零件号在多个阶段/文件中的多行归并为一条提炼记录。

    返回 list[dict]，每条含标准字段，值为去重后的原子值列表；
    额外带 _row_count（归并行数）。无零件号的行各自成组。
    """
    groups = {}
    order = []
    no_pn_idx = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        norm = {}
        for std_key, aliases in _CONSOLIDATED_FIELD_ALIASES.items():
            vals = []
            for alias in aliases:
                if alias in row and row[alias] not in (None, '', 'null'):
                    vals.extend(_split_multi(row[alias]))
            if std_key == 'zgs':
                vals = [_norm_zgs_value(v) for v in vals]
            # 去重保序
            seen = set()
            uniq = []
            for v in vals:
                if v not in seen:
                    seen.add(v)
                    uniq.append(v)
            if uniq:
                norm[std_key] = uniq

        pn_list = norm.get('part_number') or []
        pn = pn_list[0].strip() if pn_list else ''
        if pn:
            if pn not in groups:
                groups[pn] = {'_row_count': 0}
                order.append(('pn', pn))
            g = groups[pn]
        else:
            no_pn_idx += 1
            key = f'__nopn_{no_pn_idx}'
            groups[key] = {'_row_count': 0}
            order.append(('raw', key))
            g = groups[key]
        g['_row_count'] += 1
        for k, vals in norm.items():
            if k == 'part_number':
                continue
            merged = g.setdefault(k, [])
            seen = set(merged)
            for v in vals:
                if v not in seen:
                    seen.add(v)
                    merged.append(v)

    result = []
    for kind, key in order:
        g = groups[key]
        item = dict(g)
        if kind == 'pn':
            item['part_number'] = [key]
        result.append(item)
    # 归并行数多的排前面
    result.sort(key=lambda it: -it.get('_row_count', 1))
    return result


def format_consolidated_summary(rows, max_groups=10, lang='zh'):
    """把归并后的记录格式化为供 LLM 使用/直接展示的提炼文本。"""
    groups = consolidate_rows(rows)
    if not groups:
        return ''
    if lang == 'en':
        header = f"(consolidated into {len(groups)} part group(s) from {len(rows or [])} row(s))"
        rows_label = 'rows'
    elif lang == 'de':
        header = f"(in {len(groups)} Teilegruppe(n) zusammengefasst aus {len(rows or [])} Zeile(n))"
        rows_label = 'Zeilen'
    else:
        header = f"（{len(rows or [])} 行已按零件号归并为 {len(groups)} 个零件）"
        rows_label = '行'

    lines = [header]
    for i, g in enumerate(groups[:max_groups], 1):
        segs = [f"Part {i}"]
        for std_key in ('part_number', 'part_name', 'zgs', 'stage', 'ec',
                        'fav', 'kem', 'soma', 'status', 'mg', 'br', 'responsible'):
            vals = g.get(std_key)
            if vals:
                label = _CONSOLIDATED_LABELS.get(std_key, std_key)
                joined = ', '.join(str(v) for v in vals[:12])
                if len(vals) > 12:
                    joined += f' ...(+{len(vals) - 12})'
                segs.append(f"{label}: {joined}")
        segs.append(f"[{g.get('_row_count', 1)} {rows_label}]")
        lines.append('; '.join(segs))
    if len(groups) > max_groups:
        if lang == 'en':
            lines.append(f"... {len(groups) - max_groups} more part group(s)")
        elif lang == 'de':
            lines.append(f"... {len(groups) - max_groups} weitere Teilegruppe(n)")
        else:
            lines.append(f"... 另有 {len(groups) - max_groups} 个零件未列出")
    return '\n'.join(lines)


# 逐行维度摘要: SQL 结果列名(归一化) -> 标准字段
_ROWISE_COLUMN_MATCHERS = {
    'part_number': ('part number', 'partnumber', 'teilenummer', 'sachnummer'),
    'part_name': ('part name', 'partname', 'teilbenennung', 'teilbezeichnung', 'name'),
    'zgs': ('zgs',),
    'stage': ('build lot', 'buildlot', 'baulos', 'stage', 'phase'),
    'ec': ('bundle', 'buendel', 'fehler', 'ec number'),
    'kem': ('kem',),
    'fav': ('fav',),
    'br': ('vehicle series', 'baureihe', 'series'),
    'responsible': ('responsible', 'verantwortlicher'),
    'status': ('status', 'zustand'),
}
_ROWISE_LABELS = {
    'zh': {'part_number': '零件号', 'part_name': '零件名称', 'zgs': 'ZGS',
           'stage': '阶段/Build Lot', 'ec': 'EC/Bundle', 'kem': 'KEM', 'fav': 'FAV',
           'br': '车型系列', 'responsible': '负责人', 'status': '状态'},
    'en': {'part_number': 'Part Number', 'part_name': 'Part Name', 'zgs': 'ZGS',
           'stage': 'Stage/Build Lot', 'ec': 'EC/Bundle', 'kem': 'KEM', 'fav': 'FAV',
           'br': 'Vehicle Series', 'responsible': 'Responsible', 'status': 'Status'},
    'de': {'part_number': 'Teilenummer', 'part_name': 'Teilbenennung', 'zgs': 'ZGS',
           'stage': 'Stufe/Baulos', 'ec': 'EC/Buendel', 'kem': 'KEM', 'fav': 'FAV',
           'br': 'Baureihe', 'responsible': 'Verantwortlicher', 'status': 'Status'},
}


def _norm_col_name(col):
    return re.sub(r'[\s_.\-]+', ' ', str(col).strip().lower())


def _rowwise_std_key(col):
    n = _norm_col_name(col)
    for std_key, names in _ROWISE_COLUMN_MATCHERS.items():
        if n in names or any(nm in n for nm in names):
            return std_key
    return None


def _load_file_stage_map():
    """返回 {file_id: stage}，来源 uploaded_files（文件级阶段标签）。"""
    conn = None
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT id, stage FROM uploaded_files "
            "WHERE stage IS NOT NULL AND stage <> ''"
        ).fetchall()
        return {r['id']: r['stage'] for r in rows}
    except Exception:
        logger.warning("_load_file_stage_map failed", exc_info=True)
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# Build Lot 单元格值 -> 阶段标签的关键词映射（兜底：文件级 stage 缺失时使用）
_STAGE_TOKEN_MAP = [
    ('preto', 'pre-TO'), ('pre_to', 'pre-TO'), ('pre-to', 'pre-TO'),
    ('to1', 'TO1'), ('to2', 'TO2'), ('to3', 'TO3'),
    ('pro1', 'TO1'),
]


def _stage_from_buildlot(value):
    """从 Build Lot 单元格（如 'AG1_TO2_Fuz | AG1_TO3_Fuz'）解析阶段标签集合。"""
    stages = []
    for atom in _split_multi(value):
        low = atom.lower().replace('-', '_').replace(' ', '')
        for token, label in _STAGE_TOKEN_MAP:
            if token in low and label not in stages:
                stages.append(label)
    return stages


def backfill_row_stages(results, columns):
    """对缺阶段标签的维度查询结果做确定性补全（不依赖 LLM 是否 SELECT file_id）。

    维度类（阶段/ZGS 变化）查询若 LLM 没 JOIN uploaded_files 也没选 file_id，
    行里会出现 ZGS 有值但 stage 为空（如 TO2 文件 Build Lot 单元格缺失）。
    这里用零件号/零件名在 parts_data JOIN uploaded_files 反查每条归属文件的
    阶段：文件级 stage 优先，缺失时从 Build Lot 单元格关键词解析。补全结果
    直接写回每行的 'stage' / 'file_id' 键，供 format_rowwise_summary 使用。
    返回补全的行数。
    """
    if not results:
        return 0
    col_std = {c: _rowwise_std_key(c) for c in (columns or [])}
    # 结果行里阶段列名（若 LLM 起了别名 stage/build_lot 等）
    stage_cols = [c for c, k in col_std.items() if k == 'stage']
    pn_cols = [c for c, k in col_std.items() if k == 'part_number']
    name_cols = [c for c, k in col_std.items() if k == 'part_name']

    def _row_needs_stage(row):
        # 已有非空阶段值则无需补
        for c in stage_cols:
            if row.get(c) not in (None, '', 'null'):
                return False
        if row.get('stage') not in (None, '', 'null'):
            return False
        # 需要有 ZGS 或零件名这类维度信息才值得补
        has_dim = any(
            row.get(c) not in (None, '', 'null')
            for c, k in col_std.items() if k in ('zgs', 'part_name')
        ) or row.get('ZGS') not in (None, '', 'null')
        return has_dim

    # 收集需要补全的零件号/零件名
    keys = set()
    need_idx = []
    for i, row in enumerate(results):
        if not isinstance(row, dict) or not _row_needs_stage(row):
            continue
        pn = ''
        for c in pn_cols:
            pn = str(row.get(c) or '').strip()
            if pn:
                break
        if not pn:
            pn = str(row.get('part_number') or row.get('Part Number') or '').strip()
        nm = ''
        for c in name_cols:
            nm = str(row.get(c) or '').strip()
            if nm:
                break
        if not nm:
            nm = str(row.get('Name') or '').strip()
        if pn:
            keys.add(('pn', pn))
        elif nm:
            keys.add(('nm', nm))
        else:
            continue
        need_idx.append((i, pn, nm))

    if not need_idx:
        return 0

    # 反查：零件号 -> [(file_id, stage, buildlot)]；零件名同理
    pn_map = {}
    nm_map = {}
    conn = None
    try:
        conn = get_db()
        sql = (
            "SELECT pd.part_number AS pn, "
            "json_extract(pd.data,'$.\"Name\"') AS nm, "
            "json_extract(pd.data,'$.\"Build Lot Aggregate\"') AS bl, "
            "pd.file_id AS fid, uf.stage AS fstage "
            "FROM parts_data pd LEFT JOIN uploaded_files uf ON uf.id = pd.file_id "
            "WHERE uf.status = 'active'"
        )
        for r in conn.execute(sql).fetchall():
            pn = (r['pn'] or '').strip()
            nm = (r['nm'] or '').strip()
            bl = r['bl']
            fstage = (r['fstage'] or '').strip()
            labels = []
            if fstage:
                labels.append(fstage)
            labels.extend(_stage_from_buildlot(bl))
            # 去重保序
            seen = []
            for lab in labels:
                if lab and lab not in seen:
                    seen.append(lab)
            rec = (r['fid'], seen)
            if pn:
                pn_map.setdefault(pn, []).append(rec)
            if nm:
                nm_map.setdefault(nm, []).append(rec)
    except Exception:
        logger.warning("backfill_row_stages query failed", exc_info=True)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    filled = 0
    for i, pn, nm in need_idx:
        recs = pn_map.get(pn) if pn else None
        if not recs and nm:
            recs = nm_map.get(nm)
        if not recs:
            continue
        # 汇总该零件出现过的所有阶段标签（多阶段文件会给出多个）
        labels = []
        fid_val = None
        for fid, labs in recs:
            if fid_val is None:
                fid_val = fid
            for lab in labs:
                if lab not in labels:
                    labels.append(lab)
        if not labels:
            continue
        stage_str = ' | '.join(labels)
        row = results[i]
        # 写入阶段列：优先用已有的 stage 别名列，否则放 'stage'
        target_col = stage_cols[0] if stage_cols else 'stage'
        row[target_col] = stage_str
        if not row.get('file_id') and not row.get('_file_id'):
            row['file_id'] = fid_val
        filled += 1
    return filled


def format_rowwise_summary(rows, lang='zh', columns=None):
    """逐行保留字段对应关系的结果摘要。

    适用于"阶段/ZGS 变化"等不含零件号列的维度查询：按 PN 归并会把每行
    错拆成独立零件并丢失 阶段->ZGS 的对应关系。这里逐行展示，拆分
    ' | ' 多阶段单元格、ZGS 去前导零、跳过空值，列名映射为业务标签。
    若某行阶段为空但能拿到 file_id（如 TO2 文件单元格缺 Build Lot），
    用 uploaded_files.stage 兜底补全，避免出现无阶段标签的行。
    """
    if not rows:
        return ''
    # 确定性补全：缺阶段标签的维度行，用零件号/零件名反查文件阶段
    try:
        backfill_row_stages(rows, columns or [])
    except Exception:
        logger.warning("backfill_row_stages skipped", exc_info=True)
    labels = _ROWISE_LABELS.get(lang, _ROWISE_LABELS['en'])
    file_stage = None
    lines = []
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        segs = []
        # 该行各字段标准键 -> 原子值
        row_std = {}
        for col, val in row.items():
            if val is None or str(val).strip() in ('', 'null'):
                continue
            atoms = _split_multi(val)
            if not atoms:
                continue
            std_key = _rowwise_std_key(col)
            if std_key == 'zgs':
                atoms = [_norm_zgs_value(a) for a in atoms]
            row_std[std_key or str(col)] = atoms

        # 阶段缺失兜底：用文件级 stage 标签补全
        if 'stage' not in row_std and ('zgs' in row_std or 'part_name' in row_std):
            fid = row.get('file_id') or row.get('_file_id') or row.get('fid')
            if fid is not None:
                if file_stage is None:
                    file_stage = _load_file_stage_map()
                fb = file_stage.get(fid)
                if fb:
                    row_std['stage'] = [str(fb)]

        for key, atoms in row_std.items():
            if key in ('file_id', '_file_id', 'fid'):
                continue
            label = labels.get(key, str(key)) if key in labels else str(key)
            segs.append(f"{label}: {', '.join(str(a) for a in atoms)}")
        if segs:
            lines.append(f"{i}. " + '; '.join(segs))
    return '\n'.join(lines)


class OllamaAgent:
    """Ollama大模型智能体 (Phase 3: 通过 LB 路由 + 自动重试)"""

    def __init__(self, model=None):
        self.model = model or _current_model
        self.available = self._check_available()

    def _check_available(self):
        try:
            lb = _get_lb()
            if lb:
                resp_bytes, _ = lb.request('/api/tags', None, method='GET', timeout=3, max_retries=2)
                data = json.loads(resp_bytes.decode('utf-8'))
            else:
                req = urllib.request.Request(f"{OLLAMA_URL}/api/tags", method='GET')
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read())
            if data.get('models'):
                installed = [m['name'] for m in data['models']]
                if _current_model in installed:
                    self.model = _current_model
                else:
                    self.model = installed[0]
                return True
        except Exception:
            logger.warning("Ollama unavailable", exc_info=True)
            return False
        return False

    def _call_ollama(self, prompt, system_prompt=None):
        payload = {"model": self.model, "prompt": prompt, "stream": False, "options": {"temperature": 0.3}}
        if system_prompt:
            payload["system"] = system_prompt
        lb = _get_lb()
        if lb:
            # Phase 3: 通过 LB + 自动重试 (失败会切换节点)
            resp_bytes, _ = lb.request('/api/generate', payload, method='POST', timeout=60, max_retries=3)
            data = json.loads(resp_bytes.decode('utf-8'))
            return (data or {}).get('response', '')
        req = urllib.request.Request(f"{OLLAMA_URL}/api/generate", data=json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read()).get('response', '')

    def chat(self, messages, system_prompt=None, temperature=0.3, stage=None):
        """多轮对话接口 (messages: [{role, content}])。

        stage: 调用阶段标签, 仅双引擎管理器用于监控展示; Ollama 直连路径忽略。
        """
        payload = {"model": self.model, "stream": False,
                   "options": {"temperature": temperature}}
        msgs = list(messages or [])
        if system_prompt:
            msgs.insert(0, {"role": "system", "content": system_prompt})
        payload["messages"] = msgs
        lb = _get_lb()
        if lb:
            resp_bytes, _ = lb.request('/api/chat', payload, method='POST', timeout=90, max_retries=3)
            data = json.loads(resp_bytes.decode('utf-8'))
            return ((data or {}).get('message') or {}).get('content', '')
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}, method='POST'
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
            return (data.get('message') or {}).get('content', '')

    def chat_stream(self, messages, system_prompt=None, temperature=0.3,
                    stage=None):
        """流式多轮对话, 开启思考。逐段 yield ('thinking'|'answer', delta)。

        Ollama /api/chat stream=true 返回 NDJSON, 每行 message.thinking /
        message.content 为增量; think=true 显式开启推理 (兼容模型)。
        LB 路径复用 request_stream (仅建连阶段重试)。
        """
        payload = {"model": self.model, "stream": True, "think": True,
                   "options": {"temperature": temperature}}
        msgs = list(messages or [])
        if system_prompt:
            msgs.insert(0, {"role": "system", "content": system_prompt})
        payload["messages"] = msgs

        lb = _get_lb()
        if lb:
            line_iter = lb.request_stream('/api/chat', payload, method='POST',
                                          timeout=180, max_retries=2)
        else:
            req = urllib.request.Request(
                f"{OLLAMA_URL}/api/chat",
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'}, method='POST'
            )
            resp = urllib.request.urlopen(req, timeout=180)
            line_iter = resp

        yielded = False
        try:
            for raw_line in line_iter:
                if not raw_line:
                    continue
                line = raw_line.decode('utf-8', 'ignore').strip() \
                    if isinstance(raw_line, (bytes, bytearray)) else raw_line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except Exception:
                    continue
                msg = chunk.get('message') or {}
                thinking = msg.get('thinking')
                if thinking:
                    yielded = True
                    yield 'thinking', thinking
                content = msg.get('content')
                if content:
                    yielded = True
                    yield 'answer', content
                if chunk.get('done'):
                    break
        finally:
            # 直连路径需要关闭响应; LB 生成器内部自行关闭 resp
            if not lb:
                try:
                    resp.close()
                except Exception:
                    pass
        if not yielded:
            raise RuntimeError("Ollama 流式返回为空")

    def analyze_intent(self, user_query):
        lang = get_language()
        if lang == 'en':
            system_prompt = """You are an intent analysis assistant for a vehicle parts data query system. Determine if the user's question is related to the vehicle parts data table.
Data table includes: Part Number, EC, FAV Number, SOMA, SDR, MG, Vehicle Series (BR), Part Name, Status, KEM, Priority, Responsible, Current Status, Future Status, Request Number.
Reply in JSON: {is_table_related, search_field(part_number/fav/ec/soma/sdr/mg/br/part_name/status/kem/prio/responsible/request_number/current_status/future_status/null), search_value, question_type(search/summary/detail/list/null)}
Return only JSON."""
        elif lang == 'de':
            system_prompt = """Sie sind ein Intentionsanalyse-Assistent für ein Fahrzeugteiledaten-Abfragesystem. Stellen Sie fest, ob die Frage des Benutzers mit der Fahrzeugteiledaten-Tabelle zusammenhängt.
Die Datentabelle enthält: Part Number, EC, FAV Number, SOMA, SDR, MG, Vehicle Series (BR), Part Name, Status, KEM, Priority, Responsible, Current Status, Future Status, Request Number.
Antworten Sie im JSON-Format: {is_table_related, search_field(part_number/fav/ec/soma/sdr/mg/br/part_name/status/kem/prio/responsible/request_number/current_status/future_status/null), search_value, question_type(search/summary/detail/list/null)}
Geben Sie nur JSON zurück."""
        else:
            system_prompt = """你是一个车辆零件数据查询系统的意图分析助手。判断用户问题是否与车辆零件数据表相关。
数据表包含: Part Number(零件号), EC, FAV Number, SOMA, SDR, MG, Vehicle Series(BR), Part Name, Status, KEM, Priority, Responsible, Current Status, Future Status, Request Number。
以JSON回复: {is_table_related, search_field(part_number/fav/ec/soma/sdr/mg/br/part_name/status/kem/prio/responsible/request_number/current_status/future_status/null), search_value, question_type(search/summary/detail/list/null)}
只返回JSON。"""
        try:
            response = self._call_ollama(user_query, system_prompt)
            response = response.strip()
            if response.startswith('```'):
                response = re.sub(r'^```(?:json)?\s*', '', response)
                response = re.sub(r'\s*```$', '', response)
            return json.loads(response)
        except Exception:
            return None

    def generate_response(self, user_query, search_results, intent):
        data_summary = self._prepare_data_summary(search_results, intent)
        lang = get_language()
        if lang == 'en':
            system_prompt = ("You are a vehicle parts data query assistant. "
                             "Generate clear and professional responses in English based on the retrieved data. "
                             "If data is empty, inform the user that no results were found.")
            prompt = f"User question: {user_query}\n\nRetrieved data:\n{data_summary}\n\nPlease answer:"
        elif lang == 'de':
            system_prompt = ("Sie sind ein Assistent für Fahrzeugteiledatenabfragen. "
                             "Generieren Sie klare und professionelle Antworten auf Deutsch basierend auf den abgerufenen Daten. "
                             "Wenn keine Daten gefunden werden, teilen Sie dies mit.")
            prompt = f"Benutzerfrage: {user_query}\n\nAbgerufene Daten:\n{data_summary}\n\nBitte antworten:"
        else:
            system_prompt = "你是车辆零件数据查询助手。根据检索数据用中文生成清晰专业的回复。数据为空时告知未找到。"
            prompt = f"用户问题: {user_query}\n\n检索数据:\n{data_summary}\n\n请回答："
        try:
            return self._call_ollama(prompt, system_prompt).strip()
        except Exception:
            return RuleBasedAgent().generate_response(user_query, search_results, intent)

    def _prepare_data_summary(self, search_results, intent):
        if not search_results:
            lang = get_language()
            if lang == 'en':
                return "No matching data found"
            elif lang == 'de':
                return "Keine übereinstimmenden Daten gefunden"
            else:
                return "未找到匹配数据"
        # 同一零件号的多行（多阶段/多文件）归并提炼，避免重复罗列
        return format_consolidated_summary(search_results, max_groups=10,
                                           lang=get_language())


class RuleBasedAgent:
    """基于规则的智能体"""

    def analyze_intent(self, user_query):
        query_lower = user_query.lower().strip()

        if self._is_off_topic(query_lower):
            return {'is_table_related': False, 'search_field': None, 'search_value': None, 'question_type': None}

        search_field = self._detect_field(query_lower)
        search_value = self._extract_value(user_query, search_field)

        # 高置信度零件号 (A+7~12位数字) 优先按零件号处理，
        # 避免 "A4493000000 这个零件..." 因问句含 "ZGS" 等关键词被误判为其他字段。
        pn_match = re.search(r'(?<![A-Za-z0-9])(A\d{7,12})(?![A-Za-z0-9])', user_query)
        if pn_match:
            search_field = 'part_number'
            search_value = pn_match.group(1)

        question_type = self._detect_question_type(query_lower)

        if not search_field and not search_value:
            has_table_kw = any(kw in query_lower for fi in FIELD_SYNONYMS.values() for kw in fi['keywords'])
            if not has_table_kw:
                return {'is_table_related': False, 'search_field': None, 'search_value': None, 'question_type': None}

        return {'is_table_related': True, 'search_field': search_field,
                'search_value': search_value, 'question_type': question_type or 'search'}

    def _is_off_topic(self, q):
        return any(kw in q for kw in OFF_TOPIC_KEYWORDS)

    def _detect_field(self, q):
        for fk, fi in FIELD_SYNONYMS.items():
            for kw in fi['keywords']:
                if kw in q:
                    return fk
        return None

    def _extract_value(self, query, search_field):
        for pattern, ptype in VALUE_PATTERNS:
            matches = re.findall(pattern, query, re.IGNORECASE)
            if matches:
                value = matches[0] if isinstance(matches[0], str) else matches[0][0]
                if search_field:
                    field_map = {'part_number': ['part_number'], 'fav': ['fav_or_fehler'],
                                 'ec': ['fav_or_fehler'], 'kem': ['kem'], 'br': ['br'],
                                 'request_number': ['request_number'], 'sdr': ['sdr']}
                    if ptype in field_map.get(search_field, []):
                        return value
                else:
                    return value

        if search_field == 'mg':
            mg_match = re.search(r'(?:mg|MG)[\s:：]*(\d{1,3})', query)
            if mg_match: return mg_match.group(1)
            num_match = re.search(r'(?<![0-9])(\d{1,3})(?![0-9])', query)
            if num_match: return num_match.group(1)

        if search_field == 'soma':
            for val in ['ja', 'nein', 'yes', 'no']:
                if val in query.lower(): return val

        if search_field:
            fi = FIELD_SYNONYMS.get(search_field, {})
            for kw in sorted(fi.get('keywords', []), key=len, reverse=True):
                for p in [rf'{re.escape(kw)}[\s:：]+([A-Za-z0-9][A-Za-z0-9\-\.]*)',
                          rf'{re.escape(kw)}([A-Za-z0-9][A-Za-z0-9\-\.]*)']:
                    m = re.search(p, query, re.IGNORECASE)
                    if m:
                        val = m.group(1).strip().rstrip('.,;，。；')
                        if val and len(val) >= 1: return val

        if search_field:
            all_codes = re.findall(r'[A-Za-z]?\d[\w\-\.]*', query)
            valid = [c for c in all_codes if len(c) >= 2 and c.lower() not in ['mg', 'ec', 'fav', 'br', 'sdr']]
            if valid: return valid[0]

        return None

    def _detect_question_type(self, q):
        if any(kw in q for kw in ['统计', '汇总', '多少', '几个', 'count', 'summary', 'zählen', 'anzahl', 'übersicht', 'zusammenfassung', 'wie viele', 'gesamt']): return 'summary'
        if any(kw in q for kw in ['列表', '所有', '全部', '列出', '有哪些', 'list', 'all', 'liste', 'alle', 'auflisten', 'anzeigen', 'welche', 'welcher', 'welches']): return 'list'
        if any(kw in q for kw in ['详细', '详情', '具体', 'detail', 'details', 'detailliert', 'genau', 'einzelheiten', 'ausführlich']): return 'detail'
        return 'search'

    def generate_response(self, user_query, search_results, intent):
        lang = get_language()
        if not intent or not intent.get('is_table_related'):
            if lang == 'en':
                return ("Sorry, your question does not appear to be related to the vehicle parts data table.\n\n"
                        "I can help you with the following types of queries:\n"
                        "• **Part Number** - e.g. A0009901661\n"
                        "• **FAV Number** - e.g. 2993521\n"
                        "• **EC Number** - e.g. 0151937-001\n"
                        "• **SOMA status** - ja/nein\n"
                        "• **SDR / MG / Vehicle Series (BR) / KEM / Status / Responsible** etc.\n\n"
                        "Please try asking something like \"Find information for part number A0004318001\".")
            elif lang == 'de':
                return ("Entschuldigung, Ihre Frage scheint nicht mit der Fahrzeugteiledaten-Tabelle zusammenzuhängen.\n\n"
                        "Ich kann Ihnen bei folgenden Abfragetypen helfen:\n"
                        "• **Teilenummer** - z.B. A0009901661\n"
                        "• **FAV-Nummer** - z.B. 2993521\n"
                        "• **EC-Nummer** - z.B. 0151937-001\n"
                        "• **SOMA-Status** - ja/nein\n"
                        "• **SDR / MG / Baureihe (BR) / KEM / Status / Verantwortlicher** usw.\n\n"
                        "Versuchen Sie eine Frage wie \"Finden Sie Informationen zur Teilenummer A0004318001\".")
            else:
                return ("抱歉，您的问题似乎与车辆零件数据表无关。\n\n"
                        "我可以帮您查询以下类型的信息：\n"
                        "• **零件号 (Part Number)** - 如 A0009901661\n"
                        "• **FAV编号** - 如 2993521\n"
                        "• **EC编号** - 如 0151937-001\n"
                        "• **SOMA状态** - ja/nein\n"
                        "• **SDR / MG / 车型系列(BR) / KEM / 状态 / 负责人** 等\n\n"
                        "请尝试输入类似 \"查找零件号A0004318001的信息\" 的问题。")

        field_key = intent.get('search_field')
        search_value = intent.get('search_value')
        question_type = intent.get('question_type', 'search')
        if lang == 'en':
            field_desc = FIELD_SYNONYMS.get(field_key, {}).get('description', field_key) if field_key else 'All fields'
        elif lang == 'de':
            field_desc = FIELD_SYNONYMS.get(field_key, {}).get('description', field_key) if field_key else 'Alle Felder'
        else:
            field_desc = FIELD_SYNONYMS.get(field_key, {}).get('description', field_key) if field_key else '所有字段'

        if not search_results:
            val_desc = f" \"{search_value}\"" if search_value else ""
            if lang == 'en':
                return (f"No records found in the database for {field_desc}{val_desc}.\n\n"
                        f"Suggestions:\n• Please check if the entered number is correct\n"
                        f"• Try using fuzzy search\n• Confirm that this number exists in the database")
            elif lang == 'de':
                return (f"Keine Datensätze für {field_desc}{val_desc} in der Datenbank gefunden.\n\n"
                        f"Vorschläge:\n• Bitte prüfen Sie, ob die eingegebene Nummer korrekt ist\n"
                        f"• Versuchen Sie die Fuzzy-Suche\n• Bestätigen Sie, dass diese Nummer in der Datenbank vorhanden ist")
            else:
                return (f"未在数据库中找到与 {field_desc}{val_desc} 相关的记录。\n\n"
                        f"建议：\n• 请检查输入的编号是否正确\n• 尝试使用模糊搜索\n• 确认该编号已存在于数据库中")

        total = len(search_results)
        # 同一零件号在多阶段/文件中可能有多行，先归并提炼再展示
        groups = consolidate_rows(search_results)
        parts = []

        if question_type == 'summary':
            if lang == 'en':
                parts.append(f"📊 **Query Summary**\nSearch criteria: {field_desc}" + (f" = \"{search_value}\"" if search_value else "") + f"\nFound **{total}** matching records, consolidated into **{len(groups)}** part(s)")
            elif lang == 'de':
                parts.append(f"📊 **Abfragezusammenfassung**\nSuchkriterium: {field_desc}" + (f" = \"{search_value}\"" if search_value else "") + f"\n**{total}** übereinstimmende Datensätze, zu **{len(groups)}** Teil(en) zusammengefasst")
            else:
                parts.append(f"📊 **查询汇总**\n搜索条件：{field_desc}" + (f" = \"{search_value}\"" if search_value else "") + f"\n共找到 **{total}** 条匹配记录，按零件号归并为 **{len(groups)}** 个零件")
        elif question_type == 'list':
            if lang == 'en':
                parts.append(f"📋 **Query Results List**\nSearch criteria: {field_desc}" + (f" = \"{search_value}\"" if search_value else "") + f"\nFound **{total}** records, consolidated into **{len(groups)}** part(s):\n")
            elif lang == 'de':
                parts.append(f"📋 **Abfrageergebnis-Liste**\nSuchkriterium: {field_desc}" + (f" = \"{search_value}\"" if search_value else "") + f"\n**{total}** Datensätze, zu **{len(groups)}** Teil(en) zusammengefasst:\n")
            else:
                parts.append(f"📋 **查询结果列表**\n搜索条件：{field_desc}" + (f" = \"{search_value}\"" if search_value else "") + f"\n共找到 **{total}** 条记录，按零件号归并为 **{len(groups)}** 个零件：\n")
            for i, g in enumerate(groups[:10], 1):
                parts.append(f"{i}. {self._format_group(g)}")
            if len(groups) > 10:
                if lang == 'en':
                    parts.append(f"\n... {len(groups) - 10} more parts")
                elif lang == 'de':
                    parts.append(f"\n... {len(groups) - 10} weitere Teile")
                else:
                    parts.append(f"\n... 还有 {len(groups) - 10} 个零件")
        else:
            if lang == 'en':
                parts.append(f"🔍 **Query Results**\nSearch criteria: {field_desc}" + (f" = \"{search_value}\"" if search_value else "") + f"\nFound **{total}** records, consolidated into **{len(groups)}** part(s)\n")
            elif lang == 'de':
                parts.append(f"🔍 **Abfrageergebnisse**\nSuchkriterium: {field_desc}" + (f" = \"{search_value}\"" if search_value else "") + f"\n**{total}** Datensätze, zu **{len(groups)}** Teil(en) zusammengefasst\n")
            else:
                parts.append(f"🔍 **查询结果**\n搜索条件：{field_desc}" + (f" = \"{search_value}\"" if search_value else "") + f"\n共找到 **{total}** 条记录，按零件号归并为 **{len(groups)}** 个零件\n")
            for i, g in enumerate(groups[:5], 1):
                if lang == 'en':
                    parts.append(f"\n{'='*50}\n**Part {i}**\n{'='*50}")
                elif lang == 'de':
                    parts.append(f"\n{'='*50}\n**Teil {i}**\n{'='*50}")
                else:
                    parts.append(f"\n{'='*50}\n**零件 {i}**\n{'='*50}")
                for std_key in ('part_number', 'part_name', 'zgs', 'stage', 'ec',
                                'fav', 'kem', 'soma', 'status', 'mg', 'br', 'responsible'):
                    vals = g.get(std_key)
                    if vals:
                        label = _CONSOLIDATED_LABELS.get(std_key, std_key)
                        parts.append(f"  • **{label}**: {', '.join(str(v) for v in vals[:15])}")
                parts.append(f"  • _rows: {g.get('_row_count', 1)}_")
            if len(groups) > 5:
                if lang == 'en':
                    parts.append(f"\n*... {len(groups) - 5} more parts not shown*")
                elif lang == 'de':
                    parts.append(f"\n*... {len(groups) - 5} weitere Teile nicht angezeigt*")
                else:
                    parts.append(f"\n*... 还有 {len(groups) - 5} 个零件未显示*")

        return '\n'.join(parts)

    def _format_group(self, g):
        """归并后的单个零件 -> 一行关键信息。"""
        segs = []
        pn = g.get('part_number')
        if pn:
            segs.append(f"Part Number={pn[0]}")
        for std_key in ('part_name', 'zgs', 'stage', 'ec', 'fav', 'kem', 'soma', 'status'):
            vals = g.get(std_key)
            if vals:
                label = _CONSOLIDATED_LABELS.get(std_key, std_key)
                shown = ', '.join(str(v) for v in vals[:6])
                if len(vals) > 6:
                    shown += f' +{len(vals) - 6}'
                segs.append(f"{label}={shown}")
        segs.append(f"[{g.get('_row_count', 1)} rows]")
        return ' | '.join(segs)

    def _get_key_fields(self, row):
        parts = []
        for f in ['Part Number', 'part_number', 'EC', 'FAV Number', 'FAV_number', 'Part Name', 'part_name', 'Status']:
            v = row.get(f, '')
            if v and v != 'null' and v != '': parts.append(f"{f}={v}")
        lang = get_language()
        if lang == 'en':
            return ' | '.join(parts[:4]) if parts else 'No key info'
        elif lang == 'de':
            return ' | '.join(parts[:4]) if parts else 'Keine Schlüsselinformationen'
        else:
            return ' | '.join(parts[:4]) if parts else '无关键信息'


class CloudAgent:
    """云端大模型智能体（兼容OpenAI API格式）"""

    def __init__(self):
        self.available = bool(_cloud_config.get('api_url') and _cloud_config.get('api_key') and _cloud_config.get('model'))

    def _call_cloud(self, prompt, system_prompt=None):
        api_url = _cloud_config.get('api_url', '').rstrip('/')
        api_key = _cloud_config.get('api_key', '')
        model = _cloud_config.get('model', '')

        if not api_url or not api_key or not model:
            raise Exception("Cloud API not configured")

        url = f"{api_url}/chat/completions"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = json.dumps({
            "model": model,
            "messages": messages,
            "temperature": 0.3,
            "stream": False
        }).encode('utf-8')

        req = urllib.request.Request(
            url, data=payload,
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {api_key}'
            },
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            return data['choices'][0]['message']['content']

    def chat(self, messages, system_prompt=None, temperature=0.3, stage=None):
        """多轮对话接口 (messages: [{role, content}])。stage 仅用于接口签名兼容。"""
        api_url = _cloud_config.get('api_url', '').rstrip('/')
        api_key = _cloud_config.get('api_key', '')
        model = _cloud_config.get('model', '')
        if not api_url or not api_key or not model:
            raise Exception("Cloud API not configured")
        url = f"{api_url}/chat/completions"
        msgs = list(messages or [])
        if system_prompt:
            msgs.insert(0, {"role": "system", "content": system_prompt})
        payload = json.dumps({
            "model": model,
            "messages": msgs,
            "temperature": temperature,
            "stream": False,
        }).encode('utf-8')
        req = urllib.request.Request(
            url, data=payload,
            headers={'Content-Type': 'application/json',
                     'Authorization': f'Bearer {api_key}'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
            return data['choices'][0]['message']['content']

    def chat_stream(self, messages, system_prompt=None, temperature=0.3,
                    stage=None):
        """流式多轮对话 (OpenAI 兼容 SSE)。逐段 yield ('thinking'|'answer', delta);
        云端支持时透传 reasoning_content 作为思考增量。"""
        api_url = _cloud_config.get('api_url', '').rstrip('/')
        api_key = _cloud_config.get('api_key', '')
        model = _cloud_config.get('model', '')
        if not api_url or not api_key or not model:
            raise Exception("Cloud API not configured")
        url = f"{api_url}/chat/completions"
        msgs = list(messages or [])
        if system_prompt:
            msgs.insert(0, {"role": "system", "content": system_prompt})
        payload = json.dumps({
            "model": model,
            "messages": msgs,
            "temperature": temperature,
            "stream": True,
        }).encode('utf-8')
        req = urllib.request.Request(
            url, data=payload,
            headers={'Content-Type': 'application/json',
                     'Authorization': f'Bearer {api_key}'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            yielded = False
            for raw_line in resp:
                if not raw_line:
                    continue
                line = raw_line.decode('utf-8', 'ignore').strip()
                if not line.startswith('data:'):
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
                raise RuntimeError("云端流式返回为空")

    def analyze_intent(self, user_query):
        lang = get_language()
        if lang == 'en':
            system_prompt = """You are an intent analysis assistant for a vehicle parts data query system. Determine if the user's question is related to the vehicle parts data table.
Data table includes: Part Number, EC, FAV Number, SOMA, SDR, MG, Vehicle Series (BR), Part Name, Status, KEM, Priority, Responsible, Current Status, Future Status, Request Number.
Reply in JSON: {is_table_related, search_field(part_number/fav/ec/soma/sdr/mg/br/part_name/status/kem/prio/responsible/request_number/current_status/future_status/null), search_value, question_type(search/summary/detail/list/null)}
Return only JSON."""
        elif lang == 'de':
            system_prompt = """Sie sind ein Intentionsanalyse-Assistent für ein Fahrzeugteiledaten-Abfragesystem. Stellen Sie fest, ob die Frage des Benutzers mit der Fahrzeugteiledaten-Tabelle zusammenhängt.
Die Datentabelle enthält: Part Number, EC, FAV Number, SOMA, SDR, MG, Vehicle Series (BR), Part Name, Status, KEM, Priority, Responsible, Current Status, Future Status, Request Number.
Antworten Sie im JSON-Format: {is_table_related, search_field(part_number/fav/ec/soma/sdr/mg/br/part_name/status/kem/prio/responsible/request_number/current_status/future_status/null), search_value, question_type(search/summary/detail/list/null)}
Geben Sie nur JSON zurück."""
        else:
            system_prompt = """你是一个车辆零件数据查询系统的意图分析助手。判断用户问题是否与车辆零件数据表相关。
数据表包含: Part Number(零件号), EC, FAV Number, SOMA, SDR, MG, Vehicle Series(BR), Part Name, Status, KEM, Priority, Responsible, Current Status, Future Status, Request Number。
以JSON回复: {is_table_related, search_field(part_number/fav/ec/soma/sdr/mg/br/part_name/status/kem/prio/responsible/request_number/current_status/future_status/null), search_value, question_type(search/summary/detail/list/null)}
只返回JSON。"""
        try:
            response = self._call_cloud(user_query, system_prompt)
            response = response.strip()
            if response.startswith('```'):
                response = re.sub(r'^```(?:json)?\s*', '', response)
                response = re.sub(r'\s*```$', '', response)
            return json.loads(response)
        except Exception:
            return None

    def generate_response(self, user_query, search_results, intent):
        data_summary = self._prepare_data_summary(search_results, intent)
        lang = get_language()
        if lang == 'en':
            system_prompt = ("You are a vehicle parts data query assistant. "
                             "Generate clear and professional responses in English based on the retrieved data. "
                             "If data is empty, inform the user that no results were found.")
            prompt = f"User question: {user_query}\n\nRetrieved data:\n{data_summary}\n\nPlease answer:"
        elif lang == 'de':
            system_prompt = ("Sie sind ein Assistent für Fahrzeugteiledatenabfragen. "
                             "Generieren Sie klare und professionelle Antworten auf Deutsch basierend auf den abgerufenen Daten. "
                             "Wenn keine Daten gefunden werden, teilen Sie dies mit.")
            prompt = f"Benutzerfrage: {user_query}\n\nAbgerufene Daten:\n{data_summary}\n\nBitte antworten:"
        else:
            system_prompt = "你是车辆零件数据查询助手。根据检索数据用中文生成清晰专业的回复。数据为空时告知未找到。"
            prompt = f"用户问题: {user_query}\n\n检索数据:\n{data_summary}\n\n请回答："
        try:
            return self._call_cloud(prompt, system_prompt).strip()
        except Exception:
            return RuleBasedAgent().generate_response(user_query, search_results, intent)

    def _prepare_data_summary(self, search_results, intent):
        if not search_results:
            lang = get_language()
            if lang == 'en':
                return "No matching data found"
            elif lang == 'de':
                return "Keine übereinstimmenden Daten gefunden"
            else:
                return "未找到匹配数据"
        # 同一零件号的多行（多阶段/多文件）归并提炼，避免重复罗列
        return format_consolidated_summary(search_results, max_groups=10,
                                           lang=get_language())


class AgentManager:
    def __init__(self):
        self.ollama_agent = OllamaAgent()
        self.cloud_agent = CloudAgent()
        self.rule_agent = RuleBasedAgent()
        self.engine_agent = EngineChatAdapter()
        self.use_ollama = self.ollama_agent.available
        logger.info("Mode: %s | Backend: %s",
                    'Ollama' if self.use_ollama else 'Rule-based', _compute_backend)

    def reload_ollama(self):
        self.ollama_agent = OllamaAgent()
        self.use_ollama = self.ollama_agent.available
        return self.use_ollama

    def reload_cloud(self):
        """重新加载云端智能体"""
        self.cloud_agent = CloudAgent()
        return self.cloud_agent.available

    def get_active_agent(self):
        """获取当前活跃的AI智能体和模式名称。

        本地算力优先走双引擎管理器 (Ollama / vLLM 自动路由 + 故障转移),
        返回模式名以管理器当前 active 引擎为准 ('ollama' / 'vllm')。
        """
        if _compute_backend == 'cloud' and self.cloud_agent.available:
            return self.cloud_agent, 'cloud'
        elif _compute_backend == 'local':
            em = _get_engine_manager()
            if em is not None:
                # 双引擎: 只要有一个引擎健康即可服务
                if em.is_healthy('vllm') or em.is_healthy('ollama'):
                    # mode 返回实际承载流量的引擎名, 而非盲目用 active:
                    # active 初始等于首选 (可能尚未故障转移), 会导致前端误显示
                    active = em.active
                    if not em.is_healthy(active):
                        active = 'ollama' if em.is_healthy('ollama') else 'vllm'
                    return self.engine_agent, active
            # 引擎管理器不可用时回退旧 Ollama 直连路径
            if self.use_ollama:
                return self.ollama_agent, 'ollama'
        return self.rule_agent, 'rule'

    def switch_model(self, model_name):
        """切换本地模型"""
        set_model(model_name)
        self.ollama_agent.model = model_name
        self.use_ollama = self.ollama_agent.available
        return self.use_ollama

    def switch_engine(self, target):
        """一键无缝切换 LLM 引擎 (ollama / vllm)。返回 (success, message)。"""
        em = _get_engine_manager()
        if em is None:
            return False, "LLM 引擎管理器不可用"
        return em.switch_engine(target)

    def engine_status(self):
        """返回双引擎状态快照 (供 API/前端)。"""
        em = _get_engine_manager()
        if em is None:
            return None
        return em.status()

    def process_query(self, user_query, lang='zh', history=None, session_id=None, ip=None):
        """处理用户查询。
        优先由 LLM 进行语义理解 + NL2SQL；仅在无可用 LLM 时回退到规则模式。
        history: [{'role': 'user'|'agent', 'content': str}, ...] 用于多轮上下文。
        session_id: 前端并发会话 ID, 用于多用户 GPU 监控面板归因 (可选)。
        ip: 客户端 IP, 用于在途监控归因与按用户搜索日志 (可选)。
        """
        set_language(lang)
        history = history or []

        # 登记请求追踪上下文 (本线程内的 LLM 调用都会带上会话/查询/IP 标签)
        trace_set = False
        try:
            from llm_engine import set_request_trace
            set_request_trace(session_id=session_id, query=user_query, ip=ip)
            trace_set = True
        except Exception:
            pass
        try:
            return self._process_query_inner(user_query, history)
        finally:
            if trace_set:
                try:
                    from llm_engine import set_request_trace as _clear
                    _clear(None)
                except Exception:
                    pass

    def _process_query_inner(self, user_query, history):
        """process_query 的实际逻辑 (请求追踪上下文已由外层设置)。"""
        # 1. 对比意图保留专用 UI（前端有对比结果渲染）
        compare_intent = detect_compare_intent(user_query)
        if compare_intent:
            return self._handle_compare(user_query, compare_intent)

        # 2. 获取活跃智能体
        active_agent, mode = self.get_active_agent()

        # 3. 若有 LLM, 走 NL2SQL 主路径 (ollama / vllm / cloud)
        if mode in ('ollama', 'cloud', 'vllm'):
            try:
                result = self._nl2sql_search(active_agent, user_query, history)
                if result is not None:
                    result['mode'] = mode
                    return result
            except Exception:
                logger.warning("NL2SQL failed, fallback to rule", exc_info=True)

        # 4. 回退: 规则模式（无 LLM 或 NL2SQL 异常时使用）
        return self._rule_search(user_query)

    # ---------- SSE 流式问答 (思考过程实时透传) ----------

    def process_query_stream(self, user_query, lang='zh', history=None,
                             session_id=None, ip=None):
        """流式处理用户查询的生成器包装: 设置语言/请求追踪并保证清理。

        逐段 yield 事件 dict:
          {'type':'stage','stage':str}                 阶段提示
          {'type':'thinking','delta':str}              思考增量
          {'type':'answer','delta':str}                正文增量
          {'type':'result', ...response,search_results,is_compare,...}  最终结果
          {'type':'error','message':str}               错误
        """
        set_language(lang)
        history = history or []
        trace_set = False
        try:
            from llm_engine import set_request_trace
            set_request_trace(session_id=session_id, query=user_query, ip=ip)
            trace_set = True
        except Exception:
            pass
        try:
            yield from self._process_query_stream_inner(user_query, history)
        finally:
            if trace_set:
                try:
                    from llm_engine import set_request_trace as _clear
                    _clear(None)
                except Exception:
                    pass

    @staticmethod
    def _stream_stage_label(key):
        lang = get_language()
        labels = {
            'en': {'intent': 'Analyzing your question…',
                   'sql': 'Generating query…',
                   'execute': 'Querying the database…',
                   'answer': 'Composing the answer…'},
            'de': {'intent': 'Frage wird analysiert…',
                   'sql': 'Abfrage wird erstellt…',
                   'execute': 'Datenbank wird abgefragt…',
                   'answer': 'Antwort wird formuliert…'},
            'zh': {'intent': '正在分析问题…',
                   'sql': '正在生成查询…',
                   'execute': '正在查询数据库…',
                   'answer': '正在组织回答…'},
        }
        return labels.get(lang, labels['zh']).get(key, key)

    def _agent_chat_stream(self, active_agent, messages, system_prompt,
                           temperature, stage):
        """统一取 active_agent 的流; 不支持流式的智能体退化为一次性返回。

        返回生成器, yield (kind, delta)。
        """
        fn = getattr(active_agent, 'chat_stream', None)
        if fn is not None:
            return fn(messages, system_prompt=system_prompt,
                      temperature=temperature, stage=stage)

        def _fallback():
            try:
                content = active_agent.chat(
                    messages, system_prompt=system_prompt,
                    temperature=temperature, stage=stage)
                if content:
                    yield 'answer', content
            except Exception:
                raise
        return _fallback()

    def _process_query_stream_inner(self, user_query, history):
        """流式主逻辑生成器 (请求追踪已由外层设置)。"""
        # 1. 对比意图: 专用 UI, 无 LLM 思考, 直接出结果
        compare_intent = detect_compare_intent(user_query)
        if compare_intent:
            yield {'type': 'stage', 'stage': self._stream_stage_label('execute')}
            result = self._handle_compare(user_query, compare_intent)
            yield {'type': 'result',
                   'response': result.get('response', ''),
                   'intent': result.get('intent'),
                   'mode': result.get('mode'),
                   'search_results': None,
                   'is_table_related': True,
                   'is_compare': True,
                   'compare_result': result.get('compare_result')}
            return

        # 2. 获取活跃智能体
        active_agent, mode = self.get_active_agent()

        # 3. LLM 路径走流式 NL2SQL
        if mode in ('ollama', 'cloud', 'vllm'):
            try:
                yield from self._nl2sql_search_stream(
                    active_agent, user_query, history, mode)
                return
            except Exception:
                logger.warning("NL2SQL stream failed, fallback to rule",
                               exc_info=True)

        # 4. 规则兜底 (无 LLM 思考过程)
        result = self._rule_search(user_query)
        yield {'type': 'result',
               'response': result.get('response', ''),
               'intent': result.get('intent'),
               'mode': 'rule',
               'search_results': result.get('search_results'),
               'is_table_related': bool(
                   (result.get('intent') or {}).get('is_table_related')),
               'is_compare': False}

    def _nl2sql_search_stream(self, active_agent, user_query, history, mode):
        """NL2SQL 流式版: 思考过程实时透传, 最终 yield result 事件。"""
        schema = db_manager.get_schema_description()
        system_prompt = self._nl2sql_system_prompt(schema)

        messages = []
        for msg in history[-12:]:
            role = msg.get('role')
            if role not in ('user', 'assistant', 'agent'):
                continue
            content = (msg.get('content') or '').strip()
            if not content:
                continue
            messages.append({'role': 'assistant' if role == 'agent' else role,
                             'content': content[:1500]})
        messages.append({'role': 'user', 'content': user_query})

        # ---- 阶段 1: SQL 生成 (思考流实时透传, 正文累积为 JSON) ----
        yield {'type': 'stage', 'stage': self._stream_stage_label('sql')}
        answer_buf = []
        stream_ok = False
        try:
            stream = self._agent_chat_stream(
                active_agent, messages, system_prompt, 0.1, 'SQL 生成(流式)')
            for kind, delta in stream:
                if kind == 'thinking':
                    yield {'type': 'thinking', 'delta': delta}
                elif kind == 'answer':
                    stream_ok = True
                    answer_buf.append(delta)
        except Exception:
            # 流式连接失败且尚无正文: 退化为非流式再试一次
            if stream_ok:
                raise
            logger.warning("stream chat failed before first token, "
                           "fallback to non-stream", exc_info=True)
            raw = active_agent.chat(messages, system_prompt=system_prompt,
                                    temperature=0.1, stage='SQL 生成')
            answer_buf = [raw or '']

        raw = ''.join(answer_buf)
        plan = self._extract_json(raw)
        if not isinstance(plan, dict):
            logger.warning("NL2SQL stream JSON parse failed: %s", raw[:300])
            # 交回上层走 rule 兜底
            raise RuntimeError("NL2SQL plan parse failed")

        is_data_query = plan.get('is_data_query')
        sql = (plan.get('sql') or '').strip()
        sql_type = plan.get('sql_type') or ('aggregate'
                                            if re.search(r'\b(COUNT|SUM|AVG|MIN|MAX|GROUP\s+BY)\b',
                                                         sql, re.IGNORECASE)
                                            else 'rows')

        # 闲聊: reason 即回答, 作为正文一次性给出
        if not is_data_query or not sql:
            reason = plan.get('reason') or ''
            if not reason:
                reason = self.rule_agent.generate_response(
                    user_query, None, {'is_table_related': False})
            yield {'type': 'answer', 'delta': reason}
            yield {'type': 'result',
                   'response': reason,
                   'intent': {'is_table_related': False,
                              'question_type': 'chat',
                              'nl2sql_plan': plan},
                   'mode': mode,
                   'search_results': None,
                   'is_table_related': False,
                   'is_compare': False}
            return

        # ---- 阶段 2: 执行 SQL (失败按既有逻辑带列名提示非流式重试一次) ----
        yield {'type': 'stage', 'stage': self._stream_stage_label('execute')}
        results, columns, err = db_manager.execute_readonly_sql(sql)
        if err:
            logger.warning("SQL execution failed (stream attempt 1): %s | SQL=%s",
                           err, sql)
            retry_msgs = list(messages) + [
                {'role': 'assistant', 'content': json.dumps(plan, ensure_ascii=False)},
                {'role': 'user', 'content': (
                    f"The SQL failed with this database error: {err}\n"
                    f"Your SQL was: {sql}\n"
                    "Please fix the SQL. Remember: parts_data only has columns "
                    "id, part_number, file_id, row_number, data (JSON). Stage/Baulos "
                    "values live in json_extract(data, '$.\"Build Lot Aggregate\"'); "
                    "part name is json_extract(data, '$.\"Name\"'); EC/bundle is "
                    "json_extract(data, '$.\"Bundle Number\"'); file stage "
                    "labels are in uploaded_files.stage (JOIN on file_id). "
                    "Output the same strict JSON format again with the corrected SQL.")},
            ]
            raw2 = active_agent.chat(retry_msgs, system_prompt=system_prompt,
                                     temperature=0.0, stage='SQL 修正')
            plan2 = self._extract_json(raw2)
            sql2 = (plan2 or {}).get('sql', '').strip() if isinstance(plan2, dict) else ''
            if sql2 and sql2 != sql:
                results, columns, err = db_manager.execute_readonly_sql(sql2)
                if not err:
                    sql, plan = sql2, plan2
                    logger.info("SQL retry (stream) succeeded")
        if err:
            logger.warning("SQL execution failed after retry (stream): %s", err)
            raise RuntimeError("SQL execution failed after retry")

        # ---- 阶段 3: 生成回答 (事件生成器: 思考/正文实时下发) ----
        yield {'type': 'stage', 'stage': self._stream_stage_label('answer')}
        answer_parts = []
        for ev in self._generate_answer_stream(
                active_agent, user_query, messages, sql, sql_type,
                results, columns):
            if ev.get('type') == 'answer':
                answer_parts.append(ev.get('delta') or '')
            yield ev
        answer_text = ''.join(answer_parts)

        intent = {
            'is_table_related': True,
            'question_type': sql_type,
            'sql': sql,
            'nl2sql_plan': plan,
        }
        yield {'type': 'result',
               'response': answer_text,
               'intent': intent,
               'mode': mode,
               'search_results': results,
               'is_table_related': True,
               'is_compare': False}

    def _generate_answer_stream(self, active_agent, user_query, history_messages,
                                sql, sql_type, results, columns):
        """流式答案合成事件生成器, 逐段 yield SSE 事件:

        明细类 (rows) 沿用确定性提炼摘要 (无 LLM 思考, 一次性 answer 事件);
        聚合统计类流式调用 LLM, thinking/answer 增量实时下发;
        流式失败或为空时回退确定性答案。
        """
        lang = get_language()

        # 空结果: 确定性文案
        if not results:
            if sql_type != 'aggregate':
                if lang == 'en':
                    yield {'type': 'answer', 'delta': "No matching records found."}
                elif lang == 'de':
                    yield {'type': 'answer', 'delta': "Keine übereinstimmenden Datensätze gefunden."}
                else:
                    yield {'type': 'answer', 'delta': "未找到匹配的记录。"}
                return

        # 明细类: 确定性提炼摘要 (与非流式路径完全一致)
        if sql_type != 'aggregate':
            col_std = [_rowwise_std_key(c) for c in (columns or [])]
            if 'part_number' in col_std or any(
                    ('part_number' in row or 'Part Number' in row)
                    for row in (results or [])[:5]):
                summary = format_consolidated_summary(
                    results, max_groups=10, lang=lang)
            else:
                summary = format_rowwise_summary(results, lang=lang,
                                                 columns=columns)
            if summary:
                if lang == 'en':
                    lead = ("Here is the refined summary (duplicates merged, "
                            "multi-stage cells split):")
                elif lang == 'de':
                    lead = ("Hier ist die zusammengefasste Übersicht "
                            "(Duplikate zusammengefasst, Mehrfach-Stufen aufgeteilt):")
                else:
                    lead = "以下为提炼结果（已合并重复、拆分多阶段）："
                yield {'type': 'answer', 'delta': f"{lead}\n{summary}"}
                return

        # 聚合统计类 (或明细摘要为空): 流式 LLM 组织语言
        summary_lines = []
        for row in results[:20]:
            summary_lines.append(' | '.join(f"{k}={v}" for k, v in row.items()))
        data_summary = '\n'.join(summary_lines) if summary_lines else '(no rows)'

        answer_sys = self._answer_system_prompt(sql_type, rowwise=False)
        answer_messages = list(history_messages[:-1])
        prompt = (
            f"User question: {user_query}\n\n"
            f"Executed SQL: {sql}\n\n"
            f"Query result:\n{data_summary}\n\n"
            f"Please answer in {get_language()}."
        )
        answer_messages.append({'role': 'user', 'content': prompt})

        buf = []
        try:
            stream = self._agent_chat_stream(
                active_agent, answer_messages, answer_sys, 0.4, '答案合成(流式)')
            for kind, delta in stream:
                if kind == 'thinking':
                    yield {'type': 'thinking', 'delta': delta}
                elif kind == 'answer':
                    buf.append(delta)
                    yield {'type': 'answer', 'delta': delta}
        except Exception:
            logger.warning("stream answer generation failed", exc_info=True)

        if not ''.join(buf).strip():
            fallback = self._fallback_answer(user_query, sql_type, results)
            if fallback:
                yield {'type': 'answer', 'delta': fallback}

    def _rule_search(self, user_query):
        """规则模式兜底：基于关键词/正则的搜索。"""
        intent = self.rule_agent.analyze_intent(user_query)
        if not intent or not intent.get('is_table_related'):
            response = self.rule_agent.generate_response(user_query, None, intent)
            return {'response': response,
                    'intent': intent or {'is_table_related': False},
                    'search_results': None, 'mode': 'rule'}
        search_results = self._search_data(intent)
        response = self.rule_agent.generate_response(user_query, search_results, intent)
        return {'response': response, 'intent': intent,
                'search_results': search_results, 'mode': 'rule'}

    # ---------- NL2SQL 主流程 ----------

    def _nl2sql_system_prompt(self, schema):
        lang = get_language()
        if lang == 'en':
            return (
                "You are an expert SQL generator for a vehicle parts database (SQLite dialect).\n"
                "Given the user's question and conversation history, produce ONE read-only SQL "
                "that answers the question.\n\n"
                f"Database schema:\n{schema}\n\n"
                "Rules:\n"
                "1. Output STRICT JSON only, no markdown, no explanation.\n"
                "2. JSON keys: is_data_query (true/false), reason, sql, sql_type.\n"
                "   - is_data_query=false for greetings/small talk not touching the DB "
                "(then put a direct answer in 'reason', sql='').\n"
                "   - is_data_query=true when the answer needs DB data.\n"
                "   - sql_type: 'rows' (list records) or 'aggregate' (COUNT/SUM/GROUP BY stats).\n"
                "3. Use LIKE for fuzzy text matching; use exact match for known codes.\n"
                "4. For follow-up questions (e.g. 'only those with EC', 'how many in total'), "
                "resolve pronouns using the conversation history and generate a complete standalone SQL.\n"
                "5. For row queries, include 'LIMIT 100' if not already present.\n"
                "6. Never write INSERT/UPDATE/DELETE/DDL. Query only the tables listed in the schema.\n"
                "7. Access JSON columns with json_extract(data, '$.\"Field Name\"').\n"
                "8. parts_data has ONLY these real columns: id, part_number, file_id, row_number, "
                "data. There is NO column named 'stage' in parts_data. Stage/Baulos info lives "
                "inside the JSON data under the key 'Build Lot Aggregate' (access via "
                "json_extract(data, '$.\"Build Lot Aggregate\"'); a cell may contain several "
                "stages separated by ' | ', e.g. 'AG1_TO2_Fuz | AG1_TO3_Fuz'). The part name is "
                "the JSON key 'Name'; EC/bundle is 'Bundle Number'; KEM is 'KEM Number'. "
                "The stage label per uploaded file is in uploaded_files.stage "
                "(JOIN uploaded_files uf ON uf.id = parts_data.file_id).\n"
                "9. The same part number may appear in multiple rows (one per stage/file). "
                "For ANY detail/listing query (sql_type='rows'), the outermost SELECT MUST "
                "project the full columns `id, part_number, data` from parts_data (use "
                "aliases pd.id AS id, pd.part_number AS part_number, pd.data AS data when "
                "joining), so the application can expand all business fields. NEVER return "
                "only part_number or only json_extract columns for detail queries; dedup is "
                "handled downstream. SELECT DISTINCT/GROUP BY are only for pure aggregate "
                "stats (sql_type='aggregate'); if you must aggregate, return the numbers, "
                "not a narrow part list.\n"
                "10. For questions about stages / phase progression / ZGS version changes, "
                "you MUST JOIN uploaded_files uf ON uf.id = parts_data.file_id and COALESCE the "
                "two stage sources so the stage is never blank: e.g. "
                "COALESCE(NULLIF(json_extract(pd.data,'$.\"Build Lot Aggregate\"'),''), uf.stage) "
                "AS stage. Do the same in the WHERE/SELECT; never return a stage/ZGS row with a "
                "missing stage label."
            )
        if lang == 'de':
            return (
                "Sie sind ein SQL-Experte für eine Fahrzeugteiledatenbank (SQLite-Dialekt).\n"
                "Erzeugen Sie aus der Frage und dem Gesprächsverlauf EINE schreibgeschützte SQL-Anfrage.\n\n"
                f"Datenbankschema:\n{schema}\n\n"
                "Regeln:\n"
                "1. Geben Sie AUSSCHLIESSLICH JSON aus, kein Markdown, keine Erklärung.\n"
                "2. JSON-Schlüssel: is_data_query (true/false), reason, sql, sql_type.\n"
                "   - is_data_query=false bei Begrüßungen/Smalltalk ohne Datenbankbezug "
                "(dann direkte Antwort in 'reason', sql='').\n"
                "   - is_data_query=true, wenn die Antwort Daten aus der DB benötigt.\n"
                "   - sql_type: 'rows' (Datensätze) oder 'aggregate' (COUNT/SUM/GROUP BY-Statistik).\n"
                "3. Verwenden Sie LIKE für unscharfe Textsuche, genauen Vergleich für bekannte Codes.\n"
                "4. Bei Anschlussfragen lösen Sie Pronomen anhand des Verlaufs auf und erzeugen "
                "eine vollständige, eigenständige SQL-Anfrage.\n"
                "5. Fügen Sie bei Datensatzabfragen 'LIMIT 100' hinzu, falls nicht vorhanden.\n"
                "6. Keine Schreiboperationen. Nur die im Schema genannten Tabellen abfragen.\n"
                "7. JSON-Spalten mit json_extract(data, '$.\"Feldname\"') ansprechen.\n"
                "8. parts_data hat NUR diese echten Spalten: id, part_number, file_id, row_number, "
                "data. Es gibt KEINE Spalte 'stage' in parts_data. Phasen-/Baulos-Informationen "
                "stehen im JSON unter dem Schlüssel 'Build Lot Aggregate' "
                "(json_extract(data, '$.\"Build Lot Aggregate\"'); eine Zelle kann mehrere Phasen "
                "mit ' | ' getrennt enthalten, z.B. 'AG1_TO2_Fuz | AG1_TO3_Fuz'). Der Teilename ist "
                "der JSON-Schlüssel 'Name', EC/Bündel ist 'Bundle Number', KEM ist 'KEM Number'. "
                "Die Phasenbezeichnung pro Datei steht in uploaded_files.stage "
                "(JOIN uploaded_files uf ON uf.id = parts_data.file_id).\n"
                "9. Dieselbe Teilenummer kann in mehreren Zeilen vorkommen (pro Phase/Datei). "
                "Bei JEDE Detail-/Listenabfrage (sql_type='rows') MUSS das äußere SELECT die "
                "vollen Spalten `id, part_number, data` aus parts_data projizieren (bei "
                "JOINs als pd.id AS id, pd.part_number AS part_number, pd.data AS data), "
                "damit die Anwendung alle Geschäftsfelder aufklappen kann. Geben Sie bei "
                "Detailabfragen NIE nur part_number oder nur json_extract-Spalten zurück; "
                "Duplikate werden nachfolgend entfernt. SELECT DISTINCT/GROUP BY nur für "
                "reine Statistik (sql_type='aggregate').\n"
                "10. Bei Fragen zu Phasen / Phasenfortschritt / ZGS-Versionsänderungen MÜSSEN "
                "Sie uploaded_files uf ON uf.id = parts_data.file_id JOINEN und die beiden "
                "Phasenquellen mit COALESCE zusammenführen, damit die Phase nie leer ist: z.B. "
                "COALESCE(NULLIF(json_extract(pd.data,'$.\"Build Lot Aggregate\"'),''), uf.stage) "
                "AS stage. Geben Sie nie eine Phasen-/ZGS-Zeile ohne Phasenbezeichnung zurück."
            )
        return (
            "你是一个车辆零件数据库的 SQL 专家（SQLite 方言）。\n"
            "根据用户问题和对话历史，生成一条只读 SQL 来回答问题。\n\n"
            f"数据库结构:\n{schema}\n\n"
            "规则:\n"
            "1. 只输出严格的 JSON，不要 markdown、不要解释。\n"
            "2. JSON 字段: is_data_query (true/false), reason, sql, sql_type。\n"
            "   - is_data_query=false 表示与数据库无关的问候/闲聊（把直接回答写在 reason 里，sql=''）。\n"
            "   - is_data_query=true 表示需要查询数据库。\n"
            "   - sql_type: 'rows'（明细记录）或 'aggregate'（COUNT/SUM/GROUP BY 统计）。\n"
            "3. 文本模糊匹配用 LIKE；已知编号用精确匹配。\n"
            "4. 处理追问（如“只要有EC的”“一共多少条”）时，结合历史把指代补全成完整独立 SQL。\n"
            "5. 明细查询请加 LIMIT 100（若未指定）。\n"
            "6. 严禁 INSERT/UPDATE/DELETE/DDL。只查询 schema 中列出的表。\n"
            "7. JSON 字段访问使用 json_extract(data, '$.\"字段名\"')。\n"
            "8. parts_data 只有这些真实列：id、part_number、file_id、row_number、data。"
            "不存在名为 stage 的列！阶段/批次信息在 JSON 的 'Build Lot Aggregate' 键中"
            "（用 json_extract(data, '$.\"Build Lot Aggregate\"') 访问；一个单元格可能用 ' | ' "
            "分隔多个阶段，例如 'AG1_TO2_Fuz | AG1_TO3_Fuz'）；零件名是 JSON 键 'Name'，"
            "EC/Bundle 是 'Bundle Number'，KEM 是 'KEM Number'；每个文件的阶段标签在 "
            "uploaded_files.stage（JOIN uploaded_files uf ON uf.id = parts_data.file_id）。\n"
            "9. 同一零件号可能在多个阶段/文件中出现多行。任何明细/列表类查询"
            "（sql_type='rows'），最外层 SELECT 必须投影 parts_data 的完整三列 "
            "`id, part_number, data`（JOIN 时写成 pd.id AS id, pd.part_number AS "
            "part_number, pd.data AS data），以便系统展开全部业务字段；严禁明细查询只返回 "
            "part_number 或只返回若干 json_extract 列，去重由下游统一处理。SELECT "
            "DISTINCT/GROUP BY 仅用于纯统计（sql_type='aggregate'），统计时返回数值，"
            "不要返回窄的件号清单。\n"
            "10. 凡是询问阶段/各阶段进展/ZGS 版本变化的问题，必须 JOIN uploaded_files uf ON "
            "uf.id = parts_data.file_id，并用 COALESCE 把两个阶段来源合并，保证阶段不为空，例如："
            "COALESCE(NULLIF(json_extract(pd.data,'$.\"Build Lot Aggregate\"'),''), uf.stage) AS stage。"
            "不要返回阶段标签缺失的阶段/ZGS 行。"
        )

    def _answer_system_prompt(self, sql_type, rowwise=False):
        lang = get_language()
        if lang == 'en':
            base = ("You are a vehicle parts data assistant. Based on the user's question and the "
                    "retrieved data, write a concise, professional answer in English. ")
            if sql_type == 'aggregate':
                base += "These are aggregate/statistical results; state the numbers directly."
            elif rowwise:
                base += ("Each numbered line is one data record with its field values (ZGS / "
                         "Stage-Build Lot / Part Name / EC-Bundle / KEM etc.); multiple stages "
                         "in one cell are already split and listed together. Read each line as-is "
                         "and describe the relationship between fields (e.g. which stage(s) have "
                         "which ZGS). NEVER invent 'Part 1/Part 2' entities or stages/values that "
                         "are not present. If the same stage appears from multiple files, merge "
                         "them when describing. If empty, say so.")
            else:
                base += ("These rows have already been consolidated by part number: each line is "
                         "one part with its distinct values (ZGS / Stage / EC-Bundle / FAV / KEM / "
                         "Part Name). Do NOT list duplicate rows. Instead, give a refined summary: "
                         "state how many parts were found and describe each part's key information "
                         "across stages (e.g. which stages it appears in, ZGS changes, associated "
                         "EC/Bundle and KEM). If empty, say so.")
            return base
        if lang == 'de':
            base = ("Sie sind ein Assistent für Fahrzeugteiledaten. Verfassen Sie auf Grundlage "
                    "der Frage und der abgerufenen Daten eine knappe, professionelle Antwort auf Deutsch. ")
            if sql_type == 'aggregate':
                base += "Dies sind Aggregat-/Statistikergebnisse; nennen Sie die Zahlen direkt."
            elif rowwise:
                base += ("Jede nummerierte Zeile ist ein Datensatz mit seinen Feldwerten (ZGS / "
                         "Stufe-Baulos / Teilbenennung / EC-Buendel / KEM usw.); mehrere Stufen "
                         "in einer Zelle sind bereits aufgeteilt. Lesen Sie jede Zeile wie angegeben "
                         "und beschreiben Sie die Beziehung zwischen den Feldern (z. B. welche "
                         "Stufe(n) welchen ZGS-Wert haben). Erfinden Sie NIEMALS 'Part 1/Part 2' "
                         "oder nicht vorhandene Stufen/Werte. Falls leer, teilen Sie dies mit.")
            else:
                base += ("Diese Zeilen wurden bereits nach Teilenummer zusammengefasst: jede Zeile "
                         "ist ein Teil mit seinen unterschiedlichen Werten (ZGS / Phase / EC-Bundle / "
                         "FAV / KEM / Teilbenennung). Listen Sie KEINE doppelten Zeilen auf. "
                         "Fassen Sie stattdessen zusammen: nennen Sie die Anzahl der Teile und "
                         "beschreiben Sie je Teil die phasenübergreifenden Informationen "
                         "(verfügbare Phasen, ZGS-Änderungen, zugehörige EC/Bundle und KEM). "
                         "Falls leer, teilen Sie dies mit.")
            return base
        base = "你是车辆零件数据助手。根据用户问题和检索到的数据，用中文给出简洁、专业的回答。"
        if sql_type == 'aggregate':
            base += "这是统计结果，请直接陈述数字。"
        elif rowwise:
            base += ("下面每条编号记录都是一行真实数据，括号内给出该行各字段的值（ZGS / 阶段-Build Lot / "
                     "零件名称 / EC-Bundle / KEM 等）；一个单元格里的多个阶段已经拆开并列在一起。"
                     "请逐行如实读取，说明字段之间的对应关系（例如哪些阶段对应哪个 ZGS 值、ZGS 从几变为几），"
                     "不要编造 'Part 1/Part 2' 之类的数据里不存在的零件或阶段，也不要臆造没有的取值。"
                     "同一阶段来自多个文件时，描述时可合并。如果没有结果请如实告知。")
        else:
            base += ("下面的数据已按零件号归并：每行是一个零件，括号内列出该零件在各阶段/文件中的"
                     "不同取值（ZGS / 阶段 / EC-Bundle / FAV / KEM / 零件名称）。请勿罗列重复行，"
                     "而是做提炼总结：说明共涉及多少个零件，并逐个说明该零件出现在哪些阶段、"
                     "ZGS 如何变化、配套哪些 EC/Bundle 与 KEM 等关键信息；如果没有结果请如实告知。")
        return base

    @staticmethod
    def _extract_json(text):
        """从 LLM 输出中提取第一个 JSON 对象。"""
        if not text:
            return None
        s = text.strip()
        # 去除 ```json ... ``` 包裹
        if s.startswith('```'):
            s = re.sub(r'^```(?:json)?\s*', '', s)
            s = re.sub(r'\s*```$', '', s)
        try:
            return json.loads(s)
        except Exception:
            pass
        # 兜底：截取首个 { 到最后一个 }
        m = re.search(r'\{.*\}', s, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
        return None

    def _nl2sql_search(self, active_agent, user_query, history):
        """使用 LLM 进行 NL2SQL 检索 + 回答生成。失败/无关返回 None 或普通响应。"""
        schema = db_manager.get_schema_description()
        system_prompt = self._nl2sql_system_prompt(schema)

        # 组装上下文消息（最多保留最近 6 轮，即 12 条）
        messages = []
        for msg in history[-12:]:
            role = msg.get('role')
            if role not in ('user', 'assistant', 'agent'):
                continue
            content = (msg.get('content') or '').strip()
            if not content:
                continue
            messages.append({'role': 'assistant' if role == 'agent' else role,
                             'content': content[:1500]})
        messages.append({'role': 'user', 'content': user_query})

        raw = active_agent.chat(messages, system_prompt=system_prompt, temperature=0.1,
                                stage='SQL 生成')
        plan = self._extract_json(raw)
        if not isinstance(plan, dict):
            logger.warning("NL2SQL JSON parse failed: %s", raw[:300])
            return None

        is_data_query = plan.get('is_data_query')
        sql = (plan.get('sql') or '').strip()
        sql_type = plan.get('sql_type') or ('aggregate'
                                            if re.search(r'\b(COUNT|SUM|AVG|MIN|MAX|GROUP\s+BY)\b',
                                                         sql, re.IGNORECASE)
                                            else 'rows')

        # 与数据库无关的闲聊
        if not is_data_query or not sql:
            reason = plan.get('reason') or ''
            if not reason:
                reason = self.rule_agent.generate_response(
                    user_query, None, {'is_table_related': False})
            return {'response': reason,
                    'intent': {'is_table_related': False,
                               'question_type': 'chat',
                               'nl2sql_plan': plan},
                    'search_results': None, 'mode': None}

        # 执行只读 SQL；失败时把报错反馈给 LLM 重试一次 (常见: 幻觉不存在的列)
        results, columns, err = db_manager.execute_readonly_sql(sql)
        if err:
            logger.warning("SQL execution failed (attempt 1): %s | SQL=%s", err, sql)
            retry_msgs = list(messages) + [
                {'role': 'assistant', 'content': json.dumps(plan, ensure_ascii=False)},
                {'role': 'user', 'content': (
                    f"The SQL failed with this database error: {err}\n"
                    f"Your SQL was: {sql}\n"
                    "Please fix the SQL. Remember: parts_data only has columns "
                    "id, part_number, file_id, row_number, data (JSON). Stage/Baulos "
                    "values live in json_extract(data, '$.\"Build Lot Aggregate\"'); "
                    "part name is json_extract(data, '$.\"Name\"'); EC/bundle is "
                    "json_extract(data, '$.\"Bundle Number\"'); file stage "
                    "labels are in uploaded_files.stage (JOIN on file_id). "
                    "Output the same strict JSON format again with the corrected SQL.")},
            ]
            raw2 = active_agent.chat(retry_msgs, system_prompt=system_prompt,
                                     temperature=0.0, stage='SQL 修正')
            plan2 = self._extract_json(raw2)
            sql2 = (plan2 or {}).get('sql', '').strip() if isinstance(plan2, dict) else ''
            if sql2 and sql2 != sql:
                results, columns, err = db_manager.execute_readonly_sql(sql2)
                if not err:
                    sql, plan = sql2, plan2
                    logger.info("SQL retry succeeded")
        if err:
            logger.warning("SQL execution failed after retry: %s", err)
            # 把错误信息回传，让上层走 rule 兜底
            return None

        # 生成自然语言回答
        answer = self._generate_answer(active_agent, user_query, messages,
                                       sql, sql_type, results, columns)
        intent = {
            'is_table_related': True,
            'question_type': sql_type,
            'sql': sql,
            'nl2sql_plan': plan,
        }
        return {'response': answer, 'intent': intent,
                'search_results': results, 'mode': None}

    def _generate_answer(self, active_agent, user_query, history_messages,
                         sql, sql_type, results, columns):
        """让 LLM 基于查询结果生成自然语言回答；明细类直接返回确定性提炼摘要。

        本地小模型(3b)对表格数据改写时容易张冠李戴/臆造阶段，而
        format_consolidated_summary / format_rowwise_summary 已经完成
        去重、按零件号归并、多阶段拆分与 ZGS 归一，因此明细类查询直接
        返回该确定性文本；聚合统计/闲聊仍交给 LLM 组织语言。
        """
        lang = get_language()
        if not results:
            if sql_type != 'aggregate':
                if lang == 'en':
                    return "No matching records found."
                if lang == 'de':
                    return "Keine übereinstimmenden Datensätze gefunden."
                return "未找到匹配的记录。"

        # 明细类查询：使用确定性提炼摘要，避免 LLM 臆造
        if sql_type != 'aggregate':
            col_std = [_rowwise_std_key(c) for c in (columns or [])]
            if 'part_number' in col_std or any(
                    ('part_number' in row or 'Part Number' in row)
                    for row in (results or [])[:5]):
                summary = format_consolidated_summary(
                    results, max_groups=10, lang=lang)
            else:
                summary = format_rowwise_summary(results, lang=lang, columns=columns)
            if summary:
                if lang == 'en':
                    lead = ("Here is the refined summary (duplicates merged, "
                            "multi-stage cells split):")
                elif lang == 'de':
                    lead = ("Hier ist die zusammengefasste Übersicht "
                            "(Duplikate zusammengefasst, Mehrfach-Stufen aufgeteilt):")
                else:
                    lead = "以下为提炼结果（已合并重复、拆分多阶段）："
                return f"{lead}\n{summary}"

        # 构造结果摘要（传入 LLM）—— 聚合统计类
        data_summary_fmt = 'aggregate'
        summary_lines = []
        for row in results[:20]:
            summary_lines.append(' | '.join(f"{k}={v}" for k, v in row.items()))
        data_summary = '\n'.join(summary_lines) if summary_lines else '(no rows)'

        answer_sys = self._answer_system_prompt(sql_type, rowwise=False)
        answer_messages = list(history_messages[:-1])  # 不含当前 user query
        prompt = (
            f"User question: {user_query}\n\n"
            f"Executed SQL: {sql}\n\n"
            f"Query result:\n{data_summary}\n\n"
            f"Please answer in {get_language()}."
        )
        answer_messages.append({'role': 'user', 'content': prompt})
        try:
            ans = active_agent.chat(answer_messages,
                                    system_prompt=answer_sys, temperature=0.4,
                                    stage='答案合成').strip()
            if ans:
                return ans
        except Exception:
            logger.warning("answer generation failed", exc_info=True)

        # 回退：本地拼一个简短回答
        return self._fallback_answer(user_query, sql_type, results)

    def _fallback_answer(self, user_query, sql_type, results):
        lang = get_language()
        if not results:
            if lang == 'en':
                return "No matching records found."
            if lang == 'de':
                return "Keine übereinstimmenden Datensätze gefunden."
            return "未找到匹配的记录。"
        total = len(results)
        if sql_type == 'aggregate':
            if lang == 'en':
                return f"Statistical result ({total} row(s)): " + '; '.join(
                    ', '.join(f"{k}={v}" for k, v in r.items()) for r in results[:5])
            if lang == 'de':
                return f"Statistisches Ergebnis ({total} Zeile(n)): " + '; '.join(
                    ', '.join(f"{k}={v}" for k, v in r.items()) for r in results[:5])
            return f"统计结果（共 {total} 行）：" + '; '.join(
                ', '.join(f"{k}={v}" for k, v in r.items()) for r in results[:5])
        # 明细：按零件号归并提炼，避免重复罗列
        groups = consolidate_rows(results)
        if lang == 'en':
            head = (f"Found **{total}** matching record(s), consolidated into "
                    f"**{len(groups)}** part(s):")
        elif lang == 'de':
            head = (f"**{total}** übereinstimmende Zeile(n) gefunden, zu "
                    f"**{len(groups)}** Teil(en) zusammengefasst:")
        else:
            head = f"共找到 **{total}** 条记录，按零件号归并为 **{len(groups)}** 个零件："
        lines = [head]
        for i, g in enumerate(groups[:10], 1):
            segs = []
            pn = g.get('part_number')
            if pn:
                segs.append(f"Part Number: {pn[0]}")
            for std_key in ('part_name', 'zgs', 'stage', 'ec', 'fav', 'kem',
                            'soma', 'status'):
                vals = g.get(std_key)
                if vals:
                    label = _CONSOLIDATED_LABELS.get(std_key, std_key)
                    shown = ', '.join(str(v) for v in vals[:8])
                    if len(vals) > 8:
                        shown += f' ...(+{len(vals) - 8})'
                    segs.append(f"{label}: {shown}")
            lines.append(f"{i}. " + (' | '.join(segs) if segs else '(record)')
                         + f"  ({g.get('_row_count', 1)} rows)")
        if len(groups) > 10:
            if lang == 'en':
                lines.append(f"... {len(groups) - 10} more part(s)")
            elif lang == 'de':
                lines.append(f"... {len(groups) - 10} weitere Teil(e)")
            else:
                lines.append(f"... 另有 {len(groups) - 10} 个零件未列出")
        return '\n'.join(lines)

    def _handle_compare(self, user_query, compare_intent):
        """处理对比查询"""
        # 找到数据库中实际存在的列
        columns = db_manager.get_all_columns()
        col_names = [c['english_name'] for c in columns]

        # 在数据库中找到匹配的字段
        field_name = None
        for db_field in compare_intent['db_fields']:
            if db_field in col_names:
                field_name = db_field
                break

        if not field_name:
            # 尝试模糊匹配
            for col in col_names:
                for db_field in compare_intent['db_fields']:
                    if db_field.lower() in col.lower() or col.lower() in db_field.lower():
                        field_name = col
                        break
                if field_name:
                    break

        if not field_name and 'Part Number' in col_names:
            field_name = 'Part Number'
        elif not field_name:
            field_name = col_names[0] if col_names else 'Part Number'

        # 执行对比
        result = db_manager.compare_records(
            field_name,
            compare_intent['value1'],
            compare_intent['value2']
        )

        # 生成对比回复
        if not result.get('success'):
            response = self._generate_compare_error_response(compare_intent, result)
            return {
                'response': response,
                'intent': {'is_table_related': True, 'type': 'compare', 'compare': compare_intent},
                'search_results': None,
                'mode': 'rule',
                'compare_result': result,
                'is_compare': True
            }

        response = self._generate_compare_response(user_query, compare_intent, result)
        return {
            'response': response,
            'intent': {'is_table_related': True, 'type': 'compare', 'compare': compare_intent},
            'search_results': None,
            'mode': 'rule',
            'compare_result': result,
            'is_compare': True
        }

    def _generate_compare_response(self, user_query, compare_intent, result):
        """生成对比回复文本"""
        lang = get_language()
        field_desc = FIELD_SYNONYMS.get(compare_intent['field_key'], {}).get('description', compare_intent['field_name'])

        parts = []
        if lang == 'en':
            parts.append(f"📊 **Record Comparison Results**\n")
            parts.append(f"Comparison field: {field_desc}")
            parts.append(f"Record 1: {compare_intent['value1']}")
            parts.append(f"Record 2: {compare_intent['value2']}\n")
            parts.append(f"Total fields: {result['total_fields']}")
            parts.append(f"Same fields: {result['same_count']}")
            parts.append(f"Different fields: {result['diff_count']}\n")

            if result['differences']:
                parts.append("**Differences:**\n")
                for i, diff in enumerate(result['differences'], 1):
                    col_display = diff['field']
                    for col in db_manager.get_all_columns():
                        if col['english_name'] == diff['field']:
                            col_display = col['display_name']
                            break
                    parts.append(f"{i}. **{col_display}**")
                    parts.append(f"   Record 1: {diff['value1'] or '(empty)'}")
                    parts.append(f"   Record 2: {diff['value2'] or '(empty)'}")
            else:
                parts.append("✅ All fields are identical between the two records")
        elif lang == 'de':
            parts.append(f"📊 **Vergleichsergebnis der Datensätze**\n")
            parts.append(f"Vergleichsfeld: {field_desc}")
            parts.append(f"Datensatz 1: {compare_intent['value1']}")
            parts.append(f"Datensatz 2: {compare_intent['value2']}\n")
            parts.append(f"Gesamtfelder: {result['total_fields']}")
            parts.append(f"Gleiche Felder: {result['same_count']}")
            parts.append(f"Unterschiedliche Felder: {result['diff_count']}\n")

            if result['differences']:
                parts.append("**Unterschiede:**\n")
                for i, diff in enumerate(result['differences'], 1):
                    col_display = diff['field']
                    for col in db_manager.get_all_columns():
                        if col['english_name'] == diff['field']:
                            col_display = col['display_name']
                            break
                    parts.append(f"{i}. **{col_display}**")
                    parts.append(f"   Datensatz 1: {diff['value1'] or '(leer)'}")
                    parts.append(f"   Datensatz 2: {diff['value2'] or '(leer)'}")
            else:
                parts.append("✅ Alle Felder der beiden Datensätze sind identisch")
        else:
            parts.append(f"📊 **记录对比结果**\n")
            parts.append(f"对比字段: {field_desc}")
            parts.append(f"记录1: {compare_intent['value1']}")
            parts.append(f"记录2: {compare_intent['value2']}\n")
            parts.append(f"总字段数: {result['total_fields']}")
            parts.append(f"相同字段: {result['same_count']}")
            parts.append(f"不同字段: {result['diff_count']}\n")

            if result['differences']:
                parts.append("**差异详情:**\n")
                for i, diff in enumerate(result['differences'], 1):
                    col_display = diff['field']
                    for col in db_manager.get_all_columns():
                        if col['english_name'] == diff['field']:
                            col_display = col['display_name']
                            break
                    parts.append(f"{i}. **{col_display}**")
                    parts.append(f"   记录1: {diff['value1'] or '(空)'}")
                    parts.append(f"   记录2: {diff['value2'] or '(空)'}")
            else:
                parts.append("✅ 两条记录的所有字段完全相同")

        return '\n'.join(parts)

    def _generate_compare_error_response(self, compare_intent, result):
        """生成对比错误回复"""
        lang = get_language()
        field_desc = FIELD_SYNONYMS.get(compare_intent['field_key'], {}).get('description', compare_intent['field_name'])
        if lang == 'en':
            parts = [f"❌ **Comparison Failed**\n"]
            parts.append(f"Comparison field: {field_desc}")
            if not result.get('found1'):
                parts.append(f"Record not found: {compare_intent['value1']}")
            if not result.get('found2'):
                parts.append(f"Record not found: {compare_intent['value2']}")
            parts.append("\nSuggestion: Please check if the entered numbers are correct, or try using fuzzy search")
        elif lang == 'de':
            parts = [f"❌ **Vergleich fehlgeschlagen**\n"]
            parts.append(f"Vergleichsfeld: {field_desc}")
            if not result.get('found1'):
                parts.append(f"Datensatz nicht gefunden: {compare_intent['value1']}")
            if not result.get('found2'):
                parts.append(f"Datensatz nicht gefunden: {compare_intent['value2']}")
            parts.append("\nVorschlag: Bitte prüfen Sie, ob die eingegebenen Nummern korrekt sind, oder versuchen Sie die Fuzzy-Suche")
        else:
            parts = [f"❌ **对比失败**\n"]
            parts.append(f"对比字段: {field_desc}")
            if not result.get('found1'):
                parts.append(f"未找到记录: {compare_intent['value1']}")
            if not result.get('found2'):
                parts.append(f"未找到记录: {compare_intent['value2']}")
            parts.append("\n建议：请检查输入的编号是否正确，或尝试使用模糊搜索")
        return '\n'.join(parts)

    def _handle_complex_search(self, user_query, conditions):
        """处理复杂条件搜索"""
        # 将条件映射到数据库实际字段
        columns = db_manager.get_all_columns()
        col_names = [c['english_name'] for c in columns]

        db_conditions = []
        for cond in conditions:
            field_key = cond['field_key']
            field_info = FIELD_SYNONYMS.get(field_key, {})
            db_fields = field_info.get('db_fields', [])

            # 找到数据库中存在的匹配列
            matched_field = None
            for db_field in db_fields:
                if db_field in col_names:
                    matched_field = db_field
                    break

            if not matched_field:
                # 模糊匹配
                for col in col_names:
                    for db_field in db_fields:
                        if db_field.lower() in col.lower() or col.lower() in db_field.lower():
                            matched_field = col
                            break
                    if matched_field:
                        break

            if matched_field:
                db_conditions.append({
                    'field': matched_field,
                    'value': cond['value'],
                    'operator': cond['operator']
                })

        if not db_conditions:
            lang = get_language()
            if lang == 'en':
                response = "Sorry, unable to identify the query fields. Please try using known field names such as EC, FAV, SOMA, etc."
            elif lang == 'de':
                response = "Entschuldigung, die Abfragefelder konnten nicht identifiziert werden. Bitte verwenden Sie bekannte Feldnamen wie EC, FAV, SOMA usw."
            else:
                response = "抱歉，无法识别查询条件中的字段。请尝试使用已知字段名称，如 EC、FAV、SOMA 等。"
            return {
                'response': response,
                'intent': {'is_table_related': True, 'type': 'complex_search'},
                'search_results': None,
                'mode': 'rule'
            }

        results = db_manager.search_complex(db_conditions)

        # 生成回复
        lang = get_language()
        parts = []
        cond_descs = []
        for cond in db_conditions:
            field_info = FIELD_SYNONYMS.get(next((k for k, v in FIELD_SYNONYMS.items()
                                                   if any(df in cond['field'] for df in v.get('db_fields', []))), ''), {})
            field_desc = field_info.get('description', cond['field'])
            if lang == 'en':
                if cond['operator'] == 'not_null':
                    cond_descs.append(f"{field_desc} is not empty")
                elif cond['operator'] == 'is_null':
                    cond_descs.append(f"{field_desc} is empty")
                elif cond['operator'] == 'eq':
                    cond_descs.append(f"{field_desc} = \"{cond['value']}\"")
                else:
                    cond_descs.append(f"{field_desc} contains \"{cond['value']}\"")
            elif lang == 'de':
                if cond['operator'] == 'not_null':
                    cond_descs.append(f"{field_desc} ist nicht leer")
                elif cond['operator'] == 'is_null':
                    cond_descs.append(f"{field_desc} ist leer")
                elif cond['operator'] == 'eq':
                    cond_descs.append(f"{field_desc} = \"{cond['value']}\"")
                else:
                    cond_descs.append(f"{field_desc} enthält \"{cond['value']}\"")
            else:
                if cond['operator'] == 'not_null':
                    cond_descs.append(f"{field_desc}不为空")
                elif cond['operator'] == 'is_null':
                    cond_descs.append(f"{field_desc}为空")
                elif cond['operator'] == 'eq':
                    cond_descs.append(f"{field_desc} = \"{cond['value']}\"")
                else:
                    cond_descs.append(f"{field_desc} 包含 \"{cond['value']}\"")

        if lang == 'en':
            parts.append(f"🔍 **Complex Condition Search**\n")
            parts.append(f"Conditions: {' AND '.join(cond_descs)}")
            parts.append(f"Found **{len(results)}** matching records\n")
        elif lang == 'de':
            parts.append(f"🔍 **Komplexe Bedingungssuche**\n")
            parts.append(f"Bedingungen: {' UND '.join(cond_descs)}")
            parts.append(f"**{len(results)}** übereinstimmende Datensätze gefunden\n")
        else:
            parts.append(f"🔍 **复杂条件搜索**\n")
            parts.append(f"条件: {' 且 '.join(cond_descs)}")
            parts.append(f"共找到 **{len(results)}** 条匹配记录\n")

        if results:
            for i, row in enumerate(results[:5]):
                key_parts = []
                for f in ['Part Number', 'EC', 'FAV Number', 'Part Name']:
                    v = row.get(f, '')
                    if v and v != 'null' and v != '':
                        key_parts.append(f"{f}={v}")
                if lang == 'en':
                    parts.append(f"{i+1}. {' | '.join(key_parts[:3]) if key_parts else 'No key info'}")
                elif lang == 'de':
                    parts.append(f"{i+1}. {' | '.join(key_parts[:3]) if key_parts else 'Keine Schlüsselinformationen'}")
                else:
                    parts.append(f"{i+1}. {' | '.join(key_parts[:3]) if key_parts else '无关键信息'}")
            if len(results) > 5:
                if lang == 'en':
                    parts.append(f"\n... {len(results) - 5} more records")
                elif lang == 'de':
                    parts.append(f"\n... {len(results) - 5} weitere Datensätze")
                else:
                    parts.append(f"\n... 还有 {len(results) - 5} 条记录")

        return {
            'response': '\n'.join(parts),
            'intent': {'is_table_related': True, 'type': 'complex_search'},
            'search_results': results,
            'mode': 'rule'
        }

    def _search_data(self, intent):
        """通过数据库搜索"""
        field_key = intent.get('search_field')
        search_value = intent.get('search_value')
        if not search_value:
            return []

        # 获取数据库中的实际列名
        columns = db_manager.get_all_columns()
        col_names = [c['english_name'] for c in columns]

        # 如果有指定字段，尝试匹配数据库列
        if field_key and field_key in FIELD_SYNONYMS:
            db_fields = FIELD_SYNONYMS[field_key].get('db_fields', [])
            # 找到数据库中存在的匹配列
            matched_fields = [f for f in db_fields if f in col_names]

            if matched_fields:
                # 使用匹配的列搜索
                results = []
                for field in matched_fields:
                    results.extend(db_manager.search_by_field(field, search_value))
                # 去重（按record_id）
                seen = set()
                unique = []
                for r in results:
                    rid = r.get('_record_id')
                    if rid not in seen:
                        seen.add(rid)
                        unique.append(r)
                return unique

            # 如果指定字段的列不存在，尝试用Part Number搜索
            if field_key != 'part_number':
                pn_fields = [f for f in FIELD_SYNONYMS['part_number']['db_fields'] if f in col_names]
                if pn_fields:
                    return db_manager.search_by_field(pn_fields[0], search_value)

        # 没有指定字段，尝试Part Number搜索
        pn_fields = [f for f in FIELD_SYNONYMS['part_number']['db_fields'] if f in col_names]
        if pn_fields:
            return db_manager.search_by_field(pn_fields[0], search_value)

        # 最后尝试所有列
        for col in col_names:
            results = db_manager.search_by_field(col, search_value)
            if results:
                return results

        return []


agent_manager = AgentManager()
