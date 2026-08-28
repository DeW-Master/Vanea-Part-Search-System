# -*- coding: utf-8 -*-
"""
van.ea 车辆零件智能查询系统 - 配置文件
版本: build20260817 (Phase 3)
更新日期: 2026-08-17
"""

import os

# ============ 基础路径 ============
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
DATA_DIR = os.path.join(PROJECT_ROOT, 'data')

# ============ 版本信息 ============
APP_VERSION = "build20260817"
APP_NAME = "van.ea 车辆零件智能查询"
APP_CODENAME = "van.ea"

VERSION_HISTORY = [
    {
        "version": "build20260817",
        "date": "2026-08-17",
        "features": [
            "✨ 移除页面版本号显示，改用 build+日期 命名规范",
            "✨ Phase 3 架构升级: Web 应用多副本部署 (Docker Swarm / k8s)",
            "✨ Ollama 多模型负载均衡 (轮询 + 健康检查 + 自动故障转移)",
            "✨ Prometheus + Grafana 监控集成 (metrics 端点 + 预配置仪表盘)",
            "✨ SQLite → PostgreSQL 迁移支持 (抽象数据库层 + 迁移脚本)",
            "🔒 Redis Session 存储 (多副本部署必需，解决会话不一致问题)",
            "📊 /metrics 端点: 请求数/延迟/Delta耗时/Redis命中率等 20+ 指标",
            "⚖️ Nginx 上游负载均衡: least_conn + 主动健康检查",
            "🔄 Ollama 集群: 多实例轮询 + 故障节点自动摘除",
            "🐘 PostgreSQL 支持: 可切换 DB_TYPE=postgresql 应对大数据量",
        ]
    },
    {
        "version": "1.4.0",
        "date": "2026-08-17",
        "features": [
            "新增 Nginx 反向代理（静态文件加速、gzip压缩、SSE支持）",
            "新增 Redis 缓存层（搜索结果、Delta数据、统计数据缓存）",
            "新增 Delta 预计算 + 定时刷新（后台线程，毫秒级响应）",
            "新增 /api/health 健康检查接口（应用/数据库/Redis状态）",
            "新增 /api/admin/cache/clear 缓存清理接口（管理员）",
            "Redis 不可用时自动降级为无缓存模式（向后兼容）",
            "数据写入后自动失效相关缓存",
        ]
    },
    {
        "version": "1.3.0",
        "date": "2026-08-14",
        "features": [
            "新增并发搜索控制（默认上限3人，支持SSE实时通知）",
            "新增系统监控页面（GPU/内存/CPU/磁盘/请求速率实时图表）",
            "后台数据库统计新增饼图（记录数贡献、列数贡献）",
            "后台新增文件详情列表（文件大小、上传时间、占比）",
            "Delta详情改为两阶段并排对比，差异字段高亮显示",
            "替换左上角Logo为FBAC R&D品牌标识",
            "修复Delta下钻查看详情点击无反应问题",
            "删除Delta中KEM/SOMA/ZEUS变化类型，PN淘汰改为PN停用",
            "智能体建议问题动态更新，每次刷新随机变化",
        ]
    },
    {
        "version": "1.2.0",
        "date": "2026-08-13",
        "features": [
            "Delta匹配逻辑重构为PN+ZGS组合对比",
            "合并文件上传和文件管理页面",
            "区分BOM上传和补充文件上传",
            "Delta页面增加数据下钻查看详情功能",
            "版本号自动递增机制",
            "GPU效率优化（Flash Attention/KV Cache量化）",
            "前端页面奔驰风格重新设计（黑银配色）",
        ]
    },
    {
        "version": "1.1.0",
        "date": "2026-08-12",
        "features": [
            "阶段Delta显示功能",
            "智能体问答页面",
            "模型选择和自动下载功能",
            "云端/本地算力后端切换",
            "Docker部署支持",
            "操作指南文档",
        ]
    },
    {
        "version": "1.0.0",
        "date": "2026-08-10",
        "features": [
            "动态数据库管理（多Excel合并）",
            "零件号搜索功能",
            "密码认证管理后台",
            "双语界面（中文/英文）",
            "基础文件上传管理",
        ]
    },
]

# ============ 管理员配置 ============
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin2026")

# ============ 数据库配置 ============
# DB_TYPE: sqlite (默认) | postgresql (已接线, database.py 双引擎支持)
DB_TYPE = os.environ.get("DB_TYPE", "sqlite").lower()
if DB_TYPE not in ("sqlite", "postgresql"):
    raise ValueError("DB_TYPE 必须是 sqlite 或 postgresql")

