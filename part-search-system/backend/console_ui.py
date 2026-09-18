# -*- coding: utf-8 -*-
"""
服务控制台 UI (console_ui.py)
================================
在终端提供友好的运行反馈:
  1. 启动成功横幅 (含完成时间戳)
  2. 本地 / 局域网访问地址
  3. 依赖服务状态 (服务名 / 状态 / 响应时间)
  4. 交互式菜单: 重启 / 停止 / 刷新状态 / 退出菜单
  5. 服务状态按需手动刷新 (菜单按 f), 不做自动刷屏

设计说明:
  - 仅使用标准库 (socket / urllib / sqlite3 / threading / subprocess ...), 无新增依赖。
  - Windows 10+ 默认支持 ANSI 转义色; 启动时调用 ctypes 开启 VT 模式, 失败则退化为无色。
  - Flask 由 werkzeug.make_server 在后台线程运行, 主线程承担菜单循环,
    从而可"停止服务 / 重启进程 / 退出菜单"。
  - 非交互环境 (无 TTY, 如重定向 / 守护进程) 下退化为"打印横幅 + 阻塞运行", 不弹菜单。
"""

import os
import sys
import time
import socket
import sqlite3
import subprocess
import re
import unicodedata
import urllib.request
import urllib.error
from datetime import datetime

# 绕过系统代理的 opener (本机服务探测不应走公司代理, 否则会长时间超时)
_no_proxy_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ---------------------------------------------------------------------------
# ANSI 颜色
# ---------------------------------------------------------------------------
_COLORS = {
    'reset': '\033[0m',
    'bold': '\033[1m',
    'dim': '\033[2m',
    'red': '\033[31m',
    'green': '\033[32m',
    'yellow': '\033[33m',
    'blue': '\033[34m',
    'magenta': '\033[35m',
    'cyan': '\033[36m',
    'gray': '\033[90m',
    'bright_green': '\033[92m',
    'bright_yellow': '\033[93m',
    'bright_red': '\033[91m',
}

_use_color = sys.stdout.isatty()


def _enable_windows_ansi():
    """Windows 10+ 开启控制台 VT 处理 (ENABLE_VIRTUAL_TERMINAL_PROCESSING)。"""
    global _use_color
    if not _use_color:
        return
    if os.name == 'nt':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # STD_OUTPUT_HANDLE = -11
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            # 不支持则降级为无色输出
            _use_color = False


def c(text, color):
    """给文本上色 (不支持时原样返回)。"""
    if not _use_color or color not in _COLORS:
        return text
    return _COLORS[color] + text + _COLORS['reset']


# ---------------------------------------------------------------------------
# 网络工具
# ---------------------------------------------------------------------------
def get_lan_ip():
    """获取本机局域网 IPv4。通过 UDP 连接外部地址的方式拿到默认出口 IP, 不产生真实流量。"""
    ip = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        ip = None
    if not ip:
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                addr = info[4][0]
                if not addr.startswith('127.'):
                    ip = addr
                    break
        except Exception:
            ip = None
    return ip or '127.0.0.1'


def _http_probe(url, timeout=2, method='GET'):
    """探测一个 HTTP 端点, 返回 (ok: bool, elapsed_ms: int, detail: str)。
    使用绕过系统代理的 opener, 避免本机/局域网探测被代理拖慢。"""
    # localhost 会先尝试 IPv6(::1) 再回退 IPv4, 服务未启动时翻倍等待; 统一用 127.0.0.1
    url = url.replace('//localhost:', '//127.0.0.1:').replace('//[::1]:', '//127.0.0.1:')
    start = time.perf_counter()
    try:
        req = urllib.request.Request(url, method=method)
        with _no_proxy_opener.open(req, timeout=timeout) as resp:
            elapsed = int((time.perf_counter() - start) * 1000)
            ok = 200 <= resp.status < 400
            return ok, elapsed, 'HTTP %s' % resp.status
    except urllib.error.HTTPError as e:
        # 服务有应答但返回 4xx/5xx 也算"在线", 只是业务异常
        elapsed = int((time.perf_counter() - start) * 1000)
        return False, elapsed, 'HTTP %s' % e.code
    except Exception as e:
        elapsed = int((time.perf_counter() - start) * 1000)
        return False, elapsed, type(e).__name__


