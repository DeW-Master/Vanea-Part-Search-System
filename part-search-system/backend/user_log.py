# -*- coding: utf-8 -*-
"""
用户搜索日志 (按客户端 IP 为用户 ID)

- 每个用户一个 JSONL 文件: data/user_logs/<ip 转义>.jsonl, 每行一条精简搜索记录:
  {ts, ip, session_id, query, mode, sql, result_count, duration_ms, ok, answer_preview}
- 内存索引维护各 IP 的搜索次数 / 首末活跃时间 / 最近会话 / 错误数,
  供监控页"用户数量"下钻; 索引在模块加载时从日志文件重建。
- 文件追加写, 无第三方依赖; 网页端通过 /api/monitoring/user_log 直接查看。
"""

import os
import re
import json
import threading
import datetime

try:
    from config import DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')

LOG_DIR = os.path.join(DATA_DIR, 'user_logs')

_lock = threading.Lock()
# ip -> {'count', 'first_ts', 'last_ts', 'last_session', 'errors'}
_stats = {}

# 文件名里不允许出现的字符 (Windows): : \ / * ? " < > |
_UNSAFE = re.compile(r'[:\\/*?"<>|]')


def _safe_ip(ip):
    """把 IP 转成安全的文件名片段 (IPv6 的冒号 -> '_')。"""
    return _UNSAFE.sub('_', (ip or 'unknown'))


def _log_path(ip):
    return os.path.join(LOG_DIR, _safe_ip(ip) + '.jsonl')


def _ensure_dir():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except Exception:
        pass


def record_search(ip, query, mode='', sql='', result_count=0,
                  duration_ms=0, ok=True, session_id='', answer_preview=''):
    """记录一次用户搜索。精简字段, 单行 JSON。"""
    ip = ip or 'unknown'
    entry = {
        'ts': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'ip': ip,
        'session_id': (session_id or '')[:16],
        'query': (query or '')[:300],
        'mode': mode or '',
        'sql': (sql or '')[:500],
        'result_count': int(result_count or 0),
        'duration_ms': int(duration_ms or 0),
        'ok': bool(ok),
        'answer': (answer_preview or '')[:160],
    }
    line = json.dumps(entry, ensure_ascii=False)
    with _lock:
        st = _stats.get(ip)
        if st is None:
            st = {'count': 0, 'first_ts': entry['ts'], 'last_ts': entry['ts'],
                  'last_session': entry['session_id'], 'errors': 0}
            _stats[ip] = st
        st['count'] += 1
        st['last_ts'] = entry['ts']
        if not st.get('first_ts'):
            st['first_ts'] = entry['ts']
        if entry['session_id']:
            st['last_session'] = entry['session_id']
        if not ok:
            st['errors'] += 1
        try:
            _ensure_dir()
            with open(_log_path(ip), 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            # 日志失败不影响主请求
            pass


def _rebuild_stats():
    """启动/首次查询时从日志文件重建内存索引 (只读每个文件的首尾行, 控制开销)。"""
    global _stats
    stats = {}
    try:
        if not os.path.isdir(LOG_DIR):
            _stats = stats
            return
        for fn in os.listdir(LOG_DIR):
            if not fn.endswith('.jsonl'):
                continue
            path = os.path.join(LOG_DIR, fn)
            count = 0
            first_ts = ''
            last_ts = ''
            last_session = ''
            errors = 0
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except Exception:
                            continue
                        count += 1
                        if not first_ts:
                            first_ts = e.get('ts', '')
                        last_ts = e.get('ts', last_ts)
                        if e.get('session_id'):
                            last_session = e['session_id']
                        if not e.get('ok', True):
                            errors += 1
            except Exception:
                continue
            ip = fn[:-len('.jsonl')]
            stats[ip] = {'count': count, 'first_ts': first_ts, 'last_ts': last_ts,
                         'last_session': last_session, 'errors': errors}
    except Exception:
        pass
    _stats = stats


def list_users():
    """返回用户列表 (按最近活跃倒序)。"""
    with _lock:
        if not _stats:
            _rebuild_stats()
        users = [{'ip': ip, **st} for ip, st in _stats.items()]
    users.sort(key=lambda u: u.get('last_ts', ''), reverse=True)
    return users


def read_user_log(ip, limit=100):
    """读取某 IP 的最近 limit 条搜索记录 (倒序返回, 最新在前)。"""
    ip = ip or 'unknown'
    path = _log_path(ip)
    entries = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        return []
    except Exception:
        return entries[-limit:]
    return list(reversed(entries[-limit:]))


def render_user_log_text(ip, limit=200):
    """生成纯文本日志视图 (浏览器直接打开)。"""
    entries = read_user_log(ip, limit=limit)
    lines = []
    lines.append('=' * 72)
    lines.append('Vanea Part Search - 用户搜索日志')
    lines.append('用户 IP : %s' % ip)
    lines.append('生成时间: %s' % datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    lines.append('记录条数: %d (最近 %d 条)' % (len(entries), limit))
    lines.append('=' * 72)
    for i, e in enumerate(entries, 1):
        lines.append('')
        lines.append('[%d] %s  |  会话 #%s  |  模式: %s  |  耗时: %sms  |  结果: %s 行  |  %s' % (
            i,
            e.get('ts', ''),
            (e.get('session_id') or '-')[:8],
            e.get('mode') or '-',
            e.get('duration_ms', 0),
            e.get('result_count', 0),
            'OK' if e.get('ok', True) else 'ERROR',
        ))
        lines.append('    提问: %s' % (e.get('query') or ''))
        if e.get('sql'):
            lines.append('    SQL : %s' % e['sql'])
        if e.get('answer'):
            lines.append('    回复: %s' % e['answer'])
    lines.append('')
    lines.append('=' * 72)
    return '\n'.join(lines)