# SQLite 配置
DB_PATH = os.environ.get("PARTS_DB_PATH", os.path.join(DATA_DIR, 'parts.db'))

# PostgreSQL 配置 (DB_TYPE=postgresql 时使用)
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "postgres")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
POSTGRES_USER = os.environ.get("POSTGRES_USER", "van_ea")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "van_ea_2026")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "van_ea_parts")
POSTGRES_SSLMODE = os.environ.get("POSTGRES_SSLMODE", "prefer")

UPLOAD_TEMP_DIR = os.environ.get("PARTS_UPLOAD_DIR", os.path.join(DATA_DIR, 'uploads'))
os.makedirs(UPLOAD_TEMP_DIR, exist_ok=True)

# ============ Ollama 配置 (Phase 3: 多模型负载均衡) ============
# 单 Ollama 地址 (兼容旧版)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")

# Phase 3: 多 Ollama 实例集群 (逗号分隔)
# 示例: OLLAMA_URLS=http://ollama-1:11434,http://ollama-2:11434,http://ollama-3:11434
_ollama_urls_raw = os.environ.get("OLLAMA_URLS", "")
if _ollama_urls_raw:
    OLLAMA_URLS = [u.strip() for u in _ollama_urls_raw.split(",") if u.strip()]
else:
    OLLAMA_URLS = [OLLAMA_URL]

# 负载均衡策略: round_robin (轮询) | least_conn (最少连接) | random (随机)
OLLAMA_LB_STRATEGY = os.environ.get("OLLAMA_LB_STRATEGY", "round_robin").lower()
if OLLAMA_LB_STRATEGY not in ("round_robin", "least_conn", "random"):
    raise ValueError("非法负载均衡策略: " + OLLAMA_LB_STRATEGY)

# Ollama 健康检查间隔 (秒)
OLLAMA_HEALTHCHECK_INTERVAL = int(os.environ.get("OLLAMA_HEALTHCHECK_INTERVAL", "15"))
# Ollama 请求超时 (秒)
OLLAMA_REQUEST_TIMEOUT = int(os.environ.get("OLLAMA_REQUEST_TIMEOUT", "120"))
# 失败多少次后摘除节点
OLLAMA_FAILURE_THRESHOLD = int(os.environ.get("OLLAMA_FAILURE_THRESHOLD", "3"))
# 恢复后多久重新加入
OLLAMA_RECOVERY_BACKOFF = int(os.environ.get("OLLAMA_RECOVERY_BACKOFF", "60"))

# ============ vLLM 配置 (双引擎: Ollama / vLLM) ============
# vLLM 提供 OpenAI 兼容 API, 跑在 WSL2 内 (默认 8000 端口)
VLLM_URL = os.environ.get("VLLM_URL", "http://localhost:8000/v1")
VLLM_MODEL = os.environ.get("VLLM_MODEL", "qwen3-8b")
VLLM_API_KEY = os.environ.get("VLLM_API_KEY", "dummy")
# vLLM 健康检查端点 (根路径, 非 /v1)
VLLM_HEALTH_URL = os.environ.get("VLLM_HEALTH_URL", "http://localhost:8000/health")
VLLM_REQUEST_TIMEOUT = int(os.environ.get("VLLM_REQUEST_TIMEOUT", "120"))
# 引擎偏好: ollama | vllm (运行时可切换, 持久化到 DATA_DIR/llm_engine.json)
LLM_ENGINE_PREFERRED = os.environ.get("LLM_ENGINE", "ollama").lower()
# 看门狗: 健康检查间隔 / 连续失败判定 / 卡死重启
LLM_WATCHDOG_INTERVAL = int(os.environ.get("LLM_WATCHDOG_INTERVAL", "15"))
LLM_WATCHDOG_FAILURE_THRESHOLD = int(os.environ.get("LLM_WATCHDOG_FAILURE_THRESHOLD", "3"))
# vLLM 卡死时自动重启 (Windows 后端经 WSL 调用服务脚本)
VLLM_RESTART_ENABLED = os.environ.get("VLLM_RESTART_ENABLED", "1") == "1"
# WSL 内路径含空格时经 wsl.exe 传递引号不稳, 统一用无空格软链接
# (由 scripts/vllm/wsl_setup_link.sh 创建 -> /root/vllm_scripts)
VLLM_SERVICE_SCRIPT = os.environ.get(
    "VLLM_SERVICE_SCRIPT",
    "/root/vllm_scripts/vllm_service.sh",
)
# 引擎切换时等待在途请求排空的最长时间 (秒)
LLM_SWITCH_DRAIN_TIMEOUT = int(os.environ.get("LLM_SWITCH_DRAIN_TIMEOUT", "30"))

