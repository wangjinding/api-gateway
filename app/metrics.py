"""用量指标、访问日志、简单限流。

设计为无外部依赖、进程内内存态，足够网关运维排障用。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from contextvars import ContextVar
from typing import Any

log = logging.getLogger("api-gateway.metrics")

# 请求级「归属账号」上下文（ContextVar，每 task/请求独立；由 main.py require_auth 写入）。
# 访问日志与指标通过它给每条记录打上账号，实现：
#   普通账号只看自己名下（会话 + 自己的 API Key）产生的记录，admin 看全部。
ACCOUNT: ContextVar[str] = ContextVar("gw_account", default="anonymous")
ROLE: ContextVar[str] = ContextVar("gw_role", default="member")

# M4: 按 rid 暂存请求体摘要（由 main.py 中间件写入）。AccessLog.add() 用它给每条日志
#   附上「具体请求内容」，便于在日志页查看实际发了什么。有界缓存，防无界增长。
BODY_BY_RID: dict[str, str] = {}

# ── 视频轮询历史（/logs 按 video_id 合成到「创建」那一条里，不再每个轮询各占一行） ──
VIDEO_POLLS: dict[str, "deque"] = {}
_vp_lock = threading.Lock()
_VP_MAX_VIDEOS = 200   # 同时追踪的视频任务数
_VP_MAX_POLLS = 500    # 单个任务最多保留多少条轮询


def video_poll_push(video_id: str, snap: dict) -> None:
    """记录一次视频轮询快照（内存态、有界）。"""
    if not video_id:
        return
    with _vp_lock:
        dq = VIDEO_POLLS.get(video_id)
        if dq is None:
            if len(VIDEO_POLLS) >= _VP_MAX_VIDEOS:
                # 超上限：踢掉最老的视频任务
                oldest = next(iter(VIDEO_POLLS))
                VIDEO_POLLS.pop(oldest, None)
            dq = deque(maxlen=_VP_MAX_POLLS)
            VIDEO_POLLS[video_id] = dq
        dq.append(snap)


def video_polls(video_id: str) -> list[dict]:
    with _vp_lock:
        return list(VIDEO_POLLS.get(video_id) or [])
_BODY_MAX = 2000


# ── 用量指标 ──────────────────────────────────────────
class Metrics:
    """按 模型/供应商/路由 维度统计调用次数、fallback、token、耗时。"""

    def __init__(self, enabled: bool = True, started_at: float | None = None):
        self.enabled = enabled
        self._lock = threading.Lock()
        self.calls: dict[str, dict[str, int]] = {}          # model -> {ok, fail}
        self.providers: dict[str, dict[str, int]] = {}      # provider -> {ok, fail}
        self.routes: dict[str, dict[str, int]] = {}         # route -> {ok, fallback}
        self.fallback_total: int = 0
        self.tokens_prompt: int = 0
        self.tokens_completion: int = 0
        self.latency_samples: dict[str, deque[float]] = {}  # model -> 最近 N 次耗时
        # 功能 11: 在途请求计数
        self.in_flight: int = 0
        self.in_flight_peak: int = 0
        # M5+: 按账号维度的聚合（普通账号只看到自己的调用；admin 看全局）。
        #   结构：account -> {"calls","providers","routes","tokens","latency","fallback"}
        self.by_account: dict[str, dict[str, Any]] = {}
        # Bug 4: 热重载时保留 started_at，uptime 不因重载清零
        self.started_at: float = started_at if started_at is not None else time.time()

    def _acc(self, account: str) -> dict[str, Any]:
        """懒建某一账号的聚合桶（须已持 self._lock）。"""
        acc = self.by_account.get(account)
        if acc is None:
            acc = {
                "calls": {}, "providers": {}, "routes": {},
                "tokens": {"prompt": 0, "completion": 0},
                "latency": {}, "fallback": 0,
            }
            self.by_account[account] = acc
        return acc

    def increment_inflight(self) -> None:
        """功能 11：请求进入 +1。"""
        with self._lock:
            self.in_flight += 1
            if self.in_flight > self.in_flight_peak:
                self.in_flight_peak = self.in_flight

    def decrement_inflight(self) -> None:
        """功能 11：请求结束 -1。"""
        with self._lock:
            self.in_flight = max(0, self.in_flight - 1)

    def in_flight_snapshot(self) -> int:
        with self._lock:
            return self.in_flight

    @staticmethod
    def _bucket(d: dict, k: str) -> dict[str, int]:
        return d.setdefault(k, {"ok": 0, "fail": 0})

    def record_call(self, model: str, provider: str | None, ok: bool, route: str | None = None,
                    account: str | None = None) -> None:
        """记录一次调用结果。

        route 维度（Bug 8 修复）：传入 route 时同时累加 routes 的 ok/fail。
        account（M5+）：未显式传入时取请求级 ACCOUNT 上下文；非 anonymous 时额外累加
        到该账号自己的聚合桶，供「只看本人的指标」使用。
        """
        if not self.enabled:
            return
        a = account or ACCOUNT.get()
        with self._lock:
            self._bucket(self.calls, model)["ok" if ok else "fail"] += 1
            if provider:
                self._bucket(self.providers, provider)["ok" if ok else "fail"] += 1
            if route:
                self._bucket(self.routes, route)["ok" if ok else "fail"] += 1
            if a and a != "anonymous":
                acc = self._acc(a)
                self._bucket(acc["calls"], model)["ok" if ok else "fail"] += 1
                if provider:
                    self._bucket(acc["providers"], provider)["ok" if ok else "fail"] += 1
                if route:
                    self._bucket(acc["routes"], route)["ok" if ok else "fail"] += 1

    def record_fallback(self, route: str | None, account: str | None = None) -> None:
        if not self.enabled:
            return
        a = account or ACCOUNT.get()
        with self._lock:
            self.fallback_total += 1
            if route:
                self._bucket(self.routes, route).setdefault("fallback", 0)
                self.routes[route]["fallback"] += 1
            if a and a != "anonymous":
                acc = self._acc(a)
                acc["fallback"] += 1
                if route:
                    b = self._bucket(acc["routes"], route)
                    b["fallback"] = b.get("fallback", 0) + 1

    def record_tokens(self, prompt: int, completion: int, account: str | None = None) -> None:
        if not self.enabled:
            return
        a = account or ACCOUNT.get()
        with self._lock:
            self.tokens_prompt += prompt
            self.tokens_completion += completion
            if a and a != "anonymous":
                acc = self._acc(a)
                acc["tokens"]["prompt"] += prompt
                acc["tokens"]["completion"] += completion

    def record_latency(self, model: str, seconds: float, account: str | None = None) -> None:
        if not self.enabled:
            return
        a = account or ACCOUNT.get()
        with self._lock:
            dq = self.latency_samples.setdefault(model, deque(maxlen=200))
            dq.append(seconds)
            if a and a != "anonymous":
                acc = self._acc(a)
                adq = acc["latency"].setdefault(model, deque(maxlen=200))
                adq.append(seconds)

    def recent_avg_latency(self, model: str, k: int = 12) -> float | None:
        """最近 k 次调用的平均耗时（秒），用于「按速度排序」路由（第十一轮 #4）。

        没有样本返回 None（冷启动时上层会退回配置里的优先级顺序）。
        """
        if not self.enabled:
            return None
        with self._lock:
            dq = self.latency_samples.get(model)
            if not dq:
                return None
            vals = list(dq)[-k:]
        return (sum(vals) / len(vals)) if vals else None

    def snapshot(self, account: str | None = None) -> dict[str, Any]:
        """聚合快照。account=None 返回全局（admin / 未鉴权详情用）；
        account 非空则只返回该账号自己的聚合（不含全局 circuits —— 熔断是供应商级基础设施，admin 才见）。
        """
        if account and account != "anonymous":
            with self._lock:
                acc = self.by_account.get(account)
                if not acc:
                    return dict(uptime_seconds=round(time.time() - self.started_at, 1),
                                in_flight=self.in_flight, in_flight_peak=self.in_flight_peak,
                                calls={}, providers={}, routes={}, fallback_total=0,
                                tokens={"prompt": 0, "completion": 0}, latency={})
                lat = {}
                for m, dq in acc["latency"].items():
                    if dq:
                        lat[m] = _latency_summary(dq)
                return {
                    "uptime_seconds": round(time.time() - self.started_at, 1),
                    "in_flight": self.in_flight,
                    "in_flight_peak": self.in_flight_peak,
                    "calls": {k: dict(v) for k, v in acc["calls"].items()},
                    "providers": {k: dict(v) for k, v in acc["providers"].items()},
                    "routes": {k: dict(v) for k, v in acc["routes"].items()},
                    "fallback_total": acc["fallback"],
                    "tokens": dict(acc["tokens"]),
                    "latency": lat,
                }
        return self._snapshot_global()

    def _snapshot_global(self) -> dict[str, Any]:
        # Bug 7 修复: 整个快照在锁内构造，避免并发写 dict 时迭代抛
        #   RuntimeError: dictionary changed size during iteration
        with self._lock:
            latency_out: dict[str, dict[str, float]] = {}
            for m, dq in self.latency_samples.items():
                if dq:
                    latency_out[m] = _latency_summary(dq)
            return {
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "in_flight": self.in_flight,
                "in_flight_peak": self.in_flight_peak,
                "calls": {k: dict(v) for k, v in self.calls.items()},
                "providers": {k: dict(v) for k, v in self.providers.items()},
                "routes": {k: dict(v) for k, v in self.routes.items()},
                "fallback_total": self.fallback_total,
                "tokens": {"prompt": self.tokens_prompt, "completion": self.tokens_completion},
                "latency": latency_out,
            }




def _latency_summary(dq: deque) -> dict[str, float]:
    """把某模型/账号的耗时样本汇总成 avg/p50/p95（nearest-rank）。"""
    s = sorted(dq)
    n = len(s)
    p50 = s[min(n - 1, math.ceil(0.50 * n) - 1)]
    p95 = s[min(n - 1, math.ceil(0.95 * n) - 1)]
    return {"avg": round(sum(s) / n, 3), "p50": p50, "p95": p95, "count": n}

# ── 访问日志 ──────────────────────────────────────────
class AccessLog:
    """最近 N 条请求的环形日志，可经 /logs 查看。"""

    def __init__(self, maxlen: int = 500):
        self._max = maxlen
        self._buf: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, entry: dict[str, Any]) -> None:
        entry["ts"] = round(time.time(), 3)
        # M5+: 自动补充「归属账号」（普通账号只看到自己的日志，admin 看全部）。
        entry.setdefault("account", ACCOUNT.get())
        # M4: 附加请求体摘要（若有）。与 main.py 中间件按 rid 暂存的内容对应。
        rid = entry.get("rid")
        if rid:
            snip = BODY_BY_RID.pop(rid, None)
            if snip:
                entry["body"] = snip
        with self._lock:
            self._buf.append(entry)

    def tail(self, n: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._buf)
        return items[-n:] if n < len(items) else items

    def resize(self, maxlen: int) -> None:
        """调整环形缓冲上限（热重载用），保留已有历史条目。"""
        with self._lock:
            if maxlen == self._max or maxlen <= 0:
                return
            self._max = maxlen
            self._buf = deque(self._buf, maxlen=maxlen)


# ── 简单限流（per-key 令牌桶，带 LRU 桶清理） ──────────
class RateLimiter:
    """按 key 限流（QPS）。rps=None/<=0 表示不限。

    Bug 2：桶数带上限（max_buckets），超出时按最后访问顺序 LRU 淘汰，
    避免长时间运行 + 多 key 导致 dict 无界增长。
    """

    def __init__(self, max_buckets: int = 10000):
        self._max_buckets = max_buckets
        self._buckets: dict[str, list[float, float]] = {}
        self._lock = threading.Lock()

    def _evict_if_needed(self) -> None:
        """调用方需已持锁。超出上限时，按桶的 last-access（ts）淘汰最旧的一半。"""
        if len(self._buckets) <= self._max_buckets:
            return
        entries = sorted(self._buckets.items(), key=lambda kv: kv[1][1])  # 按 ts 升序
        for k in [kk for kk, _ in entries[: len(entries) // 2]]:
            self._buckets.pop(k, None)

    def allow(self, key: str | None, rps: float | None) -> bool:
        if not rps or rps <= 0:
            return True
        if not key:
            return True
        now = time.monotonic()
        with self._lock:
            cap = rps * 2  # 桶容量
            entry = self._buckets.get(key)
            if entry is None:
                self._evict_if_needed()
                # Bug 6: 首次请求也消耗一个 token（此前存满桶直接放行，等效放宽 cap+1 突发）
                self._buckets[key] = [max(0.0, cap - 1.0), now]
                return True
            tokens, ts = entry
            tokens = min(cap, tokens + (now - ts) * rps)
            entry[1] = now  # 更新 last-access
            if tokens >= 1.0:
                entry[0] = tokens - 1.0
                return True
            entry[0] = tokens
            return False

    def size(self) -> int:
        with self._lock:
            return len(self._buckets)
