# -*- coding: utf-8 -*-
"""
van.ea 车辆零件智能查询系统 - 后端服务

功能:
- Nginx 反向代理支持（静态文件加速、gzip压缩）
- Redis 缓存层（搜索结果、Delta数据、统计数据缓存）
- Delta 预计算 + 定时刷新（后台线程，毫秒级响应）
- /api/health 健康检查接口
- /api/admin/cache/clear 缓存清理接口
- Redis 不可用时自动降级为无缓存模式

支持：动态数据库、多Excel合并、密码认证、双语界面、智能体、阶段Delta、Redis缓存
"""

import os
import json
import hashlib
import hmac
import time
import threading
import uuid
from datetime import datetime, timedelta
from collections import deque
from flask import Flask, request, jsonify, session, send_from_directory, Response, stream_with_context
from flask_cors import CORS

from config import (
    SECRET_KEY, ADMIN_PASSWORD, UPLOAD_TEMP_DIR, ALLOWED_EXTENSIONS,
    FLASK_HOST, FLASK_PORT, APP_VERSION, APP_NAME, VERSION_HISTORY,
    REDIS_URL, CACHE_ENABLED, CACHE_TTL, CACHE_KEY_PREFIX,
    DELTA_REFRESH_INTERVAL, DELTA_INITIAL_DELAY,
    SESSION_TYPE, SESSION_PERMANENT, SESSION_LIFETIME_SECONDS,
    SESSION_USE_SIGNER, SESSION_KEY_PREFIX, SESSION_FILE_DIR,
    SESSION_IDLE_TIMEOUT_SECONDS,
    METRICS_ENABLED, DB_TYPE, CORS_ORIGINS,
)
from database import db_manager

# ============ Phase 3: 模块延迟导入 (避免循环引用) ============
# 延迟导入: metrics, leader_election, ollama_lb
# Flask Session 需要先初始化 app 才能设置
_metrics = None
_leader_elector = None
_ollama_lb = None


def _init_phase3_modules():
    """初始化 Phase 3 模块 (Redis 初始化完成后调用)
    - Flask Session (Redis / Filesystem)
    - Prometheus Metrics 中间件
    - Leader Election (领导者选举)
    - Ollama Load Balancer
    """
    global _metrics, _leader_elector, _ollama_lb

    # 1. Flask Session: 多副本部署必需 (Redis Session 默认)
    try:
        from flask_session import Session

        if SESSION_TYPE == 'redis' and _redis_client is not None:
            # flask-session 0.8.x 使用 msgpack 写入二进制 session 数据，
            # 必须使用 decode_responses=False 的独立 Redis 连接，
            # 不能复用缓存层的 decode_responses=True 客户端，否则会触发
            # UnicodeDecodeError 导致所有需认证的 API 返回 500。
            import redis as _redis_lib
            _session_redis = _redis_lib.Redis.from_url(
                REDIS_URL,
                decode_responses=False,
                socket_connect_timeout=3,
                socket_timeout=3,
                retry_on_timeout=False,
            )
            app.config.update(
                SESSION_TYPE='redis',
                SESSION_REDIS=_session_redis,
                SESSION_PERMANENT=SESSION_PERMANENT,
                PERMANENT_SESSION_LIFETIME=timedelta(seconds=SESSION_LIFETIME_SECONDS),
                SESSION_USE_SIGNER=SESSION_USE_SIGNER,
                SESSION_KEY_PREFIX=SESSION_KEY_PREFIX,
            )
            Session(app)
            print(f"[Phase3] ✅ Session: Redis (prefix={SESSION_KEY_PREFIX}, TTL={SESSION_LIFETIME_SECONDS}s)")
        elif SESSION_TYPE == 'filesystem':
            os.makedirs(SESSION_FILE_DIR, exist_ok=True)
            app.config.update(
                SESSION_TYPE='filesystem',
                SESSION_FILE_DIR=SESSION_FILE_DIR,
                SESSION_PERMANENT=SESSION_PERMANENT,
                PERMANENT_SESSION_LIFETIME=timedelta(seconds=SESSION_LIFETIME_SECONDS),
                SESSION_USE_SIGNER=SESSION_USE_SIGNER,
                SESSION_KEY_PREFIX=SESSION_KEY_PREFIX,
            )
            Session(app)
            print(f"[Phase3] ✅ Session: Filesystem ({SESSION_FILE_DIR})")
        else:
            print(f"[Phase3] ⚠️  Session: 默认内存 (仅单副本安全, SESSION_TYPE={SESSION_TYPE})")
    except ImportError as e:
        print(f"[Phase3] ⚠️  flask-session 未安装或初始化失败，使用默认内存 Session: {e}")
    except Exception as e:
        print(f"[Phase3] ⚠️  Session 初始化失败，降级为内存 Session: {e}")

    # 2. Prometheus Metrics
    try:
        from metrics import get_metrics
        _metrics = get_metrics()
        _metrics.register_flask(app)
        # 把 Redis 命中监控挂钩到 cache_get
        _metrics.set_redis_enabled(CACHE_ENABLED and _redis_client is not None)
        print(f"[Phase3] ✅ Metrics: 已启用 (端点 /metrics)")
    except ImportError as e:
        print(f"[Phase3] ⚠️  Metrics 跳过 (prometheus-client 未安装): {e}")
    except Exception as e:
        print(f"[Phase3] ⚠️  Metrics 初始化异常: {e}")

    # 3. Leader Election (领导者选举)
    try:
        from leader_election import get_leader_elector
        _leader_elector = get_leader_elector()
        # 注入 Redis 客户端
        _leader_elector.set_redis_client(_redis_client)
        # 状态变化回调 → metrics
        if _metrics:
            _leader_elector.on_leader_change(
                lambda is_ldr: _metrics.set_is_leader(is_ldr)
            )
        print(f"[Phase3] ✅ Leader Election: 已初始化")
    except Exception as e:
        print(f"[Phase3] ⚠️  Leader Election 初始化失败: {e}")

    # 4. Ollama Load Balancer (预初始化，保证健康检查线程启动)
    try:
        from ollama_lb import get_ollama_lb
        _ollama_lb = get_ollama_lb()
        lb_status = _ollama_lb.get_all_nodes_status()
        print(f"[Phase3] ✅ Ollama LB: {len(lb_status)} 节点 "
              f"(健康 {sum(1 for n in lb_status if n['healthy'])}/{len(lb_status)})")
    except Exception as e:
        print(f"[Phase3] ⚠️  Ollama LB 初始化异常: {e}")

    # 5. DB 统计初始化 (metrics gauge)
    if _metrics:
        try:
            stats = db_manager.get_stats()
            _metrics.set_db_stats(
                DB_TYPE,
                total_records=stats.get('total_records', 0),
                total_parts=stats.get('unique_parts', 0),
                total_files=stats.get('total_files', 0),
            )
        except Exception:
            pass

# ============ 路径配置 ============
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 前端静态文件目录（项目根目录下的 frontend/）
FRONTEND_DIR = os.path.normpath(os.path.join(BASE_DIR, '..', 'frontend'))

# 禁用 Flask 自带 static，自己控制静态文件路由（避免路径冲突）
app = Flask(__name__, static_folder=None)
app.secret_key = SECRET_KEY
# CORS: 默认同源部署 (Flask/Nginx 同域托管前端与 API), 不启用跨域;
# 仅当配置了 CORS_ORIGINS 白名单时才对 /api/* 开放跨域。
if CORS_ORIGINS:
    CORS(app, supports_credentials=True, resources={r"/api/*": {"origins": CORS_ORIGINS}})

# ============ Redis 缓存层 ============
# 尝试连接 Redis，失败则自动降级为无缓存模式
_redis_client = None
_cache_enabled = CACHE_ENABLED  # 运行时可动态调整

def _init_redis():
    """初始化 Redis 连接。失败时自动降级，不影响主功能。"""
    global _redis_client, _cache_enabled
    if not CACHE_ENABLED:
        print("[Cache] 缓存已通过配置禁用 (CACHE_ENABLED=false)")
        return
    try:
        import redis as redis_lib
        _redis_client = redis_lib.Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
            retry_on_timeout=False,
        )
        # 测试连接
        _redis_client.ping()
        print(f"[Cache] Redis 连接成功: {REDIS_URL}")
        _cache_enabled = True
    except ImportError:
        print("[Cache] redis 模块未安装，缓存已禁用")
        _cache_enabled = False
        _redis_client = None
    except Exception as e:
        print(f"[Cache] Redis 连接失败 ({e})，缓存已禁用（自动降级）")
        _cache_enabled = False
        _redis_client = None

