"""OpenAI 兼容协议适配器。

Agnes / OpenCode Go / Dots AI 均使用 OpenAI 兼容 API
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
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx

from app.adapters.base import BaseAdapter, json_or_raise
from app.config import (
    PROTOCOL_CHAT,
    EndpointSpec,
    MODEL_PARAM_KEYS,
    _default_endpoints,
)
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
    """适用于所有 OpenAI 兼容 /chat/completions 端点的适配器。

    第二十七轮（三线直传）后它同时承担**协议直传**职责：同一家供应商的三条线
    （chat/completions、messages、responses）共用这一个适配器、共用一个连接池，
    差别只在「路径 + 鉴权头」——由 provider 的 `endpoints` 配置提供（见 config.EndpointSpec）。
    直传路径**不翻译任何内容**：入站 body 原样出去，上游响应原样回来。
    """

    def __init__(
        self,
        name: str,
        base_url: str,
        headers: dict[str, str] | None = None,
        timeout: int = 120,
        media=None,
        requires_session_header: bool = False,
        model_params: dict[str, dict[str, Any]] | None = None,
        token: str = "",
        endpoints: dict[str, EndpointSpec] | None = None,
    ):
        super().__init__(name=name, base_url=base_url, timeout=timeout, media=media)
        self._headers: dict[str, str] = headers or {}
        # 第二十七轮: 每条协议线的出站配置（路径 / 鉴权方式 / 该线固定头）。
        #   不传 = 三条标准线的默认值，所以旧调用点（含单测直接 new）行为不变。
        self._endpoints: dict[str, EndpointSpec] = dict(endpoints or _default_endpoints())
        # 裸 token：每条线要发的鉴权头不同（Bearer / x-api-key），所以不能在构造时就把
        #   header 拼死。没显式传就回头从静态 headers 里解析（兼容直接 new 的调用方）。
        self._token = token or self._token_from_headers(self._headers)
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

    @staticmethod
    def _token_from_headers(headers: dict[str, str]) -> str:
        """从静态 headers 里反解 token（兼容历史调用方式：构造时就带 Authorization）。"""
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return headers.get("x-api-key") or ""

    # ── 协议线（第二十七轮） ─────────────────────────

    def endpoint(self, protocol: str) -> EndpointSpec | None:
        """取某条线的出站配置（None = 该供应商没有这条线）。"""
        return self._endpoints.get(protocol)

    def _path(self, protocol: str) -> str:
        ep = self._endpoints.get(protocol)
        if ep is None:
            # 走到这里说明配置校验漏了（validate_config 会拦「模型声明了但供应商没配」），
            #   抛清晰错误而不是退化成默认路径 —— 猜出来的地址只会收到 404，更难查。
            raise ValueError(f"供应商 {self.name} 未配置协议线 {protocol}")
        return ep.path

    def auth_headers(self, protocol: str) -> dict[str, str]:
        """按该线的鉴权方式组装鉴权头（Bearer / x-api-key / api-key / 无）。

        这是三线直传里"唯一必须做的 glue"：同一个供应商的三条线鉴权方式可能不同
        （实测 OpenCode Go：chat/completions 与 responses 认 `Authorization: Bearer`，
        messages 认 `x-api-key`；用 Bearer 打 /messages 会 401 Missing API key）。
        """
        ep = self._endpoints.get(protocol)
        style = (ep.auth if ep else "bearer")
        out: dict[str, str] = {}
        if style == "bearer" and self._token:
            out["Authorization"] = f"Bearer {self._token}"
        elif style == "x-api-key" and self._token:
            out["x-api-key"] = self._token
        elif style == "api-key" and self._token:
            # 第三十六轮：头名就叫 `api-key`（Dots AI 官方文档如此；不是 x-api-key）
            out["api-key"] = self._token
        if ep:
            out.update(ep.headers or {})       # 如 anthropic-version
        return out
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

    def _req_headers(self, session_id: str | None = None,
                     protocol: str = PROTOCOL_CHAT) -> dict[str, str]:
        """每次请求的 headers：静态头 + 本条线的鉴权头 + 会话头。

        session_id 由调用方传入（网关把对话的 conversation_id 透传过来）：
        - 有 → 同一段对话的所有请求共用同一个 session（上游据此做路由与缓存）；
        - 无 → 回退为每请求一个随机 UUID（仍能过上游的强校验，只是没有缓存收益）。

        第二十七轮实测：OpenCode Go 的**三条线都硬性要求该头**（缺任一条 → 400
        MissingSessionID），所以注入逻辑与协议线无关，统一在这里做。
        """
        headers = dict(self._headers)
        # 鉴权头按线走：先清掉静态头里可能残留的鉴权（历史写法：create_adapter 曾把
        #   Authorization 塞进静态 headers），再由 auth_headers 按该线的方式补上。
        headers.pop("Authorization", None)
        headers.pop("authorization", None)
        headers.pop("x-api-key", None)
        headers.pop("api-key", None)
        headers.update(self.auth_headers(protocol))
        # 第三十轮: 这里原来写「无 session → 每请求一个随机 UUID（仍能过上游强校验，只是没有缓存收益）」，
        #   那句话在第二十九轮之后**已经不对了** —— 网关三条协议线入口（chat 非流式/流式、batch、
        #   三线直传）现在都传 `session_id`，随机回退只在"适配器被直接调用且没给 session"时才可能命中。
        #   留 uuid4 是兜底（保证不缺头 → 不会 400 MissingSessionID），正常流量走不到这里。
        if self._requires_session:
            headers["x-opencode-session"] = session_id or uuid.uuid4().hex
        return headers

    def _media_headers(self) -> dict[str, str]:
        """媒体端点（图像生成 / 视频创建 / 视频轮询）的 headers。

        媒体端点不属于「三条协议线」，所以不走 `_req_headers` —— 历史口径是固定用 Bearer。
        第二十七轮把鉴权头改成「按协议线在 `_req_headers` 里组装」之后，这三个调用点却还在
        用静态 `self._headers`，而 `create_adapter` 已经不再往静态头里塞 Authorization
        ⇒ **token 一个都没带出去**。真机表现：`POST /v1/images/generations` 上游 401
        「未提供令牌」（同一个 key 直接 curl 同一个地址是 200）。
        这里统一补回来：静态头 + Bearer（有 token 时）+ 会话头（供应商要求时）。
        """
        headers = dict(self._headers)
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        # 第三十轮: 这里的会话头**故意**保持每请求随机，不是漏改。
        #   媒体端点（图像/视频）是一次性任务，没有"同一会话复读同一大前缀"的场景，
        #   也就没有前缀缓存可吃；反过来，若给它一个跨请求固定的会话，反而会让上游把
        #   互不相关的图片/视频任务记到同一个会话名下，污染那边的会话视图。
        #   注意当前配了媒体能力的供应商都不是 opencode（opencode 无 media），这行实际走不到。
        if self._requires_session:
            headers["x-opencode-session"] = uuid.uuid4().hex
        return headers

    # ── 协议直传（第二十七轮）────────────────────────
    # 「入站是什么格式，出站就是什么格式」：这两个原语把入站 body **原样**发到该线的上游
    # 地址，**不翻译任何字段**。网关只做三件事：① 挑对供应商与线；② 把 model 改成真实
    # id 并盖上模型级强制参数；③ 原样把响应（含 SSE）交回客户端。
    # 中间不做 has_images 之类的语义判断，也不解析 body 结构。

    def prepare_payload(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        """直传前的定点改写：model 换成真实 id + 盖模型级强制参数（其余一律不动）。

        - model：入站就是真实模型 id（第二十七轮起路由别名已删除，不再有别名解析，
          网关也不会替你换模型）—— 这里仍保留"定点改写 model"的能力：调用方写的 id
          必须落到某条线能认的 id 上；
        - 强制参数：`models[].params`（如 kimi-k2.7-code 只接受 temperature=1）在直传里
          同样必须生效，否则"直传"就成了"漏掉网关的硬约束"。
        这两处是**唯一**允许改写入站 body 的地方（改的都是"发给谁/必须用什么参数"，
        不是协议语义），其余字段一律原样。
        """
        out = dict(payload)
        out["model"] = model
        forced = self._forced_params(model)
        if forced:
            out.update(forced)
        return out

    async def raw_json(self, protocol: str, payload: dict[str, Any],
                       session_id: str | None = None) -> tuple[int, Any]:
        """非流式直传：返回 (上游状态码, 上游 JSON/文本)。

        刻意**不 raise_for_status**：直传口径下上游的错误体要原样交回客户端
        （老板决策："其他都不转" —— 错误文案也不转）。熔断/指标由调用方按状态码记。
        """
        async with self._lease() as client:
            resp = await client.post(self._path(protocol), json=payload,
                                     headers=self._req_headers(session_id, protocol))
            body = resp.content
            ctype = resp.headers.get("content-type", "")
        if "application/json" in ctype:
            try:
                return resp.status_code, json.loads(body)
            except (ValueError, json.JSONDecodeError):
                pass
        # 非 JSON（或声明 JSON 但解析失败）：把原始文本交回，别在这里编造结构
        return resp.status_code, body.decode("utf-8", "replace")

    @asynccontextmanager
    async def raw_stream(self, protocol: str, payload: dict[str, Any],
                         session_id: str | None = None):
        """流式直传：上下文内可直接读 `resp`（状态码 + 字节流）。

        为什么用 asynccontextmanager 而不是普通 async generator：调用方需要**先看到
        状态码**再决定返回 SSE（200）还是上游的 JSON 错误（4xx/5xx）。同时 lease 必须在
        整个流式期间持有（热重载/关闭不会掐断在途流，与 _stream_once 同一处理）。
        """
        async with self._lease() as client:
            async with client.stream("POST", self._path(protocol), json=payload,
                                     headers=self._req_headers(session_id, protocol)) as resp:
                yield resp

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
            resp = await client.post(self._path(PROTOCOL_CHAT), json=payload,
                                     headers=self._req_headers(session_id, PROTOCOL_CHAT))
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
                "POST", self._path(PROTOCOL_CHAT), json=payload,
                headers=self._req_headers(session_id, PROTOCOL_CHAT)
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
            resp = await client.post(endpoint, json=payload, headers=self._media_headers())
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
            resp = await client.post(endpoint, json=payload, headers=self._media_headers())
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
            resp = await client.get(url, params=params, headers=self._media_headers())
            resp.raise_for_status()
            data = json_or_raise(resp)
        return {"raw": data}