def _tcp_probe(host_port, timeout=2):
    """TCP 连通性探测, 用于无标准 HTTP 健康端点的服务 (如 Redis)。"""
    host, port = host_port
    start = time.perf_counter()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((host, port))
        finally:
            s.close()
        return True, int((time.perf_counter() - start) * 1000), 'tcp connect ok'
    except Exception as e:
        return False, int((time.perf_counter() - start) * 1000), type(e).__name__


def _parse_hostport(url, default_port):
    """从 URL 解析 (host, port)。"""
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        host = p.hostname or 'localhost'
        port = p.port or default_port
        return host, port
    except Exception:
        return 'localhost', default_port


# ---------------------------------------------------------------------------
# 依赖服务健康检查
# ---------------------------------------------------------------------------
# 状态 -> (中文标签, 颜色, 符号)
_STATUS_STYLE = {
    'ok': ('运行中', 'bright_green', '●'),
    'down': ('未启动', 'bright_red', '○'),
    'error': ('异常', 'bright_yellow', '▲'),
    'disabled': ('已禁用', 'gray', '–'),
}


def check_database(config):
    """检查数据库连通性与响应时间。"""
    db_type = getattr(config, 'DB_TYPE', 'sqlite')
    start = time.perf_counter()
    try:
        if db_type == 'postgresql':
            import psycopg2
            conn = psycopg2.connect(
                host=getattr(config, 'POSTGRES_HOST', 'localhost'),
                port=getattr(config, 'POSTGRES_PORT', 5432),
                dbname=getattr(config, 'POSTGRES_DB', 'parts'),
                user=getattr(config, 'POSTGRES_USER', 'postgres'),
                password=getattr(config, 'POSTGRES_PASSWORD', ''),
                connect_timeout=3,
            )
            try:
                cur = conn.cursor()
                cur.execute('SELECT 1')
                cur.fetchone()
                cur.close()
            finally:
                conn.close()
        else:
            db_path = getattr(config, 'DB_PATH', None)
            if not db_path or not os.path.exists(db_path):
                return {'name': '数据库 (SQLite)', 'status': 'down',
                        'latency': None, 'detail': 'parts.db 不存在'}
            conn = sqlite3.connect(db_path, timeout=3)
            try:
                cur = conn.cursor()
                cur.execute('SELECT COUNT(*) FROM parts_data')
                count = cur.fetchone()[0]
                cur.close()
            finally:
                conn.close()
            elapsed = int((time.perf_counter() - start) * 1000)
            return {'name': '数据库 (SQLite)', 'status': 'ok',
                    'latency': elapsed, 'detail': '%s 条记录' % count}
        elapsed = int((time.perf_counter() - start) * 1000)
        return {'name': '数据库 (PostgreSQL)', 'status': 'ok',
                'latency': elapsed, 'detail': 'SELECT 1 ok'}
    except Exception as e:
        elapsed = int((time.perf_counter() - start) * 1000)
        return {'name': '数据库 (%s)' % db_type, 'status': 'error',
                'latency': elapsed, 'detail': str(e)[:40]}


