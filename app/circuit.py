"""供应商级熔断器（Circuit Breaker）。

状态机：CLOSED → OPEN（连续失败达到 threshold）→ HALF_OPEN（cooldown 到期）
→ 试探一次：成功回 CLOSED，失败回 OPEN。

目的：某供应商持续 502/超时期间，直接跳过白等 60s，让 fallback 快速生效。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

log = logging.getLogger("api-gateway.circuit")


class CircuitBreaker:
    """单供应商熔断器。线程安全。

    滑动窗口按**次数**生效（最近 window 次调用里失败 ≥ failure_threshold 即熔断），
    而非写死时间窗口——window 配置项真正起作用。
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, provider: str, failure_threshold: int = 3, cooldown: float = 30.0, window: int = 10):
        self.provider = provider
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown = cooldown          # 秒
        self.window = max(1, window)      # 最近 N 次调用作为判定窗口
        self._lock = threading.Lock()
        self._state: str = self.CLOSED
        self._recent: deque[bool] = deque(maxlen=self.window)  # 最近 N 次结果（按次数）
        self._opened_at: float = 0.0
        self._probing: bool = False   # HALF_OPEN 期间是否已有试探请求在途
        self._probe_at: float = 0.0   # 试探请求发出的时刻（用于探测超时兜底）
        # 探测超时：HALF_OPEN 的试探请求可能**永远不回报**（客户端中途断开时
        # CancelledError/GeneratorExit 不会走到 record()），若不兜底，breaker 会
        # 永久停在 HALF_OPEN 并一直拒绝该供应商，直到重启或改配置。
        self.probe_timeout: float = max(30.0, cooldown * 2)

    def _probe_expired(self) -> bool:
        return self._probing and (time.time() - self._probe_at) > self.probe_timeout

    # ── 对外 API ──
    def allow(self) -> bool:
        """当前是否允许调用该供应商。"""
        with self._lock:
            if self._state == self.CLOSED:
                return True
            if self._state == self.OPEN:
                if time.time() - self._opened_at >= self.cooldown:
                    self._state = self.HALF_OPEN
                    self._probing = True   # 本次调用即唯一试探
                    self._probe_at = time.time()
                    log.warning("[%s] 熔断 HALF_OPEN（试探）", self.provider)
                    return True
                return False
            # HALF_OPEN：只放行一个试探请求（其余拒绝，避免并发试探打穿下游）
            if self._probing:
                if self._probe_expired():
                    # 试探请求超时未回报 → 认定这次探测失败，回 OPEN 等下一轮冷却，
                    # 避免 breaker 被卡死在 HALF_OPEN（永久拒绝所有请求）。
                    self._state = self.OPEN
                    self._opened_at = time.time()
                    self._probing = False
                    log.warning("[%s] 试探请求 %.0fs 未回报，判定失败回 OPEN %.0fs",
                                self.provider, self.probe_timeout, self.cooldown)
                return False
            self._probing = True
            self._probe_at = time.time()
            return True

    def release_probe(self) -> None:
        """归还 HALF_OPEN 试探令牌（调用方在 `allow()==True` 之后**没真正发请求**时调用）。

        第六轮并发审计实测：拿到试探令牌后若在 record() 之前离开（限流跳过、凭证缺失
        等），令牌永远不会归还 → 该供应商被半开状态**永久**拒绝。除了下面的探测超时兜底，
        主动归还才是正解。

        注意（第七轮回归审计）：本方法不校验令牌归属，因此**只能由令牌持有者调用**，
        且必须满足"`allow()==True` 与本调用之间没有 await"（否则可能把并发请求 B 在
        HALF_OPEN 抢到的探测令牌误归还，导致双探）。当前全部调用点均满足该约束
        （限流跳过分支、images 的 except 分支为同 tick；videos_create 的 except 分支内
        不存在能抛 HTTPException 的源）。如需放宽，请改为 `allow()` 发放 probe token
        并由 `release_probe(token)` 校验归属。
        """
        with self._lock:
            if self._state == self.HALF_OPEN and self._probing:
                self._probing = False
                self._probe_at = 0.0

    def reconfigure(self, failure_threshold: int, cooldown: float, window: int) -> None:
        """热重载更新参数，保留当前状态与最近结果（Bug 4 修复）。"""
        with self._lock:
            self.failure_threshold = max(1, int(failure_threshold))
            self.cooldown = cooldown
            new_window = max(1, int(window))
            if new_window != self.window:
                self._recent = deque(self._recent, maxlen=new_window)
                self.window = new_window
            self.probe_timeout = max(30.0, cooldown * 2)

    def reset(self) -> None:
        """强制复位到 CLOSED 并清空窗口（热重载禁用熔断时用，Bug 7）。

        否则一个已 OPEN 的 breaker 即使熔断被关掉，仍会拒绝请求直到冷却结束。
        """
        with self._lock:
            self._state = self.CLOSED
            self._recent.clear()
            self._opened_at = 0.0
            self._probing = False
            self._probe_at = 0.0

    def record(self, ok: bool) -> None:
        """记录一次调用结果（最近 window 次按次数滑动），驱动状态机。"""
        now = time.time()
        with self._lock:
            self._recent.append(ok)  # deque(maxlen=window) 自动淘汰最旧
            recent_fails = sum(1 for okk in self._recent if not okk)

            if self._state == self.HALF_OPEN:
                self._probing = False
                self._probe_at = 0.0
                if ok:
                    self._state = self.CLOSED
                    self._recent.clear()
                    log.info("[%s] 熔断恢复 CLOSED", self.provider)
                else:
                    self._state = self.OPEN
                    self._opened_at = now
                    log.warning("[%s] 试探失败，回 OPEN %.0fs", self.provider, self.cooldown)
            elif self._state == self.CLOSED and not ok and recent_fails >= self.failure_threshold:
                self._state = self.OPEN
                self._opened_at = now
                self._probing = False
                log.warning("[%s] 最近 %d 次中失败 %d，达阈值 %d，熔断 OPEN %.0fs",
                            self.provider, len(self._recent), recent_fails, self.failure_threshold, self.cooldown)

    def status(self) -> dict:
        with self._lock:
            recent_fails = sum(1 for okk in self._recent if not okk)
            return {
                "provider": self.provider,
                "state": self._state,
                "recent_failures": recent_fails,
                "probing": self._probing,
                "window_size": self.window,
                "threshold": self.failure_threshold,
                "cooldown_seconds": self.cooldown,
            }


