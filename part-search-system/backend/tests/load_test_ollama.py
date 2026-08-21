# -*- coding: utf-8 -*-
"""
van.ea 车辆零件智能查询系统 - Ollama 5 用户并发负载测试

目标
----
1. 模拟 5 名用户同时调用 /api/agent/query 触发 Ollama 推理
2. 实时采集服务器/进程资源 (CPU/内存/磁盘 I/O/网络)
3. 采集 Ollama 节点状态 (健康/失败次数/活跃请求)
4. 输出 JSON 原始数据 + Markdown 报告 (含性能对比与基线)

使用
----
    python backend/tests/load_test_ollama.py
    python backend/tests/load_test_ollama.py --users 5 --queries-per-user 4 --duration 120
    python backend/tests/load_test_ollama.py --baseline results/baseline.json --out results/

设计
----
- 用户层: 使用 ThreadPoolExecutor 并发模拟 N 个虚拟用户
- 资源层: 独立守护线程, 每秒采样 psutil 指标 + Ollama LB 状态
- 报告层: 自动生成 results/<timestamp>/ 目录, 包含:
    - raw_requests.jsonl     # 每个请求一行
    - resource_samples.jsonl # 每秒一行
    - ollama_lb_samples.jsonl# 每次 LB 状态一行
    - summary.json           # 汇总指标
    - report.md              # 可读报告 (含对比基线)

注意
----
- 5 用户是基线; --users 10/20 用于压测极限
- 请求超时取决于 OLLAMA_REQUEST_TIMEOUT (默认 120s); qwen3:8b 单次典型 5-15s
- 资源监控覆盖整个进程 (Python + Ollama 子进程), 通过 psutil.process_iter 汇总
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil
import requests

# 让脚本既可作为模块导入, 也可独立运行
THIS_FILE = Path(__file__).resolve()
BACKEND_DIR = THIS_FILE.parent.parent
PROJECT_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 默认目标后端 (可被 CLI 覆盖)
DEFAULT_BASE_URL = os.environ.get("LOADTEST_BASE_URL", "http://localhost:5000")
DEFAULT_OLLAMA_URL = os.environ.get("LOADTEST_OLLAMA_URL", "http://localhost:11434")

# 测试用查询: 覆盖简单问候 / 简单搜索 / 复杂对比 / 表无关闲聊
# 既包含需要触发 Ollama 的真实负载, 也包含非表相关用以模拟用户行为多样性
SAMPLE_QUERIES_ZH = [
    "你好, 请介绍一下你能做什么",
    "查询零件号 A123456 的详细信息",
    "对比上周和本周的 Delta 变化",
    "列出最近上传的所有 BOM 文件",
    "搜索描述里包含 'sensor' 的零件",
    "帮我看看 PN 12345 是否有历史变更",
    "统计当前数据库一共有多少条记录",
    "分析一下近期的 PN 淘汰趋势",
    "查询供应商为 Bosch 的所有零件",
    "对比 EGS 和 EGS2 两个阶段的差异",
]

SAMPLE_QUERIES_EN = [
    "Hello, what can you do?",
    "Search part number A123456",
    "Compare this week and last week Delta",
    "List all uploaded BOM files",
    "Find parts with 'sensor' in description",
    "Has PN 12345 been changed historically?",
    "How many records are in the database?",
    "Analyze recent PN retirement trend",
    "Query all parts from supplier Bosch",
    "Compare EGS vs EGS2 phase differences",
]


# ============================================================================
# 资源监控
# ============================================================================
class ResourceMonitor:
    """每秒采样一次系统与目标进程的资源指标。

    目标进程通过监听端口识别 (Flask 5000 / Ollama 11434);
    psutil 找不到时退化为仅采集系统级指标。
    """

    def __init__(self, port_targets: List[int], interval: float = 1.0):
        self.port_targets = port_targets
        self.interval = interval
        self.samples: List[Dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._disk_prev = psutil.disk_io_counters()
        self._net_prev = psutil.net_io_counters()
        self._proc_prev: Dict[int, Tuple[psutil._common.pcpu, Any, Any]] = {}
        self._start_ts = time.time()

    def _resolve_target_pids(self) -> List[psutil.Process]:
        """根据端口查找对应进程 (Flask / Ollama 等)。"""
        targets: List[psutil.Process] = []
        for conn in psutil.net_connections(kind="inet"):
            if conn.status != psutil.CONN_LISTEN:
                continue
            laddr = conn.laddr
            if laddr and laddr.port in self.port_targets:
                try:
                    p = psutil.Process(conn.pid)
                    if p not in targets:
                        targets.append(p)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        return targets

    def _snapshot(self) -> Dict[str, Any]:
        now = time.time()
        elapsed = now - self._start_ts
        cpu_pct = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        disk = psutil.disk_io_counters()
        net = psutil.net_io_counters()

        # 计算 delta 速率
        read_kbs = (disk.read_bytes - self._disk_prev.read_bytes) / 1024.0 / self.interval
        write_kbs = (disk.write_bytes - self._disk_prev.write_bytes) / 1024.0 / self.interval
        rx_kbs = (net.bytes_recv - self._net_prev.bytes_recv) / 1024.0 / self.interval
        tx_kbs = (net.bytes_sent - self._net_prev.bytes_sent) / 1024.0 / self.interval
        self._disk_prev = disk
        self._net_prev = net

        proc_summaries: List[Dict[str, Any]] = []
        for p in self._resolve_target_pids():
            try:
                with p.oneshot():
                    proc_cpu = p.cpu_percent(interval=None)
                    mem_info = p.memory_info()
                    proc_summaries.append({
                        "pid": p.pid,
                        "name": p.name(),
                        "cpu_pct": round(proc_cpu, 2),
                        "rss_mb": round(mem_info.rss / 1024 / 1024, 2),
                        "vms_mb": round(mem_info.vms / 1024 / 1024, 2),
                        "num_threads": p.num_threads(),
                        "status": p.status(),
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return {
            "t": round(elapsed, 2),
            "wall_ts": datetime.now().isoformat(timespec="seconds"),
            "system": {
                "cpu_pct": round(cpu_pct, 2),
                "mem_pct": round(vm.percent, 2),
                "mem_used_mb": round((vm.total - vm.available) / 1024 / 1024, 1),
                "mem_total_mb": round(vm.total / 1024 / 1024, 1),
                "disk_read_kbs": round(read_kbs, 1),
                "disk_write_kbs": round(write_kbs, 1),
                "net_rx_kbs": round(rx_kbs, 1),
                "net_tx_kbs": round(tx_kbs, 1),
            },
            "processes": proc_summaries,
        }

    def _run(self) -> None:
        # 预热 cpu_percent (第一次调用会返回 0)
        psutil.cpu_percent(interval=None)
        for p in self._resolve_target_pids():
            try:
                p.cpu_percent(interval=None)
            except Exception:
                pass
        while not self._stop.is_set():
            try:
                self.samples.append(self._snapshot())
            except Exception as exc:
                self.samples.append({"t": round(time.time() - self._start_ts, 2), "error": str(exc)})
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="ResourceMonitor")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


# ============================================================================
# Ollama LB 状态采样
# ============================================================================
class OllamaLBProbe:
    """持续请求 /api/health 抓取 Ollama LB 节点状态 (在 health.ollama.nodes 下)。"""

    def __init__(self, base_url: str, interval: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.interval = interval
        self.samples: List[Dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start_ts = time.time()

    def _run(self) -> None:
        while not self._stop.is_set():
            t = round(time.time() - self._start_ts, 2)
            sample: Dict[str, Any] = {"t": t}
            try:
                r = requests.get(f"{self.base_url}/api/health", timeout=5)
                sample["http_status"] = r.status_code
                if r.status_code == 200:
                    data = r.json()
                    sample["ollama_lb"] = data.get("ollama")
                    sample["redis_ok"] = bool(data.get("redis", {}).get("available"))
                    sample["db_ok"] = bool(data.get("database", {}).get("ok"))
                    sample["app_status"] = data.get("status")
            except Exception as exc:
                sample["error"] = str(exc)
            self.samples.append(sample)
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="OllamaLBProbe")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


class GPUMonitor:
    """通过 nvidia-smi 采集 GPU 利用率/显存/温度 (若可用)。"""

    def __init__(self, interval: float = 2.0):
        self.interval = interval
        self.samples: List[Dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start_ts = time.time()
        self.available = shutil.which("nvidia-smi") is not None

    def _query(self) -> List[Dict[str, Any]]:
        if not self.available:
            return []
        try:
            out = subprocess.check_output([
                "nvidia-smi",
                "--query-gpu=index,name,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,power.draw",
                "--format=csv,noheader,nounits",
            ], timeout=5, stderr=subprocess.DEVNULL).decode("utf-8", errors="replace")
        except Exception as exc:
            return [{"error": str(exc)}]
        rows: List[Dict[str, Any]] = []
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 7:
                continue
            try:
                rows.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "util_gpu_pct": float(parts[2]),
                    "util_mem_pct": float(parts[3]),
                    "mem_used_mb": float(parts[4]),
                    "mem_total_mb": float(parts[5]),
                    "temp_c": float(parts[6]),
                    "power_w": float(parts[7]) if len(parts) > 7 and parts[7] else None,
                })
            except ValueError:
                continue
        return rows

    def _run(self) -> None:
        while not self._stop.is_set():
            t = round(time.time() - self._start_ts, 2)
            try:
                self.samples.append({"t": t, "gpus": self._query()})
            except Exception as exc:
                self.samples.append({"t": t, "error": str(exc)})
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self._thread is None or not self.available:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="GPUMonitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


class ProcessCPUSampler:
    """绕过 Windows 上 psutil.cpu_percent 的 0 陷阱, 用进程级 cpu_times 计算增量。

    注意: Windows 上 psutil 的 cpu_percent(interval=None) 几乎总是返回 0,
    这是 psutil 已知行为。我们改用 cpu_times (total CPU time 累计值) 计算增量,
    更可靠且与 Get-Process CPU Time 一致。
    """

    def __init__(self, port_targets: List[int], interval: float = 1.0):
        self.port_targets = port_targets
        self.interval = interval
        self.samples: List[Dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._prev: Dict[int, float] = {}
        self._prev_ts: float = time.time()
        self._start_ts = time.time()

    def _resolve_pids(self) -> Dict[int, str]:
        out: Dict[int, str] = {}
        for conn in psutil.net_connections(kind="inet"):
            if conn.status != psutil.CONN_LISTEN:
                continue
            la = conn.laddr
            if la and la.port in self.port_targets:
                try:
                    p = psutil.Process(conn.pid)
                    out[p.pid] = p.name()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        return out

    def _snapshot(self) -> Dict[str, Any]:
        now = time.time()
        elapsed_total = now - self._start_ts
        wall = now - self._prev_ts
        self._prev_ts = now
        pids = self._resolve_pids()
        proc_list: List[Dict[str, Any]] = []
        for pid, name in pids.items():
            try:
                p = psutil.Process(pid)
                with p.oneshot():
                    total = p.cpu_times().user + p.cpu_times().system
                    prev = self._prev.get(pid)
                    delta_pct = ((total - prev) * 100.0 / psutil.cpu_count() / wall) if (prev is not None and wall > 0) else 0.0
                    self._prev[pid] = total
                    mem = p.memory_info()
                    proc_list.append({
                        "pid": pid,
                        "name": name,
                        "cpu_pct": round(delta_pct, 2),
                        "rss_mb": round(mem.rss / 1024 / 1024, 2),
                        "num_threads": p.num_threads(),
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self._prev.pop(pid, None)
        return {"t": round(elapsed_total, 2), "processes": proc_list}

    def _run(self) -> None:
        # 预热: 第一次 cpu_times 作为基线
        for pid in self._resolve_pids():
            try:
                self._prev[pid] = sum(psutil.Process(pid).cpu_times()[:2])
            except Exception:
                pass
        while not self._stop.is_set():
            try:
                self.samples.append(self._snapshot())
            except Exception as exc:
                self.samples.append({"error": str(exc)})
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="ProcessCPUSampler")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


# ============================================================================
# 虚拟用户
# ============================================================================
class VirtualUser:
    """单个虚拟用户: 串行执行若干次 agent_query, 之间加入 think time。"""

    def __init__(self, user_id: int, base_url: str, queries: List[str], think_time: float,
                 timeout: float, lang: str, results: List[Dict[str, Any]],
                 results_lock: threading.Lock):
        self.user_id = user_id
        self.base_url = base_url.rstrip("/")
        self.queries = queries
        self.think_time = think_time
        self.timeout = timeout
        self.lang = lang
        self.results = results
        self.lock = results_lock
        self.session = requests.Session()

    def _fire(self, query: str) -> Dict[str, Any]:
        url = f"{self.base_url}/api/agent/query"
        payload = {"query": query, "lang": self.lang, "history": []}
        t0 = time.time()
        rec: Dict[str, Any] = {
            "user_id": self.user_id,
            "query": query,
            "start_ts": datetime.now().isoformat(timespec="milliseconds"),
            "t0": t0,
        }
        try:
            r = self.session.post(url, json=payload, timeout=self.timeout)
            elapsed = time.time() - t0
            rec.update({
                "http_status": r.status_code,
                "elapsed_ms": round(elapsed * 1000, 1),
                "ok": r.status_code == 200,
                "bytes": len(r.content),
            })
            if r.status_code == 200:
                try:
                    data = r.json()
                    rec["intent"] = (data.get("intent") or {}).get("type") if isinstance(data.get("intent"), dict) else None
                    rec["mode"] = data.get("mode")
                    rec["response_preview"] = (data.get("response") or "")[:120]
                except Exception as e:
                    rec["parse_error"] = str(e)
            else:
                rec["error_body"] = r.text[:300]
        except requests.exceptions.Timeout:
            rec.update({"ok": False, "error": "timeout", "elapsed_ms": round((time.time() - t0) * 1000, 1)})
        except Exception as e:
            rec.update({"ok": False, "error": f"{type(e).__name__}: {e}",
                        "elapsed_ms": round((time.time() - t0) * 1000, 1)})
        rec["t_done"] = time.time() - self._run_start if hasattr(self, "_run_start") else 0
        return rec

    def run(self) -> None:
        self._run_start = time.time()
        try:
            for q in self.queries:
                rec = self._fire(q)
                rec["user_id"] = self.user_id
                rec["rel_done_s"] = round(time.time() - self._run_start, 2)
                with self.lock:
                    self.results.append(rec)
                time.sleep(self.think_time)
        except Exception as exc:
            with self.lock:
                self.results.append({"user_id": self.user_id, "ok": False, "error": f"user_crashed: {exc}",
                                     "trace": traceback.format_exc()[:500]})


# ============================================================================
# 报告生成
# ============================================================================
def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def summarize_requests(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    ok_records = [r for r in records if r.get("ok")]
    failed = [r for r in records if not r.get("ok")]
    latencies = [r["elapsed_ms"] for r in ok_records if "elapsed_ms" in r]
    return {
        "total": len(records),
        "ok": len(ok_records),
        "failed": len(failed),
        "success_rate_pct": round(100.0 * len(ok_records) / max(1, len(records)), 2),
        "latency_ms": {
            "min": round(min(latencies), 1) if latencies else 0,
            "max": round(max(latencies), 1) if latencies else 0,
            "mean": round(statistics.mean(latencies), 1) if latencies else 0,
            "p50": round(percentile(latencies, 0.50), 1),
            "p90": round(percentile(latencies, 0.90), 1),
            "p95": round(percentile(latencies, 0.95), 1),
            "p99": round(percentile(latencies, 0.99), 1),
        },
        "total_bytes": sum(r.get("bytes", 0) for r in ok_records),
        "error_breakdown": _error_breakdown(failed),
    }


def _error_breakdown(failed: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in failed:
        key = r.get("error") or f"http_{r.get('http_status', '?')}"
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def summarize_resources(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对 system 指标做最值/均值, 对进程列表做同样的合并。"""
    sys_cpu, sys_mem, sys_mem_used, sys_disk_r, sys_disk_w, sys_net_rx, sys_net_tx = (
        [], [], [], [], [], [], []
    )
    proc_cpu: Dict[str, List[float]] = {}
    proc_rss: Dict[str, List[float]] = {}

    for s in samples:
        if "error" in s:
            continue
        sysinfo = s.get("system", {})
        sys_cpu.append(sysinfo.get("cpu_pct", 0))
        sys_mem.append(sysinfo.get("mem_pct", 0))
        sys_mem_used.append(sysinfo.get("mem_used_mb", 0))
        sys_disk_r.append(sysinfo.get("disk_read_kbs", 0))
        sys_disk_w.append(sysinfo.get("disk_write_kbs", 0))
        sys_net_rx.append(sysinfo.get("net_rx_kbs", 0))
        sys_net_tx.append(sysinfo.get("net_tx_kbs", 0))
        for p in s.get("processes", []):
            name = p.get("name", "?")
            proc_cpu.setdefault(name, []).append(p.get("cpu_pct", 0))
            proc_rss.setdefault(name, []).append(p.get("rss_mb", 0))

    def _stat(arr: List[float]) -> Dict[str, float]:
        if not arr:
            return {"min": 0, "max": 0, "mean": 0}
        return {"min": round(min(arr), 2), "max": round(max(arr), 2), "mean": round(statistics.mean(arr), 2)}

    proc_summary = {}
    for name in proc_cpu:
        proc_summary[name] = {
            "cpu_pct": _stat(proc_cpu[name]),
            "rss_mb": _stat(proc_rss.get(name, [])),
            "samples": len(proc_cpu[name]),
        }

    return {
        "sample_count": len(samples),
        "duration_s": round(samples[-1]["t"] - samples[0]["t"], 2) if samples else 0,
        "system": {
            "cpu_pct": _stat(sys_cpu),
            "mem_pct": _stat(sys_mem),
            "mem_used_mb": _stat(sys_mem_used),
            "disk_read_kbs": _stat(sys_disk_r),
            "disk_write_kbs": _stat(sys_disk_w),
            "net_rx_kbs": _stat(sys_net_rx),
            "net_tx_kbs": _stat(sys_net_tx),
        },
        "processes": proc_summary,
    }