# 应用启动时初始化 Redis
_init_redis()

# Phase 3: Redis 就绪后初始化 Session / Metrics / Leader / LB
_init_phase3_modules()

def _make_key(*parts):
    """构建缓存 Key，统一前缀。
    格式: fbrain:part1:part2:...
    """
    return f"{CACHE_KEY_PREFIX}:{':'.join(str(p) for p in parts)}"

def cache_get(key):
    """从缓存获取数据。
    返回反序列化后的数据，未命中或出错返回 None。
    """
    if not _cache_enabled or _redis_client is None:
        if _metrics:
            _metrics.observe_redis(False)
        return None
    try:
        raw = _redis_client.get(key)
        hit = raw is not None
        if _metrics:
            _metrics.observe_redis(hit)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as e:
        # Redis 出错时静默失败，不影响主流程
        print(f"[Cache] cache_get 出错: {e}")
        if _metrics:
            _metrics.observe_redis_error('get')
        return None

def cache_set(key, value, ttl=None):
    """将数据写入缓存。
    value 必须是可 JSON 序列化的对象。
    ttl 为 None 时使用默认 CACHE_TTL。
    """
    if not _cache_enabled or _redis_client is None:
        return False
    try:
        if ttl is None:
            ttl = CACHE_TTL
        serialized = json.dumps(value, ensure_ascii=False, default=str)
        _redis_client.setex(key, ttl, serialized)
        return True
    except Exception as e:
        print(f"[Cache] cache_set 出错: {e}")
        return False

def cache_invalidate(pattern):
    """按模式模糊删除缓存 Key。
    例如: cache_invalidate("fbrain:search:*")
    """
    if not _cache_enabled or _redis_client is None:
        return 0
    try:
        count = 0
        # 使用 SCAN 代替 KEYS，避免阻塞
        cursor = 0
        while True:
            cursor, keys = _redis_client.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                _redis_client.delete(*keys)
                count += len(keys)
            if cursor == 0:
                break
        if count > 0:
            print(f"[Cache] 失效缓存: {pattern} ({count} 个 key)")
        return count
    except Exception as e:
        print(f"[Cache] cache_invalidate 出错: {e}")
        return 0

def cache_clear_all():
    """清空所有当前应用前缀下的缓存。"""
    return cache_invalidate(f"{CACHE_KEY_PREFIX}:*")

def is_cache_available():
    """检查 Redis 缓存是否可用（运行时检测）。"""
    if not _cache_enabled or _redis_client is None:
        return False
    try:
        _redis_client.ping()
        return True
    except Exception:
        return False

# ============ 并发搜索控制 ============
MAX_CONCURRENT_SEARCHES = 3  # 默认上限3人
_concurrent_lock = threading.RLock()
_active_sessions = {}  # session_id -> {start_time, last_heartbeat, type}
_sse_clients = []  # SSE客户端列表，用于实时通知

def get_concurrent_count():
    """获取当前活跃搜索会话数（自动清理超时会话）"""
    with _concurrent_lock:
        now = time.time()
        expired = []
        for sid, info in _active_sessions.items():
            if now - info['last_heartbeat'] > 60:  # 60秒无心跳视为超时
                expired.append(sid)
        for sid in expired:
            del _active_sessions[sid]
        return len(_active_sessions)

def recommend_max_concurrent():
    """根据系统资源推荐并发上限"""
    try:
        import psutil
        mem = psutil.virtual_memory()
        cpu_count = psutil.cpu_count()
        available_gb = mem.available / (1024**3)
        # 每并发大约需要200MB内存
        recommended = max(1, min(10, int(available_gb / 0.5)))
        # 同时考虑CPU核心数
        recommended = min(recommended, max(1, cpu_count - 1))
        return recommended
    except ImportError:
        return 3  # 默认3

def notify_concurrency_change():
    """通知所有SSE客户端并发状态变化"""
    count = get_concurrent_count()
    message = f"data: {json.dumps({'type': 'concurrency', 'count': count, 'max': MAX_CONCURRENT_SEARCHES, 'available': MAX_CONCURRENT_SEARCHES - count})}\n\n"
    dead_clients = []
    for client in _sse_clients:
        try:
            client['queue'].append(message)
            client['event'].set()
        except Exception:
            dead_clients.append(client)
    for c in dead_clients:
        if c in _sse_clients:
            _sse_clients.remove(c)

# ============ 活动用户追踪（页面访问心跳） ============
_visitors_lock = threading.RLock()
_active_visitors = {}  # visitor_id -> {first_seen, last_seen, page, is_new}
VISITOR_TIMEOUT = 90   # 90秒无心跳视为离线
_known_visitors_key = 'fbrain_known_visitors'  # Redis set，记录历史访问者


def _get_known_visitors():
    """获取已知访问者ID集合（跨重启持久化到Redis）"""
    if _redis_client is None:
        return set()
    try:
        members = _redis_client.smembers(_known_visitors_key)
        # decode_responses=True 时返回 str，否则 bytes
        return {m.decode() if isinstance(m, bytes) else m for m in members}
    except Exception:
        return set()


def _mark_visitor_known(visitor_id):
    """将访问者标记为已知用户"""
    if _redis_client is None:
        return
    try:
        _redis_client.sadd(_known_visitors_key, visitor_id)
    except Exception:
        pass


def get_active_visitors():
    """获取当前活动访问者列表（自动清理超时）"""
    with _visitors_lock:
        now = time.time()
        expired = [vid for vid, info in _active_visitors.items()
                   if now - info['last_seen'] > VISITOR_TIMEOUT]
        for vid in expired:
            del _active_visitors[vid]
        return list(_active_visitors.items())


def notify_visitor_change(extra_event=None):
    """向所有SSE客户端推送活动用户状态变化"""
    visitors = get_active_visitors()
    active_count = len(visitors)
    new_count = sum(1 for _, info in visitors if info.get('is_new'))
    payload = {
        'type': 'visitors',
        'active_count': active_count,
        'new_count': new_count,
        'visitors': [
            {
                'id': vid[:8],
                'page': info.get('page', ''),
                'first_seen': info.get('first_seen'),
                'last_seen': info.get('last_seen'),
                'is_new': info.get('is_new', False),
            } for vid, info in visitors
        ],
    }
    if extra_event:
        payload.update(extra_event)
    message = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    dead_clients = []
    for client in _sse_clients:
        try:
            client['queue'].append(message)
            client['event'].set()
        except Exception:
            dead_clients.append(client)
    for c in dead_clients:
        if c in _sse_clients:
            _sse_clients.remove(c)


# ============ 系统监控 ============
_monitoring_lock = threading.Lock()
_metrics_history = deque(maxlen=60)  # 保留60条历史记录（约5分钟）
_request_count = 0
_request_lock = threading.Lock()

def collect_metrics():
    """收集系统监控指标"""
    global _request_count
    metrics = {
        'timestamp': datetime.now().strftime('%H:%M:%S'),
        'cpu_percent': 0,
        'memory_percent': 0,
        'memory_used_gb': 0,
        'memory_total_gb': 0,
        'gpu_available': False,
        'gpu_percent': 0,
        'gpu_memory_used_gb': 0,
        'gpu_memory_total_gb': 0,
        'disk_percent': 0,
        'requests_per_minute': 0,
        'active_searches': 0,
        'total_requests': _request_count,
    }
    try:
        import psutil
        metrics['cpu_percent'] = round(psutil.cpu_percent(interval=0.1), 1)
        mem = psutil.virtual_memory()
        metrics['memory_percent'] = round(mem.percent, 1)
        metrics['memory_used_gb'] = round(mem.used / (1024**3), 2)
        metrics['memory_total_gb'] = round(mem.total / (1024**3), 2)
        disk = psutil.disk_usage(os.path.dirname(os.path.abspath(__file__)))
        metrics['disk_percent'] = round(disk.percent, 1)
    except ImportError:
        pass

    # GPU信息（通过nvidia-smi）
    try:
        import subprocess
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=utilization.gpu,memory.used,memory.total',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split(',')
            if len(parts) >= 3:
                metrics['gpu_available'] = True
                metrics['gpu_percent'] = float(parts[0].strip())
                metrics['gpu_memory_used_gb'] = round(float(parts[1].strip()) / 1024, 2)
                metrics['gpu_memory_total_gb'] = round(float(parts[2].strip()) / 1024, 2)
    except Exception:
        pass

    metrics['active_searches'] = get_concurrent_count()

    # 计算每分钟请求数
    with _monitoring_lock:
        if _metrics_history:
            first_ts = _metrics_history[0].get('total_requests', 0)
            elapsed = max(1, len(_metrics_history))  # 约5秒采样一次
            metrics['requests_per_minute'] = round(
                (_request_count - first_ts) / elapsed * 12, 1)  # 12个采样点/分钟
        _metrics_history.append(metrics)

    return metrics

