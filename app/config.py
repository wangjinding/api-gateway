"""配置加载模块：从 YAML 文件读取供应商、模型、鉴权、运维配置。"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("gateway.config")


class ConfigError(ValueError):
    """配置结构性错误（缺必填项等）。启动/热重载时抛出，附友好说明。"""


# ── 模型 / 供应商 ──
# 模型级「强制参数」（providers.<key>.models[].params）允许出现的键。
#
# 只放采样 / 长度类参数：它们不影响"调哪个模型、走什么协议"，被覆盖最坏也只是改变生成风格。
# 而 model / messages / stream / tools / stop 这类**结构性**键一旦允许出现在这里，
# 等于把路由与协议语义交给配置文件，等于重开一条越权面（见 app/models.py 的
# PROTECTED_PAYLOAD_KEYS 说明），故一律不放行。
MODEL_PARAM_KEYS: frozenset[str] = frozenset({
    "temperature", "top_p", "top_k", "min_tokens", "max_tokens",
    "presence_penalty", "frequency_penalty", "repetition_penalty",
    "seed", "logit_bias", "n",
})


# ── 协议线（方言）标准名（第二十七轮「三线直传」） ──────────────
# 老板决策：**入站是什么格式，出站就是什么格式**；网关不做任何跨格式翻译，
# 只在「供应商与模型」里声明每个模型能用哪几条线。
#
# 这里直接用各家文档 / SDK 里的**端点名**当协议名（"标准的来"）：
#   chat/completions —— OpenAI Chat Completions（POST {base}/chat/completions）
#   messages         —— Anthropic Messages（POST {base}/messages，认 x-api-key）
#   responses        —— OpenAI Responses（POST {base}/responses，认 Bearer）
# 好处：配置、日志、前端标签、上游文档四处用的是同一个词，不用查对照表。
#
# 纯透传的边界：
#   - 同一格（入站 = 出站）→ 原样转发，只定点改写 model + 盖模型级强制参数；
#   - 不同格（如用 messages 打一个只支持 chat/completions 的模型）→ 入站直接 400
#     报「该模型不支持 messages 线」，**绝不偷偷翻译**（翻译出来的东西一定有损，
#     而且失败现场离用户十万八千里）。
PROTOCOL_CHAT = "chat/completions"
PROTOCOL_MESSAGES = "messages"
PROTOCOL_RESPONSES = "responses"
PROTOCOLS: tuple[str, ...] = (PROTOCOL_CHAT, PROTOCOL_MESSAGES, PROTOCOL_RESPONSES)
# 模型没写 protocols 时的默认值：只认 chat/completions。
#   为什么要默认而不是"全支持"：老配置（Agnes / 火山）从未声明过协议，
#   默认全支持等于把「没实测过的线」当成可用，用户打过去只会收到上游 500。
DEFAULT_PROTOCOLS: tuple[str, ...] = (PROTOCOL_CHAT,)

# 出站鉴权方式：bearer（Authorization: Bearer xxx）/ x-api-key（Anthropic 系）/ none
AUTH_STYLES: frozenset[str] = frozenset({"bearer", "x-api-key", "none"})

# ── 虚拟模型 id（别名，第三十一轮）───────────────────────────────────
# 老板决策（2026-09-18）：「设置一个 wjd 模型 id，路由到指定的模型上；在管理员的网关配置里
# 可以选择 wjd 映射的模型」。落到实现上就是**一对一指针**：
#
#   aliases:
#     wjd:
#       enabled: true                     # false = 这个 id 不存在（调用返回未知模型）
#       target: opencode/deepseek-v4.1-flash   # 必须是已配置的真实模型 id
#       note: "给客户用的固定入口"           # 任意备注，只给人看
#
# 五条硬规则（都在保存期校验，不靠前端自觉）：
#   ① 别名**固定走 chat/completions 线**（OpenAI 格式）—— 客户端要一个"名字稳定"的入口，
#      不是"格式稳定"的转换器；用 messages / responses 打 wjd 在入口 400 指路，网关不做
#      任何跨格式翻译（与三线直传的边界一致，见 PROTOCOLS 说明）。
#   ② 因此 target 必须**支持 chat/completions**：面板里其余模型置灰不可选，服务端同样拒绝
#      （置灰只是 UX，校验才是边界 —— 手改 YAML 也绕不过去）。
#   ③ target 必须存在（写错 id 直接报错，不等到调用时才 404）。
#   ④ 别名 id 不得与任何真实模型 id 重名（否则"wjd 到底指哪个"说不清）。
#   ⑤ 别名不复制目标的任何配置：协议线、视觉能力、强制参数、定价一律**继承 target**
#      —— 唯一事实来源，免得两份配置各自漂移。
#
# 与第二十七轮删掉的「路由链」（routes/fallback）的关系：那套是按 API Key 选链路、可多跳，
# 别名是"这个名字转发给谁"的单跳指针。两者混在一起后没人看得懂配置，所以**不复活 routes**。
# 配置里残留的 routes 段继续只告警、不解析。
ALIAS_PROTOCOLS: tuple[str, ...] = (PROTOCOL_CHAT,)


@dataclass
class EndpointSpec:
    """某供应商在**一条协议线**上的出站配置（providers.<key>.endpoints.<protocol>）。

    为什么放在供应商级而不是模型级：同一条线在同一个供应商下永远是同一个地址与同一套
    鉴权头（OpenCode Go 三条线共用 https://opencode.ai/zen/go/v1，只是路径和鉴权头不同），
    模型之间只有"能不能走"的差别 —— 那是 models[].protocols 的事。
    """

    path: str = ""                                  # 相对 base_url 的路径，如 /chat/completions
    auth: str = "bearer"                            # bearer | x-api-key | none
    headers: dict[str, str] = field(default_factory=dict)  # 该线固定附加头（如 anthropic-version）


def _default_endpoints() -> dict[str, EndpointSpec]:
    """三条标准线的默认出站配置（未写 providers.*.endpoints 时用）。

    默认值就是三家协议的标准形态，写出来只是为了让"默认"可见：
      chat/completions → /chat/completions + Bearer
      messages         → /messages + x-api-key + anthropic-version
      responses        → /responses + Bearer
    """
    return {
        PROTOCOL_CHAT: EndpointSpec(path="/chat/completions", auth="bearer"),
        PROTOCOL_MESSAGES: EndpointSpec(
            path="/messages", auth="x-api-key",
            headers={"anthropic-version": "2023-06-01"}),
        PROTOCOL_RESPONSES: EndpointSpec(path="/responses", auth="bearer"),
    }


@dataclass
class ModelInfo:
    id: str
    display_name: str = ""
    type: str = "text"  # text | image | video
    # 第十三轮: 是否支持「图像输入」（多模态 / 视觉）。
    #   Agnes 3.0 Flash 文档明确「输入模态：文本、图像 URL」，实测 data:image/png;base64 也能识别；
    # 其余文本模型（ark-code 等）文档未声明图像输入 → 默认 False。
    #   用途：① /v1/models 暴露给前端标注/告警；② 前端据此提示「换个支持视觉的模型」力的模型排到前面。
    vision: bool = False
    # 2026-09-21: 是否**免费模型**（官方价格表标 Free、不按 token 计费）。
    #   纯标注用途：/v1/models 带出 `free` 字段，前端「能力」列打「免费」标签；
    #   不参与路由/限流/熔断，也不影响成本估算（额度由上游控制，网关无法感知）。
    #   配置写法：`models[]` 里加 `free: true`（OpenCode Go 当前只有 space-bunny-free）。
    free: bool = False
    # 2026-09-24（老板要求：Agnes 也要标免费 / 限时免费）: 免费是否**带期限**。
    #   语义：`free: true` + `free_limited: false` → 免费；`free_limited: true` → 限时免费。
    #   来源是 Agnes 定价页「### 当前优惠」小节的措辞（「当前免费」vs「当前限时免费」），
    #   由 scripts/sync_models.py 按文档自动维护（撤标注时整行删掉）。
    #   同样只是标注：/v1/models 带出、前端标签文案据此切换，不参与路由/限流/计费。
    free_limited: bool = False
    # 第三十三轮（2026-09-24，老板要求：接口加入上下文参数）: 模型的**上下文窗口**
    #   与**最大输出**（单位 token）。0 = 未知/未标注（非文本模型天然没有）。
    #   来源与维护：
    #     · Agnes 文本模型 → 官方文档每个模型页「限制与价格」表（权威值）；
    #     · OpenCode Go → models.dev 的 `opencode-go` 供应商 `limit.context` / `limit.output`
    #       （OpenCode 官方文档与 /models 接口都不给这两个数，models.dev 是唯一可用来源）；
    #     · 同名模型跨源不一致时取**保守下限**（宁可早压缩，不能超窗口）。
    #   由 scripts/sync_models.py 每日同步；不参与路由/限流，仅作展示与调用方参考。
    context_window: int = 0
    max_output_tokens: int = 0
    # 第二十五轮（模型参数硬约束）: 该模型**必须用**的上游参数，发送前强制覆盖客户端传值。
    #   背景：OpenCode Go 的 kimi-k2.7-code 只接受 temperature=1，客户端传 0（含一键测速的
    #   探针，它固定发 temperature=0）会被上游 400：
    #     "invalid temperature: only 1 is allowed for this model"
    #   配置成 {"temperature": 1} 后，网关无论收到什么值都按 1 发 —— 用户在前端把温度滑块
    #   拉到 0 也不会再 400（值被网关改成上游唯一允许的那个）。
    #   键必须在 MODEL_PARAM_KEYS 白名单内，否则 validate_config 直接报错（不静默失效）。
    params: dict[str, Any] = field(default_factory=dict)
    # 第二十七轮（三线直传）: 该模型**能用哪几条协议线**（取值见 PROTOCOLS）。
    #   语义是「入站也用这条线」—— 入站 /v1/messages 打到只声明了 chat/completions 的模型
    #   时，网关直接 400，不替用户翻译（详见 PROTOCOLS 的说明）。
    #   不写 = 默认 ["chat/completions"]（老配置行为完全不变）。
    #   实测口径（OpenCode Go 2026-09-18）：**文档标注的线一定可用；有些模型实测能走多条线**
    #   （如 kimi-k3 / qwen 系列 chat + messages 都 200，deepseek 四个模型三线全通），
    #   这类按实测把两条线都写上 —— 但只写实测 200 过的，猜测的线不写。
    protocols: list[str] = field(default_factory=lambda: list(DEFAULT_PROTOCOLS))

    def supports(self, protocol: str) -> bool:
        return protocol in self.protocols


@dataclass
class AuthConfig:
    token: str = ""
    token_env: str = ""

    def resolve_token(self) -> str:
        """优先读环境变量，其次用配置中的 token。"""
        if self.token_env:
            env_val = os.environ.get(self.token_env, "")
            if env_val:
                return env_val
        return self.token


@dataclass
class MediaConfig:
    """供应商的图像/视频端点覆盖。

    默认按 OpenAI 兼容路径；Agnes 等非标准端点在此覆盖。
    全部为 None 表示该供应商不支持对应的媒体生成。
    """

    image_endpoint: str | None = None        # 相对 base_url，默认 /images/generations
    image_default_size: str | None = None     # 图像默认档位（Agnes 需 1K/2K/3K/4K；其它供应商可覆盖）
    video_create_endpoint: str | None = None  # 相对 base_url，默认 /videos
    video_poll_base: str | None = None       # 绝对 base（视频查询，默认同 base_url 去掉 /v1）
    video_poll_path: str | None = None       # 默认 /agnesapi（Agnes）
    video_model_name: str | None = None      # 轮询用的 model_name 参数

    @property
    def supports_image(self) -> bool:
        return self.image_endpoint is not None

    @property
    def supports_video(self) -> bool:
        return self.video_create_endpoint is not None


@dataclass
class ProviderConfig:
    key: str
    name: str
    base_url: str
    auth: AuthConfig
    extra_headers: dict[str, str] = field(default_factory=dict)
    models: list[ModelInfo] = field(default_factory=list)
    media: MediaConfig = field(default_factory=MediaConfig)
    rate_limit_rps: float | None = None  # 该供应商全局 QPS 上限（可选）
    requires_session_header: bool = False  # OpenCode Go 等要求每请求动态会话头（x-opencode-session）
    # 第二十七轮（三线直传）: 该供应商**每条协议线**的出站地址与鉴权头。
    #   未配置时用 _default_endpoints()（三条标准线 + 标准鉴权）—— 所以 Agnes / 火山这类
    #   只走 chat/completions 的供应商一个字段都不用加，行为与以前完全一致。
    endpoints: dict[str, EndpointSpec] = field(default_factory=_default_endpoints)

    def endpoint(self, protocol: str) -> EndpointSpec | None:
        """取某条线的出站配置（没有该线 → None，调用方按"供应商不支持这条线"处理）。"""
        return self.endpoints.get(protocol)

    def supports_protocol(self, protocol: str) -> bool:
        return protocol in self.endpoints


# ── 虚拟模型 id（别名）──
@dataclass
class AliasSpec:
    """虚拟模型 id 的定义（详见 ALIAS_PROTOCOLS 上方的五条硬规则）。

    它**不是模型**：不进 `_model_index`，也没有自己的 protocols/params/定价 ——
    网关只拿它做一件事：把客户端写的 `wjd` 换成 target 那个真实模型 id，其余一切照旧
    （挑供应商、盖强制参数、记账全按 target 走）。
    """

    enabled: bool = True     # false = 该 id 不生效（客户端拿到"未知模型"，等同于没配）
    target: str = ""         # 目标真实模型 id（必须支持 chat/completions，保存期校验）
    note: str = ""           # 备注，只给人看（面板/配置里显示）


# ── 网关入口鉴权 ──
# 账号体系（用户名/密码/API Key）由页面管理，保存在 data/users.json（见 app/users.py），
# 配置文件**不再承载**鉴权用户/密钥 —— 这里只保留前端会话时长这一个运维开关。
@dataclass
class AuthGate:
    session_ttl_minutes: int = 1440   # 前端 /auth/login 签发的 session 有效时长（分钟，默认 24h）


@dataclass
class CircuitConfig:
    """供应商熔断器配置。"""
    enabled: bool = False
    failure_threshold: int = 3   # 连续/滑动窗口失败次数达到即熔断
    cooldown: int = 30           # 熔断冷却秒
    window: int = 10            # 滑动窗口


@dataclass
class ObservabilityConfig:
    """可观测性配置。"""
    log_level: str = "INFO"
    log_json: bool = False            # 结构化 JSON 行日志
    access_log_maxlen: int = 500      # 访问日志环形缓冲长度


@dataclass
class Pricing:
    """按模型估算成本（人民币 / 百万 token）。None 表示不估算。"""
    per_model: dict[str, dict[str, float]] = field(default_factory=dict)  # model -> {input, output}

    def estimate(self, model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
        m = self.per_model.get(model)
        if not m:
            return None
        return (prompt_tokens * m.get("input", 0) + completion_tokens * m.get("output", 0)) / 1_000_000.0


@dataclass
class DownloadConfig:
    """媒体下载代理（GET /v1/media/download）的白名单与大小限制。

    产物挂在第三方 CDN 上且**不带 CORS 头**：浏览器里 `fetch` 拿不到 blob、跨域
    `<a download>` 也不生效（只会变成"打开新标签页"）。所以让网关代拉一次再加上
    `Content-Disposition: attachment`，前端才能真下载。

    这也是全网关**唯一一个服务端主动出网**的接口，因此白名单是硬性要求：
      - allowed_hosts 为空 = 该功能整体关闭（任何 url 都 400，不会退化成开放代理）；
      - 命中的域名还要过公网 IP 校验（见 main.py `_dl_host_is_public`），挡 DNS 指内网。
    """

    allowed_hosts: list[str] = field(default_factory=list)  # 允许代下载的产物域名（含子域）
    max_bytes: int = 64 * 1024 * 1024                       # 单文件上限（默认 64MB）


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8808
    timeout: int = 120
    retry: int = 1
    # 运维开关
    health_auth: bool = False          # /health/* 是否也要鉴权（默认否，留给探活）
    hot_reload: bool = False           # 监听 yaml mtime 变化自动重载
    hot_reload_interval: int = 15      # 重载检查间隔（秒）
    metrics_enabled: bool = True       # 是否记录调用指标
    rate_limit_rps: float | None = None  # 全局单 user QPS 上限（None=不限）
    max_body_size: int = 2_000_000     # 请求体字节上限（防 DDoS）
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    graceful_shutdown: int = 30        # 优雅关闭等待秒
    # ── 防滥用 / 安全 ──
    batch_max_concurrency: int = 16    # /batch 并发上限（客户端 max_concurrency 会被钳到这个值）
    batch_max_items: int = 64          # /batch 单次最大条目数（超过直接 400）
    webhook_allow_private: bool = False  # 视频 callback_url 是否允许指向内网/回环地址（SSRF 防护，默认否）
    docs_enabled: bool = False           # 是否开放 FastAPI 文档（/docs、/redoc、/openapi.json）——安全默认关
    auth_fail_limit_per_min: int = 20    # 同一来源 IP 每分钟允许的鉴权失败次数，超过 → 429（防暴力枚举）


@dataclass
class GatewayConfig:
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    server: ServerConfig = field(default_factory=ServerConfig)
    auth: AuthGate = field(default_factory=AuthGate)
    circuit: CircuitConfig = field(default_factory=CircuitConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    pricing: Pricing = field(default_factory=Pricing)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    # 第三十一轮: 虚拟模型 id（别名）。键是客户端写的名字（当前就一个 `wjd`），
    #   值是"转发到哪个真实模型"。**不建反向索引**：别名不属于 _model_index，
    #   因为它不是模型 —— 谁想用别名，谁就得显式 resolve_alias()（见 main.py 的三处入口）。
    #   这样媒体端点 / 未知模型分支不会"顺手"接受别名，行为可预测。
    aliases: dict[str, AliasSpec] = field(default_factory=dict)
    # 模型 → (pkey, ProviderConfig, ModelInfo) 索引，避免每次请求线性扫描（性能）
    _model_index: dict[str, tuple[str, ProviderConfig, ModelInfo]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self._rebuild_index()

    def alias_of(self, model_id: str) -> AliasSpec | None:
        """该 id 是不是一个**已启用**的别名；是则返回定义，否则 None。

        停用（enabled: false）的别名与"没配过"完全同义 —— 调用方不需要知道它存在过。
        """
        spec = self.aliases.get(model_id)
        return spec if (spec is not None and spec.enabled) else None

    def resolve_alias(self, model_id: str) -> str:
        """别名 → 目标真实模型 id；不是别名（或已停用、target 为空）→ 原样返回。

        target 为空只可能出现在"已停用的半成品配置"里（保存期校验会拦住启用的空 target），
        这里一律原样返回，绝不让空字符串流到下游去拼 URL。
        """
        spec = self.alias_of(model_id)
        return spec.target if (spec is not None and spec.target) else model_id

    def _rebuild_index(self) -> None:
        index: dict[str, tuple[str, ProviderConfig, ModelInfo]] = {}
        for pkey, pconfig in self.providers.items():
            for m in pconfig.models:
                index.setdefault(m.id, (pkey, pconfig, m))  # 重名保留首个（与原线性扫描一致）
        self._model_index = index

    def find_provider_for_model(self, model_id: str) -> tuple[str, ProviderConfig, ModelInfo] | None:
        return self._model_index.get(model_id)

    def find_model_for_protocol(
        self, model_id: str, protocol: str
    ) -> tuple[str, ProviderConfig, ModelInfo] | None:
        """按「模型 + 入站协议线」查供应商；模型不存在或该线不支持 → None。

        三线直传的核心闸门：入站是哪条线，就必须在这条线上找到模型，否则 400 ——
        **不做任何跨线翻译**（老板决策）。返回 None 的两种情况（未知模型 / 不支持该线）
        由调用方分别给不同文案，别混成一句"模型不存在"。
        """
        found = self._model_index.get(model_id)
        if found is None:
            return None
        pkey, pconfig, minfo = found
        if not minfo.supports(protocol):
            return None
        if not pconfig.supports_protocol(protocol):
            return None
        return found

    def models_with_protocol(self, protocol: str) -> list[ModelInfo]:
        """能走某条线的全部模型（/v1/models 与前端标签用）。"""
        return [m for p in self.providers.values() for m in p.models if m.supports(protocol)]


    def all_models(self) -> list[dict[str, Any]]:
        result = []
        for pkey, pconfig in self.providers.items():
            for m in pconfig.models:
                result.append({
                    "id": m.id,
                    "display_name": m.display_name or m.id,
                    "type": m.type,
                    "vision": m.vision,
                    # 2026-09-21: 免费模型标注（官方标 Free）。前端「能力」列打「免费」标签，
                    #   /v1/models 也带出该字段；不参与路由/限流/计费。
                    "free": bool(m.free),
                    # 2026-09-24: 「免费」还是「限时免费」—— 前端标签文案据此切换
                    #   （免费 / 限时免费），也带进 /v1/models 让接口调用方知道优惠可能到期。
                    "free_limited": bool(m.free_limited),
                    # 第三十三轮（2026-09-24）: 上下文窗口 / 最大输出（token）。0 = 未标注。
                    #   前端「能力」列与 /v1/models 均带出，调用方据此决定单次请求塞多长的对话。
                    "context_window": int(m.context_window or 0),
                    "max_output_tokens": int(m.max_output_tokens or 0),
                    "provider": pkey,
                    "provider_name": pconfig.name,
                    # 第二十五轮: 暴露模型级强制参数（如 {"temperature": 1}）。
                    #   前端据此标注「该模型温度已由网关固定」，API 调用方也能提前知道
                    #   自己传的采样参数会被上游要求覆盖，省掉一轮「传了没生效」的困惑。
                    "params": dict(m.params or {}),
                    # 第二十七轮: 暴露该模型能走哪几条协议线（取值见 PROTOCOLS）。
                    #   前端「供应商与模型」页据此打格式标签；API 调用方也能提前知道自己
                    #   该把请求发到 /v1/chat/completions、/v1/messages 还是 /v1/responses。
                    "protocols": list(m.protocols),
                })
        return result


def _opt_float(v: Any, field_name: str = "rate_limit_rps") -> float | None:
    """可选数值解析：None/空串 → None；数字或数字字符串 → float；其它 → ConfigError。

    修复：此前直接把 YAML 值塞进配置，写成字符串（如 `rate_limit_rps: "5"`）时
    会在限流计算 `rps * 2` 处抛 TypeError 崩掉请求。
    """
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{field_name} 必须是数字，得到: {v!r}")


def _opt_bool(v: Any, field_name: str, default: bool = False) -> bool:
    """健壮的可选布尔解析（第二十三轮）。

    起因：管理页面提交的是 JSON 真布尔，但 yaml 里手写 `"false"`（带引号）或
    `yes/no/on/off` 也常见 —— 原来一律 `bool(v)`，于是字符串 `"false"` 变成 **True**，
    与作者本意相反且毫无提示。现在认这些写法，认不出来直接报错（带字段名）。
    """
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on", "y", "t"):
            return True
        if s in ("0", "false", "no", "off", "n", "f", ""):
            return False
    raise ConfigError(f"{field_name} 必须是布尔值（true / false），得到: {v!r}")


def _opt_int(v: Any, field_name: str, default: int, minimum: int | None = None) -> int:
    """健壮的可选整数解析（第六轮）。

    此前这里全是裸 `int(...)`：配置里写成 `null` / 字符串 / 留空会抛未包装的
    TypeError/ValueError，而 `load_config` 在 import 期就被调用 → **进程直接起不来**
    （热重载路径反而是被 try 兜住的，启动/热重载行为不一致）。
    现在统一：None/空串 → 默认值；非法 → ConfigError（带字段名）；可选下限校验。
    """
    if v is None or v == "":
        val = default
    else:
        try:
            val = int(v)
        except (TypeError, ValueError):
            raise ConfigError(f"{field_name} 必须是整数，得到: {v!r}")
    if minimum is not None and val < minimum:
        raise ConfigError(f"{field_name} 不能小于 {minimum}，得到: {val}")
    return val


def _parse_model_params(v: Any, where: str) -> dict[str, Any]:
    """解析模型级「强制参数」（第二十五轮）。

    形如 `params: {temperature: 1}` —— 网关发往上游前用这些值**覆盖**客户端传的同名参数。
    用来应对上游的硬约束（某模型只接受 temperature=1 之类）：否则客户端传别的值就 400，
    而且用户在前端拉个滑块就踩雷，根本不可能自己猜出上游要什么。

    容错口径（宁可报错，不可静默失效）：
      - 缺失 / None → {}（不是错误，只是这个模型没有约束）；
      - 不是映射 → ConfigError；
      - 参数名不在 MODEL_PARAM_KEYS 白名单内 → ConfigError（防止把 model/messages/stream
        这类结构性键配进来，那等于让配置文件改写路由语义）；
      - 值类型必须是标量 / list / dict。
    """
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise ConfigError(
            f"{where} 必须是映射（如 {{temperature: 1}}），得到 {type(v).__name__}: {v!r}")
    out: dict[str, Any] = {}
    for k, val in v.items():
        key = str(k).strip()
        if not key:
            raise ConfigError(f"{where} 出现空参数名")
        if key not in MODEL_PARAM_KEYS:
            raise ConfigError(
                f"{where}.{key} 不是允许的模型参数（允许：{', '.join(sorted(MODEL_PARAM_KEYS))}）")
        if val is None or isinstance(val, (bool, int, float, str, list, dict)):
            out[key] = val
        else:
            raise ConfigError(f"{where}.{key} 的值类型不支持: {type(val).__name__}")
    return out


def _parse_protocols(v: Any, where: str) -> list[str]:
    """解析模型级 `protocols`（第二十七轮三线直传）。

    容错口径（宁可报错，不可静默失效 —— 写错协议名是最容易发生的配置失误，
    而它的表现是"调用该模型永远 400"，静默接受等于让用户自己 debug 配置）：
      - 缺失 / None → 默认 ["chat/completions"]（老配置行为不变）；
      - 字符串 → 当作单元素列表（写 `protocols: messages` 也认）；
      - 列表 → 逐项校验，必须是 PROTOCOLS 里的标准名；
      - 去重保序；空列表 → ConfigError（"一个模型一条线都不支持"多半是手滑）。
    """
    if v is None:
        return list(DEFAULT_PROTOCOLS)
    if isinstance(v, str):
        raw_items: list[Any] = [v]
    elif isinstance(v, (list, tuple)):
        raw_items = list(v)
    else:
        raise ConfigError(
            f"{where} 必须是字符串或列表（可选值：{', '.join(PROTOCOLS)}），得到 {type(v).__name__}: {v!r}")
    out: list[str] = []
    for item in raw_items:
        name = str(item or "").strip()
        if not name:
            raise ConfigError(f"{where} 出现空的协议名")
        if name not in PROTOCOLS:
            raise ConfigError(
                f"{where} 里的 '{name}' 不是标准协议名（允许：{', '.join(PROTOCOLS)}）")
        if name not in out:
            out.append(name)
    if not out:
        raise ConfigError(f"{where} 为空列表 —— 每个模型至少要声明一条协议线")
    return out


def _parse_aliases(v: Any) -> dict[str, AliasSpec]:
    """解析顶层 `aliases` 段（第三十一轮虚拟模型 id）。

    容错口径（宁可报错，不可静默失效 —— 别名写错的后果是"客户端调这个 id 全部被拒"，
    而且现场离配置很远）：

      - 缺失 / None → {}（没配别名 = 功能没启用，不是错误）；
      - 不是映射 → ConfigError；
      - 别名 id 必须是 [A-Za-z0-9._-] 组成的 1~64 字符串（要能当 model 字段原样传）；
      - 每个别名必须是映射（也认 `wjd: opencode/kimi-k3` 这种只写 target 的简写）；
      - enabled 走 _opt_bool（认 "false"/no/off，默认 true）；
      - target 首尾空白清掉。

    **target 是否真实存在、是否支持 chat/completions 不在这里校验** —— 那需要模型索引，
    统一放到 validate_config（保存期/启动期都会走它，一处口径）。
    """
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise ConfigError(f"aliases 必须是映射（如 {{wjd: {{target: 模型id}}}}），得到 {type(v).__name__}")
    out: dict[str, AliasSpec] = {}
    for raw_name, raw_spec in v.items():
        name = str(raw_name or "").strip()
        where = f"aliases.{name or '（空名）'}"
        if not name:
            raise ConfigError("aliases 里有空的别名 id")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
            raise ConfigError(
                f"别名 id '{name}' 不合法：只能用字母/数字/点/下划线/连字符，长度 1~64"
                f"（它会被客户端原样写进 model 字段）")
        if isinstance(raw_spec, str):
            # 简写：`wjd: opencode/kimi-k3` 等价于只填 target（其余用默认值）
            spec = AliasSpec(target=raw_spec.strip())
        elif isinstance(raw_spec, dict):
            spec = AliasSpec(
                enabled=_opt_bool(raw_spec.get("enabled"), f"{where}.enabled", True),
                target=str(raw_spec.get("target") or "").strip(),
                note=str(raw_spec.get("note") or ""),
            )
        else:
            raise ConfigError(
                f"{where} 必须是映射（enabled/target/note）或直接写目标 id，得到 {type(raw_spec).__name__}")
        out[name] = spec
    return out


def _parse_endpoints(pval: dict[str, Any], pkey: str) -> dict[str, EndpointSpec]:
    """解析供应商级 `endpoints`（每条协议线的出站路径 / 鉴权 / 固定头）。

    未配置 → 三条标准线的默认值（见 _default_endpoints）。配置了就**只认配的那些线**：
    "没写这条线" 有语义 —— 该供应商没有这条线，模型声明了也没用（调用会明确报错，
    而不是发到一个猜出来的地址上收 404）。

    形如：
        endpoints:
          messages:
            path: "/messages"
            auth: "x-api-key"
            headers: {anthropic-version: "2023-06-01"}
    """
    raw = pval.get("endpoints")
    if raw is None:
        return _default_endpoints()
    if not isinstance(raw, dict):
        raise ConfigError(f"providers.{pkey}.endpoints 必须是映射，得到 {type(raw).__name__}")
    out: dict[str, EndpointSpec] = {}
    for pname, spec in raw.items():
        name = str(pname or "").strip()
        where = f"providers.{pkey}.endpoints.{name}"
        if name not in PROTOCOLS:
            raise ConfigError(f"{where} 不是标准协议名（允许：{', '.join(PROTOCOLS)}）")
        if not isinstance(spec, dict):
            raise ConfigError(f"{where} 必须是映射（path/auth/headers），得到 {type(spec).__name__}")
        path = str(spec.get("path") or "").strip()
        if not path:
            raise ConfigError(f"{where}.path 不能为空（相对 base_url 的路径，如 /messages）")
        auth = str(spec.get("auth") or "bearer").strip()
        if auth not in AUTH_STYLES:
            raise ConfigError(
                f"{where}.auth 只能是 {'/'.join(sorted(AUTH_STYLES))}，得到 {auth!r}")
        headers = spec.get("headers") or {}
        if not isinstance(headers, dict):
            raise ConfigError(f"{where}.headers 必须是映射（如 anthropic-version）")
        out[name] = EndpointSpec(path=path, auth=auth,
                                 headers={str(k): str(v) for k, v in headers.items()})
    return out


def _parse_media(pval: dict[str, Any]) -> MediaConfig:
    m = pval.get("media", {}) or {}
    return MediaConfig(
        image_endpoint=m.get("image_endpoint"),
        image_default_size=m.get("image_default_size"),
        video_create_endpoint=m.get("video_create_endpoint"),
        video_poll_base=m.get("video_poll_base"),
        video_poll_path=m.get("video_poll_path"),
        video_model_name=m.get("video_model_name"),
    )


def _parse_download(pval: dict[str, Any]) -> DownloadConfig:
    """解析 `download:` 段（媒体下载代理白名单）。

    容错：`allowed_hosts` 允许写成字符串（单个域名）或列表；自动去空白、去前导点、
    转小写并去重 —— 这样 `cos-platform-outputs.agnes-ai.cn`、
    `.cos-platform-outputs.agnes-ai.cn`、`COS-...` 三种写法等价。
    """
    d = pval.get("download", {}) or {}
    raw_hosts = d.get("allowed_hosts", []) or []
    if isinstance(raw_hosts, str):
        raw_hosts = [raw_hosts]
    hosts: list[str] = []
    for h in raw_hosts:
        if not isinstance(h, str):
            continue
        hh = h.strip().lower().lstrip(".")
        if hh and hh not in hosts:
            hosts.append(hh)
    return DownloadConfig(
        allowed_hosts=hosts,
        max_bytes=_opt_int(d.get("max_bytes"), "download.max_bytes", 64 * 1024 * 1024, minimum=1),
    )


def validate_config(cfg: GatewayConfig) -> list[str]:
    """结构性校验（Bug 12 修复）：返回问题列表，空 = 通过。

    校验供应商必填项与模型字段，给出可读的中文说明（而非运行到一半才崩）。
    """
    issues: list[str] = []
    if not cfg.providers:
        issues.append("未配置任何供应商（providers 为空）")
    for pkey, p in cfg.providers.items():
        if not p.base_url:
            issues.append(f"供应商 '{pkey}' 缺少 base_url")
        if not p.models:
            issues.append(f"供应商 '{pkey}' 未配置任何模型（models 为空）")
        for idx, m in enumerate(p.models):
            if not m.id:
                issues.append(f"供应商 '{pkey}' 的第 {idx + 1} 个模型缺少 id")
            # 第二十七轮: 模型声明了某条线，但供应商根本没配这条线的出站地址 ——
            #   这种配置下调用必然失败（而且会失败在"拼 URL"那一步），必须启动就拦住。
            for proto in m.protocols:
                if not p.supports_protocol(proto):
                    issues.append(
                        f"供应商 '{pkey}' 的模型 '{m.id}' 声明了协议线 '{proto}'，"
                        f"但该供应商的 endpoints 里没有这条线")
    # 账号体系已由页面管理（data/users.json，见 app/users.py），配置文件不再承载鉴权。
    #   首次启动会创建 admin/admin，不存在「自锁」场景。
    #
    # 第三十一轮: 虚拟模型 id（别名）—— 五条硬规则的 ①~④（见 ALIAS_PROTOCOLS 上方）。
    #   这里是**唯一的边界**：面板把不支持 chat/completions 的模型置灰只是 UX，
    #   手改 YAML / 直接打 PUT /config 都得在同一处被拦下，否则别名就成了绕过三线直传的后门。
    for name, spec in (cfg.aliases or {}).items():
        if cfg.find_provider_for_model(name) is not None:
            issues.append(
                f"别名 '{name}' 与真实模型 id 重名 —— 同一个名字不能既是模型又是别名，请改其中一方")
        if not spec.enabled:
            # 停用的别名不校验 target：允许"先停用、再慢慢改目标"，
            #   也允许目标模型被删掉后先把别名停着（否则用户会被逼着立刻填一个不想要的 id）。
            continue
        if not spec.target:
            issues.append(f"别名 '{name}' 已启用但没写 target（要转发到哪个真实模型）")
            continue
        found = cfg.find_provider_for_model(spec.target)
        if found is None:
            issues.append(
                f"别名 '{name}' 的目标 '{spec.target}' 不存在"
                f"（当前共 {len(cfg.all_models())} 个真实模型，可在「供应商与模型」页选择）")
            continue
        _pkey, _pconfig, minfo = found
        proto = ALIAS_PROTOCOLS[0]
        if minfo.type != "text":
            # 图像/视频模型在 yaml 里没写 protocols → 会走 DEFAULT_PROTOCOLS(chat/completions)，
            #   于是"格式上"看着可选，但它们真正的调用端点是 /v1/images/generations 之类，
            #   拿别名去聊天只会把上游的 400 抛给用户。这里直接拦掉（面板里也不列出它们）。
            issues.append(
                f"别名 '{name}' 的目标 '{spec.target}' 是 {minfo.type} 模型 —— 别名只用于文本对话"
                f"（chat/completions 线），请选一个文本模型")
        elif not minfo.supports(proto):
            issues.append(
                f"别名 '{name}' 固定走 {proto} 线，但目标 '{spec.target}' 不支持这条线"
                f"（它支持：{'、'.join(minfo.protocols) or '无'}）—— 网关不做跨格式转换，"
                f"请选一个支持 {proto} 的模型")
        elif not _pconfig.supports_protocol(proto):
            issues.append(
                f"别名 '{name}' 的目标 '{spec.target}' 所在供应商 '{_pkey}' 没有配置 {proto} 线的出站地址")
    return issues


def validate_warnings(cfg: GatewayConfig) -> list[str]:
    """非致命告警（启动/热重载时打印，不阻止运行）。"""
    warns: list[str] = []
    seen: dict[str, str] = {}
    for pkey, p in cfg.providers.items():
        for m in p.models:
            if not m.id:
                continue
            owner = seen.get(m.id)
            if owner is not None and owner != pkey:
                warns.append(f"模型 id '{m.id}' 被多个供应商定义（{owner}、{pkey}），"
                             f"按 id 调用将始终命中 '{owner}'")
            else:
                seen.setdefault(m.id, pkey)
    # 第六轮安全审计: 「以为已保护」的两处错觉，显式告警
    # 网关鉴权恒定开启（账号体系页面管理），health_auth 总是生效。
    if "*" in (cfg.server.cors_origins or []):
        warns.append("cors_origins 含通配 '*' —— 任意网站的浏览器页面都能跨域驱动本网关（含消耗你的上游额度）。"
                     "建议把 cors_origins 收敛为可信前端域名白名单（网关鉴权已恒定开启）。")
    # 供应商缺 Key / 缺模型：上线前就点出来，而不是调用时才 503 / fallback 悄悄失效
    for pkey, p in cfg.providers.items():
        if not p.models:
            warns.append(f"供应商 '{pkey}' 没有任何模型 —— 它不会出现在任何一条协议线上，永远不可用")
        if not p.auth.token and not (p.auth.token_env and os.environ.get(p.auth.token_env)):
            warns.append(f"供应商 '{pkey}' 未配置 API Key（token 与 token_env 均空/环境变量未设置）"
                         f" —— 调用该供应商的任何模型都会 503")
    # （账号/密码/API Key 已改由页面管理，弱凭证检查不再适用于配置文件。）
    return warns


def load_config(path: str | Path) -> GatewayConfig:
    """从 YAML 文件加载完整网关配置。

    结构性错误（缺供应商/缺 base_url/模型缺 id 等）抛 ConfigError，带友好说明。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}
    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> GatewayConfig:
    """把一份 YAML 字典解析成 GatewayConfig（不做文件 IO）。

    第二十三轮拆出来：管理页面（app/settings.py）保存前要**先在内存里校验**这份配置，
    不能让"写坏文件 → 重启起不来"发生；同时面板也用同一套解析得到"面板视图"。
    解析与校验口径与 load_config 完全一致（load_config 现在就是"读文件 + 这里"）。
    """
    raw = raw or {}
    providers: dict[str, ProviderConfig] = {}
    for pkey, pval in raw.get("providers", {}).items():
        auth_raw = pval.get("auth", {}) or {}
        auth = AuthConfig(
            token=auth_raw.get("token", ""),
            token_env=auth_raw.get("token_env", ""),
        )
        models = [
            ModelInfo(id=m.get("id", ""), display_name=m.get("display_name", ""),
                      type=m.get("type", "text"), vision=_opt_bool(m.get("vision"), "providers.*.models.*.vision", False),
                      free=_opt_bool(m.get("free"), "providers.*.models.*.free", False),
                      free_limited=_opt_bool(m.get("free_limited"), "providers.*.models.*.free_limited", False),
                      context_window=_opt_int(m.get("context_window"), "providers.*.models.*.context_window", 0),
                      max_output_tokens=_opt_int(m.get("max_output_tokens"), "providers.*.models.*.max_output_tokens", 0),
                      params=_parse_model_params(
                          m.get("params"), f"providers.{pkey}.models[{idx}].params"),
                      protocols=_parse_protocols(
                          m.get("protocols"), f"providers.{pkey}.models[{idx}].protocols"))
            for idx, m in enumerate(pval.get("models", []))
        ]
        providers[pkey] = ProviderConfig(
            key=pkey,
            name=pval.get("name", pkey),
            base_url=pval.get("base_url", ""),
            auth=auth,
            extra_headers=pval.get("extra_headers", {}) or {},
            models=models,
            media=_parse_media(pval),
            rate_limit_rps=_opt_float(pval.get("rate_limit_rps")),
            requires_session_header=_opt_bool(
                pval.get("requires_session_header"), "providers.*.requires_session_header", False),
            endpoints=_parse_endpoints(pval, pkey),
        )

    srv_raw = raw.get("server", {}) or {}
    # 第二十三轮: port 也走 _opt_int —— 此前是裸 `srv_raw.get("port", 8808)`，
    #   配置里写成字符串/空值会**穿过校验**，直到 uvicorn 绑定端口时才炸（而且报的是
    #   一个跟配置无关的堆栈）。管理页面可以直接改端口，更得在这里拦住。
    port = _opt_int(srv_raw.get("port"), "server.port", 8808, minimum=1)
    if port > 65535:
        raise ConfigError(f"server.port 必须在 1-65535 之间，得到: {port}")
    server = ServerConfig(
        host=str(srv_raw.get("host") or "127.0.0.1"),
        port=port,
        timeout=_opt_int(srv_raw.get("timeout"), "server.timeout", 120, minimum=1),
        retry=_opt_int(srv_raw.get("retry"), "server.retry", 1, minimum=1),
        health_auth=_opt_bool(srv_raw.get("health_auth"), "server.health_auth", False),
        hot_reload=_opt_bool(srv_raw.get("hot_reload"), "server.hot_reload", False),
        hot_reload_interval=_opt_int(srv_raw.get("hot_reload_interval"), "server.hot_reload_interval", 15, minimum=1),
        metrics_enabled=_opt_bool(srv_raw.get("metrics_enabled"), "server.metrics_enabled", True),
        rate_limit_rps=_opt_float(srv_raw.get("rate_limit_rps")),
        max_body_size=_opt_int(srv_raw.get("max_body_size"), "server.max_body_size", 2_000_000, minimum=1),
        cors_origins=list(srv_raw.get("cors_origins", ["*"])),
        graceful_shutdown=_opt_int(srv_raw.get("graceful_shutdown"), "server.graceful_shutdown", 30, minimum=0),
        batch_max_concurrency=_opt_int(srv_raw.get("batch_max_concurrency"), "server.batch_max_concurrency", 16, minimum=1),
        batch_max_items=_opt_int(srv_raw.get("batch_max_items"), "server.batch_max_items", 64, minimum=1),
        webhook_allow_private=_opt_bool(srv_raw.get("webhook_allow_private"), "server.webhook_allow_private", False),
        docs_enabled=_opt_bool(srv_raw.get("docs_enabled"), "server.docs_enabled", False),
        auth_fail_limit_per_min=_opt_int(srv_raw.get("auth_fail_limit_per_min"),
                                          "server.auth_fail_limit_per_min", 20, minimum=1),
    )

    # 网关入口鉴权（客户端 key 数组，可配多用户）
    auth_raw = raw.get("auth", {}) or {}
    # 账号体系已改由页面管理（data/users.json，见 app/users.py）；配置文件不再含用户/密钥，
    #   仅保留前端会话时长这一个运维开关。
    auth = AuthGate(session_ttl_minutes=_opt_int(auth_raw.get("session_ttl_minutes"),
                                                  "auth.session_ttl_minutes", 1440, minimum=1))

    # 第二十七轮: 「路由别名 / fallback 链」功能已整体移除。
    #   老配置里残留的 routes 段不再解析，只在日志里提醒一声（不报错，免得旧配置起不来）。
    #   第三十一轮补一句：现在想要"给客户端一个固定模型名"用 `aliases` 段（见 ALIAS_PROTOCOLS
    #   上方的说明）——它是一对一指针，不是按 Key 分链路，别拿旧概念往上套。
    if raw.get("routes"):
        log.warning("配置里的 'routes' 段已废弃（路由链功能已移除），本次加载忽略该段 —— "
                    "如果只是想要一个固定模型名指向某个真实模型，请改用 'aliases' 段")

    # 熔断器
    c_raw = raw.get("circuit", {}) or {}
    circuit = CircuitConfig(
        enabled=_opt_bool(c_raw.get("enabled"), "circuit.enabled", False),
        failure_threshold=_opt_int(c_raw.get("failure_threshold"), "circuit.failure_threshold", 3, minimum=1),
        cooldown=_opt_int(c_raw.get("cooldown"), "circuit.cooldown", 30, minimum=1),
        window=_opt_int(c_raw.get("window"), "circuit.window", 10, minimum=1),
    )

    # 可观测性
    o_raw = raw.get("observability", {}) or {}
    observability = ObservabilityConfig(
        log_level=str(o_raw.get("log_level", "INFO")),
        log_json=_opt_bool(o_raw.get("log_json"), "observability.log_json", False),
        access_log_maxlen=_opt_int(o_raw.get("access_log_maxlen"), "observability.access_log_maxlen", 500, minimum=1),
    )

    # 定价（用于成本估算）
    p_raw = raw.get("pricing", {}) or {}
    per_model: dict[str, dict[str, float]] = {}
    for pm, pv in (p_raw.get("per_model", {}) or {}).items():
        per_model[pm] = {
            "input": _opt_float(pv.get("input"), f"pricing.per_model.{pm}.input") or 0.0,
            "output": _opt_float(pv.get("output"), f"pricing.per_model.{pm}.output") or 0.0,
        }
    pricing = Pricing(per_model=per_model)

    cfg = GatewayConfig(
        providers=providers, server=server, auth=auth,
        circuit=circuit, observability=observability, pricing=pricing,
        download=_parse_download(raw),
        aliases=_parse_aliases(raw.get("aliases")),
    )
    # Bug 12 修复: 结构性校验，坏配置立刻抛友好错误（启动/热重载都能拦住）
    issues = validate_config(cfg)
    if issues:
        raise ConfigError("配置校验失败:\n  - " + "\n  - ".join(issues))
    return cfg