def check_all_services(config, port=None, app_objects=None):
    """
    汇总所有依赖服务状态。
    app_objects: dict, 可含 db_manager / redis_client / cache_enabled /
                 ollama_lb / engine_manager / session_type
    返回 list[dict]，每项 {name, status, latency, detail}。
    """
    app_objects = app_objects or {}
    services = []

    # 0) Flask 自身 (HTTP 自探测)
    if port:
        ok, ms, detail = _http_probe('http://127.0.0.1:%s/api/health' % port, timeout=3)
        services.append({'name': 'Web 服务 (Flask)',
                         'status': 'ok' if ok else 'error',
                         'latency': ms, 'detail': detail})

    # 1) 数据库
    dbm = app_objects.get('db_manager')
    if dbm is not None:
        start = time.perf_counter()
        try:
            stats = dbm.get_stats()
            ms = int((time.perf_counter() - start) * 1000)
            services.append({'name': '数据库 (%s)' % getattr(config, 'DB_TYPE', 'sqlite'),
                             'status': 'ok', 'latency': ms,
                             'detail': '%s 条 / %s 件号' % (
                                 stats.get('total_records', 0),
                                 stats.get('unique_parts', 0))})
        except Exception as e:
            ms = int((time.perf_counter() - start) * 1000)
            services.append({'name': '数据库 (%s)' % getattr(config, 'DB_TYPE', 'sqlite'),
                             'status': 'error', 'latency': ms,
                             'detail': str(e)[:40]})
    else:
        services.append(check_database(config))

    # 2) Redis 缓存
    cache_enabled = app_objects.get('cache_enabled', False)
    redis_client = app_objects.get('redis_client')
    if cache_enabled and redis_client is not None:
        start = time.perf_counter()
        try:
            redis_client.ping()
            ms = int((time.perf_counter() - start) * 1000)
            services.append({'name': '缓存 (Redis)', 'status': 'ok',
                             'latency': ms, 'detail': 'PING -> PONG'})
        except Exception as e:
            ms = int((time.perf_counter() - start) * 1000)
            services.append({'name': '缓存 (Redis)', 'status': 'down',
                             'latency': ms, 'detail': str(e)[:40]})
    else:
        # 即便未启用, 也探测一下端口是否在跑 (提示用户可启用)
        host, rport = _parse_hostport(getattr(config, 'REDIS_URL', 'redis://localhost:6379/0'), 6379)
        ok, ms, _ = _tcp_probe((host, rport), timeout=1)
        services.append({'name': '缓存 (Redis)',
                         'status': 'disabled' if not ok else 'down',
                         'latency': None,
                         'detail': '未启用缓存' if not ok else '端口在线但未启用'})

    # 3) Ollama 负载均衡节点
    ollama_lb = app_objects.get('ollama_lb')
    if ollama_lb is not None:
        try:
            nodes = ollama_lb.get_all_nodes_status()
            healthy = sum(1 for n in nodes if n.get('healthy'))
            total = len(nodes)
            if total == 0:
                services.append({'name': 'Ollama 引擎', 'status': 'disabled',
                                 'latency': None, 'detail': '无节点'})
            else:
                # 取第一个健康节点的 URL 探一次活, 得到响应时间
                latency = None
                probe_url = getattr(config, 'OLLAMA_URL', 'http://localhost:11434')
                ok, ms, _ = _http_probe(probe_url.rstrip('/') + '/api/tags', timeout=2)
                latency = ms
                status = 'ok' if healthy == total else ('down' if healthy == 0 else 'error')
                services.append({'name': 'Ollama 引擎', 'status': status,
                                 'latency': latency,
                                 'detail': '%s/%s 节点健康' % (healthy, total)})
        except Exception as e:
            services.append({'name': 'Ollama 引擎', 'status': 'error',
                             'latency': None, 'detail': str(e)[:40]})
    else:
        url = getattr(config, 'OLLAMA_URL', 'http://localhost:11434')
        ok, ms, _ = _http_probe(url.rstrip('/') + '/api/tags', timeout=2)
        services.append({'name': 'Ollama 引擎',
                         'status': 'ok' if ok else 'down', 'latency': ms,
                         'detail': '在线' if ok else '未启动'})

    # 4) vLLM 引擎
    health_url = getattr(config, 'VLLM_HEALTH_URL', 'http://localhost:8000/health')
    ok, ms, detail = _http_probe(health_url, timeout=3)
    services.append({'name': 'vLLM 引擎',
                     'status': 'ok' if ok else 'down', 'latency': ms,
                     'detail': detail if ok else '未启动'})

    return services


