"""适配器基类：定义统一的 chat / 流式 / 图像 / 视频接口。"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx

from app.config import MediaConfig


class UpstreamResponseError(RuntimeError):
    """上游返回了"协议外"的响应（HTTP 200 但不是 JSON / body 为空）。

    第六轮: 此前这类响应直接在 `resp.json()` 处抛 json.JSONDecodeError，
    错误信息是 `Expecting value: line 1 column 1 (char 0)` —— 与真正的上游 5xx
    无法区分，排查时极具误导性。
    """


def json_or_raise(resp) -> dict:
    """解析上游 JSON；失败时抛带 HTTP 状态与响应片段的 UpstreamResponseError。"""
    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError) as e:
        snippet = (resp.text or "")[:200]
        raise UpstreamResponseError(
            f"上游返回非 JSON 响应（HTTP {resp.status_code}）: {snippet!r}") from e
    if not isinstance(data, dict):
        raise UpstreamResponseError(
            f"上游返回了非预期的 JSON 类型（{type(data).__name__}），HTTP {resp.status_code}")
    return data


class BaseAdapter(ABC):
    """所有供应商适配器必须实现此接口。"""

    def __init__(self, name: str, base_url: str, timeout: int = 120, media: MediaConfig | None = None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.media = media or MediaConfig()
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()   # Bug 1: 防并发竞态建多个 client
        self._closing = False                # 正在物理关闭连接
        self._close_requested = False        # 收到关闭请求，但仍有在途请求，等待排空后再关
        self._active = 0                     # 在途请求/流占用数（Bug 4: 热重载不打断在途请求）

    async def get_client(self) -> httpx.AsyncClient:
        """获取共享 client 并占用一次（并发安全）。

        之前：两个 coroutine 同时 if self._client is None 各建一个 → 泄漏。
        现在：用 asyncio.Lock 保证只建一次，并 +1 占用计数（每次在途请求/流占用一次）。
        """
        async with self._client_lock:
            if self._client is None or self._closing:
                self._closing = False
                self._client = httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=httpx.Timeout(self.timeout),
                )
            self._active += 1
            return self._client

    async def _release(self) -> None:
        """归还一次占用。最后一次归还且曾收到关闭请求时，才真正关闭连接池。

        Bug 4 修复: 让 close() 能感知『仍有请求在用』而延迟真正的网络关闭，
        避免热重载关闭 client 时打断正在流式/在途的请求。
        """
        async with self._client_lock:
            self._active = max(0, self._active - 1)
            if self._active == 0 and self._close_requested and self._client is not None:
                self._closing = True
                try:
                    await self._client.aclose()
                except Exception:
                    pass
                self._client = None
                self._closing = False
                self._close_requested = False

    @asynccontextmanager
    async def _lease(self) -> AsyncIterator[httpx.AsyncClient]:
        """async context manager：占用 client 执行一次网络操作，结束自动归还占用。"""
        client = await self.get_client()
        try:
            yield client
        finally:
            await self._release()

    async def _close_now_locked(self) -> None:
        """物理关闭连接池。**调用方必须已持有 _client_lock。**

        第六轮并发审计实测的竞态：`close()` 先在锁内判定 `_active == 0`，出锁后再调
        `_close_now()` 重新抢锁 —— 两步之间排队的 `get_client()` 可能先拿到锁、把
        `_active` 加到 1 并返回该 client，随后它被 aclose() 掉，在途请求直接
        `RuntimeError: Cannot send a request, as the client has been closed.`
        现在把"判定 + 关闭"合并到同一次持锁内完成。
        """
        if self._client is not None:
                self._closing = True
                try:
                    await self._client.aclose()
                except Exception:
                    pass
                self._client = None
                self._closing = False
                self._close_requested = False

    async def close(self) -> None:
        """关闭 client（幂等可重入）。

        之前：并发两次 close → 第二次对已关闭 client 抛 RuntimeError。
        现在：有在途请求时置 _close_requested 等待排空（由最后一次 _release 关闭）；
              无在途请求则立即关闭。重复调用安全。
        """
        async with self._client_lock:
            if self._client is None and not self._close_requested:
                return
            if self._active > 0:
                self._close_requested = True  # 等在途请求排空后由最后一次 _release 关闭
                return
            # 第六轮: 置位 + 关闭都在同一次持锁内完成（见 _close_now_locked 的说明）
            self._close_requested = True
            await self._close_now_locked()

    @abstractmethod
    async def chat(
        self,
        model: str,
        messages: list[dict],
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
        stop: str | list[str] | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """非流式 chat completion，返回统一格式的 dict。"""
        ...

    @abstractmethod
    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        stop: str | list[str] | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict]:
        """流式 chat completion，逐块 yield。"""
        ...

    # ── 图像 / 视频（可选实现，默认不支持） ──────────
    async def generate_image(
        self,
        model: str,
        prompt: str,
        size: str | None = None,
        ratio: str | None = None,
        image: list[str] | None = None,
        n: int = 1,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError(f"供应商 {self.name} 不支持图像生成")

    async def create_video(
        self,
        model: str,
        prompt: str,
        seconds: str = "5",
        mode: str = "text",
        aspect_ratio: str = "16:9",
        size: str = "720P",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError(f"供应商 {self.name} 不支持视频创建")

    async def poll_video(self, video_id: str, model: str = "", extra: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError(f"供应商 {self.name} 不支持视频查询")
