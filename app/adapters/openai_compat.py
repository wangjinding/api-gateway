"""OpenAI 兼容协议适配器。

Agnes / 火山方舟 两家均使用 OpenAI 兼容 API
（POST {base_url}/chat/completions），本适配器统一处理。

图像/视频端点可通过 media 配置覆盖：
  - 图像：默认 {base_url}/images/generations（OpenAI 兼容）
  - 视频：Agnes 用 {base_url}/videos 创建 + {poll_base}{poll_path}?video_id=...&model_name=... 轮询
"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator


from app.adapters.base import BaseAdapter, json_or_raise
from app.models import CHAT_CORE_KEYS, PROTECTED_VIDEO_KEYS, strip_protected


class OpenAICompatAdapter(BaseAdapter):
    """适用于所有 OpenAI 兼容 /chat/completions 端点的适配器。"""

    def __init__(
        self,
        name: str,
        base_url: str,
        headers: dict[str, str] | None = None,
        timeout: int = 120,
        media=None,
        requires_session_header: bool = False,
    ):
        super().__init__(name=name, base_url=base_url, timeout=timeout, media=media)
        self._headers: dict[str, str] = headers or {}
        # OpenCode Go 要求每个 /chat/completions 带稳定 x-opencode-session。
        # 不能放静态 headers（_get_adapter 会缓存适配器、冻结 header），
        # 所以在每次请求时动态生成一个 UUID 会话头。
        self._requires_session = requires_session_header

    def _req_headers(self) -> dict[str, str]:
        """每次请求的 headers。需要会话头的供应商动态注入 x-opencode-session。"""
        if not self._requires_session:
            return self._headers
        headers = dict(self._headers)
        headers["x-opencode-session"] = uuid.uuid4().hex
        return headers

    def _build_payload(
        self,
        model: str,
        messages: list[dict],
        temperature: float | None,
        top_p: float | None,
        max_tokens: int | None,
        stream: bool,
        stop: str | list[str] | None,
        tools: list[dict] | None,
        tool_choice: str | dict | None,
        extra: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": stream}
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if stop is not None:
            payload["stop"] = stop
        if tools:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        # 合并供应商特有参数（如 Agnes 的 chat_template_kwargs）。
        # 第六轮安全修复: 先剔除核心键 —— 否则 `extra={"model": ..., "stream": ...}`
        # 会覆盖掉上面写好的值（路由劫持 / 协议错配）。models.upstream_extra() 已过滤，
        # 这里是适配器侧的最后一道防线。
        # 只剔除"本函数上面刚写好的核心键"；其余（网关 _PASSTHROUGH 的标准参数、
        # 以及供应商特有参数）必须放行 —— 见 models.CHAT_CORE_KEYS 的说明。
        if extra:
            payload.update(strip_protected(extra, CHAT_CORE_KEYS))
        return payload

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
        payload = self._build_payload(
            model, messages, temperature, top_p, max_tokens,
            stream, stop, tools, tool_choice, extra,
        )
        # Bug 4 修复: 用 lease 占用 client，热重载 close 会等本次请求结束才真正关连接
        async with self._lease() as client:
            resp = await client.post("/chat/completions", json=payload, headers=self._req_headers())
            resp.raise_for_status()
            data = json_or_raise(resp)

        # 统一返回格式
        return {
            "provider": self.name,
            "model": model,
            "choices": data.get("choices", []),
            "usage": data.get("usage"),
            "raw": data,
            "id": data.get("id", ""),
        }

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
        payload = self._build_payload(
            model, messages, temperature, top_p, max_tokens,
            True, stop, tools, tool_choice, extra,
        )
        # Bug 4 修复: lease 在整个流式期间持有占用，热重载/关闭不会掐断在途流
        async with self._lease() as client:
            async with client.stream(
                "POST", "/chat/completions", json=payload, headers=self._req_headers()
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:]
                    if raw.strip() == "[DONE]":
                        yield {"done": True}
                        break
                    # 本轮修复: 上游可能有非 JSON 的行（keep-alive `data: ping`、空行、
                    #   注释、被截断的半行）。此前 json.loads 抛异常会直接**中断整个流式响应**，
                    #   客户端拿到一半就断。现在跳过坏行并继续。
                    try:
                        data = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    yield data

    # ── 图像生成 ────────────────────────────────────
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
        endpoint = self.media.image_endpoint or "/images/generations"
        # Bug 12: 默认档位来自 media.image_default_size（默认 1K 兜底，Agnes 要求 size 必填）
        size = size or self.media.image_default_size or "1K"
        payload: dict[str, Any] = {"model": model, "prompt": prompt, "size": size}
        # #5: 仅当需要多张才带 n（Agnes 一次一张，n=1 省略避免噪音）
        if n and n != 1:
            payload["n"] = n
        if ratio:
            payload["ratio"] = ratio
        # 图生图 / 多图：图像 URL 放 extra_body.image（Agnes 写法）
        extra_body: dict[str, Any] = dict(extra or {})
        if image:
            extra_body["image"] = image
        if extra_body:
            payload["extra_body"] = extra_body

        async with self._lease() as client:
            resp = await client.post(endpoint, json=payload, headers=self._headers)
            resp.raise_for_status()
            data = json_or_raise(resp)
        # 统一：返回 data 数组
        return {"data": data.get("data", []), "raw": data}

    # ── 视频创建（异步任务） ────────────────────────
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
        # ── Agnes 风格：POST {base}/videos，seconds/mode/size/aspect_ratio/n ──
        endpoint = self.media.video_create_endpoint or "/videos"
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "seconds": seconds,
            "mode": mode,
            "size": size,
            "aspect_ratio": aspect_ratio,
            "n": 1,
        }
        # 合并模式专用字段（first_frame/last_frame/images/audios 等）；
        # 第六轮: 但不允许覆盖 model/prompt/seconds/mode/size/aspect_ratio（同 chat 的越权面）。
        for k, v in strip_protected(extra, PROTECTED_VIDEO_KEYS).items():
            payload[k] = v

        async with self._lease() as client:
            resp = await client.post(endpoint, json=payload, headers=self._headers)
            resp.raise_for_status()
            data = json_or_raise(resp)
        # video_id 用于查询；id/task_id 是任务 id
        return {
            "video_id": data.get("video_id") or data.get("id") or data.get("task_id"),
            "raw": data,
        }

    # ── 视频轮询 ────────────────────────────────────
    async def poll_video(self, video_id: str, model: str = "", extra: dict[str, Any] | None = None) -> dict[str, Any]:
        mname = self.media.video_model_name or model or ""
        poll_base = self.media.video_poll_base or self.base_url
        poll_path = self.media.video_poll_path or "/agnesapi"

        params: dict[str, str] = {"video_id": video_id}
        if mname:
            params["model_name"] = mname

        # 轮询用绝对 URL（poll_base 可能是去掉 /v1 的域名根）
        url = poll_base.rstrip("/") + poll_path
        async with self._lease() as client:
            resp = await client.get(url, params=params, headers=self._headers)
            resp.raise_for_status()
            data = json_or_raise(resp)
        return {"raw": data}
