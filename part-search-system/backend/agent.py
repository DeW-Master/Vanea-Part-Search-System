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

from database import db_manager
from config import OLLAMA_URL, OLLAMA_MODEL, OLLAMA_REQUEST_TIMEOUT

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
    except Exception as e:
        print(f"[Agent] Ollama LB 不可用，回退直连 {OLLAMA_URL}: {e}")
        _lb_cache[0] = False
        return None

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
    except Exception as e:
        print(f"[Agent] Failed to load cloud config: {e}")


def _save_cloud_config():
    """保存云端配置到文件"""
    try:
        os.makedirs(os.path.dirname(CLOUD_CONFIG_PATH), exist_ok=True)
        with open(CLOUD_CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(_cloud_config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Agent] Failed to save cloud config: {e}")


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
    except Exception as e:
        print(f"[Agent] Failed to get models: {e}")
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
                    print(f"[Agent] Delete model {model_name} on {n['url']} failed: {ee}")
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
        print(f"[Agent] Failed to delete model {model_name}: {e} (ok={ok_count}/{total_attempt})")
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
        'db_fields': ['EC', 'EC Number', 'Fehler Nr.', 'Fehler_Nr', 'BuendelNr'],
        'description': 'EC编号 (EC / Fehler Nr. / BuendelNr)'
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
        'keywords': ['teilbenennung', '零件名', '零件名称', 'part name', 'partname', '部件名', '部件名称', '描述', 'teilbezeichnung'],
        'db_fields': ['Part Name', 'Teilbenennung', 'result.Teilbenennung'],
        'description': '零件名称 (Part Name / Teilbenennung)'
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
        except Exception as e:
            print(f"[Agent] Ollama unavailable: {e}")
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
        lang = get_language()
        if lang == 'en':
            record_label = "Record"
        elif lang == 'de':
            record_label = "Datensatz"
        else:
            record_label = "记录"
        parts = []
        for i, row in enumerate(search_results[:5]):
            parts.append(f"\n{record_label} {i+1}:")
            for k, v in row.items():
                if not k.startswith('_') and v and v != 'null' and v != '':
                    parts.append(f"  {k}: {str(v)[:200]}")
        return '\n'.join(parts)


class RuleBasedAgent:
    """基于规则的智能体"""

    def analyze_intent(self, user_query):
        query_lower = user_query.lower().strip()

        if self._is_off_topic(query_lower):
            return {'is_table_related': False, 'search_field': None, 'search_value': None, 'question_type': None}

        search_field = self._detect_field(query_lower)
        search_value = self._extract_value(user_query, search_field)
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
        parts = []

        if question_type == 'summary':
            if lang == 'en':
                parts.append(f"📊 **Query Summary**\nSearch criteria: {field_desc}" + (f" = \"{search_value}\"" if search_value else "") + f"\nFound **{total}** matching records")
            elif lang == 'de':
                parts.append(f"📊 **Abfragezusammenfassung**\nSuchkriterium: {field_desc}" + (f" = \"{search_value}\"" if search_value else "") + f"\n**{total}** übereinstimmende Datensätze gefunden")
            else:
                parts.append(f"📊 **查询汇总**\n搜索条件：{field_desc}" + (f" = \"{search_value}\"" if search_value else "") + f"\n共找到 **{total}** 条匹配记录")
        elif question_type == 'list':
            if lang == 'en':
                parts.append(f"📋 **Query Results List**\nSearch criteria: {field_desc}" + (f" = \"{search_value}\"" if search_value else "") + f"\nFound **{total}** matching records:\n")
            elif lang == 'de':
                parts.append(f"📋 **Abfrageergebnis-Liste**\nSuchkriterium: {field_desc}" + (f" = \"{search_value}\"" if search_value else "") + f"\n**{total}** übereinstimmende Datensätze gefunden:\n")
            else:
                parts.append(f"📋 **查询结果列表**\n搜索条件：{field_desc}" + (f" = \"{search_value}\"" if search_value else "") + f"\n共找到 **{total}** 条匹配记录：\n")
            for i, row in enumerate(search_results[:10]):
                key = self._get_key_fields(row)
                src = row.get('_source_file', '')
                parts.append(f"{i+1}. {key}" + (f"  [{src}]" if src else ""))
            if total > 10:
                if lang == 'en':
                    parts.append(f"\n... {total - 10} more records")
                elif lang == 'de':
                    parts.append(f"\n... {total - 10} weitere Datensätze")
                else:
                    parts.append(f"\n... 还有 {total - 10} 条记录")
        else:
            if lang == 'en':
                parts.append(f"🔍 **Query Results**\nSearch criteria: {field_desc}" + (f" = \"{search_value}\"" if search_value else "") + f"\nFound **{total}** matching records\n")
            elif lang == 'de':
                parts.append(f"🔍 **Abfrageergebnisse**\nSuchkriterium: {field_desc}" + (f" = \"{search_value}\"" if search_value else "") + f"\n**{total}** übereinstimmende Datensätze gefunden\n")
            else:
                parts.append(f"🔍 **查询结果**\n搜索条件：{field_desc}" + (f" = \"{search_value}\"" if search_value else "") + f"\n共找到 **{total}** 条匹配记录\n")
            for i, row in enumerate(search_results[:3]):
                src = row.get('_source_file', '')
                sheet = row.get('_source_sheet', '')
                if lang == 'en':
                    parts.append(f"\n{'='*50}\n**Record {i+1}**" + (f" (Source: {src}/{sheet})" if src else "") + f"\n{'='*50}\n")
                elif lang == 'de':
                    parts.append(f"\n{'='*50}\n**Datensatz {i+1}**" + (f" (Quelle: {src}/{sheet})" if src else "") + f"\n{'='*50}\n")
                else:
                    parts.append(f"\n{'='*50}\n**记录 {i+1}**" + (f" (来源: {src}/{sheet})" if src else "") + f"\n{'='*50}\n")
                for k, v in row.items():
                    if k.startswith('_'): continue
                    if v and v != 'null' and v != '':
                        parts.append(f"  • **{k}**: {str(v)[:300]}")
            if total > 3:
                if lang == 'en':
                    parts.append(f"\n*... {total - 3} more records not shown*")
                elif lang == 'de':
                    parts.append(f"\n*... {total - 3} weitere Datensätze nicht angezeigt*")
                else:
                    parts.append(f"\n*... 还有 {total - 3} 条记录未显示*")

        return '\n'.join(parts)

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
        lang = get_language()
        if lang == 'en':
            record_label = "Record"
        elif lang == 'de':
            record_label = "Datensatz"
        else:
            record_label = "记录"
        parts = []
        for i, row in enumerate(search_results[:5]):
            parts.append(f"\n{record_label} {i+1}:")
            for k, v in row.items():
                if not k.startswith('_') and v and v != 'null' and v != '':
                    parts.append(f"  {k}: {str(v)[:200]}")
        return '\n'.join(parts)


class AgentManager:
    def __init__(self):
        self.ollama_agent = OllamaAgent()
        self.cloud_agent = CloudAgent()
        self.rule_agent = RuleBasedAgent()
        self.use_ollama = self.ollama_agent.available
        print(f"[Agent] Mode: {'Ollama' if self.use_ollama else 'Rule-based'} | Backend: {_compute_backend}")

    def reload_ollama(self):
        self.ollama_agent = OllamaAgent()
        self.use_ollama = self.ollama_agent.available
        return self.use_ollama

    def reload_cloud(self):
        """重新加载云端智能体"""
        self.cloud_agent = CloudAgent()
        return self.cloud_agent.available

    def get_active_agent(self):
        """获取当前活跃的AI智能体和模式名称"""
        if _compute_backend == 'cloud' and self.cloud_agent.available:
            return self.cloud_agent, 'cloud'
        elif _compute_backend == 'local' and self.use_ollama:
            return self.ollama_agent, 'ollama'
        else:
            return self.rule_agent, 'rule'

    def switch_model(self, model_name):
        """切换本地模型"""
        set_model(model_name)
        self.ollama_agent.model = model_name
        self.use_ollama = self.ollama_agent.available
        return self.use_ollama

    def process_query(self, user_query, lang='zh'):
        # 设置当前语言
        set_language(lang)

        # 1. 优先检测对比意图
        compare_intent = detect_compare_intent(user_query)
        if compare_intent:
            return self._handle_compare(user_query, compare_intent)

        # 2. 检测复杂条件搜索
        complex_conditions = detect_complex_search_intent(user_query)
        if complex_conditions:
            return self._handle_complex_search(user_query, complex_conditions)

        # 3. 获取当前活跃智能体
        active_agent, mode = self.get_active_agent()

        # 4. 常规意图分析
        if mode in ('ollama', 'cloud'):
            intent = active_agent.analyze_intent(user_query)
            if intent is None:
                intent = self.rule_agent.analyze_intent(user_query)
                mode = 'rule'
        else:
            intent = self.rule_agent.analyze_intent(user_query)

        if not intent or not intent.get('is_table_related'):
            response = self.rule_agent.generate_response(user_query, None, intent)
            return {'response': response, 'intent': intent or {'is_table_related': False},
                    'search_results': None, 'mode': mode}

        search_results = self._search_data(intent)

        if mode in ('ollama', 'cloud'):
            response = active_agent.generate_response(user_query, search_results, intent)
        else:
            response = self.rule_agent.generate_response(user_query, search_results, intent)

        return {'response': response, 'intent': intent, 'search_results': search_results,
                'mode': mode}

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
