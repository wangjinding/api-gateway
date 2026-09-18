"""视频任务归属（第三十轮新增）。

**为什么需要**：`GET /v1/videos/{video_id}` 原先不做归属判断（第十轮的口径是"轮询端点不记
访问行，故不取 caller"）。结果是：任何**已登录账号**只要拿到 video_id，就能读到别人视频任务
的状态与产物 URL。视频 id 是供应商私有的随机串、猜不到，但与 `/logs`、`/metrics`、`/history`
那套「按账号隔离」的口径不一致 —— 一个账号能看到的、另一个账号也能看到的资源，就不该靠"猜不到"当防线。

**怎么做**：创建视频成功那一刻（`POST /v1/videos` 拿到上游 `video_id`）记一条
`video_id → 账号`，轮询时按它判归属。

**口径（刻意选择）**：
  · 管理员放行一切（管理员本来就该能排障；且 admin 的历史/日志本就不隔离）。
  · 记录里**有**这个 id 且不是请求者 → 404「视频任务不存在」。用 404 而不是 403：
    403 等于确认"这个 id 存在"，会把 id 变成可探测的。
  · 记录里**没有**这个 id（本次改动之前创建的视频、落盘文件丢失、超 TTL 被清）→ **放行**。
    这是刻意的渐进收紧：宁可对老任务宽松，也不要让正常流程在部署当天突然 404。
    新创建的任务一律有记录，所以保护是即时生效的。

**存储**：`data/video_owners.json`（600，原子写）。量小（只存 id/账号/时间），
按 TTL（默认 7 天）与条数上限滚动清理 —— 视频产物本身也不会在历史里留太久。
path=None → 纯内存（测试/库内直连构造用）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("api-gateway.video_owners")

_FILE_VERSION = 1


class VideoOwnerStore:
    def __init__(self, path: str | os.PathLike | None = None,
                 ttl_seconds: int = 7 * 86400, max_items: int = 2000):
        self._path = Path(path) if path else None
        self._ttl = int(ttl_seconds)
        self._max = int(max_items)
        self._m: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._load()

    # ── 落盘 ─────────────────────────────────────────
    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            items = raw.get("videos") if isinstance(raw, dict) else None
        except Exception as e:
            log.warning("视频归属文件读取失败（按空表启动）：%s", e)
            return
        now = time.time()
        for k, v in (items or {}).items():
            if not isinstance(v, dict):
                continue
            created = v.get("created")
            if not isinstance(created, (int, float)) or now - created > self._ttl:
                continue
            self._m[str(k)] = {"account": str(v.get("account") or ""),
                               "provider": str(v.get("provider") or ""),
                               "created": float(created)}
        if self._m:
            log.info("已载入 %d 条视频归属记录", len(self._m))

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps({"version": _FILE_VERSION, "videos": self._m},
                              ensure_ascii=False, separators=(",", ":"))
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except Exception as e:
            # 落盘失败只影响"重启后还认不认归属"（会退化成放行），不该让创建视频这条主链路失败。
            log.warning("视频归属落盘失败：%s", e)

    # ── 读写 ─────────────────────────────────────────
    def record(self, video_id: str, account: str, provider: str = "") -> None:
        """创建视频成功后登记归属。video_id 为空则忽略（上游没回 id 时不记脏数据）。"""
        if not video_id:
            return
        with self._lock:
            now = time.time()
            self._m[video_id] = {"account": account or "", "provider": provider or "",
                                 "created": now}
            self._prune(now)
            self._save()

    def owner(self, video_id: str) -> str | None:
        """返回登记归属；没有记录返回 None（≠"不属于任何人"，见模块头部的口径）。"""
        if not video_id:
            return None
        with self._lock:
            ent = self._m.get(video_id)
            if not ent:
                return None
            if time.time() - ent["created"] > self._ttl:
                del self._m[video_id]
                self._save()
                return None
            return ent.get("account") or None

    def allows(self, video_id: str, account: str, is_admin: bool = False) -> bool:
        """该账号能否访问这个视频任务。无记录 → 放行（渐进收紧，见模块头部）。"""
        if is_admin:
            return True
        who = self.owner(video_id)
        return who is None or who == (account or "")

    def count(self) -> int:
        with self._lock:
            self._prune(time.time())
            return len(self._m)

    def _prune(self, now: float) -> None:
        for k in [k for k, v in self._m.items() if now - v["created"] > self._ttl]:
            del self._m[k]
        while len(self._m) > self._max:   # 超上限：先清最老的
            oldest = min(self._m, key=lambda k: self._m[k]["created"])
            del self._m[oldest]
