"""QwenPaw 引擎适配器（第三十八轮 · 路线 A）。

把网关的对话请求转成 QwenPaw 的 **AgentScope Agent API** 形状，再把它的 SSE 事件流
翻译回网关前端认识的形式。

协议事实全部来自实测 —— 见 `docs/qwenpaw-agent-api-sse.md` 与
`tests/fixtures/sse-tool-flow.sse`。要点：

- `POST {base}/api/console/chat`，body `{input: [Message], session_id, stream: true}`
- 头 `X-Agent-Id: <profile>` 选择 QwenPaw 侧用哪个智能体 profile
- 回 SSE（前缀固定 `data: `，**没有 event: 行**）
- 事件：`text` / `plugin_call` / `plugin_call_output` / `data` / `message` / `response` / `turn_usage`
- `role: "system"` 的输入消息**被接受且生效**（实测）→ `system_suffix` 走这条
- **图片输入**：`{"type":"image", "image_url":"<data-URI/URL>"}`（实测可识别）
- `model` 字段**被忽略**（实测）→ 模型是 profile 级，不在请求里指定

网关对前端的输出形状（自定义、简单）：

```jsonc
{"type": "text",   "text": "增量"}            // 正文增量
{"type": "step",   "id": "...", "name": "read_file", "args": {...}, "status": "running"}
{"type": "step",   "id": "...", "status": "done", "ms": 320, "output": "..."}
{"type": "usage",  "prompt": 1, "completion": 2, "cached": 0, "model": "wjd", "context_ratio": 0.02}
{"type": "done",   "status": "completed"}
{"type": "error",  "message": "..."}
```

这样前端不必理解 AgentScope 的事件细节。
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

log = logging.getLogger("api-gateway.agents")

#: 单轮对话的超时（秒）。引擎可能跑很久（多步工具），给足。
DEFAULT_TIMEOUT = 300.0

#: 引擎不可达时的兜底文案。
ENGINE_DOWN_MSG = "引擎不可达（QwenPaw 未运行或未就绪）"


class EngineError(Exception):
    """引擎层错误（映射为 502）。"""


# ── 请求构造 ──────────────────────────────────────────────────────────
def build_input(messages: list[dict[str, Any]], system_suffix: str = "") -> list[dict[str, Any]]:
    """把网关侧的消息列表转成 AgentScope 的 `input`。

    - 网关历史里的 role/文本 → `{role, content:[{type:"text", text}]}`
    - `system_suffix` → 在最前面插一条 **system 消息**（实测被接受且生效）
    - `system` 角色的历史消息也照传（引擎接受）
    """
    out: list[dict[str, Any]] = []
    if system_suffix:
        out.append({
            "role": "system",
            "content": [{"type": "text", "text": system_suffix}],
        })
    for m in messages:
        role = (m.get("role") or "user").lower()
        if role not in ("user", "assistant", "system"):
            role = "user"
        content = m.get("content")
        # content 允许：字符串 / [{type:text,text}] / [{type:image,image_url}]
        # 图片格式（2026-09-29 实测确认）：{"type":"image","image_url":"<data-URI 或 http URL>"}
        #   —— 端到端验证过：塞一张纯红 64×64 PNG 的 data URI，模型正确答出"红色"。
        if isinstance(content, str):
            parts = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, str):
                    parts.append({"type": "text", "text": c})
                elif isinstance(c, dict):
                    ctype = c.get("type")
                    if ctype == "text":
                        parts.append({"type": "text", "text": str(c.get("text") or "")})
                    elif ctype in ("image", "image_url"):
                        # 两种写法都认（实测见 test_qwenpaw_engine_image_content_passthrough）：
                        #   AgentScope 原生 `{"type":"image","image_url":"<uri>"}`
                        #   OpenAI 风格   `{"type":"image_url","image_url":{"url":"<uri>"}}`
                        # —— 前端「对话」页发的是后者，所以必须同时支持。
                        url = _image_url(c)
                        if url:
                            parts.append({"type": "image", "image_url": url})
                        else:
                            log.debug("图片块缺 image_url，已跳过")
                    # 其余类型（audio/video/file）暂不支持，忽略
        else:
            parts = [{"type": "text", "text": str(content or "")}]
        out.append({"role": role, "content": parts})
    return out


def _image_url(block: dict[str, Any]) -> str:
    """从图片内容块里取出 URL。兼容两种写法：

    - AgentScope 原生：`{"type":"image", "image_url":"data:image/png;base64,..."}`
    - OpenAI 风格：    `{"type":"image_url", "image_url":{"url":"..."}}`
    """
    v = block.get("image_url")
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        u = v.get("url")
        return str(u).strip() if u else ""
    return ""


def build_body(messages: list[dict[str, Any]], session_id: str,
               system_suffix: str = "") -> dict[str, Any]:
    return {
        "input": build_input(messages, system_suffix),
        "session_id": session_id,
        "stream": True,
    }


# ── SSE 解析与翻译 ────────────────────────────────────────────────────
def parse_sse_line(line: str) -> dict[str, Any] | None:
    """把一行 SSE 解析成事件 dict；非数据行返回 None。"""
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        log.debug("引擎 SSE 非 JSON 行（忽略）：%.120s", payload)
        return None
    return obj if isinstance(obj, dict) else None


class StreamTranslator:
    """把引擎的事件序列翻译成网关前端的消息序列（有状态）。

    状态：
      - `_text_buf`: 已累积的正文（用来在结束时兜底补全）
      - `_steps`: call_id → 步骤信息（配对 plugin_call / plugin_call_output）
    """

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.steps: dict[str, dict[str, Any]] = {}
        self.usage: dict[str, Any] | None = None
        self.final_status: str = "completed"
        self._step_started: dict[str, float] = {}

    # ── 单事件 → 若干前端消息 ──
    def feed(self, ev: dict[str, Any], now: float | None = None) -> list[dict[str, Any]]:
        import time
        now = now or time.time()
        t = ev.get("type")
        out: list[dict[str, Any]] = []

        if t == "text":
            # delta:true 是增量；delta:false 是整段快照 —— 忽略，否则会重复
            if ev.get("delta") is False:
                return out
            piece = ev.get("text")
            if isinstance(piece, str) and piece:
                self.text_parts.append(piece)
                out.append({"type": "text", "text": piece})
            return out

        if t == "plugin_call":
            info = _extract_call(ev, key="arguments")
            if info:
                cid = info.get("call_id") or info.get("name") or "?"
                self.steps[cid] = info
                self._step_started[cid] = now
                out.append({
                    "type": "step", "id": cid, "name": info.get("name"),
                    "args": info.get("args"), "status": "running",
                })
            return out

        if t == "plugin_call_output":
            info = _extract_call(ev, key="output")
            if info:
                cid = info.get("call_id") or info.get("name") or "?"
                started = self._step_started.pop(cid, None)
                ms = int((now - started) * 1000) if started else None
                if cid in self.steps:
                    self.steps[cid]["status"] = "done"
                msg: dict[str, Any] = {"type": "step", "id": cid, "status": "done"}
                if ms is not None:
                    msg["ms"] = ms
                if info.get("output") is not None:
                    msg["output"] = _clip(info["output"])
                out.append(msg)
            return out

        if t == "turn_usage":
            u = ev.get("usage") or {}
            self.usage = u
            ctx = ev.get("context_usage") or {}
            out.append({
                "type": "usage",
                "prompt": int(u.get("prompt_tokens") or 0),
                "completion": int(u.get("completion_tokens") or 0),
                "cached": int(u.get("cache_read_tokens") or 0),
                "model": u.get("model_name") or "",
                "provider": u.get("provider_id") or "",
                "context_ratio": ctx.get("context_usage_ratio"),
            })
            return out

        if t == "response":
            st = ev.get("status")
            if st:
                self.final_status = st
            if st in ("failed", "cancelled"):
                out.append({"type": "done", "status": st})
            return out

        # data / message 等其余事件：忽略（分片由快照覆盖）
        return out

    def finish(self) -> list[dict[str, Any]]:
        """流结束时收尾。"""
        out: list[dict[str, Any]] = []
        if self.final_status not in ("failed", "cancelled"):
            out.append({"type": "done", "status": self.final_status})
        elif self.final_status == "cancelled":
            out.append({"type": "done", "status": "cancelled"})
        return out

    @property
    def text(self) -> str:
        return "".join(self.text_parts)


def _extract_call(ev: dict[str, Any], *, key: str) -> dict[str, Any] | None:
    """从 plugin_call / plugin_call_output 里取 {call_id, name, args|output}。

    结构（实测）：`content[0].data = {call_id, name, arguments|output}`
    注意 `arguments` 是 **JSON 字符串**，要二次解析。
    """
    content = ev.get("content")
    if not isinstance(content, list):
        return None
    for c in content:
        if not isinstance(c, dict):
            continue
        data = c.get("data")
        if not isinstance(data, dict) or key not in data:
            continue
        info: dict[str, Any] = {
            "call_id": data.get("call_id"),
            "name": data.get("name"),
        }
        raw = data.get(key)
        if key == "arguments":
            info["args"] = _maybe_json(raw)
        else:
            info["output"] = raw
        return info
    return None


def _maybe_json(v: Any) -> Any:
    """`arguments` 是 JSON 字符串 → 解析成对象；解析不了就原样返回字符串。"""
    if not isinstance(v, str):
        return v
    try:
        return json.loads(v)
    except (json.JSONDecodeError, TypeError):
        return v


def _clip(v: Any, limit: int = 4000) -> Any:
    """工具输出可能很长 —— 转发给前端前裁一刀（避免 SSE 帧过大）。"""
    if isinstance(v, str) and len(v) > limit:
        return v[:limit] + "…(已截断)"
    return v


# ── 调用引擎 ──────────────────────────────────────────────────────────
async def stream_chat(base_url: str, agent_id: str, messages: list[dict[str, Any]],
                      session_id: str, *, system_suffix: str = "",
                      timeout: float = DEFAULT_TIMEOUT) -> AsyncIterator[dict[str, Any]]:
    """打引擎并产出"已翻译"的前端消息。

    用法：
        async for msg in stream_chat(...):  # msg 就是 {type: text|step|usage|done|error}
            yield sse(msg)
    """
    url = base_url.rstrip("/") + "/api/console/chat"
    body = build_body(messages, session_id, system_suffix)
    tr = StreamTranslator()
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "X-Agent-Id": agent_id,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code >= 400:
                    raw = (await resp.aread()).decode("utf-8", "replace")[:300]
                    yield {"type": "error",
                           "message": f"引擎返回 {resp.status_code}：{raw}"}
                    return
                async for line in resp.aiter_lines():
                    ev = parse_sse_line(line)
                    if ev is None:
                        continue
                    for msg in tr.feed(ev):
                        yield msg
        for msg in tr.finish():
            yield msg
    except httpx.ConnectError:
        yield {"type": "error", "message": ENGINE_DOWN_MSG}
    except httpx.ReadTimeout:
        yield {"type": "error", "message": "引擎响应超时（本轮未完成）"}
    except httpx.RemoteProtocolError:
        # 引擎中途断开 —— 保留已收到的内容（前端自己累积着），只提示中断
        for msg in tr.finish():
            pass
        yield {"type": "error", "message": "引擎连接中断，本轮未完成"}
    except Exception as exc:  # noqa: BLE001 —— 兜底，别让流把异常抛给客户端
        log.exception("引擎调用失败")
        yield {"type": "error", "message": f"引擎调用失败：{exc}"}


async def check_engine(base_url: str, *, timeout: float = 5.0) -> tuple[bool, str]:
    """探活：引擎是否可达。返回 (ok, 说明)。

    不引 `/api/healthz`（那是 QwenPaw 的）—— 用最轻的请求确认端口活着。
    """
    url = base_url.rstrip("/") + "/api/healthz"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url)
        if r.status_code < 500:
            return True, "就绪"
        return False, f"引擎异常（HTTP {r.status_code}）"
    except httpx.ConnectError:
        return False, ENGINE_DOWN_MSG
    except Exception as exc:  # noqa: BLE001
        return False, f"引擎不可达：{exc}"