class CircuitBreakerRegistry:
    """按供应商 key 管理多个熔断器。"""

    def __init__(self, default_config: dict | None = None):
        self._lock = threading.Lock()
        self._breakers: dict[str, CircuitBreaker] = {}
        self._cfg = default_config or {}

    def _defaults(self, provider: str) -> CircuitBreaker:
        enabled = self._cfg.get("enabled", False)
        if not enabled:
            return CircuitBreaker(provider, failure_threshold=10**6, cooldown=3600)
        return CircuitBreaker(
            provider,
            failure_threshold=self._cfg.get("failure_threshold", 3),
            cooldown=self._cfg.get("cooldown", 30),
            window=self._cfg.get("window", 10),
        )

    def get(self, provider: str) -> CircuitBreaker:
        with self._lock:
            if provider not in self._breakers:
                self._breakers[provider] = self._defaults(provider)
            return self._breakers[provider]

    def update_config(self, cfg: dict) -> None:
        """热重载更新熔断参数（Bug 4 修复）：就地更新已有熔断器，**保留其状态**。

        之前热重载会整个重建 registry → 已熔断的坏供应商被立刻恢复重试。
        """
        with self._lock:
            self._cfg = dict(cfg)
            enabled = self._cfg.get("enabled", False)
            for cb in self._breakers.values():
                if not enabled:
                    cb.reset()  # Bug 7: 关掉熔断时复位已 OPEN 的 breaker，立刻恢复放行
                    cb.reconfigure(10**6, cb.cooldown, cb.window)  # 等效禁用
                else:
                    cb.reconfigure(
                        self._cfg.get("failure_threshold", 3),
                        self._cfg.get("cooldown", 30),
                        self._cfg.get("window", 10),
                    )

    def statuses(self) -> list[dict]:
        with self._lock:
            return [b.status() for b in self._breakers.values()]
