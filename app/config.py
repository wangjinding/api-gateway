"""配置加载模块：从 YAML 文件读取供应商、模型、路由、鉴权、运维配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """配置结构性错误（缺必填项等）。启动/热重载时抛出，附友好说明。"""


# ── 模型 / 供应商 ──
@dataclass
class ModelInfo:
    id: str
    display_name: str = ""
    type: str = "text"  # text | image | video
    # 第十三轮: 是否支持「图像输入」（多模态 / 视觉）。
    #   Agnes 3.0 Flash 文档明确「输入模态：文本、图像 URL」，实测 data:image/png;base64 也能识别；
    #   其余文本模型（ark-code / b.ai 系）文档未声明图像输入 → 默认 False。
    #   用途：① /v1/models 暴露给前端标注/告警；② 带图请求走路由别名时把有视觉能力的模型排到前面。
    vision: bool = False


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
    video_model_name: str | None = None      # 轮询用的 model_name 参数（Agnes）
    video_style: str | None = None           # 视频契约风格："zhipu"=智谱异步(videos/generations + /async-result/{id})；None=Agnes 风格
    video_default_size: str | None = None     # 视频默认尺寸（智谱为像素串如 1920x1080；Agnes 用 size 档位）

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


# ── 网关入口鉴权 ──
# 账号体系（用户名/密码/API Key）由页面管理，保存在 data/users.json（见 app/users.py），
# 配置文件**不再承载**鉴权用户/密钥 —— 这里只保留前端会话时长这一个运维开关。
@dataclass
class AuthGate:
    session_ttl_minutes: int = 1440   # 前端 /auth/login 签发的 session 有效时长（分钟，默认 24h）


# ── 路由别名 + 优先级 fallback ──
@dataclass
class RouteInfo:
    """路由别名 → 按优先级排列的 fallback 模型链。

    models: 第 0 个为最高优先级，调用失败/超时依次切换。
    timeout: 单个模型尝试的超时（秒），None 用 server.timeout。
    retry:   切到下一个模型前，同一模型的重试次数（含首次）。
    rate_limit_rps: 该路由别名整体 QPS 上限（None=不限）。
    """

    name: str
    models: list[str] = field(default_factory=list)
    timeout: int | None = None
    retry: int = 1
    rate_limit_rps: float | None = None


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
    routes: dict[str, RouteInfo] = field(default_factory=dict)
    circuit: CircuitConfig = field(default_factory=CircuitConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    pricing: Pricing = field(default_factory=Pricing)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    # 模型 → (pkey, ProviderConfig, ModelInfo) 索引，避免每次请求线性扫描（性能）
    _model_index: dict[str, tuple[str, ProviderConfig, ModelInfo]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        index: dict[str, tuple[str, ProviderConfig, ModelInfo]] = {}
        for pkey, pconfig in self.providers.items():
            for m in pconfig.models:
                index.setdefault(m.id, (pkey, pconfig, m))  # 重名保留首个（与原线性扫描一致）
        self._model_index = index

    def find_provider_for_model(self, model_id: str) -> tuple[str, ProviderConfig, ModelInfo] | None:
        return self._model_index.get(model_id)

    def find_route(self, model_id: str) -> RouteInfo | None:
        return self.routes.get(model_id)

    def validate_routes(self) -> list[str]:
        """启动校验：路由里的 model ID 是否都存在。返回告警列表（空=全合法）。"""
        known = {m["id"] for m in self.all_models()}
        warnings: list[str] = []
        for rname, route in self.routes.items():
            for mid in route.models:
                if mid not in known:
                    warnings.append(f"路由 {rname} 引用了未知模型 '{mid}'")
        return warnings

    def all_models(self) -> list[dict[str, Any]]:
        result = []
        for pkey, pconfig in self.providers.items():
            for m in pconfig.models:
                result.append({
                    "id": m.id,
                    "display_name": m.display_name or m.id,
                    "type": m.type,
                    "vision": m.vision,
                    "provider": pkey,
                    "provider_name": pconfig.name,
                })
        return result

    def all_routes(self) -> list[dict[str, Any]]:
        out = []
        for rname, r in self.routes.items():
            out.append({
                "name": rname,
                "models": r.models,
                "timeout": r.timeout,
                "retry": r.retry,
            })
        return out


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


def _parse_media(pval: dict[str, Any]) -> MediaConfig:
    m = pval.get("media", {}) or {}
    return MediaConfig(
        image_endpoint=m.get("image_endpoint"),
        image_default_size=m.get("image_default_size"),
        video_create_endpoint=m.get("video_create_endpoint"),
        video_poll_base=m.get("video_poll_base"),
        video_poll_path=m.get("video_poll_path"),
        video_model_name=m.get("video_model_name"),
        video_style=m.get("video_style"),
        video_default_size=m.get("video_default_size"),
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

    校验供应商必填项与路由链，给出可读的中文说明（而非运行到一半才崩）。
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
    for rname, r in cfg.routes.items():
        if not r.models:
            issues.append(f"路由 '{rname}' 的模型链为空")
    # 账号体系已由页面管理（data/users.json，见 app/users.py），配置文件不再承载鉴权。
    #   首次启动会创建 admin/admin，不存在「自锁」场景。
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
            warns.append(f"供应商 '{pkey}' 没有任何模型 —— 路由链里指向它的 fallback 项永远不可用")
        if not p.auth.token and not (p.auth.token_env and os.environ.get(p.auth.token_env)):
            warns.append(f"供应商 '{pkey}' 未配置 API Key（token 与 token_env 均空/环境变量未设置）"
                         f" —— 调用该供应商的任何模型都会 503")
    # （账号/密码/API Key 已改由页面管理，弱凭证检查不再适用于配置文件。）
    # 路由引用了不存在的模型 id → 该 fallback 项永远不可用（多半是打错字）
    known = {m["id"] for m in cfg.all_models()}
    for r in cfg.all_routes():
        miss = [mid for mid in r["models"] if mid not in known]
        if miss:
            warns.append(f"路由 '{r['name']}' 引用了未定义的模型 {miss} —— 这些 fallback 项永远不可用")
    return warns


def load_config(path: str | Path) -> GatewayConfig:
    """从 YAML 文件加载完整网关配置。

    结构性错误（缺供应商/缺 base_url/空模型链等）抛 ConfigError，带友好说明。
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
                      type=m.get("type", "text"), vision=_opt_bool(m.get("vision"), "providers.*.models.*.vision", False))
            for m in pval.get("models", [])
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

    # 路由别名 → 优先级 fallback 链（支持 list 或 dict 两种写法）
    routes_raw = raw.get("routes", {}) or {}
    routes: dict[str, RouteInfo] = {}
    for rname, rval in routes_raw.items():
        if isinstance(rval, dict):
            chain: list[str] = []
            for item in (rval.get("models", []) or []):
                if isinstance(item, str):
                    chain.append(item)
                elif isinstance(item, dict) and item.get("model"):
                    chain.append(item["model"])
            routes[rname] = RouteInfo(
                name=rname,
                models=chain,
                # 注意 minimum=0：缺省值是 0（=未设置，下面 `or None` 转 None）。
                # 若写 minimum=1，会让"路由没写 timeout"的配置直接报 ConfigError —— 这正是
                # 第七轮自查抓到的一个自伤 bug（之前只有在配置恰好写了 timeout 时才不炸）。
                timeout=(_opt_int(rval.get("timeout"), f"routes.{rname}.timeout", 0, minimum=0) or None),
                retry=_opt_int(rval.get("retry"), f"routes.{rname}.retry", 1, minimum=1),
                rate_limit_rps=_opt_float(rval.get("rate_limit_rps")),
            )
        else:
            chain = [x for x in (rval or []) if isinstance(x, str)]
            routes[rname] = RouteInfo(name=rname, models=chain)

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
        providers=providers, server=server, auth=auth, routes=routes,
        circuit=circuit, observability=observability, pricing=pricing,
        download=_parse_download(raw),
    )
    # Bug 12 修复: 结构性校验，坏配置立刻抛友好错误（启动/热重载都能拦住）
    issues = validate_config(cfg)
    if issues:
        raise ConfigError("配置校验失败:\n  - " + "\n  - ".join(issues))
    return cfg