# ---------------------------------------------------------------------------
# 渲染: 横幅 + 状态面板 + 菜单
# ---------------------------------------------------------------------------
_ANSI_RE = re.compile(r'\033\[[0-9;]*m')
# 常见 emoji 区间 (显示宽度按 2 计, 与终端等宽对齐)
_EMOJI_RANGES = [
    (0x1F300, 0x1FAFF), (0x2600, 0x27BF), (0x1F1E6, 0x1F1FF),
    (0x2190, 0x21FF), (0x2B00, 0x2BFF), (0xFE0F, 0xFE0F),
]


def _char_width(ch):
    """单个字符在等宽终端里的显示列宽: 中文/全角/emoji=2, 其余=1。"""
    cp = ord(ch)
    if any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES):
        return 2
    if unicodedata.combining(ch) or unicodedata.east_asian_width(ch) in ('W', 'F'):
        return 2
    return 1


def _visible_len(text):
    """去掉 ANSI 转义后的可视列宽 (用于对齐)。"""
    plain = _ANSI_RE.sub('', text)
    return sum(_char_width(ch) for ch in plain)


def _pad(text, width):
    return text + ' ' * max(0, width - _visible_len(text))


def print_banner(config, port):
    """启动成功横幅。"""
    lan = get_lan_ip()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    version = getattr(config, 'APP_VERSION', 'dev')

    inner = 62
    line = '═' * inner

    def row(content, color=None):
        body = _pad(content, inner)
        if color:
            body = c(body, color)
        print(c('║', 'cyan') + body + c('║', 'cyan'))

    print(c('╔' + line + '╗', 'cyan'))
    row('  🚀 Vanea Part Search System (F-Brain)', 'bright_green')
    ver = version if str(version).lower().startswith('build') else ('build-' + str(version))
    row('  版本 %s' % ver)
    row('  ✅ 服务启动成功', 'bright_green')
    row('  🕐 启动时间: %s' % now)
    print(c('╠' + line + '╣', 'cyan'))
    row('  🌐 本地访问:   ' + c('http://localhost:%s' % port, 'bright_yellow'))
    row('  🔗 局域网访问: ' + c('http://%s:%s' % (lan, port), 'bright_yellow'))
    row('  🛠  管理后台:   ' + c('http://localhost:%s/admin' % port, 'gray'))
    print(c('╚' + line + '╝', 'cyan'))
    print()


def print_status_panel(services, title='依赖服务状态'):
    """渲染状态表格: 服务名 / 状态 / 响应时间 / 详情。"""
    now = datetime.now().strftime('%H:%M:%S')
    print(c('┌─[%s] %s %s' % (now, title, '─' * 40), 'blue'))

    name_w = max([_visible_len(s['name']) for s in services] + [18])
    for s in services:
        label, color, sym = _STATUS_STYLE.get(s['status'], ('未知', 'gray', '?'))
        lat = ('%sms' % s['latency']) if s.get('latency') is not None else '—'
        status_cell = c('%s %s' % (sym, label), color)
        detail = s.get('detail') or ''
        row = '│ ' + _pad(s['name'], name_w + 2)
        row += _pad(status_cell, 14)
        row += _pad(c(_pad(lat, 8), 'cyan'), 10)
        row += c(detail, 'gray')
        print(row)

    # 汇总
    ok_n = sum(1 for s in services if s['status'] == 'ok')
    down_n = sum(1 for s in services if s['status'] == 'down')
    err_n = sum(1 for s in services if s['status'] == 'error')
    dis_n = sum(1 for s in services if s['status'] == 'disabled')
    summary = '运行中 %s · 未启动 %s · 异常 %s · 禁用 %s · 共 %s' % (
        c(str(ok_n), 'bright_green'),
        c(str(down_n), 'bright_red' if down_n else 'gray'),
        c(str(err_n), 'bright_yellow' if err_n else 'gray'),
        c(str(dis_n), 'gray'),
        len(services))
    print(c('└─ 汇总: ' + summary + ' ' + '─' * 20, 'blue'))
    print()