def summarize_lb(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """解析 /api/health.ollama 节点状态。"""
    node_evolution: Dict[str, List[Dict[str, Any]]] = {}
    for s in samples:
        lb = s.get("ollama_lb")
        if not isinstance(lb, dict):
            continue
        for node in lb.get("nodes", []):
            url = node.get("url", "?")
            node_evolution.setdefault(url, []).append({
                "t": s.get("t"),
                "healthy": node.get("healthy"),
                "active_requests": node.get("active_requests"),
                "total_requests": node.get("total_requests"),
                "total_failures": node.get("total_failures"),
            })
    return {
        "sample_count": len(samples),
        "nodes": {
            url: {
                "samples": len(series),
                "first_seen_t": series[0]["t"] if series else None,
                "healthy_changes": sum(1 for i in range(1, len(series))
                                       if series[i]["healthy"] != series[i - 1]["healthy"]),
                "max_active_requests": max((s.get("active_requests") or 0) for s in series) if series else 0,
                "total_requests_end": series[-1]["total_requests"] if series else 0,
                "total_failures_end": series[-1]["total_failures"] if series else 0,
            } for url, series in node_evolution.items()
        },
    }


def summarize_gpu(samples: List[Dict[str, Any]], available: bool) -> Dict[str, Any]:
    if not available:
        return {"available": False, "note": "nvidia-smi not found; skip GPU sampling"}
    if not samples:
        return {"available": True, "samples": 0}

    by_idx: Dict[int, Dict[str, List[float]]] = {}
    name_by_idx: Dict[int, str] = {}
    for s in samples:
        for gpu in s.get("gpus", []):
            if not isinstance(gpu, dict) or "error" in gpu:
                continue
            idx = gpu.get("index")
            by_idx.setdefault(idx, {
                "util_gpu_pct": [], "util_mem_pct": [],
                "mem_used_mb": [], "temp_c": [], "power_w": [],
            })
            by_idx[idx]["util_gpu_pct"].append(gpu.get("util_gpu_pct", 0))
            by_idx[idx]["util_mem_pct"].append(gpu.get("util_mem_pct", 0))
            by_idx[idx]["mem_used_mb"].append(gpu.get("mem_used_mb", 0))
            by_idx[idx]["temp_c"].append(gpu.get("temp_c", 0))
            if gpu.get("power_w") is not None:
                by_idx[idx]["power_w"].append(gpu["power_w"])
            name_by_idx[idx] = gpu.get("name", "?")

    def _stat(arr: List[float]) -> Dict[str, float]:
        if not arr:
            return {"min": 0, "max": 0, "mean": 0, "samples": 0}
        return {
            "min": round(min(arr), 1),
            "max": round(max(arr), 1),
            "mean": round(statistics.mean(arr), 1),
            "samples": len(arr),
        }

    return {
        "available": True,
        "sample_count": len(samples),
        "devices": {
            idx: {
                "name": name_by_idx.get(idx, "?"),
                "util_gpu_pct": _stat(d["util_gpu_pct"]),
                "util_mem_pct": _stat(d["util_mem_pct"]),
                "mem_used_mb": _stat(d["mem_used_mb"]),
                "temp_c": _stat(d["temp_c"]),
                "power_w": _stat(d["power_w"]) if d["power_w"] else None,
            } for idx, d in by_idx.items()
        },
    }


def load_baseline(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"[警告] 读取基线失败: {exc}")
        return None


def render_report(summary: Dict[str, Any], baseline: Optional[Dict[str, Any]]) -> str:
    req = summary["requests"]
    lat = req["latency_ms"]
    res = summary["resources"]
    proc_cpu = summary.get("process_cpu", {})
    gpu = summary.get("gpu", {})
    lb = summary["ollama_lb"]

    lines: List[str] = []
    lines.append("# 5 用户并发 Ollama 负载测试报告")
    lines.append("")
    lines.append(f"- 起始时间: `{summary['meta']['started_at']}`")
    lines.append(f"- 结束时间: `{summary['meta']['finished_at']}`")
    lines.append(f"- 目标后端: `{summary['meta']['base_url']}`")
    lines.append(f"- 并发用户数: **{summary['meta']['users']}**")
    lines.append(f"- 每用户查询数: {summary['meta']['queries_per_user']}")
    lines.append(f"- 思考间隔: {summary['meta']['think_time']}s")
    lines.append(f"- 请求超时: {summary['meta']['timeout']}s")
    lines.append("")

    lines.append("## 1. 测试结论 (TL;DR)")
    lines.append("")
    lines.append(f"- 总请求数: **{req['total']}** | 成功: **{req['ok']}** | 失败: **{req['failed']}** | 成功率: **{req['success_rate_pct']}%**")
    lines.append(f"- 延迟 P50 / P90 / P95 / P99 = **{lat['p50']} / {lat['p90']} / {lat['p95']} / {lat['p99']} ms**")
    lines.append(f"- 平均延迟: **{lat['mean']} ms**, 最大: **{lat['max']} ms**")
    if gpu.get("available") and gpu.get("devices"):
        for idx, dev in gpu["devices"].items():
            lines.append(f"- GPU {idx} `{dev['name']}`: 利用率平均 {dev['util_gpu_pct']['mean']}% / 峰值 {dev['util_gpu_pct']['max']}%, 显存均值 {dev['mem_used_mb']['mean']} MB / 峰值 {dev['mem_used_mb']['max']} MB, 温度峰值 {dev['temp_c']['max']}°C")
    if proc_cpu.get("processes"):
        for name, st in proc_cpu["processes"].items():
            lines.append(f"- 进程 `{name}`: CPU 平均 {st['cpu_pct']['mean']}% / 峰值 {st['cpu_pct']['max']}%, RSS 平均 {st['rss_mb']['mean']} MB / 峰值 {st['rss_mb']['max']} MB")
    if lb.get("nodes"):
        for url, st in lb["nodes"].items():
            lines.append(f"- Ollama 节点 `{url}`: 总请求 {st['total_requests_end']}, 失败 {st['total_failures_end']}, 健康切换 {st['healthy_changes']} 次, 峰值活跃 {st['max_active_requests']}")
    lines.append("")

    # 性能对比
    lines.append("## 2. 性能对比 (vs 基线)")
    lines.append("")
    if baseline is None:
        lines.append("_未提供基线文件; 下次运行可通过 `--baseline <path>` 启用对比。_")
    else:
        b_req = baseline.get("requests", {})
        b_lat = b_req.get("latency_ms", {})
        lines.append("| 指标 | 本次 | 基线 | 差值 |")
        lines.append("|---|---:|---:|---:|")
        for k in ("p50", "p90", "p95", "p99", "mean", "max"):
            cur = lat.get(k, 0)
            base = b_lat.get(k, 0)
            delta = round(cur - base, 1)
            lines.append(f"| 延迟 {k} (ms) | {cur} | {base} | {delta:+} |")
        cur_sr = req["success_rate_pct"]
        base_sr = b_req.get("success_rate_pct", 0)
        lines.append(f"| 成功率 (%) | {cur_sr} | {base_sr} | {round(cur_sr - base_sr, 2):+} |")
    lines.append("")

    # 资源使用
    lines.append("## 3. 资源使用")
    lines.append("")
    sys_stat = res["system"]
    lines.append("### 3.1 系统级 (psutil)")
    lines.append("")
    lines.append("| 指标 | 最小 | 平均 | 最大 |")
    lines.append("|---|---:|---:|---:|")
    lines.append(f"| 系统 CPU (%) | {sys_stat['cpu_pct']['min']} | {sys_stat['cpu_pct']['mean']} | {sys_stat['cpu_pct']['max']} |")
    lines.append(f"| 系统内存使用 (MB) | {sys_stat['mem_used_mb']['min']} | {sys_stat['mem_used_mb']['mean']} | {sys_stat['mem_used_mb']['max']} |")
    lines.append(f"| 系统内存占比 (%) | {sys_stat['mem_pct']['min']} | {sys_stat['mem_pct']['mean']} | {sys_stat['mem_pct']['max']} |")
    lines.append(f"| 磁盘读 (KB/s) | {sys_stat['disk_read_kbs']['min']} | {sys_stat['disk_read_kbs']['mean']} | {sys_stat['disk_read_kbs']['max']} |")
    lines.append(f"| 磁盘写 (KB/s) | {sys_stat['disk_write_kbs']['min']} | {sys_stat['disk_write_kbs']['mean']} | {sys_stat['disk_write_kbs']['max']} |")
    lines.append(f"| 网络下行 (KB/s) | {sys_stat['net_rx_kbs']['min']} | {sys_stat['net_rx_kbs']['mean']} | {sys_stat['net_rx_kbs']['max']} |")
    lines.append(f"| 网络上行 (KB/s) | {sys_stat['net_tx_kbs']['min']} | {sys_stat['net_tx_kbs']['mean']} | {sys_stat['net_tx_kbs']['max']} |")
    lines.append("")

    # 进程 CPU
    if proc_cpu.get("processes"):
        lines.append("### 3.2 目标进程 CPU/RSS (端口识别: 5000/11434)")
        lines.append("")
        lines.append("| 进程 | CPU 均值 (%) | CPU 峰值 (%) | RSS 均值 (MB) | RSS 峰值 (MB) | 线程 |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for name, st in proc_cpu["processes"].items():
            th = st.get("samples", 0)
            lines.append(f"| `{name}` | {st['cpu_pct']['mean']} | {st['cpu_pct']['max']} | {st['rss_mb']['mean']} | {st['rss_mb']['max']} | {th} |")
        lines.append("")

    # GPU
    if gpu.get("available") and gpu.get("devices"):
        lines.append("### 3.3 GPU (nvidia-smi)")
        lines.append("")
        lines.append("| GPU | 名称 | 利用率均 (%) | 利用率峰 (%) | 显存均 (MB) | 显存峰 (MB) | 温度峰 (°C) | 功耗峰 (W) |")
        lines.append("|---:|---|---:|---:|---:|---:|---:|---:|")
        for idx, dev in gpu["devices"].items():
            pw = dev.get("power_w") or {}
            lines.append(f"| {idx} | {dev['name']} | {dev['util_gpu_pct']['mean']} | {dev['util_gpu_pct']['max']} | {dev['mem_used_mb']['mean']} | {dev['mem_used_mb']['max']} | {dev['temp_c']['max']} | {pw.get('max', '-')} |")
        lines.append("")
    elif gpu.get("available") is False:
        lines.append("### 3.3 GPU")
        lines.append("")
        lines.append(f"_{gpu.get('note', 'nvidia-smi 不可用')}_")
        lines.append("")
    lines.append("")

    # 错误明细
    lines.append("## 4. 错误明细")
    lines.append("")
    eb = req.get("error_breakdown", {})
    if not eb:
        lines.append("_无错误_ ✅")
    else:
        lines.append("| 错误类型 | 次数 |")
        lines.append("|---|---:|")
        for k, v in eb.items():
            lines.append(f"| `{k}` | {v} |")
    lines.append("")

    # 优化建议
    lines.append("## 5. 优化建议 (自动生成)")
    lines.append("")
    tips: List[str] = []
    if req["success_rate_pct"] < 99.0:
        tips.append(f"- ⚠️ 成功率 {req['success_rate_pct']}% 低于 99%; 建议检查 Ollama 节点稳定性, 或调高 `OLLAMA_REQUEST_TIMEOUT`。")
    if lat["p95"] > 15000:
        tips.append(f"- ⚠️ P95 延迟 {lat['p95']} ms 偏高; 考虑:")
        tips.append("  - 启用 `OLLAMA_LB_STRATEGY=least_conn` 在多节点间分散请求")
        tips.append("  - 引入更小的模型 (例如 `qwen3:4b`) 应对延迟敏感型查询")
        tips.append("  - 启用 Redis 缓存 (`CACHE_ENABLED=true`) 减少重复推理")
    if res["processes"]:
        for name, st in res["processes"].items():
            if st["cpu_pct"]["max"] > 90:
                tips.append(f"- ⚠️ `{name}` CPU 峰值 {st['cpu_pct']['max']}%; 关注是否需要横向扩容或多实例负载均衡。")
            if st["rss_mb"]["max"] > 4000:
                tips.append(f"- ⚠️ `{name}` 内存峰值 {st['rss_mb']['max']} MB; 检查是否存在内存泄漏或大对象缓存。")
    if lb.get("nodes"):
        for url, st in lb["nodes"].items():
            if st["healthy_changes"] > 0:
                tips.append(f"- ⚠️ Ollama 节点 `{url}` 在测试期间发生 {st['healthy_changes']} 次健康状态切换; 建议查看节点日志并调高 `OLLAMA_FAILURE_THRESHOLD`。")
            if st["total_failures_end"] > 0:
                tips.append(f"- ⚠️ Ollama 节点 `{url}` 累计失败 {st['total_failures_end']} 次; 排查网络抖动或模型加载异常。")
    if not tips:
        tips.append("- ✅ 所有指标在健康阈值内; 当前架构可承载 5 用户并发。")
        tips.append("- 建议维持 `OLLAMA_LB_STRATEGY=round_robin` 与 `OLLAMA_HEALTHCHECK_INTERVAL=15s`。")
        tips.append("- 持续关注 P95 趋势, 若 P95 持续 > 10s 考虑引入缓存层。")
    lines.extend(tips)
    lines.append("")

    lines.append("## 6. 文件清单")
    lines.append("")
    lines.append("- `raw_requests.jsonl` — 每个请求的原始记录")
    lines.append("- `resource_samples.jsonl` — 每秒一次的资源采样")
    lines.append("- `ollama_lb_samples.jsonl` — Ollama LB 节点状态采样")
    lines.append("- `summary.json` — 汇总指标 (可用于后续对比)")
    lines.append("")
    return "\n".join(lines)


# ============================================================================
# 主流程
# ============================================================================
def run_load_test(args: argparse.Namespace) -> Dict[str, Any]:
    queries_pool = SAMPLE_QUERIES_ZH if args.lang == "zh" else SAMPLE_QUERIES_EN
    # 为每个用户准备查询列表: 循环采样
    user_queries: List[List[str]] = []
    for i in range(args.users):
        user_qs = []
        for j in range(args.queries_per_user):
            q = queries_pool[(i * 7 + j) % len(queries_pool)]
            user_qs.append(q)
        user_queries.append(user_qs)

    results: List[Dict[str, Any]] = []
    results_lock = threading.Lock()

    started_at = datetime.now().isoformat(timespec="seconds")
    run_start = time.time()

    # 启动监控
    monitor = ResourceMonitor(port_targets=[5000, 11434], interval=args.resource_interval)
    proc_cpu = ProcessCPUSampler(port_targets=[5000, 11434], interval=args.resource_interval)
    gpu_mon = GPUMonitor(interval=args.gpu_interval)
    lb_probe = OllamaLBProbe(base_url=args.base_url, interval=args.lb_interval)
    monitor.start()
    proc_cpu.start()
    gpu_mon.start()
    lb_probe.start()

    print(f"[loadtest] 启动 {args.users} 个虚拟用户, 每用户 {args.queries_per_user} 查询, "
          f"think={args.think_time}s, timeout={args.timeout}s, base={args.base_url}")

    # 后台预热 1 次, 让 Ollama 模型从冷启动到热状态 (避免首请求 60s 误判)
    try:
        print("[loadtest] 预热请求 (避免冷启动延迟) ...")
        requests.post(f"{args.base_url}/api/agent/query",
                      json={"query": "hi", "lang": args.lang}, timeout=args.timeout)
        print("[loadtest] 预热完成")
    except Exception as exc:
        print(f"[loadtest] 预热失败 (忽略): {exc}")

    # 并发运行虚拟用户
    users = [VirtualUser(uid, args.base_url, user_queries[uid], args.think_time,
                         args.timeout, args.lang, results, results_lock)
             for uid in range(args.users)]
    with ThreadPoolExecutor(max_workers=args.users, thread_name_prefix="VU") as ex:
        futures = [ex.submit(u.run) for u in users]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as exc:
                print(f"[loadtest] 用户线程异常: {exc}")

    run_end = time.time()
    monitor.stop()
    proc_cpu.stop()
    gpu_mon.stop()
    lb_probe.stop()
    finished_at = datetime.now().isoformat(timespec="seconds")

    summary = {
        "meta": {
            "base_url": args.base_url,
            "users": args.users,
            "queries_per_user": args.queries_per_user,
            "think_time": args.think_time,
            "timeout": args.timeout,
            "lang": args.lang,
            "started_at": started_at,
            "finished_at": finished_at,
            "wall_duration_s": round(run_end - run_start, 2),
        },
        "requests": summarize_requests(results),
        "resources": summarize_resources(monitor.samples),
        "process_cpu": summarize_resources(proc_cpu.samples),
        "gpu": summarize_gpu(gpu_mon.samples, gpu_mon.available),
        "ollama_lb": summarize_lb(lb_probe.samples),
    }
    return {"summary": summary,
            "raw_requests": results,
            "resource_samples": monitor.samples,
            "process_cpu_samples": proc_cpu.samples,
            "gpu_samples": gpu_mon.samples,
            "ollama_lb_samples": lb_probe.samples}


def write_outputs(data: Dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "raw_requests.jsonl", "w", encoding="utf-8") as f:
        for r in data["raw_requests"]:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(out_dir / "resource_samples.jsonl", "w", encoding="utf-8") as f:
        for s in data["resource_samples"]:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    with open(out_dir / "process_cpu_samples.jsonl", "w", encoding="utf-8") as f:
        for s in data.get("process_cpu_samples", []):
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    with open(out_dir / "gpu_samples.jsonl", "w", encoding="utf-8") as f:
        for s in data.get("gpu_samples", []):
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    with open(out_dir / "ollama_lb_samples.jsonl", "w", encoding="utf-8") as f:
        for s in data["ollama_lb_samples"]:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(data["summary"], f, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description="van.ea Ollama 5 用户并发负载测试")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="后端服务地址 (默认 http://localhost:5000)")
    parser.add_argument("--users", type=int, default=5, help="并发用户数 (默认 5)")
    parser.add_argument("--queries-per-user", type=int, default=4, help="每用户查询数 (默认 4)")
    parser.add_argument("--think-time", type=float, default=1.5, help="查询间隔秒数 (默认 1.5)")
    parser.add_argument("--timeout", type=float, default=120.0, help="单请求超时秒数 (默认 120)")
    parser.add_argument("--lang", default="zh", choices=["zh", "en"], help="查询语言 (默认 zh)")
    parser.add_argument("--resource-interval", type=float, default=1.0, help="资源采样间隔 (默认 1s)")
    parser.add_argument("--lb-interval", type=float, default=2.0, help="LB 状态采样间隔 (默认 2s)")
    parser.add_argument("--gpu-interval", type=float, default=2.0, help="GPU 采样间隔 (默认 2s)")
    parser.add_argument("--out", default=None, help="输出目录 (默认 results/<timestamp>)")
    parser.add_argument("--baseline", default=None, help="对比基线 summary.json 路径")
    args = parser.parse_args()

    # 输出目录
    if args.out:
        out_dir = Path(args.out).resolve()
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = (PROJECT_ROOT / "results" / f"ollama_loadtest_{args.users}u_{ts}").resolve()

    print(f"[loadtest] 输出目录: {out_dir}")
    data = run_load_test(args)
    write_outputs(data, out_dir)

    baseline = load_baseline(args.baseline)
    report_md = render_report(data["summary"], baseline)
    with open(out_dir / "report.md", "w", encoding="utf-8") as f:
        f.write(report_md)
    print(f"[loadtest] 报告已生成: {out_dir / 'report.md'}")

    # 终端摘要
    s = data["summary"]
    r = s["requests"]
    print("\n========== 摘要 ==========")
    print(f"  请求: 总 {r['total']} / 成功 {r['ok']} / 失败 {r['failed']} "
          f"(成功率 {r['success_rate_pct']}%)")
    print(f"  延迟: P50={r['latency_ms']['p50']}ms P90={r['latency_ms']['p90']}ms "
          f"P95={r['latency_ms']['p95']}ms P99={r['latency_ms']['p99']}ms "
          f"max={r['latency_ms']['max']}ms")
    print(f"  耗时: {s['meta']['wall_duration_s']}s")
    print(f"  输出: {out_dir}")
    print("==========================\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
