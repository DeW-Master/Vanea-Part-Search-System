# -*- coding: utf-8 -*-
"""
van.ea 车辆零件智能查询系统 - 多副本领导者选举

功能:
    - 基于 Redis SETNX 的简单领导者选举
    - 只有 leader 实例执行 Delta 预计算等全局后台任务
    - 自动心跳续期 + 失效转移
    - Leader 身份变化回调 (metrics, 日志)
    - Redis 不可用时自动降级为 "所有实例认为自己是 leader"
      (单副本或无 Redis 场景下安全)
"""

import time
import uuid
import threading
from typing import Callable, Optional

from config import (
    LEADER_ELECTION_ENABLED, LEADER_LOCK_KEY, LEADER_LOCK_TTL,
    INSTANCE_ID,
)


class LeaderElector:
    """
    简易 Redis-based Leader Election.

    设计原则:
        - 非侵入: 不保证绝对互斥 (极端网络分区下短时双 leader 可接受)
        - 仅用于避免重复 Delta 预计算, 非强一致选主
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, redis_client=None, lock_key: str = None):
        if getattr(self, '_initialized', False):
            return
        self._initialized = True

        self.enabled = LEADER_ELECTION_ENABLED
        self._lock_key = lock_key or LEADER_LOCK_KEY
        self._ttl = LEADER_LOCK_TTL
        self._token = f"{INSTANCE_ID}-{uuid.uuid4().hex[:8]}"
        self._is_leader = False
        self._redis = redis_client  # 懒加载，app 启动后注入

        self._on_change_callbacks: list = []

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        if self.enabled:
            self._thread = threading.Thread(
                target=self._loop,
                name="LeaderElector",
                daemon=True,
            )
            self._thread.start()
            print(f"[Leader] 选举启用, 实例: {self._token}, TTL={self._ttl}s")
        else:
            self._is_leader = True  # 禁用选举时默认是 leader (单副本模式)
            print("[Leader] 选举已禁用, 本实例默认作为 leader 运行")

    # ============== 生命周期 ==============
    def set_redis_client(self, redis_client):
        """App 启动后 Redis 就绪时注入客户端"""
        self._redis = redis_client

    def on_leader_change(self, cb: Callable[[bool], None]):
        self._on_change_callbacks.append(cb)

    def _fire_change(self, new_state: bool):
        old = self._is_leader
        self._is_leader = new_state
        if old != new_state:
            print(f"[Leader] 状态变化: {'leader' if new_state else 'follower'}")
            for cb in self._on_change_callbacks:
                try:
                    cb(new_state)
                except Exception as e:
                    print(f"[Leader] 回调异常: {e}")

    def _loop(self):
        # 给 Redis 一点时间就绪
        time.sleep(5)
        while not self._stop.is_set():
            try:
                self._try_acquire_or_renew()
            except Exception as e:
                print(f"[Leader] 选举循环异常: {e}")
                # Redis 故障时保守退化为 leader (避免所有实例都不跑 Delta)
                self._fire_change(True)
            self._stop.wait(max(1, self._ttl // 3))

    def _try_acquire_or_renew(self):
        r = self._redis
        if r is None:
            self._fire_change(True)
            return

        try:
            # 先 SETNX 抢锁
            acquired = r.set(self._lock_key, self._token, nx=True, ex=self._ttl)
            if acquired:
                self._fire_change(True)
                return

            # 锁已存在，检查 owner
            owner = r.get(self._lock_key)
            if owner == self._token:
                # 我们就是 owner，续期
                r.expire(self._lock_key, self._ttl)
                self._fire_change(True)
            else:
                # 其他实例是 leader
                self._fire_change(False)
        except Exception as e:
            print(f"[Leader] Redis 操作异常: {e}")
            # 网络或 Redis 故障时，短时间内保持原状态，避免抖动
            pass

    # ============== 对外 API ==============
    @property
    def is_leader(self) -> bool:
        """本实例是不是 leader"""
        if not self.enabled:
            return True
        return self._is_leader

    def shutdown(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        # 释放锁
        if self._is_leader and self._redis is not None:
            try:
                current = self._redis.get(self._lock_key)
                if current == self._token:
                    self._redis.delete(self._lock_key)
            except Exception:
                pass


def get_leader_elector() -> LeaderElector:
    return LeaderElector()