# 启动后台指标采集线程
def _metrics_collector():
    while True:
        try:
            collect_metrics()
        except Exception:
            pass
        time.sleep(5)  # 每5秒采集一次

_metrics_thread = threading.Thread(target=_metrics_collector, daemon=True)
_metrics_thread.start()

# ============ Delta 预计算后台刷新 ============
_delta_last_update = None  # 记录最后一次刷新时间
_delta_update_lock = threading.Lock()

def _refresh_delta_dashboard():
    """执行一次 Delta 仪表盘数据预计算并写入缓存。

    Phase 3: 多副本下仅 leader 执行预计算，避免重复计算。
    """
    global _delta_last_update
    # Phase 3: 领导者检查
    if _leader_elector is not None and not _leader_elector.is_leader:
        # 非 leader，跳过本轮预计算
        return

    t0 = time.perf_counter()
    err_type = None
    try:
        # 计算 Delta 仪表盘数据
        data = db_manager.get_delta_dashboard_data()
        # 写入 Redis 缓存（设置较长 TTL，后台会持续刷新）
        cache_key = _make_key("delta", "dashboard")
        cache_set(cache_key, data, ttl=max(DELTA_REFRESH_INTERVAL * 2, 600))
        with _delta_update_lock:
            _delta_last_update = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        leader_tag = ""
        if _leader_elector is not None:
            leader_tag = " [LEADER]"
        print(f"[Delta]{leader_tag} 仪表盘数据预计算完成，已写入缓存")
        if _metrics:
            _metrics.inc_delta_compute_run('success')
    except Exception as e:
        err_type = type(e).__name__
        import traceback
        traceback.print_exc()
        print(f"[Delta] 预计算出错: {e}")
        if _metrics:
            _metrics.inc_delta_compute_run('error')
            _metrics.inc_delta_compute_error(err_type)
    finally:
        if _metrics:
            _metrics.observe_delta_compute(time.perf_counter() - t0, 'dashboard')

def _delta_background_updater():
    """Delta 仪表盘数据后台刷新线程。
    启动后先等待 DELTA_INITIAL_DELAY 秒（等待数据库就绪），
    然后每隔 DELTA_REFRESH_INTERVAL 秒刷新一次。
    """
    # 启动延迟，等待数据库就绪
    print(f"[Delta] 后台预计算线程启动，等待 {DELTA_INITIAL_DELAY} 秒后首次计算...")
    time.sleep(DELTA_INITIAL_DELAY)

    while True:
        try:
            _refresh_delta_dashboard()
        except Exception as e:
            print(f"[Delta] 后台刷新异常: {e}")

        # 间隔为 0 表示只计算一次
        if DELTA_REFRESH_INTERVAL <= 0:
            print("[Delta] DELTA_REFRESH_INTERVAL=0，后台刷新已停止")
            break

        time.sleep(DELTA_REFRESH_INTERVAL)

def start_delta_background_updater():
    """启动 Delta 预计算后台刷新线程。
    仅在缓存可用且刷新间隔 > 0 时启动。
    """
    if DELTA_REFRESH_INTERVAL <= 0:
        print("[Delta] DELTA_REFRESH_INTERVAL=0，跳过后台预计算线程")
        return
    if not _cache_enabled:
        print("[Delta] 缓存未启用，跳过后台预计算线程")
        return
    t = threading.Thread(target=_delta_background_updater, daemon=True)
    t.start()
    print(f"[Delta] 后台预计算线程已启动，刷新间隔: {DELTA_REFRESH_INTERVAL}秒")

# 启动 Delta 后台刷新线程
start_delta_background_updater()

# 请求计数中间件 + 会话 idle 超时回收
@app.before_request
def count_request():
    global _request_count
    with _request_lock:
        _request_count += 1

    # 会话空闲超时：若用户已登录但超过 SESSION_IDLE_TIMEOUT_SECONDS 没有活动，
    # 主动让出 session 资源（清空 admin_logged_in 与 _last_seen），
    # 下一次需要鉴权的请求会得到 401，由前端引导重新登录。
    if SESSION_IDLE_TIMEOUT_SECONDS > 0 and session.get('admin_logged_in'):
        last_seen = session.get('_last_seen')
        now_ts = time.time()
        if last_seen is not None and (now_ts - float(last_seen)) > SESSION_IDLE_TIMEOUT_SECONDS:
            # 记录一次空闲过期事件（便于监控/报告）
            try:
                with _session_metrics_lock:
                    _session_idle_evictions += 1
            except Exception:
                pass
            session.pop('admin_logged_in', None)
            session.pop('_last_seen', None)
        else:
            # 活跃请求：刷新 last_seen
            session['_last_seen'] = now_ts

# 会话空闲回收统计
_session_metrics_lock = threading.Lock()
_session_idle_evictions = 0

# ============ 认证 ============

def is_authenticated():
    return session.get('admin_logged_in', False)


@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.json
    password = data.get('password', '')
    if isinstance(password, str) and hmac.compare_digest(password.encode('utf-8'), ADMIN_PASSWORD.encode('utf-8')):
        session['admin_logged_in'] = True
        session['_last_seen'] = time.time()
        return jsonify({
            'success': True,
            'message': 'Login successful',
            'idle_timeout_seconds': SESSION_IDLE_TIMEOUT_SECONDS,
        })
    return jsonify({'success': False, 'error': 'Incorrect password'}), 401


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.pop('admin_logged_in', None)
    session.pop('_last_seen', None)
    return jsonify({'success': True})


@app.route('/api/auth/status')
def auth_status():
    last_seen = session.get('_last_seen')
    now_ts = time.time()
    if SESSION_IDLE_TIMEOUT_SECONDS > 0 and last_seen is not None:
        remaining = max(0, SESSION_IDLE_TIMEOUT_SECONDS - (now_ts - float(last_seen)))
    else:
        remaining = None
    return jsonify({
        'authenticated': is_authenticated(),
        'idle_timeout_seconds': SESSION_IDLE_TIMEOUT_SECONDS,
        'idle_remaining_seconds': remaining,
    })


# ============ 页面路由 ============

@app.route('/')
def index():
    return send_from_directory(FRONTEND_DIR, 'index.html')


@app.route('/admin')
def admin():
    return send_from_directory(FRONTEND_DIR, 'admin.html')


@app.route('/monitoring')
def monitoring():
    return send_from_directory(FRONTEND_DIR, 'monitoring.html')


@app.route('/delta')
def delta_page():
    return send_from_directory(FRONTEND_DIR, 'index.html')


@app.route('/<path:filename>')
def serve_static(filename):
    """提供前端静态文件（JS、CSS、图片等）。"""
    # 安全检查：防止路径遍历
    if '..' in filename or filename.startswith('/'):
        return 'Invalid path', 400
    return send_from_directory(FRONTEND_DIR, filename)


@app.route('/api/version')
def version():
    """获取系统版本信息"""
    return jsonify({
        'success': True,
        'version': APP_VERSION,
        'name': APP_NAME,
        'build_date': VERSION_HISTORY[0]['date'] if VERSION_HISTORY else datetime.now().strftime('%Y-%m-%d'),
        'history': VERSION_HISTORY,
    })