# ============ Flask 配置 ============
SECRET_KEY = os.environ.get("SECRET_KEY", "parts-search-secret-key-2026")
FLASK_HOST = os.environ.get("FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.environ.get("FLASK_PORT", "5000"))

# ============ Session 存储 (Phase 3: 多副本必需) ============
SESSION_TYPE = os.environ.get("SESSION_TYPE", "redis").lower()
# SESSION_TYPE 可选: redis | filesystem | null (内存，仅单副本)
if SESSION_TYPE not in ("redis", "filesystem", "null"):
    raise ValueError("非法 SESSION_TYPE: " + SESSION_TYPE)

SESSION_PERMANENT = os.environ.get("SESSION_PERMANENT", "true").lower() == "true"
SESSION_LIFETIME_SECONDS = int(os.environ.get("SESSION_LIFETIME_SECONDS", "86400"))
SESSION_USE_SIGNER = os.environ.get("SESSION_USE_SIGNER", "true").lower() == "true"
SESSION_KEY_PREFIX = os.environ.get("SESSION_KEY_PREFIX", "fbrain_session:")

# 会话最大空闲时间（秒）：超过该时间没有任何请求即视为过期并回收资源
# 默认 60s = 1 分钟。可通过环境变量 SESSION_IDLE_TIMEOUT_SECONDS 调整。
# 设为 0 或负数表示禁用空闲超时。
SESSION_IDLE_TIMEOUT_SECONDS = int(os.environ.get("SESSION_IDLE_TIMEOUT_SECONDS", "60"))

# 文件 Session 目录 (SESSION_TYPE=filesystem 时使用)
SESSION_FILE_DIR = os.environ.get("SESSION_FILE_DIR", os.path.join(DATA_DIR, 'sessions'))

# ============ 文件上传 ============
ALLOWED_EXTENSIONS = {'.xlsx', '.xlsm'}

# ============ Part Number 识别 ============
PART_NUMBER_HEADERS = [
    'part number', 'part_number', 'sachnummer', 'result.sachnummer',
    'pn', 'p/n', '零件号', '零件编号'
]

# ============ Delta 展示字段配置 ============
# 注意: 导入管线已把所有原始列统一成英文名 (column_mapping + unified_columns),
# 数据库里 parts_data.data 存的也是统一英文名。下面所有 field 必须是 unified_columns.english_name。
# field 列名优先引用 DELTA_BUSINESS_FIELDS (单点映射), 避免同一列在两处重复配置。
# key = 业务键 (Part 模型取值/diff 特判用); key=None 表示纯展示字段。

# 阶段权威来源: 上传时选择的 uploaded_files.stage (pre-TO/TO1/TO2, BOM文件) + 关联 file_id。
# 不再依赖 Baulos_aggr 文本 PRO1/PRO2 匹配 (旧逻辑会把 supplementary 文件行也卷进来导致全 0)。
DELTA_STAGE_FIELD = "Build Lot Aggregate"  # 保留仅作参考/向后兼容，实际 stage 取 uploaded_files.stage
DELTA_STAGE_VALUES = ["pre-TO", "TO1", "TO2"]

# Dashboard 业务字段的 unified_name 映射 (供 database.py 内部使用)
# 旧德文字段名 -> 新统一英文名:
#   BuendelNr            -> Bundle Number
#   FAV_fav              -> FAV
#   FAVStatusKurz_fav    -> FAV Status Short
#   KEM_Nummer           -> KEM Number
#   ProzessStatusDetail  -> ProzessStatusDetail (已是英文)
#   ZGS_DiaP_*           -> ZGS DiaP Controlled Build Lot / ZGS DiaP Max Vit (非必要, 此处用顶部 ZGS)
DELTA_BUSINESS_FIELDS = {
    "zgs":          "ZGS",                  # 取代 ZGS DiaP, BOM 文件均带此列
    "ec":           "Bundle Number",        # 取代 BuendelNr
    "ec_status":    "ProzessStatusDetail",  # EC 流程状态 (用于 EC 饼图)
    "fav":          "FAV",                  # 取代 FAV_fav (ZEUS ID)
    "fav_status":   "FAV Status Short",     # 取代 FAVStatusKurz_fav
    "kem":          "KEM Number",           # 取代 KEM_Nummer
    "soma":         "SOMA in ZEUS",         # SOMA 是否已在 ZEUS 登记 (ja/nein)
}

# Delta 两阶段对比 (diff) 的字段配置。
# business 标签是 API 契约 (前端筛选 / summary 统计使用), 保持稳定;
# field 列名通过 DELTA_BUSINESS_FIELDS 单点引用, 未来新表任何语言表头
# 只需在导入时统一列名 (+ 必要时扩展 DELTA_BUSINESS_FIELDS), 此处零改动。
DELTA_FIELD_CONFIG = [
    {"business": "PN",           "field": "Part Number",                    "priority": 1, "track": True,  "key": None},
    {"business": "ZGS",          "field": DELTA_BUSINESS_FIELDS["zgs"],     "priority": 1, "track": True,  "key": "zgs"},
    {"business": "ZGS KEM",      "field": "ZGS KEM",                        "priority": 1, "track": True,  "key": None},
    {"business": "ZGS ACM",      "field": "ZGS ACM",                        "priority": 1, "track": True,  "key": None},
    {"business": "ZGS (完整版)", "field": "SNR ZGS KEM Aggregate",          "priority": 1, "track": True,  "key": None},
    {"business": "EC",           "field": DELTA_BUSINESS_FIELDS["ec"],      "priority": 2, "track": True,  "key": "ec"},
    {"business": "零件名称",     "field": "Part Name",                      "priority": 3, "track": True,  "key": None},
]

# ============ Redis 缓存配置 ============
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CACHE_ENABLED = os.environ.get("CACHE_ENABLED", "true").lower() == "true"
CACHE_TTL = int(os.environ.get("CACHE_TTL", "300"))
CACHE_KEY_PREFIX = "fbrain"

# ============ CORS 配置 ============
# 逗号分隔的允许跨域来源列表。默认空 = 同源部署 (Flask/Nginx 同域托管前端与 API),
# 不启用跨域。仅当前端与 API 分属不同域名时才配置, 例如:
#   CORS_ORIGINS=https://parts.example.com,https://parts2.example.com
CORS_ORIGINS = [
    o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()
]

# ============ Delta 预计算配置 ============
DELTA_REFRESH_INTERVAL = int(os.environ.get("DELTA_REFRESH_INTERVAL", "300"))
DELTA_INITIAL_DELAY = int(os.environ.get("DELTA_INITIAL_DELAY", "10"))

# ============ Prometheus Metrics (Phase 3) ============
METRICS_ENABLED = os.environ.get("METRICS_ENABLED", "true").lower() == "true"
METRICS_ENDPOINT = os.environ.get("METRICS_ENDPOINT", "/metrics")
# 基础认证 (可选)，留空表示不启用认证
METRICS_BASIC_AUTH_USER = os.environ.get("METRICS_BASIC_AUTH_USER", "")
METRICS_BASIC_AUTH_PASS = os.environ.get("METRICS_BASIC_AUTH_PASS", "")

# ============ 多副本部署配置 (Phase 3) ============
# 当前副本实例 ID (用于日志区分，自动生成)
INSTANCE_ID = os.environ.get(
    "INSTANCE_ID",
    f"van-ea-{os.getpid()}-{os.environ.get('HOSTNAME', 'local')}"
)
# 是否启用领导者选举 (多副本下只有 leader 执行 Delta 预计算等后台任务)
LEADER_ELECTION_ENABLED = os.environ.get("LEADER_ELECTION_ENABLED", "true").lower() == "true"
LEADER_LOCK_KEY = os.environ.get("LEADER_LOCK_KEY", "fbrain:leader_lock")
LEADER_LOCK_TTL = int(os.environ.get("LEADER_LOCK_TTL", "30"))

# ============ 安全基线告警 ============
# 检测到使用默认凭据 (且未通过环境变量显式设置) 时打印告警, 提醒生产环境必须修改。
def _is_unset_default(name, value, default):
    return os.environ.get(name) is None and value == default

if _is_unset_default("ADMIN_PASSWORD", ADMIN_PASSWORD, "admin2026"):
    print("[SECURITY] WARNING: ADMIN_PASSWORD uses default 'admin2026'; set it via env var in production!")
if _is_unset_default("SECRET_KEY", SECRET_KEY, "parts-search-secret-key-2026"):
    print("[SECURITY] WARNING: SECRET_KEY uses default; set a random key via env var in production!")
if DB_TYPE == "postgresql":
    print("[DB] Database engine: PostgreSQL")
