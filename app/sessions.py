"""前端登录会话（短时、内存态）。

设计：前端用**账号 + 密码**调 POST /auth/login 换一个 short-lived 的 `sess-` token，
后续请求带这个会话 token —— 密码只在登录那一刻提交一次，前端不长期持有；
API Key 也不需要交给浏览器。会话存内存，单实例必需；
多实例如需共享会话再换 Redis（当前网关单实例，足够）。

吊销语义（第八轮审计补）：账号被删除、改密或被管理员重置密码时，其名下**全部会话**
立即失效 —— 否则「删号 / 重置密码」无法止损（旧 token 仍能调用到 TTL 结束）。
"""

from __future__ import annotations

import secrets
import threading
import time


class SessionStore:
    def __init__(self, ttl_seconds: int = 86400, max_tokens: int = 5000):
        self._ttl = ttl_seconds
        self._max = max_tokens
        self._s: dict[str, dict] = {}
        self._lock = threading.Lock()

    def ttl(self) -> int:
        return self._ttl

    def set_ttl(self, seconds: int) -> None:
        """按当前配置动态调整会话时长（下限 60s，防误配成 0 让会话瞬间过期）。"""
        self._ttl = max(60, int(seconds))

    def count(self) -> int:
        with self._lock:
            self._purge()
            return len(self._s)

    def create(self, ip: str, account: str = "") -> str:
        with self._lock:
            self._purge()
            if len(self._s) >= self._max:  # 超上限：踢最老的
                oldest = min(self._s, key=lambda k: self._s[k]["created"])
                del self._s[oldest]
            tok = "sess-" + secrets.token_hex(24)
            # ip 只做记录、不做绑定：移动网络/NAT 下换出口 IP 是常态，绑定会把人踢下线。
            self._s[tok] = {"created": time.time(), "ip": ip or "", "account": account or ""}
            return tok

    def validate(self, token: str, ip: str | None = None) -> bool:
        if not token:
            return False
        with self._lock:
            ent = self._s.get(token)
            if not ent:
                return False
            if time.time() - ent["created"] > self._ttl:
                del self._s[token]
                return False
            return True

    def account_for(self, token: str) -> str | None:
        """返回会话所属账号（用于指标/日志归属与鉴权 identity）。"""
        if not token or not self.validate(token):
            return None
        with self._lock:
            ent = self._s.get(token)
            return (ent or {}).get("account")

    def is_session(self, token: str) -> bool:
        """该凭证是否为**密码登录换来的会话**（区别于长期 API Key）。

        账号管理/改密/铸 Key 这类「改凭证」操作只认会话凭证 —— 泄漏的 API Key
        就不可能再给自己铸一把新 Key 或改密码，堵住持久化后门。
        """
        return bool(token and token.startswith("sess-") and self.validate(token))

    def revoke(self, token: str) -> None:
        with self._lock:
            self._s.pop(token, None)

    def revoke_account(self, account: str) -> int:
        """吊销某账号的全部会话，返回被踢掉的会话数（改密/重置密码/删号时调用）。"""
        if not account:
            return 0
        with self._lock:
            dead = [k for k, v in self._s.items() if v.get("account") == account]
            for k in dead:
                del self._s[k]
            return len(dead)

    def clear(self) -> None:
        with self._lock:
            self._s.clear()

    def _purge(self) -> None:
        now = time.time()
        for k in list(self._s):
            if now - self._s[k]["created"] > self._ttl:
                del self._s[k]