@app.route('/api/health')
def health_check():
    """健康检查接口，返回系统各组件状态 (Phase 3 增强版)。

    返回:
        - status: overall (healthy/degraded/unhealthy)
        - app / database / redis / session / ollama / leader 各子系统状态
        - version / instance_id / db_type / timestamp
    """
    from config import INSTANCE_ID as _iid
    result = {
        'status': 'healthy',
        'app': {'status': 'ok', 'version': APP_VERSION, 'instance_id': _iid, 'db_type': DB_TYPE},
        'database': {'status': 'unknown', 'type': DB_TYPE},
        'redis': {'status': 'unknown'},
        'session': {'status': 'unknown', 'type': SESSION_TYPE},
        'ollama': {'status': 'unknown'},
        'leader': {'status': 'unknown'},
        'cache_enabled': _cache_enabled,
        'metrics_enabled': bool(_metrics),
        'timestamp': datetime.now().isoformat(),
    }

    # 1. 检查数据库
    try:
        stats = db_manager.get_stats()
        result['database'] = {
            'status': 'ok',
            'type': DB_TYPE,
            'total_records': stats.get('total_records', 0),
            'unique_parts': stats.get('unique_parts', 0),
            'file_count': stats.get('file_count', 0),
        }
    except Exception as e:
        result['database'] = {'status': 'error', 'error': str(e), 'type': DB_TYPE}
        result['status'] = 'unhealthy'

    # 2. 检查 Redis
    try:
        if is_cache_available():
            result['redis'] = {'status': 'ok', 'url': REDIS_URL, 'session_ok': SESSION_TYPE == 'redis'}
            result['session'] = {'status': 'ok' if SESSION_TYPE == 'redis' else 'degraded', 'type': SESSION_TYPE}
        else:
            result['redis'] = {'status': 'unavailable'}
            if SESSION_TYPE == 'redis':
                result['session'] = {'status': 'degraded', 'type': SESSION_TYPE, 'note': 'Redis unavailable, session fell back to memory'}
            if _cache_enabled:
                result['status'] = 'degraded' if result['status'] == 'healthy' else result['status']
    except Exception as e:
        result['redis'] = {'status': 'error', 'error': str(e)}

    # 3. 检查 Leader Election (Phase 3)
    if _leader_elector is not None:
        try:
            result['leader'] = {
                'status': 'ok',
                'is_leader': bool(_leader_elector.is_leader),
                'election_enabled': _leader_elector.enabled,
            }
        except Exception as e:
            result['leader'] = {'status': 'error', 'error': str(e)}
    else:
        result['leader'] = {'status': 'disabled', 'is_leader': True, 'note': 'Running without leader election'}

    # 4. 检查 Ollama LB (Phase 3)
    if _ollama_lb is not None:
        try:
            status = _ollama_lb.get_all_nodes_status()
            healthy = sum(1 for n in status if n['healthy'])
            total = len(status)
            if total == 0:
                result['ollama'] = {'status': 'disabled', 'nodes': 0}
            elif healthy == 0:
                result['ollama'] = {
                    'status': 'unavailable',
                    'healthy_nodes': 0,
                    'total_nodes': total,
                    'nodes': status,
                }
                result['status'] = 'degraded' if result['status'] == 'healthy' else result['status']
            else:
                result['ollama'] = {
                    'status': 'ok' if healthy == total else 'degraded',
                    'healthy_nodes': healthy,
                    'total_nodes': total,
                    'nodes': status,
                }
        except Exception as e:
            result['ollama'] = {'status': 'error', 'error': str(e)}

    # 综合健康度判定
    db_ok = result['database']['status'] == 'ok'
    redis_ok = (result['redis']['status'] == 'ok') or (not _cache_enabled and SESSION_TYPE != 'redis')
    ollama_not_critical = result['ollama']['status'] in ('ok', 'degraded', 'disabled', 'unknown')  # AI 降级可接受

    if db_ok and redis_ok and ollama_not_critical:
        result['status'] = 'healthy'
    elif db_ok:
        result['status'] = 'degraded'
    else:
        result['status'] = 'unhealthy'

    # DB stats gauge 刷新
    if _metrics and db_ok:
        try:
            s = result['database']
            _metrics.set_db_stats(DB_TYPE,
                                  total_records=s.get('total_records', 0),
                                  total_parts=s.get('unique_parts', 0),
                                  total_files=s.get('file_count', 0))
        except Exception:
            pass

    return jsonify(result)


# ============ 查询 API（公开） ============

@app.route('/api/stats')
def get_stats():
    """获取统计数据（支持 Redis 缓存，TTL: 120秒）"""
    cache_key = _make_key("stats")
    # 尝试从缓存获取
    cached = cache_get(cache_key)
    if cached is not None:
        return jsonify({'success': True, 'data': cached, 'from_cache': True})
    try:
        data = db_manager.get_stats()
        # 写入缓存
        cache_set(cache_key, data, ttl=120)
        return jsonify({'success': True, 'data': data, 'from_cache': False})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/columns')
def get_columns():
    """获取列信息（支持 Redis 缓存，TTL: 3600秒）"""
    cache_key = _make_key("columns")
    # 尝试从缓存获取
    cached = cache_get(cache_key)
    if cached is not None:
        return jsonify({'success': True, 'data': cached, 'from_cache': True})
    try:
        cols = db_manager.get_all_columns()
        # 写入缓存
        cache_set(cache_key, cols, ttl=3600)
        return jsonify({'success': True, 'data': cols, 'from_cache': False})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/search')
def search():
    """零件号搜索（支持 Redis 缓存，TTL: 300秒）"""
    try:
        part_number = request.args.get('part_number', '').strip()
        if not part_number:
            return jsonify({'success': False, 'error': 'Part Number required'}), 400

        # 构建缓存 Key（基于查询参数）
        params_hash = hashlib.md5(f"search:{part_number.lower()}".encode()).hexdigest()[:12]
        cache_key = _make_key("search", params_hash)

        # 尝试从缓存获取
        cached = cache_get(cache_key)
        if cached is not None:
            cached['from_cache'] = True
            return jsonify(cached)

        results = db_manager.search_by_part_number(part_number)
        columns = db_manager.get_all_columns()

        response_data = {
            'success': True,
            'search_term': part_number,
            'total_results': len(results),
            'columns': columns,
            'data': results,
            'from_cache': False
        }

        # 写入缓存
        cache_set(cache_key, response_data, ttl=300)
        return jsonify(response_data)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/search_field')
