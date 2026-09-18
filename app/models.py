"""请求 / 响应数据模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


# 不允许被客户端 `extra` / 未知字段覆盖的"核心键"。
# 这些键承载网关已解析的路由结果与协议语义，一旦让 extra 覆盖就会出现：
#   - `extra={"model": "..."}` → 劫持路由，用网关的供应商 Key 调任意（更贵/未登记）模型；
#   - `extra={"stream": ...}`  → 与网关自身的解析结果矛盾，非流式客户端收到 SSE（502）或反之；
#   - `extra={"messages": [...]}` → 绕过入参校验。
# 第六轮安全审计实测确认了上述越权路径，故在**组装时**统一剔除。
PROTECTED_PAYLOAD_KEYS: frozenset[str] = frozenset({
    "model", "messages", "stream", "tools", "tool_choice",
    "temperature", "top_p", "max_tokens", "stop", "n",
    "presence_penalty", "frequency_penalty", "seed", "user",
    "response_format", "logprobs", "top_logprobs",
    "parallel_tool_calls", "stream_options",
    "prompt", "input", "image", "images", "seconds", "mode",
    "aspect_ratio", "size", "extra", "extra_body",
})

# 视频路径的保护集**刻意更窄**：first_frame / last_frame / images / audios / seed
# 是模式专用参数，必须允许透传；只挡住会改变"调哪个模型/多长/什么模式"的键。
PROTECTED_VIDEO_KEYS: frozenset[str] = frozenset({
    "model", "prompt", "seconds", "mode", "size", "aspect_ratio", "n", "extra", "extra_body",
})

# 图像路径同理：只挡"会改变调哪个模型/什么尺寸/几张/输入图"的键。
# 注意**不能**沿用 PROTECTED_PAYLOAD_KEYS —— 那会把 response_format / user 一起删掉。
PROTECTED_IMAGE_KEYS: frozenset[str] = frozenset({
    "model", "prompt", "size", "n", "image", "images", "extra", "extra_body",
})

# 适配器 `_build_payload` **自己写进去**的键（chat）。
# 只有这些才需要防覆盖 —— 其余键（n / seed / user / response_format /
# presence_penalty / logprobs …）恰恰是 ChatRequest._PASSTHROUGH 特意塞进 extra
# 要透传给上游的，第七轮回归审计实测：若用宽集合过滤，这些参数会被**静默丢弃**
# （等于废掉 _PASSTHROUGH 机制），属本轮引入的兼容性倒退。
CHAT_CORE_KEYS: frozenset[str] = frozenset({
    "model", "messages", "stream", "tools", "tool_choice",
    "temperature", "top_p", "max_tokens", "stop",
})


def strip_protected(extra: dict[str, Any] | None,
                    keys: frozenset[str] = PROTECTED_PAYLOAD_KEYS) -> dict[str, Any]:
    """剔除受保护的核心键，其余原样透传（适配器侧的最后一道防线）。"""
    return {k: v for k, v in (extra or {}).items() if k not in keys}


class Message(BaseModel):
    # 兼容性修复（第六轮）: 此前只有 role/content，导致 function calling 的多轮对话
    #   **直接坏掉** —— assistant 消息上的 tool_calls、tool 消息上的 tool_call_id 被静默丢弃，
    #   上游收到的是 `{"role":"assistant","content":null}` 这种缺胳膊少腿的历史。
    #   现在显式声明这几个字段，并用 extra="allow" 兜住其余规范字段（refusal / audio 等）。
    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = None            # string / array（多模态）/ null（带 tool_calls 的 assistant 消息）
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None

    def to_upstream(self) -> dict[str, Any]:
        """转发给上游用：丢掉 None 字段，其余（含未知字段）原样带上。"""
        return self.model_dump(exclude_none=True)


class ToolFunction(BaseModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


class ToolDef(BaseModel):
    type: str = "function"
    function: ToolFunction


# `conversation_id` 会被放进 **HTTP header**（`x-opencode-session`）转发给上游，所以
# 长度必须收敛：header 有上限，几十 KB 的值就算上游收得下，中间的反代/网关也可能先炸。
CONVERSATION_ID_MAX = 128


def normalize_conversation_id(v: Any) -> str | None:
    """会话 id 的入口校验（第二十六轮，第二十七轮抽成函数给直传端点多路复用）。

    它最终会作为 `x-opencode-session` **HTTP header** 发往上游，所以必须在入站拦掉
    两类值——否则失败发生在组 header 的那一刻，会被误报成"上游挂了"：

    ① **非 ASCII**（中文、全角符号等）：httpx 组 header 抛 `UnicodeEncodeError`
       （实测 `'ascii' codec can't encode characters`），网关把它当成上游失败
       **重试 3 次**（3 次必然都失败）→ 最终回 502「路由 x 全部模型调用失败」+
       记一次熔断。真实原因是客户端传错了值，却被伪装成上游故障，排查成本极高。
    ② **过长**：header 超限同样会失败，且一行 header 塞几百 KB 毫无意义。

    空白串按"未传"处理（返回 None → 上游每次随机会话），与旧行为保持一致。
    校验失败抛 ValueError，由调用方转成 400（pydantic 校验器与直传端点共用同一份规则）。
    """
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError(f"类型必须是字符串，得到 {type(v).__name__}")
    v = v.strip()
    if not v:
        return None
    if len(v) > CONVERSATION_ID_MAX:
        raise ValueError(f"过长：最多 {CONVERSATION_ID_MAX} 个字符（收到 {len(v)} 个）")
    try:
        v.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("只能包含 ASCII 字符（不能有中文/全角符号/emoji）；建议用 uuid4().hex")
    return v


class ChatRequest(BaseModel):
    # 兼容性修复：OpenAI 官方 SDK 会带不少可选参数（n / presence_penalty / response_format …），
    # 之前未声明的字段被 pydantic 静默丢弃，导致"网关号称 OpenAI 全兼容但参数悄悄丢失"。
    # 现在：常用标准参数显式声明，其余未知字段用 extra="allow" 捕获后一并透传上游。
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[Message] = Field(min_length=1)   # 空 messages 上游会报错，这里先给 400
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    # strict: `"stream": "yes"` 这类拼错不应被宽松强转成 True（会拿到 SSE 而非 JSON）
    stream: bool = Field(default=False, strict=True)
    stop: str | list[str] | None = None
    tools: list[ToolDef] | None = None
    tool_choice: str | dict | None = None
    # ── OpenAI 标准可选参数（透传上游） ──
    n: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    seed: int | None = None
    user: str | None = None
    response_format: dict[str, Any] | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = None
    parallel_tool_calls: bool | None = None
    stream_options: dict[str, Any] | None = None
    # 网关内部字段（显式声明 → 不进 model_extra → 不透传上游）：
    # 由前端对话页为「同一段对话」提供一个稳定 id；后端只拿它当 OpenCode Go 的
    # x-opencode-session（同一对话共享，吃到上游提示词缓存）。非 opencode 供应商忽略。
    conversation_id: str | None = None
    # 透传供应商特有参数
    extra: dict[str, Any] = Field(default_factory=dict)

    # 需要透传的标准参数白名单
    _PASSTHROUGH = (
        "n", "presence_penalty", "frequency_penalty", "seed", "user",
        "response_format", "logprobs", "top_logprobs",
        "parallel_tool_calls", "stream_options",
    )

    def upstream_extra(self) -> dict[str, Any]:
        """组装要透传给上游的额外参数。

        优先级（后者覆盖前者）：显式声明的标准参数 < 未知透传字段(model_extra) < `extra`。
        但**核心键一律剔除**（见 PROTECTED_PAYLOAD_KEYS）：`extra` 是"供应商特有参数"的
        逃生舱，不是用来覆盖 model/messages/stream 的 —— 否则等同把路由与协议语义交给客户端。
        """
        out: dict[str, Any] = {}
        for k in self._PASSTHROUGH:
            v = getattr(self, k, None)
            if v is not None:
                out[k] = v
        merged: dict[str, Any] = {}
        merged.update(self.model_extra or {})
        merged.update(self.extra or {})
        for k, v in merged.items():
            if k in PROTECTED_PAYLOAD_KEYS:
                continue
            out[k] = v
        return out

    def unknown_fields(self) -> list[str]:
        """未被显式声明、靠 extra="allow" 兜住的字段名（用于日志提示，避免悄悄透传）。"""
        return sorted(k for k in (self.model_extra or {}) if k not in PROTECTED_PAYLOAD_KEYS)

    @field_validator("conversation_id")
    @classmethod
    def _validate_conversation_id(cls, v: str | None) -> str | None:
        """见 normalize_conversation_id（第二十七轮把规则抽成公共函数，直传端点多路复用）。"""
        return normalize_conversation_id(v)


# ── 图像生成 ─────────────────────────────────────────
class ImageRequest(BaseModel):
    # 第六轮: extra="allow" —— quality/style/response_format/user 等标准参数此前被
    #   pydantic 静默丢弃（客户端以为生效了）。现在统一收进 model_extra 再透传。
    model_config = ConfigDict(extra="allow")

    model: str
    prompt: str
    size: str | None = None
    ratio: str | None = None
    image: list[str] | None = None          # 图生图 / 多图输入 URL 或 DataURI
    n: int = 1
    return_base64: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)


# ── 视频生成（异步任务） ─────────────────────────────
class VideoCreateRequest(BaseModel):
    model: str
    prompt: str
    seconds: str = "5"
    mode: str = "text"                       # text | keyframe | reference
    size: str = "720P"
    aspect_ratio: str = "16:9"
    n: int = 1                               # 第十三轮: 上游只支持 1，>1 在网关入站就回 400
    seed: int | None = None
    # 模式专用
    first_frame: str | None = None
    last_frame: str | None = None
    images: list[str] | None = None
    audios: list[str] | None = None
    # 第十三轮: 声明出来只为**明确拒绝**（Agnes Video 2.5 Flash 不支持参考视频）。
    #   此前该字段未声明 → pydantic 静默丢弃，用户以为"传了参考视频"，实际被悄悄忽略。
    videos: list[Any] | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


# ── 视频等待模式 / Webhook ───────────────────────────
class VideoCreateRequestExtended(VideoCreateRequest):
    """支持 wait=true 和 callback_url 的视频创建请求。"""
    wait: bool = False
    wait_timeout: int = 120          # 等待完成的最大秒数
    callback_url: str | None = None  # 任务完成/失败时 POST 通知


# ── Batch 调用 ────────────────────────────────────────
class BatchItem(BaseModel):
    """Batch 中单个调用项。"""
    messages: list[Message]
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    label: str = ""                  # 自定义标签（结果对应）


class BatchRequest(BaseModel):
    """一次 POST 并发调多个 prompt（同模型）。"""
    model: str
    items: list[BatchItem]
    max_concurrency: int = 8         # 最大并发数
    extra: dict[str, Any] = Field(default_factory=dict)