def print_menu():
    """打印交互菜单。"""
    print(c('  操作菜单 (输入字母后回车):', 'magenta'))
    print('   ' + c('[r]', 'bright_yellow') + ' 重启服务   '
          + c('[s]', 'bright_red') + ' 停止服务   '
          + c('[f]', 'bright_green') + ' 刷新状态   '
          + c('[x]', 'gray') + ' 退出菜单')
    print(c('  (状态不会自动刷新; 需要时按 f 手动查看; 退出菜单后服务继续后台运行)', 'dim'))
    print()


# ---------------------------------------------------------------------------
# 控制台主控 (菜单循环; 状态仅手动刷新)
# ---------------------------------------------------------------------------


def _restart_process():
    """重启当前进程: 用相同的解释器与参数另起一个新进程, 然后退出本进程。
    通过 VANEA_RESTART 环境变量通知新进程延迟监听, 避免与正在 shutdown 的旧进程抢端口。"""
    print(c('  ↻ 正在重启服务…', 'bright_yellow'))
    env = dict(os.environ)
    env['VANEA_RESTART'] = '1'
    try:
        if os.name == 'nt':
            # Windows: 新开独立控制台, 避免随父进程退出
            subprocess.Popen([sys.executable] + sys.argv,
                             cwd=os.getcwd(), env=env,
                             creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))
        else:
            subprocess.Popen([sys.executable] + sys.argv, cwd=os.getcwd(),
                             env=env, start_new_session=True)
    except Exception as e:
        print(c('  ✗ 重启失败: %s' % e, 'bright_red'))
        return False
    return True


def run_console(server, config, port, app_objects=None):
    """
    启动控制台主循环。server 为已 serve_forever(后台线程) 的 werkzeug server。
    服务状态不自动刷新, 仅在启动时与用户按 f 时打印。
    返回控制码: 'stop' / 'exit_menu' / 'restart'。
    """
    _enable_windows_ansi()

    print_banner(config, port)
    services = check_all_services(config, port=port, app_objects=app_objects)
    print_status_panel(services)
    print_menu()

    while True:
        try:
            line = sys.stdin.readline()
        except KeyboardInterrupt:
            print()
            print(c('  ◼ 正在安全停止服务…', 'bright_red'))
            try:
                server.shutdown()
            finally:
                print(c('  ✓ 服务已停止。', 'bright_red'))
            return 'stop'
        # EOF (输入流被关闭): 退出菜单, 服务继续后台运行
        if line == '':
            print(c('  输入流已关闭, 退出菜单; 服务继续后台运行。(Ctrl+C 可结束进程)', 'gray'))
            return 'exit_menu'
        key = line.strip().lower()

        if key in ('f', ''):
            services = check_all_services(config, port=port, app_objects=app_objects)
            print_status_panel(services, title='手动刷新')
            print_menu()

        elif key == 'x':
            print(c('  已退出菜单, 服务继续在后台运行。(Ctrl+C 可结束进程)', 'gray'))
            return 'exit_menu'

        elif key == 's':
            print(c('  ◼ 正在安全停止服务…', 'bright_red'))
            try:
                server.shutdown()
            finally:
                print(c('  ✓ 服务已停止。', 'bright_red'))
            return 'stop'

        elif key == 'r':
            if _restart_process():
                try:
                    server.shutdown()
                finally:
                    pass
                return 'restart'
            print_menu()

        else:
            print(c('  无效指令, 请输入 r / s / f / x', 'yellow'))
            print_menu()