def search_field():
    """按任意字段搜索"""
    try:
        field = request.args.get('field', '').strip()
        value = request.args.get('value', '').strip()
        if not field or not value:
            return jsonify({'success': False, 'error': 'Field and value required'}), 400

        results = db_manager.search_by_field(field, value)
        columns = db_manager.get_all_columns()

        return jsonify({
            'success': True,
            'search_field': field,
            'search_value': value,
            'total_results': len(results),
            'columns': columns,
            'data': results
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/all_part_numbers')
def get_all_part_numbers():
    try:
        pns = db_manager.get_all_part_numbers()
        return jsonify({'success': True, 'data': pns, 'count': len(pns)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/update_cell', methods=['POST'])
def update_cell():
    try:
        data = request.json
        record_id = data.get('record_id')
        field = data.get('field')
        value = data.get('value', '')

        if not record_id or not field:
            return jsonify({'success': False, 'error': 'record_id and field required'}), 400

        success = db_manager.update_cell(record_id, field, value)
        if success:
            # 单元格更新后，失效搜索和统计缓存
            cache_invalidate(_make_key("search", "*"))
            cache_invalidate(_make_key("stats"))
            cache_invalidate(_make_key("delta", "*"))
            return jsonify({'success': True, 'message': 'Cell updated'})
        return jsonify({'success': False, 'error': 'Record not found'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============ 管理员 API（需认证） ============

@app.route('/api/admin/files')
def admin_list_files():
    if not is_authenticated():
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    try:
        files = db_manager.list_files()
        return jsonify({'success': True, 'data': files})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/files/<int:file_id>', methods=['DELETE'])
def admin_delete_file(file_id):
    if not is_authenticated():
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    try:
        db_manager.delete_file(file_id)
        # 删除文件后清空所有缓存（数据已变更）
        cache_clear_all()
        return jsonify({'success': True, 'message': 'File deleted'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/analyze', methods=['POST'])
def admin_analyze_excel():
    """分析上传的Excel文件，返回表头信息和映射建议"""
    if not is_authenticated():
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file uploaded'}), 400

        file = request.files['file']
        if not file.filename:
            return jsonify({'success': False, 'error': 'No filename'}), 400

        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return jsonify({'success': False, 'error': f'Only {ALLOWED_EXTENSIONS} files allowed'}), 400

        # 保存临时文件
        temp_path = os.path.join(UPLOAD_TEMP_DIR, f"temp_{datetime.now().strftime('%Y%m%d%H%M%S')}_{file.filename}")
        file.save(temp_path)

        # 分析Excel
        sheets_info = db_manager.analyze_excel(temp_path, file.filename)

        # 为每个sheet创建映射建议
        all_mappings = {}
        for sheet in sheets_info:
            mapping = db_manager.create_column_mapping(sheet['headers'], sheet['sheet_name'])
            all_mappings[sheet['sheet_name']] = mapping

        return jsonify({
            'success': True,
            'temp_path': temp_path,
            'original_filename': file.filename,
            'sheets': sheets_info,
            'mappings': all_mappings
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/upload_bom', methods=['POST'])
def upload_bom():
    """BOM物料清单上传：分析Excel文件并标记file_type为BOM，接收stage参数"""
    if not is_authenticated():
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file uploaded'}), 400

        file = request.files['file']
        if not file.filename:
            return jsonify({'success': False, 'error': 'No filename'}), 400

        # 接收阶段参数
        stage = request.form.get('stage', '').strip()
        valid_stages = ['pre-TO', 'TO1', 'TO2']
        if stage not in valid_stages:
            return jsonify({'success': False, 'error': f'Invalid stage. Must be one of: {valid_stages}'}), 400

        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return jsonify({'success': False, 'error': f'Only {ALLOWED_EXTENSIONS} files allowed'}), 400

        # 保存临时文件
        temp_path = os.path.join(UPLOAD_TEMP_DIR, f"bom_{stage}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{file.filename}")
        file.save(temp_path)

        # 分析Excel
        sheets_info = db_manager.analyze_excel(temp_path, file.filename)

        # 为每个sheet创建映射建议
        all_mappings = {}
        for sheet in sheets_info:
            mapping = db_manager.create_column_mapping(sheet['headers'], sheet['sheet_name'])
            all_mappings[sheet['sheet_name']] = mapping

        return jsonify({
            'success': True,
            'temp_path': temp_path,
            'original_filename': file.filename,
            'sheets': sheets_info,
            'mappings': all_mappings,
            'file_type': 'BOM',
            'stage': stage
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/import', methods=['POST'])
def admin_import_excel():
    """确认导入Excel数据（管理员确认表头映射后调用）"""
    if not is_authenticated():
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    try:
        data = request.json
        temp_path = data.get('temp_path')
        original_filename = data.get('original_filename')
        sheet_mappings = data.get('sheet_mappings', {})
        admin_english_names = data.get('admin_english_names', {})
        file_type = data.get('file_type', 'supplementary')
        stage = data.get('stage', None)

        if not temp_path or not os.path.exists(temp_path):
            return jsonify({'success': False, 'error': 'Temp file not found, please re-upload'}), 400

        results = db_manager.import_excel_data(
            temp_path, original_filename, sheet_mappings, admin_english_names,
            file_type=file_type, stage=stage
        )

        # 清理临时文件
        try:
            os.remove(temp_path)
        except OSError:
            pass

        total_imported = sum(r['imported_rows'] for r in results)

        # 导入成功后清空所有缓存（数据已变更）
        cache_clear_all()

        return jsonify({
            'success': True,
            'message': f'Imported {total_imported} records from {len(results)} sheet(s)',
            'results': results,
            'total_imported': total_imported
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/columns/<int:col_id>', methods=['PUT'])
def admin_update_column(col_id):
    """更新列显示名称"""
    if not is_authenticated():
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    try:
        data = request.json
        db_manager.update_column_display(col_id, data.get('english_name'), data.get('display_name'))
        # 列配置变更后，失效列信息缓存
        cache_invalidate(_make_key("columns"))
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/admin/cache/clear', methods=['POST'])
def admin_cache_clear():
    """清空所有 Redis 缓存（管理员接口）。
    用于数据变更后手动刷新缓存，或排查缓存问题时使用。
    """
    if not is_authenticated():
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    try:
        count = cache_clear_all()
        return jsonify({
            'success': True,
            'message': f'Cache cleared ({count} keys)',
            'cleared_keys': count,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============ 智能体 API ============

try:
    from agent import agent_manager
    AGENT_AVAILABLE = True
except Exception as e:
    print(f"[Agent] Module load failed: {e}")
    AGENT_AVAILABLE = False
    agent_manager = None


@app.route('/api/agent/status')
def agent_status():
    if not AGENT_AVAILABLE:
        return jsonify({'success': True, 'available': False, 'mode': 'unavailable'})
    from agent import get_compute_backend, get_cloud_config
    backend = get_compute_backend()
    cloud_config = get_cloud_config()
    cloud_available = bool(cloud_config.get('api_url') and cloud_config.get('api_key') and cloud_config.get('model'))
    active_agent, active_mode = agent_manager.get_active_agent()
    model_name = None
    if active_mode == 'ollama':
        model_name = getattr(agent_manager.ollama_agent, 'model', None)
    elif active_mode == 'cloud':
        model_name = cloud_config.get('model')
    return jsonify({
        'success': True, 'available': True, 'mode': active_mode,
        'model': model_name,
        'backend': backend,
        'cloud_available': cloud_available,
        'ollama_available': agent_manager.use_ollama,
    })


@app.route('/api/agent/query', methods=['POST'])
def agent_query():
    if not AGENT_AVAILABLE:
        return jsonify({'success': False, 'error': 'Agent module not loaded'}), 500
    try:
        data = request.json
        user_query = data.get('query', '').strip()
        if not user_query:
            return jsonify({'success': False, 'error': 'Query required'}), 400

        lang = data.get('lang', 'zh')
        # 多轮对话上下文: [{role: 'user'|'assistant', content}]
        history = data.get('history') or []
        if not isinstance(history, list):
            history = []
        # 过滤掉畸形条目，并限制单条长度
        clean_history = []
        for h in history[-20:]:
            if not isinstance(h, dict):
                continue
            role = h.get('role')
            content = h.get('content')
            if role in ('user', 'assistant', 'agent') and isinstance(content, str) and content.strip():
                clean_history.append({
                    'role': 'assistant' if role == 'agent' else role,
                    'content': content.strip()[:2000],
                })

        result = agent_manager.process_query(user_query, lang=lang, history=clean_history)
        response_data = {
            'success': True,
            'response': result['response'],
            'intent': result['intent'],
            'mode': result['mode'],
            'search_results': result.get('search_results'),
            'is_table_related': result['intent'].get('is_table_related', True),
            'is_compare': result.get('is_compare', False),
        }
        if result.get('compare_result'):
            response_data['compare_result'] = result['compare_result']
        return jsonify(response_data)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ============ 模型管理 API ============

@app.route('/api/agent/models')
def agent_models():
    """获取已安装的模型列表"""
    try:
        from agent import get_available_models, get_current_model, RECOMMENDED_MODELS
        models = get_available_models()
        current = get_current_model()
        return jsonify({
            'success': True,
            'models': models,
            'current_model': current,
            'recommended': RECOMMENDED_MODELS
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/agent/model', methods=['POST'])
def agent_switch_model():
    """切换当前模型"""
    try:
        from agent import agent_manager
        if not AGENT_AVAILABLE:
            return jsonify({'success': False, 'error': 'Agent not available'}), 500
        data = request.json
        model_name = data.get('model', '').strip()
        if not model_name:
            return jsonify({'success': False, 'error': 'Model name required'}), 400
        agent_manager.switch_model(model_name)
        return jsonify({
            'success': True,
            'current_model': model_name,
            'mode': 'ollama' if agent_manager.use_ollama else 'rule'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/agent/pull-model', methods=['POST'])
def agent_pull_model():
    """开始下载模型"""
    try:
        from agent import pull_model
        data = request.json
        model_name = data.get('model', '').strip()
        if not model_name:
            return jsonify({'success': False, 'error': 'Model name required'}), 400
        pull_model(model_name)
        return jsonify({'success': True, 'message': f'Started pulling {model_name}'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/agent/pull-status')
def agent_pull_status():
    """获取模型下载状态"""
    try:
        from agent import get_pull_status
        model_name = request.args.get('model', None)
        status = get_pull_status(model_name)
        return jsonify({'success': True, 'status': status})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/agent/model/delete', methods=['POST'])
def agent_delete_model():
    """删除已安装的模型"""
    try:
        from agent import delete_model, get_current_model
        data = request.json
        model_name = data.get('model', '').strip()
        if not model_name:
            return jsonify({'success': False, 'error': 'Model name required'}), 400
        if model_name == get_current_model():
            return jsonify({'success': False, 'error': 'Cannot delete the current active model'}), 400
        success = delete_model(model_name)
        return jsonify({'success': success})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============ 算力后端管理 API ============

@app.route('/api/agent/backend')
def agent_backend():
    """获取当前算力后端状态"""
    try:
        from agent import get_compute_backend, get_cloud_config, RECOMMENDED_CLOUD_PROVIDERS
        backend = get_compute_backend()
        cloud_config = get_cloud_config()
        cloud_available = bool(cloud_config.get('api_url') and cloud_config.get('api_key') and cloud_config.get('model'))
        return jsonify({
            'success': True,
            'backend': backend,
            'cloud_config': cloud_config,
            'cloud_available': cloud_available,
            'ollama_available': agent_manager.use_ollama if AGENT_AVAILABLE else False,
            'providers': RECOMMENDED_CLOUD_PROVIDERS,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/agent/backend', methods=['POST'])
def agent_switch_backend():
    """切换算力后端"""
    try:
        from agent import set_compute_backend, get_compute_backend
        if not AGENT_AVAILABLE:
            return jsonify({'success': False, 'error': 'Agent not available'}), 500
        data = request.json
        backend = data.get('backend', '').strip()
        if backend not in ('local', 'cloud'):
            return jsonify({'success': False, 'error': 'Backend must be local or cloud'}), 400
        set_compute_backend(backend)
        # 重新加载云端智能体
        if backend == 'cloud':
            agent_manager.reload_cloud()
        active_agent, active_mode = agent_manager.get_active_agent()
        return jsonify({
            'success': True,
            'backend': get_compute_backend(),
            'active_mode': active_mode,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/agent/cloud-config', methods=['POST'])
def agent_cloud_config():
    """保存云端API配置 (仅管理员)"""
    if not is_authenticated():
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    try:
        from agent import set_cloud_config, get_cloud_config
        if not AGENT_AVAILABLE:
            return jsonify({'success': False, 'error': 'Agent not available'}), 500
        data = request.json
        api_url = data.get('api_url', '').strip()
        api_key = data.get('api_key', '').strip()
        model = data.get('model', '').strip()

        if not api_url or not model:
            return jsonify({'success': False, 'error': 'API URL and model are required'}), 400

        set_cloud_config(api_url, api_key, model)
        # 重新加载云端智能体
        agent_manager.reload_cloud()
        config = get_cloud_config()
        return jsonify({
            'success': True,
            'cloud_config': config,
            'cloud_available': agent_manager.cloud_agent.available,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/agent/cloud-test', methods=['POST'])
def agent_cloud_test():
    """测试云端API连通性 (仅管理员, api_url 经过 SSRF 校验)"""
    if not is_authenticated():
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    try:
        from agent import _cloud_config, validate_cloud_api_url
        if not AGENT_AVAILABLE:
            return jsonify({'success': False, 'error': 'Agent not available'}), 500
        data = request.json
        api_url = validate_cloud_api_url(data.get('api_url', '').strip()).rstrip('/')
        api_key = data.get('api_key', '').strip()
        model = data.get('model', '').strip()

        # 如果传入了掩码key（含*），使用已存储的key
        if '*' in api_key:
            api_key = _cloud_config.get('api_key', '')

        if not api_url or not api_key or not model:
            return jsonify({'success': False, 'error': 'API URL, key, and model are required'}), 400

        import urllib.request as ur
        url = f"{api_url}/chat/completions"
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 5,
            "stream": False
        }).encode('utf-8')
        req = ur.Request(url, data=payload,
                         headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'},
                         method='POST')
        with ur.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            reply = result.get('choices', [{}])[0].get('message', {}).get('content', '')
            return jsonify({'success': True, 'message': 'Connection OK', 'reply': reply[:50]})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============ 仪表盘 API ============

@app.route('/api/dashboard')
def get_dashboard():
    """获取仪表盘统计数据（已废弃，保留兼容）"""
    try:
        stats = db_manager.get_dashboard_stats()
        return jsonify({'success': True, 'data': stats})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ============ Delta 可视化面板 API ============

@app.route('/api/delta/dashboard')
def delta_dashboard():
    """获取Delta可视化面板所需的全部数据（支持 Redis 缓存，TTL: 60秒）。

    返回:
        - stages: 各阶段(pre-TO/TO1/TO2)的记录数、PN数、EC数、FAV数、KEM数、SOMA数
        - delta1: pre-TO→TO1 的KPI和饼图数据
        - delta2: TO1→TO2 的KPI和饼图数据
        - bar_line: 柱状折线图数据（三阶段）
    """
    cache_key = _make_key("delta", "dashboard")
    # 尝试从缓存获取（后台预计算线程会定期刷新此缓存）
    cached = cache_get(cache_key)
    if cached is not None:
        return jsonify({'success': True, 'data': cached, 'from_cache': True})
    try:
        data = db_manager.get_delta_dashboard_data()
        # 写入缓存（后台线程刷新更快，此处设置较短 TTL）
        cache_set(cache_key, data, ttl=60)
        return jsonify({'success': True, 'data': data, 'from_cache': False})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ============ 仪表盘下钻 API ============

@app.route('/api/dashboard/drilldown', methods=['POST'])
def dashboard_drilldown():
    """仪表盘下钻查询：按维度和值获取详细记录"""
    try:
        data = request.json or {}
        dimension = data.get('dimension', '').strip()
        value = data.get('value', '').strip()
        page = int(data.get('page', 1))
        page_size = int(data.get('page_size', 20))

        if not dimension:
            return jsonify({'success': False, 'error': 'dimension required'}), 400

        result = db_manager.get_drilldown_data(dimension, value, page, page_size)
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ============ 对比 API ============

@app.route('/api/compare', methods=['POST'])
def compare_records():
    """对比两条记录"""
    try:
        data = request.json
        field = data.get('field', '').strip()
        value1 = data.get('value1', '').strip()
        value2 = data.get('value2', '').strip()

        if not field or not value1 or not value2:
            return jsonify({'success': False, 'error': 'field, value1 and value2 are required'}), 400

        result = db_manager.compare_records(field, value1, value2)
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ============ Delta API ============
@app.route('/api/delta')
def get_delta():
    """阶段间 Delta 查询（支持 Redis 缓存，TTL: 300秒）"""
    try:
        from_stage = request.args.get('from', 'TO1').strip()
        to_stage = request.args.get('to', 'TO2').strip()
        change_filter = request.args.getlist('filter')  # 多选: ?filter=PN&filter=ZGS
        part_number = request.args.get('pn', '').strip() or None
        page = int(request.args.get('page', 1))
        page_size = int(request.args.get('page_size', 50))

        # 构建缓存 Key（基于所有查询参数）
        filter_str = ','.join(sorted(change_filter)) if change_filter else 'none'
        pn_str = part_number or 'all'
        cache_key = _make_key("delta", f"{from_stage}_{to_stage}", f"page{page}", hashlib.md5(
            f"f:{filter_str}_pn:{pn_str}_ps:{page_size}".encode()
        ).hexdigest()[:8])

        # 尝试从缓存获取
        cached = cache_get(cache_key)
        if cached is not None:
            cached['from_cache'] = True
            return jsonify(cached)

        result = db_manager.calculate_delta(
            from_stage=from_stage,
            to_stage=to_stage,
            change_filter=change_filter if change_filter else None,
            part_number=part_number,
            page=page,
            page_size=page_size
        )

        # Delta 数据校验失败（阶段数据为空等）：透传错误信息
        if isinstance(result, dict) and result.get('success') is False:
            return jsonify(result), 400

        response_data = {'success': True, 'data': result, 'from_cache': False}

        # 写入缓存
        cache_set(cache_key, response_data, ttl=300)
        return jsonify(response_data)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/delta_detail')
def get_delta_detail():
    """获取Delta详情（下钻数据）：两阶段并排对比，高亮差异字段"""
    try:
        part_number = request.args.get('pn', '').strip()
        from_stage = request.args.get('from', '').strip() or None
        to_stage = request.args.get('to', '').strip() or None
        if not part_number:
            return jsonify({'success': False, 'error': 'Part Number required'}), 400
        result = db_manager.get_delta_detail(part_number, from_stage, to_stage)
        if 'error' in result:
            return jsonify({'success': False, 'error': result['error']}), 404
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/agent/suggestions')
def agent_suggestions():
    """基于数据库真实字段和样例值生成可用的搜索提示词。

    返回的提示词均为可直接执行的自然语言搜索语句，而非知识性问答。
    """
    try:
        import random
        hints = db_manager.get_search_hint_samples(limit_per_field=2)

        # 字段 -> (中文模板, 英文模板, 德文模板)，{v} 为样例值占位符
        templates = {
            'part_number': (
                '查询零件号 {v} 的信息', 'Query part number {v}', 'Teilenummer {v} abfragen'),
            'BuendelNr': (
                '查找EC号为 {v} 的零件', 'Find parts with EC {v}', 'Teile mit EC {v} finden'),
            'KEM': (
                '查找KEM为 {v} 的零件', 'Find parts with KEM {v}', 'Teile mit KEM {v} finden'),
            'FAV_fav': (
                '查找FAV为 {v} 的零件', 'Find parts with FAV {v}', 'Teile mit FAV {v} finden'),
            'ZGS DiaP': (
                '查找ZGS版本为 {v} 的零件', 'Find parts with ZGS {v}', 'Teile mit ZGS {v} finden'),
            'SOMA in ZEUS': (
                '查找SOMA为 {v} 的零件', 'Find parts with SOMA {v}', 'Teile mit SOMA {v} finden'),
            'Baulos_aggr': (
                '查找阶段 {v} 的零件', 'Find parts in stage {v}', 'Teile in Stufe {v} finden'),
            'bndverantwortlicher': (
                '查找负责人为 {v} 的零件', 'Find parts responsible by {v}', 'Teile verantwortlich von {v} finden'),
            'status': (
                '查找状态为 {v} 的零件', 'Find parts with status {v}', 'Teile mit Status {v} finden'),
            'teilbenennung': (
                '查找名称包含 {v} 的零件', 'Find parts named like {v}', 'Teile mit Name {v} finden'),
        }

        zh, en, de = [], [], []
        # 组合提示词池：每个字段选取一个样例值
        pool = []
        for field, info in hints.items():
            tmpls = templates.get(field)
            if not tmpls or not info.get('samples'):
                continue
            sample = random.choice(info['samples'])
            pool.append((tmpls[0].format(v=sample),
                         tmpls[1].format(v=sample),
                         tmpls[2].format(v=sample)))

        random.shuffle(pool)
        for item in pool[:6]:
            zh.append(item[0])
            en.append(item[1])
            de.append(item[2])

        # 若数据库中可用样例不足，补充通用但仍可执行的搜索提示词
        fallback_zh = ['列出所有可用零件', '查找有EC号的零件', '查找SOMA为ja的零件']
        fallback_en = ['List all available parts', 'Find parts with EC numbers', 'Find parts where SOMA is ja']
        fallback_de = ['Alle verfügbaren Teile auflisten', 'Teile mit EC-Nummern finden', 'Teile mit SOMA ja finden']
        i = 0
        while len(zh) < 4 and i < len(fallback_zh):
            zh.append(fallback_zh[i]); en.append(fallback_en[i]); de.append(fallback_de[i]); i += 1

        return jsonify({
            'success': True,
            'suggestions_zh': zh[:6],
            'suggestions_en': en[:6],
            'suggestions_de': de[:6],
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'suggestions_zh': ['列出所有可用零件', '查找有EC号的零件', '查找SOMA为ja的零件',
                              '查询零件号信息', '查找ZGS版本变更', '对比两个零件号'],
            'suggestions_en': ['List all available parts', 'Find parts with EC numbers',
                              'Find parts where SOMA is ja', 'Query part number info',
                              'Find ZGS version changes', 'Compare two part numbers'],
            'suggestions_de': ['Alle verfügbaren Teile auflisten', 'Teile mit EC-Nummern finden',
                              'Teile mit SOMA ja finden', 'Teilenummer abfragen',
                              'ZGS-Versionsänderungen finden', 'Zwei Teilenummern vergleichen'],
        })


@app.route('/api/search_complex', methods=['POST'])
def search_complex():
    """复杂条件搜索"""
    try:
        data = request.json
        conditions = data.get('conditions', [])
        if not conditions:
            return jsonify({'success': False, 'error': 'conditions required'}), 400

        results = db_manager.search_complex(conditions)
        columns = db_manager.get_all_columns()
        return jsonify({
            'success': True,
            'total_results': len(results),
            'columns': columns,
            'data': results
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ============ 并发搜索控制 API ============
@app.route('/api/concurrent/status')
def concurrent_status():
    """获取当前并发状态"""
    count = get_concurrent_count()
    # Phase 3: 更新 metrics gauge
    if _metrics:
        try:
            _metrics.set_concurrent_slots(count)
        except Exception:
            pass
    return jsonify({
        'success': True,
        'count': count,
        'max': MAX_CONCURRENT_SEARCHES,
        'available': MAX_CONCURRENT_SEARCHES - count,
        'recommended': recommend_max_concurrent(),
    })


@app.route('/api/concurrent/acquire', methods=['POST'])
def concurrent_acquire():
    """尝试获取一个并发搜索槽位"""
    global MAX_CONCURRENT_SEARCHES
    session_id = request.json.get('session_id') if request.is_json else request.form.get('session_id')
    if not session_id:
        session_id = str(uuid.uuid4())

    with _concurrent_lock:
        count = get_concurrent_count()
        # 如果该会话已持有槽位，直接成功
        if session_id in _active_sessions:
            _active_sessions[session_id]['last_heartbeat'] = time.time()
            return jsonify({
                'success': True,
                'session_id': session_id,
                'acquired': True,
                'count': len(_active_sessions),
                'max': MAX_CONCURRENT_SEARCHES,
                'message': '槽位已持有',
            })
        # 检查是否还有空闲槽位
        if count >= MAX_CONCURRENT_SEARCHES:
            return jsonify({
                'success': True,
                'session_id': session_id,
                'acquired': False,
                'count': count,
                'max': MAX_CONCURRENT_SEARCHES,
                'available': MAX_CONCURRENT_SEARCHES - count,
                'message': f'当前并发已满（{count}/{MAX_CONCURRENT_SEARCHES}），请稍候再试',
            }), 429
        # 获取槽位
        _active_sessions[session_id] = {
            'start_time': time.time(),
            'last_heartbeat': time.time(),
            'type': 'search',
        }
        new_count = len(_active_sessions)

    # 通知所有客户端
    notify_concurrency_change()

    return jsonify({
        'success': True,
        'session_id': session_id,
        'acquired': True,
        'count': new_count,
        'max': MAX_CONCURRENT_SEARCHES,
        'available': MAX_CONCURRENT_SEARCHES - new_count,
        'message': '槽位已获取',
    })


@app.route('/api/concurrent/release', methods=['POST'])
def concurrent_release():
    """释放并发搜索槽位"""
    session_id = request.json.get('session_id') if request.is_json else request.form.get('session_id')
    if not session_id:
        return jsonify({'success': False, 'error': 'session_id required'}), 400

    with _concurrent_lock:
        if session_id in _active_sessions:
            del _active_sessions[session_id]

    notify_concurrency_change()

    return jsonify({
        'success': True,
        'count': get_concurrent_count(),
        'max': MAX_CONCURRENT_SEARCHES,
    })


@app.route('/api/concurrent/heartbeat', methods=['POST'])
def concurrent_heartbeat():
    """心跳保活"""
    session_id = request.json.get('session_id') if request.is_json else request.form.get('session_id')
    if not session_id:
        return jsonify({'success': False, 'error': 'session_id required'}), 400

    with _concurrent_lock:
        if session_id in _active_sessions:
            _active_sessions[session_id]['last_heartbeat'] = time.time()
            return jsonify({'success': True, 'count': len(_active_sessions)})
    return jsonify({'success': False, 'error': 'session not found'}), 404


# ============ 活动用户追踪 API ============
@app.route('/api/presence/ping', methods=['POST'])
def presence_ping():
    """前端页面心跳：报告访客在线状态。

    请求体: {visitor_id?, page?}
    响应: {success, visitor_id, is_new_user, active_count}
    """
    data = request.get_json(silent=True) or {}
    visitor_id = data.get('visitor_id') or str(uuid.uuid4())
    page = data.get('page', 'home')
    now = time.time()

    known = _get_known_visitors()
    is_new_user = visitor_id not in known

    with _visitors_lock:
        # 先清理过期访客，避免返回包含僵尸条目的计数
        expired = [vid for vid, info in _active_visitors.items()
                   if now - info['last_seen'] > VISITOR_TIMEOUT]
        for vid in expired:
            del _active_visitors[vid]

        if visitor_id not in _active_visitors:
            _active_visitors[visitor_id] = {
                'first_seen': now,
                'last_seen': now,
                'page': page,
                'is_new': is_new_user,
            }
            # 新访客加入时通知所有在线用户
            _mark_visitor_known(visitor_id)
            should_notify = True
            extra = {'event': 'user_joined', 'new_visitor_id': visitor_id[:8]}
        else:
            _active_visitors[visitor_id]['last_seen'] = now
            _active_visitors[visitor_id]['page'] = page
            should_notify = False
            extra = None
        active_count = len(_active_visitors)

    if should_notify:
        notify_visitor_change(extra)

    return jsonify({
        'success': True,
        'visitor_id': visitor_id,
        'is_new_user': is_new_user,
        'active_count': active_count,
    })


@app.route('/api/presence/status')
def presence_status():
    """获取当前活动用户状态（供管理后台轮询）"""
    visitors = get_active_visitors()
    return jsonify({
        'success': True,
        'active_count': len(visitors),
        'new_count': sum(1 for _, info in visitors if info.get('is_new')),
        'visitors': [
            {
                'id': vid[:8],
                'page': info.get('page', ''),
                'first_seen': info.get('first_seen'),
                'last_seen': info.get('last_seen'),
                'is_new': info.get('is_new', False),
            } for vid, info in visitors
        ],
    })


@app.route('/api/concurrent/stream')
def concurrent_stream():
    """SSE实时推送并发状态变化"""
    def event_stream():
        client = {
            'queue': [],
            'event': threading.Event(),
        }
        _sse_clients.append(client)
        try:
            # 先发送当前状态
            count = get_concurrent_count()
            yield f"data: {json.dumps({'type': 'concurrency', 'count': count, 'max': MAX_CONCURRENT_SEARCHES, 'available': MAX_CONCURRENT_SEARCHES - count})}\n\n"

            while True:
                client['event'].wait(timeout=30)
                client['event'].clear()
                while client['queue']:
                    msg = client['queue'].pop(0)
                    yield msg
                # 定期发送心跳
                count = get_concurrent_count()
                yield f"data: {json.dumps({'type': 'heartbeat', 'count': count, 'max': MAX_CONCURRENT_SEARCHES, 'available': MAX_CONCURRENT_SEARCHES - count})}\n\n"
        except GeneratorExit:
            if client in _sse_clients:
                _sse_clients.remove(client)

    return Response(
        stream_with_context(event_stream()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )


@app.route('/api/admin/concurrent/max', methods=['POST'])
def set_concurrent_max():
    """设置最大并发数（后台管理）"""
    if not is_authenticated():
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    global MAX_CONCURRENT_SEARCHES
    data = request.json or {}
    new_max = int(data.get('max', 3))
    if new_max < 1:
        new_max = 1
    if new_max > 50:
        new_max = 50
    MAX_CONCURRENT_SEARCHES = new_max
    notify_concurrency_change()
    return jsonify({
        'success': True,
        'max': MAX_CONCURRENT_SEARCHES,
        'recommended': recommend_max_concurrent(),
    })


# ============ 系统监控 API ============
@app.route('/api/monitoring/current')
def monitoring_current():
    """获取当前系统监控指标"""
    metrics = collect_metrics()
    return jsonify({'success': True, 'data': metrics})


@app.route('/api/monitoring/history')
def monitoring_history():
    """获取历史监控指标"""
    with _monitoring_lock:
        history = list(_metrics_history)
    return jsonify({
        'success': True,
        'data': history,
        'interval_seconds': 5,
    })


# ============ 增强版统计 API（用于饼图） ============
@app.route('/api/admin/stats_enhanced')
def stats_enhanced():
    """增强版统计：包含文件级别的记录数和列数贡献（用于饼图）"""
    if not is_authenticated():
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    basic = db_manager.get_stats()

    # 文件级别的记录数统计
    from database import get_db
    conn = get_db()
    try:
        files = conn.execute(
            "SELECT id, filename, original_filename, upload_date, total_rows, file_type, stage "
            "FROM uploaded_files ORDER BY upload_date DESC"
        ).fetchall()

        total_records = 0
        for f in files:
            total_records += f['total_rows'] or 0

        # 计算每个文件对列数的贡献
        file_column_contributions = []
        for f in files:
            rows = conn.execute(
                "SELECT data FROM parts_data WHERE file_id = ? LIMIT 1", [f['id']]
            ).fetchall()
            col_count = 0
            if rows:
                data = json.loads(rows[0]['data'])
                col_count = len([k for k, v in data.items() if v not in (None, '', 'N/A')])
            file_column_contributions.append({
                'file_id': f['id'],
                'filename': f['original_filename'],
                'columns': col_count,
            })

        total_columns = len(conn.execute(
            "SELECT id FROM unified_columns"
        ).fetchall())
    finally:
        conn.close()

    file_details = []
    for f in files:
        file_size_kb = 0
        filepath = os.path.join(UPLOAD_TEMP_DIR, f['filename'])
        if os.path.exists(filepath):
            file_size_kb = round(os.path.getsize(filepath) / 1024, 1)

        col_info = next((c for c in file_column_contributions if c['file_id'] == f['id']), None)

        file_details.append({
            'id': f['id'],
            'filename': f['original_filename'],
            'system_filename': f['filename'],
            'upload_date': f['upload_date'],
            'total_rows': f['total_rows'] or 0,
            'file_type': f['file_type'] or 'supplementary',
            'stage': f['stage'] or '',
            'file_size_kb': file_size_kb,
            'columns_contributed': col_info['columns'] if col_info else 0,
            'record_percent': round((f['total_rows'] or 0) / total_records * 100, 1) if total_records > 0 else 0,
        })

    return jsonify({
        'success': True,
        'data': {
            'basic': basic,
            'total_records': total_records,
            'total_columns': total_columns,
            'file_count': len(files),
            'file_details': file_details,
        }
    })


# ============ Phase 3: Leader 诊断 API ============
@app.route('/api/admin/leader/status')
def admin_leader_status():
    """Phase3 领导者选举诊断接口 (需管理员认证)"""
    if not is_authenticated():
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    info = {
        'module_loaded': bool(_leader_elector),
    }
    if _leader_elector:
        info['enabled'] = _leader_elector.enabled
        info['is_leader'] = bool(_leader_elector.is_leader)
        info['redis_connected'] = _leader_elector._redis is not None
        info['key'] = _leader_elector.lock_key
        info['ttl_seconds'] = _leader_elector.lock_ttl
    else:
        info['note'] = 'Leader Election disabled: single-node mode'
    info['delta_last_update'] = _delta_last_update
    return jsonify({'success': True, 'data': info})


if __name__ == '__main__':
    cache_status = 'enabled (Redis)' if (_cache_enabled and _redis_client is not None) else 'disabled (降级为无缓存)'
    delta_status = 'enabled' if DELTA_REFRESH_INTERVAL and DELTA_REFRESH_INTERVAL > 0 else 'disabled'
    frontend_ok = os.path.isdir(FRONTEND_DIR)
    print("=" * 68)
    print(f"  {APP_NAME}")
    print(f"  ├─ 版本:         {APP_VERSION}")
    print(f"  ├─ 监听:         {FLASK_HOST}:{FLASK_PORT}")
    print(f"  ├─ 数据库:       {DB_TYPE}")
    print(f"  ├─ 缓存:         {cache_status}")
    print(f"  ├─ Session:      {SESSION_TYPE}")
    print(f"  ├─ Delta预计算:  {delta_status} (间隔 {DELTA_REFRESH_INTERVAL}s)")
    print(f"  ├─ Metrics:      {'/metrics' if _metrics else 'disabled'}")
    print(f"  ├─ LeaderElec:   {'enabled' if _leader_elector else 'disabled'}", end="")
    if _leader_elector:
        print(f"  is_leader={bool(_leader_elector.is_leader)} redis={_leader_elector._redis is not None}")
    else:
        print()
    if _ollama_lb:
        lb_nodes = _ollama_lb.get_all_nodes_status()
        lb_healthy = sum(1 for n in lb_nodes if n.get('healthy'))
        print(f"  ├─ Ollama LB:    {lb_healthy}/{len(lb_nodes)} nodes healthy")
    else:
        print(f"  ├─ Ollama LB:    disabled")
    print(f"  ├─ 前端目录:     {FRONTEND_DIR}  exists={frontend_ok}")
    print(f"  ├─ Query:        http://localhost:{FLASK_PORT}/")
    print(f"  ├─ Admin:        http://localhost:{FLASK_PORT}/admin")
    print(f"  ├─ Health:       http://localhost:{FLASK_PORT}/api/health")
    if _metrics:
        print(f"  └─ Metrics:      http://localhost:{FLASK_PORT}/metrics")
    print("  (管理员密码不在此处显示, 通过环境变量 ADMIN_PASSWORD 设置)")
    print("=" * 68)
    # 本地/原生运行: Flask 内置服务器 (threaded 提升并发);
    # Docker 生产环境通过 Dockerfile 使用 gunicorn 多进程运行。
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False, threaded=True)
