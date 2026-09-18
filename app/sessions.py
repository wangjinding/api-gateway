"""前端登录会话（短时；可落盘，默认内存）。

设计：前端用**账号 + 密码**调 POST /auth/login 换一个 short-lived 的 `sess-` token，
后续请求带这个会话 token —— 密码只在登录那一刻提交一次，前端不长期持有；
API Key 也不需要交给浏览器。

吊销语义（第八轮审计补）：账号被删除、改密或被管理员重置密码时，其名下**全部会话**
立即失效 —— 否则「删号 / 重置密码」无法止损（旧 token 仍能调用到 TTL 结束）。

落盘（第三十轮补）：此前是纯内存，进程一重启**所有人被踢下线**（每次上线/改配置都掉线，
运维体感很差，也逼着"为了不掉线而不重启"）。现在可选落盘 `data/sessions.json`：

  · **盘上只存 token 的 sha256**（`_fp()`），不存 token 本身 —— 备份/误传该文件也无法
    直接拿来冒充登录；代价是吊销/校验都要现算一次哈希（可忽略）。
  · 原子写（临时文件 + os.replace）+ 权限 600；写失败只告警、不抛 —— 落盘是"少掉线一次"
    的体验优化，不能让它把登录本身弄挂（真失败就退化成原来的纯内存行为）。
  · 载入时直接丢掉已过期的会话（TTL 判定与运行期一致）。
  · 仍不解决**多实例共享**（要共享还是得换 Redis）；当前网关单实例，够用。

  path=None（默认）→ 纯内存，测试与库内直连构造维持原语义。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

log = logging.getLogger("api-gateway.sessions")

# 落盘文件名固定为 sessions.json；结构带 version，便于以后改格式时兼容读取。
_FILE_VERSION = 1


def _fp(token: str) -> str:
    """token → 落盘用的指纹（sha256）。盘上永远不出现可直接使用的凭证。"""
    return hashlib.sha256(("sess:" + token).encode("utf-8")).hexdigest()


class SessionStore:
    def __init__(self, ttl_seconds: int = 86400, max_tokens: int = 5000,
                 path: str | os.PathLike | None = None):
        self._ttl = ttl_seconds
        self._max = max_tokens
        self._path = Path(path) if path else None
        self._s: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._load()

    # ── 落盘 ─────────────────────────────────────────
    def _load(self) -> None:
        """启动时载入；已过期的直接丢弃（不留到运行期再清）。"""
        if self._path is None or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            entries = raw.get("sessions") if isinstance(raw, dict) else None
        except Exception as e:  # 文件损坏/被手工改坏 → 当没有，别拦启动
            log.warning("会话文件读取失败（按空会话启动）：%s", e)
            return
        now = time.time()
        kept = dropped = 0
        for k, v in (entries or {}).items():
            if not isinstance(v, dict):
                continue
            created = v.get("created")
            if not isinstance(created, (int, float)) or now - created > self._ttl:
                dropped += 1
                continue
            self._s[str(k)] = {"created": float(created), "ip": str(v.get("ip") or ""),
                               "account": str(v.get("account") or "")}
            kept += 1
        if kept or dropped:
            log.info("已载入 %d 个会话（丢弃过期 %d 个）", kept, dropped)

    def _save(self) -> None:
        """整体重写（会话量小：上限 max_tokens 条）。写失败只告警。"""
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps({"version": _FILE_VERSION, "sessions": self._s},
                              ensure_ascii=False, separators=(",", ":"))
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except Exception as e:
            log.warning("会话落盘失败（本次改动只在内存生效）：%s", e)

    # ── 基本属性 ─────────────────────────────────────
    def ttl(self) -> int:
        return self._ttl

    def set_ttl(self, seconds: int) -> None:
        """按当前配置动态调整会话时长（下限 60s，防误配成 0 让会话瞬间过期）。"""
        self._ttl = max(60, int(seconds))

    def count(self) -> int:
        with self._lock:
            changed = self._purge()
            n = len(self._s)
            if changed:
                self._save()
            return n

    # ── 增删查改 ─────────────────────────────────────
    def create(self, ip: str, account: str = "") -> str:
        with self._lock:
            self._purge()
            if len(self._s) >= self._max:  # 超上限：踢最老的
                oldest = min(self._s, key=lambda k: self._s[k]["created"])
                del self._s[oldest]
            tok = "sess-" + secrets.token_hex(24)
            # ip 只做记录、不做绑定：移动网络/NAT 下换出口 IP 是常态，绑定会把人踢下线。
            self._s[_fp(tok)] = {"created": time.time(), "ip": ip or "", "account": account or ""}
            self._save()
            return tok

    def validate(self, token: str, ip: str | None = None) -> bool:
        if not token:
            return False
        with self._lock:
            ent = self._s.get(_fp(token))
            if not ent:
                return False
            if time.time() - ent["created"] > self._ttl:
                del self._s[_fp(token)]
                self._save()
                return False
            return True

    def account_for(self, token: str) -> str | None:
        """返回会话所属账号（用于指标/日志归属与鉴权 identity）。"""
        if not token or not self.validate(token):
            return None
        with self._lock:
            ent = self._s.get(_fp(token))
            return (ent or {}).get("account")

    def is_session(self, token: str) -> bool:
        """该凭证是否为**密码登录换来的会话**（区别于长期 API Key）。

        账号管理/改密/铸 Key 这类「改凭证」操作只认会话凭证 —— 泄漏的 API Key
        就不可能再给自己铸一把新 Key 或改密码，堵住持久化后门。
        """
        return bool(token and token.startswith("sess-") and self.validate(token))

    def revoke(self, token: str) -> None:
        with self._lock:
            if self._s.pop(_fp(token), None) is not None:
                self._save()

    def revoke_account(self, account: str) -> int:
        """吊销某账号的全部会话，返回被踢掉的会话数（改密/重置密码/删号时调用）。"""
        if not account:
            return 0
        with self._lock:
            dead = [k for k, v in self._s.items() if v.get("account") == account]
            for k in dead:
                del self._s[k]
            if dead:
                self._save()
            return len(dead)

    def clear(self) -> None:
        with self._lock:
            had = bool(self._s)
            self._s.clear()
            if had:
                self._save()

    def _purge(self) -> bool:
        """清过期会话；返回是否有改动（有才值得落盘）。"""
        now = time.time()
        changed = False
        for k in list(self._s):
            if now - self._s[k]["created"] > self._ttl:
                del self._s[k]
                changed = True
        return changed
