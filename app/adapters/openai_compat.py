"""OpenAI 兼容协议适配器。

Agnes / 火山方舟 两家均使用 OpenAI 兼容 API
（POST {base_url}/chat/completions），本适配器统一处理。

图像/视频端点可通过 media 配置覆盖：
  - 图像：默认 {base_url}/images/generations（OpenAI 兼容）
  - 视频：Agnes 用 {base_url}/videos 创建 + {poll_base}{poll_path}?video_id=...&model_name=... 轮询
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, AsyncIterator

import httpx

from app.adapters.base import BaseAdapter, json_or_raise
from app.config import MODEL_PARAM_KEYS
from app.models import CHAT_CORE_KEYS, PROTECTED_VIDEO_KEYS, strip_protected

log = logging.getLogger("api-gateway")

# ── 上游"某参数只允许取某个值"的报错形态（第二十五轮）──
# 各家措辞不同，这里收录已实测到的两类：
#   OpenCode Go: "Error from provider (Console Go): [invalid_request_error]
#                 invalid temperature: only 1 is allowed for this model"
#   "max_tokens must be greater than 2"
# 参数名不写死 —— 同类约束也可能出现在 top_p / max_tokens 上。
_PARAM_ONLY_RE = re.compile(
    r"invalid\s+([a-z_][a-z0-9_]*)\s*:\s*only\s+([-+]?\d+(?:\.\d+)?)\s+is allowed", re.I)
_PARAM_MIN_RE = re.compile(
    r"([a-z_][a-z0-9_]*)\s+must be greater than\s+(\d+)", re.I)



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
        model_params: dict[str, dict[str, Any]] | None = None,
    ):
        super().__init__(name=name, base_url=base_url, timeout=timeout, media=media)
        self._headers: dict[str, str] = headers or {}
        # OpenCode Go 要求每个 /chat/completions 带稳定 x-opencode-session。
        # 不能放静态 headers（_get_adapter 会缓存适配器、冻结 header），
        # 所以在每次请求时动态生成一个 UUID 会话头。
        self._requires_session = requires_session_header
        # 模型级强制参数：model id → {参数名: 强制值}（来自 gateway.yaml 的 models[].params）
        self._model_params: dict[str, dict[str, Any]] = {
            k: dict(v) for k, v in (model_params or {}).items() if v
        }
        # 运行期自学习到的参数约束（进程内，不落盘）：见 _learn_from_upstream_error。
        #   仅作兜底 —— 管理员在 gateway.yaml 写死 params 后就不再需要它。
        self._learned_params: dict[str, dict[str, Any]] = {}

    # ── 模型级强制参数 / 上游参数约束自学习（第二十五轮） ──

    def _forced_params(self, model: str) -> dict[str, Any]:
        """该模型发往上游前**必须使用**的参数值。

        两层，配置优先：`gateway.yaml` 的 `models[].params` > 运行期自学习值。
        配置优先是刻意的 —— 管理员写死的口径是最终口径，自学习绝不能把它改掉。
        """
        out = dict(self._learned_params.get(model) or {})
        out.update(self._model_params.get(model) or {})
        return out

    def _learn_from_upstream_error(self, model: str, exc: Exception) -> dict[str, Any] | None:
        """上游 400 说"这个参数只允许 X"→ 记下 X，并返回修正后的参数集供重发。

        返回 None = 报文里没识别出可修正的参数（调用方按原样抛错）。

        这不是「重试」：报文一模一样地重发只会再吃一次同样的 400，必须先把参数改成
        上游允许的值。学到之后会缓存到本适配器实例，后续请求直接按修正值组装 payload，
        不再多跑这一轮往返；配置变更时 main 会丢弃适配器缓存，学习值随之失效。
        """
        resp = getattr(exc, "response", None)
        if resp is None or getattr(resp, "status_code", None) != 400:
            return None
        try:
            text = resp.text or ""
        except Exception:
            return None

        fixes: dict[str, Any] = {}
        for m in _PARAM_ONLY_RE.finditer(text):
            name = m.group(1).lower()
            if name in MODEL_PARAM_KEYS:
                raw = m.group(2)
                fixes[name] = float(raw) if "." in raw else int(raw)
        for m in _PARAM_MIN_RE.finditer(text):
            name = m.group(1).lower()
            if name in MODEL_PARAM_KEYS and name not in fixes:
                fixes[name] = int(m.group(2)) + 1     # “must be greater than N” → 发 N+1
        if not fixes:
            return None

        cfg_params = self._model_params.get(model) or {}
        learned = dict(self._learned_params.get(model) or {})
        changed: dict[str, Any] = {}
        for k, v in fixes.items():
            if k in cfg_params:
                # 管理员已在 params 里写死该参数 → 不偷偷覆盖配置；但冲突必须说出来，
                #   否则用户只会看到「明明配了却还是 400」，无从下手。
                if cfg_params[k] != v:
                    log.warning("模型 %s 的 params.%s=%r 与上游要求（%r）冲突，仍按配置发送",
                                model, k, cfg_params[k], v)
                continue
            if learned.get(k) != v:
                learned[k] = v
                changed[k] = v
        if not changed:
            return None      # 没学到新东西 → 不重发，避免在同一约束上来回打转
        self._learned_params[model] = learned
        log.warning("上游限制了模型 %s 的参数：%s —— 已自动修正并按修正值重发一次；"
                    "建议在 gateway.yaml 该模型下写死 params，省掉以后每次这一轮额外往返",
                    model, changed)
        return self._forced_params(model)

    def _req_headers(self, session_id: str | None = None) -> dict[str, str]:
        """每次请求的 headers。需要会话头的供应商动态注入 x-opencode-session。

        session_id 由调用方传入（网关把对话的 conversation_id 透传过来）：
        - 有 → 同一段对话的所有请求共用同一个 session（吃到上游提示词缓存）；
        - 无 → 回退为每请求一个随机 UUID（仍能过上游的强校验，只是没有缓存收益）。
        """
        if not self._requires_session:
            return self._headers
        headers = dict(self._headers)
        headers["x-opencode-session"] = session_id or uuid.uuid4().hex
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
        # 第二十五轮: 模型级强制参数放在**最后**覆盖 —— 客户端（含 extra 逃生舱）改不动它。
        #   这是应对"上游只接受某个值"的最终口径，优先级高于客户端的一切输入。
        forced = self._forced_params(model)
        if forced:
            payload.update(forced)
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
        session_id: str | None = None,
    ) -> dict[str, Any]:
        payload = self._build_payload(
            model, messages, temperature, top_p, max_tokens,
            stream, stop, tools, tool_choice, extra,
        )
        try:
            return await self._post_chat(payload, session_id)
        except httpx.HTTPStatusError as e:
            # 上游嫌参数（如 temperature）不合法时，按它要求的值修正后**重发一次**。
            #   fixed 为 None（不是参数约束 / 已学过仍失败）则原样抛错，不做无谓重发。
            fixed = self._learn_from_upstream_error(model, e)
            if fixed is None:
                raise
            return await self._post_chat({**payload, **fixed}, session_id)

    async def _post_chat(self, payload: dict[str, Any], session_id: str | None) -> dict[str, Any]:
        """执行一次非流式 /chat/completions 请求（不含参数修正逻辑）。"""
        # Bug 4 修复: 用 lease 占用 client，热重载 close 会等本次请求结束才真正关连接
        async with self._lease() as client:
            resp = await client.post("/chat/completions", json=payload, headers=self._req_headers(session_id))
            resp.raise_for_status()
            data = json_or_raise(resp)

        # 统一返回格式
        return {
            "provider": self.name,
            "model": payload.get("model", ""),
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
        session_id: str | None = None,
    ) -> AsyncIterator[dict]:
        payload = self._build_payload(
            model, messages, temperature, top_p, max_tokens,
            True, stop, tools, tool_choice, extra,
        )
        started = False
        try:
            async for chunk in self._stream_once(payload, session_id):
                started = True
                yield chunk
            return
        except httpx.HTTPStatusError as e:
            # 已经开始吐内容就不能重发（会重复输出 / 串话）；只有"开局就被上游 400 顶回来"
            #   才允许按上游要求修正参数重来一次。
            if started:
                raise
            fixed = self._learn_from_upstream_error(model, e)
            if fixed is None:
                raise
            payload = {**payload, **fixed}
        async for chunk in self._stream_once(payload, session_id):
            yield chunk

    async def _stream_once(self, payload: dict[str, Any],
                           session_id: str | None) -> AsyncIterator[dict]:
        """执行一次流式 /chat/completions（不含参数修正逻辑）。"""
        # Bug 4 修复: lease 在整个流式期间持有占用，热重载/关闭不会掐断在途流
        async with self._lease() as client:
            async with client.stream(
                "POST", "/chat/completions", json=payload, headers=self._req_headers(session_id)
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
