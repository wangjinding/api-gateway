"""API 网关主入口：FastAPI 应用（v2.4 全功能版）。

特性：
- 多供应商 OpenAI 兼容 /chat/completions（同模型重试 + 熔断；第二十七轮起三线直传：
  /v1/chat/completions、/v1/messages、/v1/responses 各按自家协议原样转发，网关不翻译字段）
- 图像 /v1/images/generations、视频 /v1/videos（异步 + 等待模式 + Webhook）
- 客户端 Bearer 鉴权（多用户）+ 全局/供应商级限流
- 用量指标 /metrics + Prometheus /metrics/prometheus + 访问日志 /logs
- 配置热加载、X-Request-ID 贯穿、CORS、gzip、请求体限制
- OpenAI 兼容错误格式 + /v1/models 标准端点
- Token 成本估算、Batch 并发调用、优雅关闭
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, quote

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request, Security, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, PlainTextResponse, Response, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.middleware.base import BaseHTTPMiddleware

from app.adapters import create_adapter
from app.circuit import CircuitBreakerRegistry
from app.config import (ConfigError, GatewayConfig, load_config, validate_warnings,
                        PROTOCOL_CHAT, PROTOCOL_MESSAGES, PROTOCOL_RESPONSES, PROTOCOLS,
                        ALIAS_PROTOCOLS)
from app.metrics import (Metrics, AccessLog, RateLimiter, BODY_BY_RID, _BODY_MAX,
                         video_poll_push, video_polls,
                         ACCOUNT, ROLE)
from app.models import (ChatRequest, ImageRequest, VideoCreateRequestExtended, BatchRequest,
                        strip_protected, PROTECTED_VIDEO_KEYS, PROTECTED_IMAGE_KEYS,
                        normalize_conversation_id)
from app.routing import resolve_with_auth, ModelNotFoundError, AuthConfigError
from app.sessions import SessionStore
from app.users import UsersStore, UserError, key_limit_for_role
from app.history import HistoryStore, HistoryError, MAX_ITEMS as HISTORY_CAP
from app.feedback import FeedbackStore, FeedbackError, MAX_ENTRIES as FEEDBACK_CAP
from app.video_owners import VideoOwnerStore
from app import settings as gw_settings

# 功能 8: 读取 .env（若存在），补充 token 等环境变量。
#   项目根 .env：不覆盖真实 env（override=False）；
#   data/.env：是「网关配置页」管理 API Key 的那层，写盘后需在重启时优先生效 → override=True，
#   否则根 .env 里残留的旧 key 会顶掉页面刚存的 data/.env 新值。
try:
    from dotenv import load_dotenv
    for _p, _ov in ((Path(__file__).parent.parent, False), (Path(__file__).parent.parent / "data", True)):
        if (_p / ".env").exists():
            load_dotenv(_p / ".env", override=_ov)
except ImportError:
    pass


class UTF8JSONResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"


# ── 配置加载（可热重载） ──
_PROJECT_ROOT = Path(__file__).parent.parent
_ENV_CONFIG = os.environ.get("GATEWAY_CONFIG", "")
# 配置唯一入口：GATEWAY_CONFIG（可选），否则用 data/gateway.yaml（config/ 已随拆 Docker 合并删除）。
if _ENV_CONFIG:
    CONFIG_PATH = Path(_ENV_CONFIG)
else:
    CONFIG_PATH = _PROJECT_ROOT / "data" / "gateway.yaml"

config: GatewayConfig = load_config(CONFIG_PATH)
_metrics = Metrics(enabled=config.server.metrics_enabled)
_access = AccessLog(maxlen=config.observability.access_log_maxlen)
_limiter = RateLimiter()

# ── 页面配置的账号体系（替代配置文件里的鉴权） ──
# 用户/密码/API Key 全部管理在 data/users.json（见 app/users.py），配置文件不再承载鉴权。
_USERS_PATH = Path(os.environ.get("GATEWAY_USERS", str(_PROJECT_ROOT / "data" / "users.json")))
_users = UsersStore(str(_USERS_PATH))

# ── 前端历史落盘（图像 / 视频 / 对话） ──
# 原先这三类只存在浏览器里（图像视频 localStorage、对话 sessionStorage），
# 换设备看不见、关标签页就丢、配额写满还是静默失败 —— 现在统一存服务端，按账号隔离。
_HISTORY_PATH = Path(os.environ.get("GATEWAY_HISTORY", str(_PROJECT_ROOT / "data" / "history.json")))
_history = HistoryStore(str(_HISTORY_PATH))

# ── 意见反馈（第二十二轮） ──
# 普通账号提交（可附图）→ 管理员在「用户反馈」页看全部。正文进 JSON、图片落文件，
# 详见 app/feedback.py 顶部的设计说明。
_FEEDBACK_PATH = Path(os.environ.get("GATEWAY_FEEDBACK", str(_PROJECT_ROOT / "data" / "feedback.json")))
_FEEDBACK_MEDIA = Path(os.environ.get("GATEWAY_FEEDBACK_MEDIA", str(_PROJECT_ROOT / "data" / "feedback_media")))
_feedback = FeedbackStore(str(_FEEDBACK_PATH), str(_FEEDBACK_MEDIA))

# ── 视频任务归属（第三十轮） ──
# 轮询端点 GET /v1/videos/{video_id} 原先不判归属（任何登录账号拿到 id 就能读别人任务）。
# 现在创建成功时记一条 id→账号，轮询按它判；无记录的旧任务放行（见 app/video_owners.py 头部口径）。
_OWNERS_PATH = Path(os.environ.get("GATEWAY_VIDEO_OWNERS", str(_PROJECT_ROOT / "data" / "video_owners.json")))
_owners = VideoOwnerStore(str(_OWNERS_PATH))

# 请求级账号上下文（ContextVar 每 task 独立，异步并发安全；定义在 app/metrics.py，
#   供访问日志/指标标记「归属账号」，从而实现所有人（含 admin）只看自己的调用日志；
#   admin 若要审计别人的活动，走用户管理页的 admin 专属端点 /auth/users/{name}/(logs|history/*)）
def _set_identity(request: Request, account: str, role: str = "member") -> None:
    request.state.account = account
    request.state.role = role
    request.state.caller = account
    request.state.auth_key = ""       # 不再回显任何明文凭证
    ACCOUNT.set(account)
    ROLE.set(role)
_circuits = CircuitBreakerRegistry({
    "enabled": config.circuit.enabled,
    "failure_threshold": config.circuit.failure_threshold,
    "cooldown": config.circuit.cooldown,
    "window": config.circuit.window,
})
_state: dict = {"mtime": 0.0}


def _setup_logging() -> None:
    """按 observability 配置设置日志（可选 JSON 行）。

    Bug 1：用 handler 替换而非 basicConfig（后者只首次生效），
    保证热重载改 log_level / log_json 时日志格式/级别真正变化。
    """
    o = _cfg().observability
    level = getattr(logging, o.log_level.upper(), logging.INFO)
    if o.log_json:
        fmt = '{"ts":"%(asctime)s","lvl":"%(levelname)s","mod":"%(name)s","msg":"%(message)s"}'
    else:
        fmt = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter(fmt))
    root.addHandler(h)


# 基础日志先起来，进 lifespan 后按 observability 重配
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("api-gateway")


def _config_mtime() -> float:
    try:
        return CONFIG_PATH.stat().st_mtime
    except OSError:
        return 0.0


_state["mtime"] = _config_mtime()


def _cfg() -> GatewayConfig:
    return config


def _warn_security_posture(cfg) -> None:
    """启动/热重载时对"高危但合法"的配置打醒目告警（只提示，不改行为）。

    背景：默认 host=127.0.0.1（只监听回环）是安全的；一旦把 host 改成非回环地址，
    任何能连到该端口的人都可尝试登录/调用。网关鉴权现在恒定开启（账号体系见
    app/users.py），入站请求必须有账号会话或账号 API Key，因此不再是「裸代理」，
    但仍建议只对内网/反代开放并配合防火墙。
    """
    host = (cfg.server.host or "").strip()
    loopback = host in ("127.0.0.1", "localhost", "::1")
    # 网关鉴权恒定开启（账号体系页面管理，见 app/users.py），不再有 enabled 开关；
    # 这里仅提示监听地址是否对外。若对外监听，账号体系 + 会话/API Key 已能阻止未授权调用。
    if not loopback:
        log.warning(
            "⚠ 安全提示：网关监听 %s（非本机回环）。入站请求需账号登录会话或账号 API Key 方可调用；"
            "账号数据（密码/Key 哈希）保存在 data/users.json，生产环境建议仅对内网/反代开放并配合防火墙。",
            host or "127.0.0.1")
    if not cfg.server.health_auth:
        log.info("提示：/health/* 未启用鉴权（health_auth=false），仅建议内网探活使用。")
    if cfg.server.webhook_allow_private:
        log.warning("⚠ 安全提示：webhook_allow_private=true，视频 callback_url 允许指向内网/回环地址（存在 SSRF 风险）。")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global config
    _setup_logging()
    log.info("API Gateway 启动，已加载 %d 供应商", len(config.providers))
    _warn_security_posture(config)
    for w in validate_warnings(config):
        log.warning("配置告警: %s", w)
    # ⚠ 修复（第五轮，P0 级）：这里**绝不能**再注册 SIGTERM/SIGINT 处理器。
    #   原实现是：
    #       loop.add_signal_handler(sig, stop_event.set)
    #   而 uvicorn 在 install_signal_handlers() 里也是用 loop.add_signal_handler 注册
    #   SIGTERM/SIGINT → 我们的注册把它**覆盖**掉了，于是 SIGTERM/SIGINT 再也不会
    #   触发 uvicorn 的 should_exit：进程停不下来，只能被 SIGKILL 硬杀。
    #   后果：supervisorctl stop / Ctrl+C 会卡到超时后强杀，在途请求被硬切、
    #   adapter 连接池不会优雅关闭（实测 SIGTERM 后 10s+ 仍存活）。
    #   现在改为：完全交给 uvicorn 的信号处理 + timeout_graceful_shutdown（run.py 已按
    #   server.graceful_shutdown 传入），关闭逻辑只放在下方的 lifespan 收尾里执行。
    hot_task = None
    if config.server.hot_reload:
        hot_task = asyncio.create_task(_hot_reload_loop())
    yield
    # 关闭阶段：先停热重载，再等在途请求（uvicorn 自身也做，这里补逻辑日志）
    if hot_task:
        hot_task.cancel()
        try:
            await hot_task            # 第六轮: 消费掉取消，避免 "Task was destroyed but pending"
        except (asyncio.CancelledError, Exception):
            pass
    # 第六轮: 视频异步回调的后台任务也要收尾 —— 此前它们从不受生命周期管理，
    #   关停时被事件循环硬切 → callback_url 静默丢通知（与文档的"异步通知"承诺不符）。
    if _bg_tasks:
        log.info("等待 %d 个后台回调任务收尾（最多 5s）", len(_bg_tasks))
        _, pending = await asyncio.wait(set(_bg_tasks), timeout=5)
        for t in pending:
            t.cancel()
    log.info("API Gateway 关闭，等待在途请求（graceful_shutdown=%ds）", config.server.graceful_shutdown)
    # 第六轮: 快照后再迭代（避免迭代中字典被改动 → RuntimeError）
    for adapter in list(_state.get("adapters", {}).values()):
        await adapter.close()


async def _apply_config(new: GatewayConfig, *, source: str = "热重载") -> None:
    """把新配置切到运行态 —— mtime 热重载与页面保存**共用这一条路径**。

    第二十三轮：页面保存（PUT /config）也要"保存即生效"，如果它自己另写一套切换逻辑，
    迟早和热重载跑偏（此前光是热重载就有过 mtime 顺序 / adapter 泄漏两处坑）。所以：
    两边都只调用这里，语义只有一份。

    就地更新（保留累积状态）：
      - 指标保留（uptime / 调用数不丢）；访问日志保留但按新容量调整；
      - 熔断状态保留；**限流桶保留**（避免切换瞬间清空令牌桶造成突发放行）。
    整体丢弃：adapter 缓存 —— 供应商/模型/超时可能全变了，旧连接池必须重建。
      顺序是"先原子摘掉旧字典，再逐个 close()"（第六轮并发审计）：反过来的话，中间的
      await 会让并发请求仍能从旧字典拿到 adapter 并新建连接池，随后字典被置空 →
      那个连接池再没人关闭（连接泄漏）。
    """
    global config
    old_cfg = config
    config = new
    _metrics.enabled = new.server.metrics_enabled
    _access.resize(new.observability.access_log_maxlen)
    _circuits.update_config({
        "enabled": new.circuit.enabled,
        "failure_threshold": new.circuit.failure_threshold,
        "cooldown": new.circuit.cooldown,
        "window": new.circuit.window,
    })
    old_adapters = _state.get("adapters", {})
    _state["adapters"] = {}
    for a in old_adapters.values():
        try:
            await a.close()
        except Exception as e:      # 关不掉也要继续换新配置（否则一次异常就卡住整轮）
            log.warning("关闭旧 adapter 失败（忽略）: %s", e)
    _setup_logging()  # Bug 1: 日志级别 / JSON 真正变化
    for w in validate_warnings(new):
        log.warning("配置告警: %s", w)
    _warn_security_posture(new)
    log.warning("配置已生效（%s：%d 供应商 / 熔断=%s）",
                source, len(new.providers), new.circuit.enabled)
    # 第三十一轮（虚拟模型 id）: 别名决定"客户端看到的那个名字实际打给谁"，谁在什么时候
    #   把它改到哪个模型上，必须能在日志里查到（页面保存的 source 里带着账号，热重载也写明），
    #   否则这个入口被改过就只能靠猜。
    _log_alias_changes(old_cfg, new, source)


def _enabled_aliases(cfg: GatewayConfig) -> dict[str, str]:
    """别名 → 目标（只取启用的）。停用的别名与"没配过"同义，不参与改动比较。"""
    return {n: s.target for n, s in (cfg.aliases or {}).items() if s.enabled}


def _log_alias_changes(old: GatewayConfig, new: GatewayConfig, source: str) -> None:
    """别名指向的改动逐条记日志（新增 / 改指向 / 停用），没变则一句话都不说。"""
    before, after = _enabled_aliases(old), _enabled_aliases(new)
    for name in sorted(set(before) | set(after)):
        b, a = before.get(name), after.get(name)
        if b == a:
            continue
        if b is None:
            log.warning("虚拟模型 id 生效（%s）: %s → %s", source, name, a)
        elif a is None:
            log.warning("虚拟模型 id 停用（%s）: %s（原指向 %s）", source, name, b)
        else:
            log.warning("虚拟模型 id 改指向（%s）: %s → %s（原 %s）", source, name, a, b)


async def _hot_reload_loop() -> None:
    """周期检查配置文件 mtime，变化则重新加载（热重载）。

    Bug 4/E/O 修复：
      - hot_reload_interval 每轮重新读取（改动即时生效，不再只读一次）
      - 保留 metrics / 访问日志 / 限流桶 / 熔断状态（不再整体清空）
      - 重载后重新计算并打印配置告警
    第二十三轮：实际切换逻辑抽到 `_apply_config`（与页面保存共用）；
      server.hot_reload 关掉后循环即停手（不再"关了还在后台重载"）；
      注意从"关"改回"开"需要重启进程（任务只在 lifespan 里创建）。
    """
    while True:
        interval = max(1, _cfg().server.hot_reload_interval)  # 每轮读取，允许热改间隔
        await asyncio.sleep(interval)
        try:
            if not _cfg().server.hot_reload:
                continue                      # 关掉就彻底不动手（改回开需重启）
            m = _config_mtime()
            if m and m != _state.get("mtime"):
                new = load_config(CONFIG_PATH)
                _state["mtime"] = m
                await _apply_config(new, source="mtime 热重载")
        except Exception as e:
            log.error("配置热重载失败，继续使用旧配置: %s", e)


_docs_on = config.server.docs_enabled   # 安全默认关（report: /docs、/openapi.json 公开降低侦察成本）

# 本轮修复: 版本号收敛到单一常量。此前 "2.4.0" 硬编码在 FastAPI / / 导航 / /auth/login
#   三处，/health 干脆没返回版本 —— 前端「网关版本」一直显示 v-。
VERSION = "2.4.0"

app = FastAPI(
    title="API Gateway",
    description="统一多供应商 LLM 接入网关（Agnes / 火山方舟 / OpenCode Go）",
    version=VERSION,
    lifespan=lifespan,
    default_response_class=UTF8JSONResponse,
    docs_url="/docs" if _docs_on else None,
    redoc_url="/redoc" if _docs_on else None,
    openapi_url="/openapi.json" if _docs_on else None,
)

# P1-11 gzip 响应压缩（大文本 / Base64 图像受益明显）
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Fix 6: 动态 CORS —— 每次请求实时读取配置，热重载改 cors_origins 即时生效。
# 原有 CORSMiddleware 在启动时固化了 origins，改配置也不生效（需重启）。
# 兼容原行为：allow_origins 含 "*" 时用通配来源、不带 credentials（浏览器规范）。
#
# Fix 5（本轮）: 注册被移到最后（见下方 app.add_middleware(BaseHTTPMiddleware…)）。
# Starlette 的 add_middleware 是「后注册者更外层」，此前 CORS 在 limit_body 内层，
# 导致 limit_body 短路返回的 413（体积超限）没有 CORS 头 —— 浏览器只能看到 CORS 报错、
# 读不到「请求体超限」的真实原因。放到最外层后，所有短路响应都会带上 CORS 头。
def _cors_decision(request: Request) -> str:
    """根据当前配置与请求 Origin 决定 Access-Control-Allow-Origin 取值（空串=不放行）。"""
    origins = _cfg().server.cors_origins or []
    if "*" in origins:
        return "*"
    origin = request.headers.get("origin", "")
    return origin if origin in origins else ""


def _apply_cors(request: Request, response) -> None:
    """给响应补 CORS 头。CORS 中间件与最外层 500 兜底中间件共用。

    第六轮: 500 由 Starlette 的 ServerErrorMiddleware（恒在最外层）直接生成，
    **绕过 CORS 层** → 浏览器读不到 500 的响应体。兜底中间件复用本函数补头。
    """
    allowed = _cors_decision(request)
    if allowed:
        response.headers["Access-Control-Allow-Origin"] = allowed
        response.headers["Access-Control-Expose-Headers"] = "X-Request-ID"
        if allowed != "*":
            response.headers["Vary"] = "Origin"


async def dynamic_cors(request: Request, call_next):
    allowed = _cors_decision(request)

    # 预检请求（OPTIONS + Access-Control-Request-Method）直接应答，不进路由
    if request.method == "OPTIONS" and request.headers.get("access-control-request-method"):
        headers: dict[str, str] = {}
        if allowed:
            headers["Access-Control-Allow-Origin"] = allowed
            headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
            headers["Access-Control-Allow-Headers"] = request.headers.get(
                "access-control-request-headers", "*")
            headers["Access-Control-Max-Age"] = "600"
            if allowed != "*":
                headers["Vary"] = "Origin"
        return Response(status_code=200, headers=headers)

    response = await call_next(request)
    _apply_cors(request, response)
    return response


# ── 请求体大小限制（Bug 3：同时拦 Content-Length 和 chunked） ──
_BODY_METHODS = ("POST", "PUT", "PATCH", "DELETE")


async def _check_body_size(request: Request, max_size: int) -> int | None:
    """检查请求体；返回拒绝状态码（413 超限 / 400 非法 Content-Length），None 表示放行。

    - 只对有请求体的方法检查（GET/HEAD/OPTIONS 直接放行，省一次无谓读取）
    - 有 Content-Length：直接比（快速路径，不消费 body）；畸形值返回 400（此前进 int() 抛异常→500）
    - chunked / 无 CL：逐块流式读，超上限立即拒绝（有界内存，不读完整包）；
      未超限时把已读块回贴到 request._body，保证 handler 仍能读到完整 body。
    """
    if request.method not in _BODY_METHODS:
        return None
    cl = request.headers.get("Content-Length")
    if cl is not None:
        try:
            return 413 if int(cl) > max_size else None
        except ValueError:
            return 400  # Bug 3: 畸形 Content-Length，按客户端错误处理（而非 500）
    # chunked / 无 CL：有界流式读
    count = 0
    chunks: list[bytes] = []
    try:
        async for chunk in request.stream():
            count += len(chunk)
            if count > max_size:
                return 413  # 超限：拒绝，不再读剩余（有界内存）
            chunks.append(chunk)
    except Exception:
        # 本轮修复: 读 body 出错时不再静默放行 —— 否则 handler 会拿到**不完整**的 body，
        #   报出一个与真实原因无关的 422，排查时极具误导性。直接按客户端错误 400 返回。
        return 400
    # 未超限：回贴 body，让 handler 的 request.json()/body() 仍可读
    if chunks:
        try:
            request._body = b"".join(chunks)  # Starlette 约定：handler 读 body 时优先用 _body 缓存
        except Exception:
            pass
    return None


@app.middleware("http")
async def limit_body(request: Request, call_next):
    max_size = _cfg().server.max_body_size
    code = await _check_body_size(request, max_size)
    if code:
        # 第六轮: 复用 _sanitize_rid（此前直接回显客户端头，未消毒）
        rid = _sanitize_rid(request.headers.get("X-Request-ID")) or "-"
        if code == 413:
            msg = f"请求体超过上限 {max_size} 字节"
        else:
            msg = "非法或不完整的请求体"
        return UTF8JSONResponse(
            status_code=code,
            content={"error": {"message": msg,
                               "type": "invalid_request_error", "code": str(code)},
                    "x_request_id": rid},
        )
    return await call_next(request)


# ── 功能 11 + Bug 3：在途请求计数（按响应实际类型区分流式） ──
@app.middleware("http")
async def in_flight_tracker(request: Request, call_next):
    # 第六轮并发审计: 原来的实现有两个方向相反的缺陷，且被 max(0,…) 夹逼互相掩盖：
    #   ① `except Exception` 抓不到 CancelledError（客户端断连/优雅关闭）→ 计数永久 +1 泄漏；
    #   ② `isinstance(response, StreamingResponse)` 判定恒为假（Starlette 1.6 的
    #      BaseHTTPMiddleware 返回的是 `_StreamingResponse`，它是 Response 的子类、
    #      **不是** StreamingResponse）→ 流式请求被"这里减一次 + event_stream 再减一次"
    #      双重递减，gauge 长期偏低。
    # 现在统一语义：**一个请求 = 一次 increment，进入 → 响应头生成完毕**，只在 finally 减一次。
    _metrics.increment_inflight()
    # M4: 缓存请求体摘要（截断到约 2KB），供访问日志展示"具体请求的内容"。
    #   Starlette 的 request.body() 会缓存，之后 FastAPI 解析 body 复用同一份，不影响路由。
    rid = getattr(request.state, "rid", "")
    if rid:
        try:
            raw = await request.body()
            BODY_BY_RID[rid] = raw[:2000].decode("utf-8", "replace")
            if len(BODY_BY_RID) > 2 * _BODY_MAX:
                BODY_BY_RID.clear()
        except Exception:
            BODY_BY_RID.pop(rid, None)
    try:
        response = await call_next(request)
    except BaseException:
        _metrics.decrement_inflight()
        raise
    # 第七轮: `call_next` 在"收到 http.response.start"时就返回（body 由外层继续迭代），
    #   所以只在 finally 里减会让 SSE 传输期间的读数**提前归零**（实测 yield#1 起就是 0）。
    #   这里把递减挂到响应体迭代结束/中断时；拿不到 body_iterator 才退回立即递减。
    body_iter = getattr(response, "body_iterator", None)
    if body_iter is None:
        _metrics.decrement_inflight()
        return response

    async def _counted(inner):
        try:
            async for chunk in inner:
                yield chunk
        finally:
            _metrics.decrement_inflight()

    try:
        response.body_iterator = _counted(body_iter)
    except Exception:
        _metrics.decrement_inflight()
    return response


# ── 请求 ID 中间件（P2-3） ──
_RID_MAX = 128


def _sanitize_rid(raw: str | None) -> str | None:
    """只接受 [A-Za-z0-9._-] 且长度 ≤128 的请求 ID，避免 CR/LF 注入响应头或日志。

    客户端可自由传 X-Request-ID，若不消毒，把 `a\\r\\nX-Evil: 1` 塞进来会造成
    响应头注入（部分服务器/代理会拒绝或截断）。非法值返回 None，由调用方生成 UUID。
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw or len(raw) > _RID_MAX:
        return None
    if all(c.isalnum() or c in "._-" for c in raw):
        return raw
    return None


def _logsafe(v, maxlen: int = 200) -> str:
    """日志用：剥离控制字符（CR/LF/TAB 等）。

    第六轮安全审计: `video_id` 是路径参数（客户端可控），直接进 `%s` 明文日志时
    `%0a` 会解码成换行 → 可伪造日志行、污染审计。这里统一替换不可打印字符。
    """
    return "".join(ch if ch.isprintable() else "?" for ch in str(v))[:maxlen]


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    rid = _sanitize_rid(request.headers.get("X-Request-ID")) or str(uuid.uuid4())
    request.state.rid = rid
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


# Fix 5（本轮）: CORS 注册放在最后 → 位于最外层，覆盖 limit_body 等短路响应。
app.add_middleware(BaseHTTPMiddleware, dispatch=dynamic_cors)


# 第六轮: 最外层兜底 —— 未捕获异常也要返回 OpenAI 兼容的 500，且**必须带 CORS 头**。
#   未捕获异常由 Starlette 的 ServerErrorMiddleware 处理，而它恒在所有用户中间件之外，
#   响应不经过 CORS 层 → 浏览器只能看到 CORS 报错、读不到 500 的响应体。
#   本中间件注册在 dynamic_cors 之后（= 更外层），因此自己调用 _apply_cors 补头。
@app.middleware("http")
async def unhandled_error_guard(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        log.exception("未捕获异常: %s", e)
        rid = getattr(request.state, "rid", "-")
        resp = UTF8JSONResponse(
            status_code=500,
            content={"error": {"message": f"网关内部错误: {type(e).__name__}",
                               "type": "server_error", "code": "500"},
                     "x_request_id": rid},
        )
        _apply_cors(request, resp)
        return resp


# ── 安全响应头（安全报告：加固项）。注册在最外层，覆盖正常/错误/预检响应。 ──
@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    # CSP：自己托管的前端在 /web 同源加载，故允许同源脚本/样式/数据图，
    # 但仍禁外部 CDN 脚本、框架嵌入、表单与 base 跳转（XSS/点击劫持防护）。
    resp.headers.setdefault(
        "Content-Security-Policy",
        # M5+ 修复: vue-i18n 的 message 编译在运行时用 new Function（动态编译），
        #   原先 script-src 'self' 不带 'unsafe-eval' 会把整个前端拦成白屏（本地只有
        #   无 CSP 的 vite preview 能渲染时很难发现）。这里给 script-src 补
        #   'unsafe-eval'；其余保持全强（禁外部脚本/连接/框架/base/form）。
        "default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https:; media-src 'self' blob: https:; "
        "font-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    )
    return resp


# ── OpenAI 兼容错误格式（P0-2） ──
# OpenAI SDK 期望 {"error": {"message","type","code"}}
_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "invalid_request_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
    501: "not_supported_error",
    502: "upstream_error",
    500: "server_error",
}


def _jsonable_errors(errors: list) -> list:
    """把 pydantic 的 `errors()` 清洗成一定能 JSON 序列化的形式。

    第二十六轮实测踩到：自定义校验器 `raise ValueError(...)` 时，pydantic v2 会把**原始异常
    对象**放进 `errors()[i]["ctx"]["error"]`。这里原先直接把 `exc.errors()` 塞进响应体，
    于是 422 响应在序列化时抛 `TypeError: Object of type ValueError is not JSON serializable`
    → 中间件兜底成 **500「网关内部错误: TypeError」**：明明只是客户端参数写错，却被伪装成
    "网关崩了"，且**任何新增的校验器都会踩同一个坑**。所以这里统一清洗：
    `ctx` 里的异常对象转字符串、`input` 收敛成 JSON 安全类型，拿不准的一律 str()。
    """
    out: list = []
    for e in errors or []:
        try:
            item = dict(e)
        except Exception:
            out.append({"msg": str(e)})
            continue
        ctx = item.get("ctx")
        if isinstance(ctx, dict):
            item["ctx"] = {
                k: (str(v) if isinstance(v, BaseException) else v) for k, v in ctx.items()
            }
        inp = item.get("input")
        if inp is not None and not isinstance(inp, (str, int, float, bool, list, dict)):
            item["input"] = str(inp)
        out.append(item)
    return out


@app.exception_handler(RequestValidationError)
async def openai_compat_validation_error(request: Request, exc: RequestValidationError):
    """Bug 2 修复: 请求体校验失败也返回 OpenAI 兼容格式。

    此前 FastAPI 默认返回 {"detail":[...]}，OpenAI SDK 无法解析。
    现在统一为 {"error": {"message","type","code"}}（并保留 detail 供调试）。
    """
    rid = getattr(request.state, "rid", "-")
    try:
        err0 = exc.errors()[0]
        loc = ".".join(str(x) for x in err0.get("loc", []) if x != "body")
        raw = str(err0.get("msg", ""))
        # pydantic 会给校验器抛出的 ValueError 加 "Value error, " 前缀；字段名已在 loc 里，
        # 保留前缀只会让调用方看到「conversation_id: Value error, …」这种噪音。
        if raw.startswith("Value error, "):
            raw = raw[len("Value error, "):]
        msg = f"请求参数校验失败: {loc or 'body'}: {raw}"
    except Exception:
        msg = "请求参数校验失败"
    # 第二十六轮: detail 必须清洗后再输出 —— 校验器抛出的异常对象不可 JSON 序列化，
    #   直接放进去会让 422 变成 500（详见 _jsonable_errors 的说明）。
    detail = _jsonable_errors(exc.errors())
    try:
        json.dumps(detail, ensure_ascii=False)
    except Exception:
        detail = []
    return UTF8JSONResponse(
        status_code=422,
        content={"error": {"message": msg, "type": "invalid_request_error",
                           "code": "422", "detail": detail},
                 "x_request_id": rid},
    )


@app.exception_handler(HTTPException)
async def openai_compat_error(request: Request, exc: HTTPException):
    rid = getattr(request.state, "rid", "-")
    if isinstance(exc.detail, dict):
        # 已经是结构化错误（如 fallback errors[]）
        err = exc.detail
    else:
        err = {"message": str(exc.detail)}
    err.setdefault("type", _ERROR_TYPES.get(exc.status_code, "error"))
    err.setdefault("code", str(exc.status_code))
    return UTF8JSONResponse(
        status_code=exc.status_code,
        content={"error": err, "x_request_id": rid},
        headers=getattr(exc, "headers", None) or {},
    )


def _get_adapter(model_id: str, timeout: int | None = None, cfg: GatewayConfig | None = None):
    """根据 model_id 获取适配器，缓存到 _state（缓存 key 含 timeout + headers 指纹）。"""
    cfg = cfg or _cfg()
    pkey, pconfig, minfo, token = resolve_with_auth(cfg, model_id)
    t = timeout or cfg.server.timeout
    hfp = hashlib.md5(json.dumps(sorted(pconfig.extra_headers.items())).encode()).hexdigest()[:8]
    # 第七轮: 缓存 key 必须含 base_url。热重载"先原子摘除适配器字典再关闭"之后，
    #   一个持有旧 cfg 的在途请求仍可能重建 adapter 并写回新字典；key 若不含 base_url，
    #   后续按新配置发起的请求会命中这个旧地址的 adapter（改了 base_url 却不生效）。
    bfp = hashlib.md5((pconfig.base_url or "").encode()).hexdigest()[:8]
    # Bug 13 修复: 缓存 key 纳入 token 指纹 —— env token 变化（配置 mtime 不变）也能刷新，
    #   避免旧 Authorization 头残留。
    tfp = hashlib.md5((token or "").encode()).hexdigest()[:8]
    cache_key = f"{pkey}:{t}:{hfp}:{tfp}:{bfp}"
    if cache_key not in _state.get("adapters", {}):
        _state.setdefault("adapters", {})[cache_key] = create_adapter(pconfig, token, timeout=t)
    return _state["adapters"][cache_key], pkey, pconfig


# ── 鉴权 + 限流（全局 / 供应商级；路由级限流随路由功能一并移除） ──
_bearer = HTTPBearer(auto_error=False)
# 前端登录会话（/auth/login 换取 sess- token，见 app/sessions.py）。
# 第三十轮起落盘到 data/sessions.json（只存 token 的 sha256）→ 网关重启不再把所有人踢下线；
# 测试用 GATEWAY_SESSIONS 指到独立文件，避免污染真实会话。
_SESSIONS_PATH = Path(os.environ.get("GATEWAY_SESSIONS", str(_PROJECT_ROOT / "data" / "sessions.json")))
_sess = SessionStore(path=_SESSIONS_PATH)


def _effective_rps(cfg, pkey: str | None) -> float | None:
    """限流优先级：供应商级 > 全局。"""
    if pkey and cfg.providers.get(pkey) and cfg.providers[pkey].rate_limit_rps:
        return cfg.providers[pkey].rate_limit_rps
    return cfg.server.rate_limit_rps


def _enforce_rate_limit(cfg, request, pkey: str | None, model_id: str) -> None:
    """按 供应商级 > 全局 两级限流（Bug 1 修复）。

    供应商级用 bucket=prov:<pkey>（与媒体端点共桶），限流则 429；
    全局级在 require_auth 里已按 key 处理。路由级限流随路由功能一并移除。
    """
    rps = _effective_rps(cfg, pkey)
    if not rps:
        return
    if pkey and (cfg.providers.get(pkey) or None) and cfg.providers[pkey].rate_limit_rps:
        if not _limiter.allow(f"prov:{pkey}", cfg.providers[pkey].rate_limit_rps):
            raise HTTPException(429, f"供应商 {pkey} 触发限流（{cfg.providers[pkey].rate_limit_rps} rps）")


def _enforce_media_rate_limit(cfg, pkey: str | None) -> None:
    """#4：图像/视频端点的供应商级限流。

    媒体是高成本资源，与 chat 共用**同一供应商桶** prov:<pkey>
    （Bug 3 修复：之前媒体用 media:prov:<pkey> 独立桶，导致同一供应商实际放行量翻倍，
     与本文档声称的"按供应商桶"不符）。配置 providers.<key>.rate_limit_rps 时生效。
    全局限流已由 require_auth 按 key 处理，这里补供应商维度。
    """
    if pkey and cfg.providers.get(pkey) and cfg.providers[pkey].rate_limit_rps:
        rps = cfg.providers[pkey].rate_limit_rps
        if not _limiter.allow(f"prov:{pkey}", rps):
            raise HTTPException(429, f"供应商 {pkey} 媒体请求触发限流（{rps} rps）")


# ── 后台任务（视频异步 webhook 通知） ──
# asyncio 只持任务弱引用，不显式保存的话可能被 GC 掉；同时统一打异常日志，
# 避免「后台任务里抛异常，控制台一声不响」。
_bg_tasks: set = set()


def _spawn_bg(coro, name: str) -> None:
    task = asyncio.create_task(coro, name=name)
    _bg_tasks.add(task)

    def _done(t) -> None:
        _bg_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            log.warning("后台任务 %s 异常: %s", name, exc)

    task.add_done_callback(_done)


# ── 媒体熔断桶 + webhook 回调地址校验（本轮新增） ──
def _media_circuit(pkey: str):
    """媒体（图像/视频）用**独立**熔断桶，与 chat 的供应商桶分开。

    修复：此前媒体调用完全不喂熔断器，而视频轮询却拿 chat 的桶做 allow() 检查 ——
    两边对不上（一个永不计、一个永远查），坏掉的媒体供应商永远拦不住。
    独立成桶还能避免「客户端传坏 prompt 导致上游 4xx」把 chat 的熔断误打开。
    """
    return _circuits.get(f"{pkey}#media")


def _media_circuit_check(cb, pkey: str, pconfig) -> None:
    if not cb.allow():
        raise HTTPException(503, f"供应商 {pconfig.name} 媒体服务熔断中，请稍后重试")


async def _validate_callback_url(raw: str | None, allow_private: bool) -> None:
    """校验视频 webhook 回调地址（SSRF 防护），非法直接 400。

    - 仅允许 http/https
    - 默认禁止指向 回环/私网/链路本地/保留/多播 地址（可用 server.webhook_allow_private 放行）
    - 域名会做异步 DNS 解析后校验解析结果，避免用「指向内网的域名」绕过字面量检查
    """
    if not raw:
        return
    p = urlparse(raw)
    if p.scheme not in ("http", "https"):
        raise HTTPException(400, f"callback_url 只支持 http/https，得到: {p.scheme or '（空）'}")
    host = p.hostname
    if not host:
        raise HTTPException(400, "callback_url 缺少主机名")
    if allow_private:
        return
    ips: list = []
    try:
        ips.append(ipaddress.ip_address(host))
    except ValueError:
        # 第六轮安全审计: `p.port` 对 ":99999"/":abc" 会抛 ValueError（端口越界/非数字），
        #   此前未捕获 → 500（纯文本）。按客户端错误 400 返回。
        try:
            port = p.port or (443 if p.scheme == "https" else 80)
        except ValueError:
            raise HTTPException(400, f"callback_url 端口非法: {_logsafe(host)}")
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port)
        except OSError as e:
            raise HTTPException(400, f"callback_url 域名无法解析: {host}（{e}）")
        for info in infos:
            try:
                ips.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                pass  # 带 scope id 的 IPv6 等，跳过
    for ip in ips:
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise HTTPException(
                400,
                f"callback_url 指向内网/保留地址（{ip}）已被拒绝；如确需内网回调，"
                f"请设置 server.webhook_allow_private=true",
            )


# ── IP 级鉴权失败限流（安全报告：中低危——无 IP 限流可暴力枚举 / 刷流量）──
_AUTH_FAILS: dict[str, deque] = {}
_AUTH_FAIL_MAX_IPS = 1000   # 最多跟踪的 IP，超限清空一次（防内存膨胀）


_TRUSTED_PROXY_IPS = {"127.0.0.1", "::1"}   # 本机反代（nginx/openresty）：只有它写的 XFF 可信


def _client_ip(request: Request) -> str:
    """取真实客户端 IP：网关只被可信反代（127.0.0.1）访问，X-Forwarded-For 可信任。

    第十八轮（安全修复）: 取 XFF 的**最右段**，不再是 `parts[0]`。
    - nginx 用 `$proxy_add_x_forwarded_for`：它把 `$remote_addr` **追加**到末尾，
      而客户端自带的 `X-Forwarded-For: 1.2.3.4` 会原样留在**最前面**；
    - 此前取最左段 = 取攻击者可控值 → 每次换一个假 IP 就能重置登录失败计数，
      「60s 内 N 次」的登录限流形同虚设（还能把 _AUTH_FAILS 顶到上限后整体清空）。
    - 最右段由我们的反代写入，不可伪造；同时只在直连方确实是本机反代时才认这个头
      （万一 8808 被直接暴露，伪造的 XFF 一律忽略）。取值再截断到 64 字符，防脏键。
    """
    peer = request.client.host if request.client is not None else ""
    if peer in _TRUSTED_PROXY_IPS:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                return parts[-1][:64]
    return peer or "unknown"


def _record_auth_fail(request: Request, limit_per_min: int) -> bool:
    """记录一次鉴权失败；返回 True 表示该来源 60s 内已超限（应直接 429）。"""
    ip = _client_ip(request)
    now = time.time()
    dq = _AUTH_FAILS.get(ip)
    if dq is None:
        if len(_AUTH_FAILS) >= _AUTH_FAIL_MAX_IPS:
            _AUTH_FAILS.clear()
        dq = deque()
        _AUTH_FAILS[ip] = dq
    dq.append(now)
    while dq and now - dq[0] > 60.0:
        dq.popleft()
    return len(dq) >= limit_per_min


def _key_token(key: str) -> str | None:
    """丢 API Key 前缀（wjd- / 旧 ak-）取随机串本体；非 Key 凭证（登录会话）返回 None。

    第二十九轮：缺 conversation_id 时用「这个 API Key 自己」派生稳定的上游会话 —— 也就是
    老板要的「x-opencode-session 就传 wjd- 后面那串」的落地版。但**不能把随机串本体原样
    传给上游**：那串就是网关 Key 的凭证部分，发出去等于把 Key 关键内容泄给 opencode（第三
    方）。所以这里取出来只用于哈希（_upstream_session 里 sha256），稳定性和"原样传"完全
    一致，且不落地上游任何明文。
    """
    for p in ("wjd-", "ak-"):
        if key.startswith(p):
            return key[len(p):]
    return None


def _identity_from_credentials(key: str) -> tuple[str, str, str] | None:
    """Bearer 凭证 → (account, role, kind)。kind ∈ {"session", "apikey"}。

    第八轮审计补: ① 要区分凭证类型（改密/铸 Key/管账号只认会话）；
    ② 会话所属账号若已被删除，会话必须一并作废 —— 只靠删除时主动吊销不够，
       手工编辑 users.json 等路径也要在鉴权时兜底判定。
    """
    if not key:
        return None
    if _sess.is_session(key):
        acc = _sess.account_for(key)
        if not acc:
            return None
        if _users.find(acc) is None:          # 账号已不存在 → 会话作废
            _sess.revoke_account(acc)
            return None
        # 第二十四轮: 账号被停用 → 会话一并作废（停用时已主动吊销，这条防手工改 users.json）。
        if _users.is_disabled(acc):
            _sess.revoke_account(acc)
            return None
        return acc, _users.role(acc), "session"
    ident = _users.resolve_key(key)
    if not ident:
        return None
    return ident[0], ident[1], "apikey"


def _upstream_detail(text: str) -> str:
    """从上游 4xx/5xx 响应体里抽出原文（可能是 {message} 或 {error:{message}}）。"""
    try:
        j = json.loads(text or "{}")
        m = j.get("message") or (j.get("error") or {}).get("message") or j.get("detail")
        # 兜底：上游只回空对象 / 只回 HTTP 状态没有正文时，m 会是 "{}" 或 ""。
        #   空 body 的 4xx 在 httpx 里异常文本形如 'HTTP 502: Empty response body'，
        #   但 200+body-err 等罕见情况会走到 str(j)=="{}" → 直接回空串让调用方给兜底文案。
        m = str(m).strip() if m is not None else ""
        if m in ("{}", "", "null", "None"):
            return ""
        return m[:200]
    except Exception:
        return text[:200]


def _speedtest_retryable(e: Exception) -> bool:
    """测速探针是否值得重试：仅上游 429 / 5xx（免费档瞬时限流 / 抖动）。
    4xx（模型不存在 / 鉴权 / 参数）与超时不重试——前者重试无意义、后者会拖慢整轮。"""
    status = getattr(getattr(e, "response", None), "status_code", None)
    return status == 429 or (status is not None and status >= 500)


def _speedtest_error_text(e: Exception, timeout: float) -> str:
    """测速探针的失败文案（第二十五轮）：把「超时」和「上游原文」这两个关键信息说清楚。

    修复前只回 `str(e)`，形如 `Client error '400 Bad Request' for url ...` ——
    上游真正的原因（如 `invalid temperature: only 1 is allowed for this model`）
    根本看不到；超时更是只显示成含糊的「调用失败」。结果是：
      「参数被上游拒绝」（每次都失败）与「模型响应慢」（偶发失败）
    在前端长得一模一样，只能靠猜 —— 排查这两个模型时就是这么被误导的。
    """
    if isinstance(e, (httpx.TimeoutException, TimeoutError)):
        return f"上游超时（>{int(timeout)}s 未响应）"
    resp = getattr(e, "response", None)
    if resp is not None:
        detail = _upstream_detail(getattr(resp, "text", "") or "")
        if detail:
            return detail
        return f"上游返回 HTTP {getattr(resp, 'status_code', '?')}（无响应正文）"
    text = str(e)
    return _upstream_detail(text) or text[:200] or "调用失败"


def _clear_auth_fails(request: Request) -> None:
    """登录成功后清掉该来源的失败计数：否则同一出口 IP（办公室 NAT）被前人失败连带，
    真人输对密码后紧接一次手误就吃 429。"""
    _AUTH_FAILS.pop(_client_ip(request), None)


async def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
):

    cfg = _cfg()
    _set_identity(request, "anonymous", "member")
    key = credentials.credentials if credentials else ""
    # 第二十七轮（三线直传）: Anthropic 系客户端（Claude Code / anthropic-sdk）默认把
    #   凭证放在 `x-api-key` 而不是 `Authorization: Bearer` —— 既然网关对外提供
    #   /v1/messages，就必须认这个头，否则"格式对了但连鉴权都过不去"。
    #   优先级：Authorization 优先（显式标准头），没有才回落到 x-api-key。
    if not key:
        key = (request.headers.get("x-api-key") or "").strip()
    identity = _identity_from_credentials(key)
    if identity is None:
        # 第二十四轮: Key 本身是有效的、只是所属账号被管理员停用 → 回 403 明确告知原因，
        # 不混进 401「无效凭证」（调用方会以为 Key 打错了），也不计入登录失败限流
        # （拿到有效 Key 的人不是暴力破解者，别把办公室出口 IP 一起限了）。
        owner = _users.disabled_key_owner(key) if key else None
        if owner:
            log.info("已停用账号 %s 的 API Key 被调用，已拒绝（%s %s）",
                     owner, request.method, request.url.path)
            raise HTTPException(403, "账号已被停用，请联系管理员")
        if _record_auth_fail(request, cfg.server.auth_fail_limit_per_min):
            raise HTTPException(429, "鉴权失败次数过多，已在 60 秒内触发限流，请稍后重试")
        raise HTTPException(401, "无效或缺失的账号 / API Key", headers={"WWW-Authenticate": "Bearer"})
    account, role, kind = identity
    _set_identity(request, account, role)
    request.state.credential = kind
    # 第二十九轮：把「这个 API Key 的随机串」暂存到 state，供 _upstream_session 在缺
    #   conversation_id 时派生出稳定的上游会话（见 _key_token 的说明）。登录会话没有 Key，
    #   置 None，由 _upstream_session 按账号稳定兜底。
    request.state.key_token = _key_token(key) if kind == "key" else None
    # 全局限流按**账号**分桶（yaml/注释都写"单 user QPS 上限"）。
    # 第八轮审计: 原先是 account + 凭证前缀，同一个人多开几个会话/多建几把 Key 就能
    # 成倍放大自己的配额，账号级限流形同虚设。
    if cfg.server.rate_limit_rps and not _limiter.allow(account, cfg.server.rate_limit_rps):
        raise HTTPException(429, "请求过于频繁，触发限流")


async def require_auth_or_open(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
):
    """/health 专用：health_auth=True 时强制鉴权；否则放行但标记"是否带了有效凭证"。
    未鉴权访问 `GET /health` 只返回 {"status":"ok"}（安全报告：收敛 /health 信息量，避免泄露内部拓扑）。"""
    cfg = _cfg()
    request.state.caller = "probe"
    request.state.health_authed = False
    _set_identity(request, "probe", "member")
    if credentials and credentials.credentials:
        identity = _identity_from_credentials(credentials.credentials)
        if identity:
            account, role, kind = identity
            _set_identity(request, account, role)
            request.state.credential = kind
            request.state.caller = account
            request.state.health_authed = True
    if cfg.server.health_auth and not request.state.health_authed:
        raise HTTPException(401, "无效或缺失的账号 / API Key", headers={"WWW-Authenticate": "Bearer"})


def _alias_line_mismatch(spec, name: str, protocol: str) -> str:
    """用别名去打非 chat/completions 线时的 400 文案。

    老板要的是一个"名字稳定"的入口，不是"格式稳定"的转换器：别名固定走 chat/completions
    （见 config.ALIAS_PROTOCOLS）。这里把话说全 —— 它指向谁、该用哪个端点、哪个端点能用别名
    —— 否则用户只看到一句"不支持"，还得回来问人。
    """
    return (f"{name} 是虚拟模型 id（别名，指向 {spec.target or '（未配置目标）'}），"
            f"只支持 {ALIAS_PROTOCOLS[0]} 格式。网关不做跨格式转换："
            f"请把请求发到 POST /v1/{ALIAS_PROTOCOLS[0]}；"
            f"要用 {protocol} 格式请直接写它指向的真实模型 id {spec.target or ''}".rstrip())


def _require_protocol_model(cfg: GatewayConfig, model: str, protocol: str) -> str:
    """校验「模型存在 + 声明了这条协议线」，返回**真实**模型 id（第二十七轮三线直传的闸门）。

    路由链 / fallback 已在第二十七轮整体移除：入站 model 必须是真实模型 id（或第三十一轮
    起的**别名**），网关不再"帮你挑一个"、也不会跨线降级。为什么必须在这里挡：实测
    （OpenCode Go 2026-09-18）把 chat 线的模型打到 /messages 上，上游回的是 **500 Internal
    server error**；把 messages 线的模型打到 /responses 上，回的是 401 "not supported for
    format openai"。两种都不像"你选错了格式"，与其让用户对着这种错误猜，不如网关直接 400
    说清楚——候选支持哪几条线也一并报出来。

    第三十一轮（虚拟模型 id）：入站 model 允许写别名（如 wjd），这里把它换成目标真实模型 id
    **后**再往下走 —— 于是"挑供应商 / 盖强制参数 / 记账 / 熔断"全部作用在真实模型上（一份事实），
    而客户端请求名与真实目标名的对应关系由响应壳（model / served_model）与访问日志（model /
    served）分别体现。返回真实 id 是**有意**的：下游每一处都该按真实模型办事。
    """
    spec = cfg.alias_of(model)
    if spec is not None:
        if protocol not in ALIAS_PROTOCOLS:
            raise HTTPException(400, _alias_line_mismatch(spec, model, protocol))
        if not spec.target:
            raise HTTPException(400, f"虚拟模型 id {model} 没有配置目标模型（请在网关配置里设置）")
        model = spec.target      # 之后一切按真实模型走
    found = cfg.find_provider_for_model(model)
    if found is None:
        raise HTTPException(400, f"未知模型: {model}")
    if cfg.find_model_for_protocol(model, protocol) is None:
        raise HTTPException(
            400,
            f"模型 {model} 不支持 {protocol} 线（它支持：{'、'.join(found[2].protocols) or '无'}）。"
            f"网关不做跨格式转换，请改用模型支持的线，或换一个支持 {protocol} 的模型。")
    return model


# ── 第十三轮: 视觉（带图）请求的模型选择 ─────────────────────────
def _message_parts(content: Any) -> list:
    """把消息 content 归一成"内容块数组"；纯文本（str/None）返回空列表。"""
    if isinstance(content, list):
        return content
    return []


def messages_have_images(messages: list[dict]) -> bool:
    """请求里是否带了图像块（OpenAI 视觉格式：content=[{type:image_url,...}]）。

    只认规范里的 image_url / input_image；分不清形状的块一律忽略（宁可漏判也不要误判）。
    """
    for m in messages or []:
        for part in _message_parts(m.get("content")):
            if not isinstance(part, dict):
                continue
            if part.get("type") in ("image_url", "input_image"):
                if part.get("image_url") or part.get("image_url_url") or part.get("url"):
                    return True
    return False


def _resolve_media_model(cfg, model: str, media_type: str):
    """Bug 4: 解析媒体模型。

    - model 是真实模型 ID 且类型匹配（或 text）→ 直接 resolve
    - 找不到 → 返回 None

    第二十七轮: 路由链解析分支已随路由功能整体移除 —— 媒体端点也只认真实模型 id。
    第三十一轮: 别名（wjd）在这里**显式拒绝**（抛 400），不是"找不到"。理由：别名只在文本
      chat/completions 线上存在；图像/视频端点的契约（尺寸/时长/参考图/回调）跟文本完全不同，
      "换个名字"在这边等于换一条业务线，很容易让人以为改名就能用。给一句能直接照做的错误，
      比返回"模型不存在"更省事。
    """
    aspec = cfg.alias_of(model)
    if aspec is not None:
        kind = {"image": "图像生成", "video": "视频生成"}.get(media_type, f"{media_type} 调用")
        raise HTTPException(
            400,
            f"模型 {model} 是虚拟模型 id（别名，指向 {aspec.target}，只用于文本对话的 "
            f"{ALIAS_PROTOCOLS[0]} 线），不能用于{kind}。请直接使用该端点支持的真实模型 id。")
    found = cfg.find_provider_for_model(model)
    if found:
        pkey, pconfig, minfo = found
        if minfo.type == media_type or media_type == "text":
            return pkey, pconfig, minfo, pconfig.auth.resolve_token(), model



def _resp_text(res: dict) -> str:
    """从已完成的上游结果里提取回复正文（用于访问日志展示流式/非流式的输出内容）。

    本轮补充: 推理型模型（火山方舟 / Kimi thinking 等）在小 max_tokens 下正文可能为空、
    内容全在 reasoning_content，日志里就会显示成空白 —— 这时回落到推理段并标注，
    保证「有输出就能在日志里看到」。
    """
    try:
        choices = res.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            content = str(msg.get("content") or "")
            if content.strip():
                return content
            reasoning = str(msg.get("reasoning_content") or "")
            if reasoning.strip():
                return "[推理] " + reasoning
        # 第二十七轮: messages / responses 线的响应结构完全不同（没有 choices）。
        #   访问日志要能看到"这次到底回了什么"，所以顺手认一下这两家的正文位置
        #   —— 只影响日志展示，**不改任何响应内容**。
        #   messages:      content=[{type:text,text:...}]
        #   responses:     output_text（部分实现直接给）/ output[].content[].text
        blocks = res.get("content")
        if isinstance(blocks, list):
            text = "".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict))
            if text.strip():
                return text
        if isinstance(res.get("output_text"), str) and res["output_text"].strip():
            return res["output_text"]
        out = res.get("output")
        if isinstance(out, list):
            parts = []
            for item in out:
                for b in (item.get("content") or []) if isinstance(item, dict) else []:
                    if isinstance(b, dict) and b.get("text"):
                        parts.append(str(b["text"]))
            if parts:
                return "".join(parts)
    except Exception:
        pass
    return ""


def _stream_log_text(resp_parts: list[str], reason_parts: list[str]) -> str:
    """流式访问日志正文：优先正文，正文为空则回落推理段（与 _resp_text 语义一致）。"""
    text = "".join(resp_parts)
    if text.strip():
        return text
    reasoning = "".join(reason_parts)
    return ("[推理] " + reasoning) if reasoning.strip() else ""


def _attach_cost(resp: dict, model: str, usage: dict, cfg) -> None:
    """P1-13 Token 成本估算（若配置了 pricing）。

    第六轮: 上游没给 usage 时不再输出 `estimated_cost_cny: 0.0` —— 那是**假数据**
    （看起来像"这次调用免费"，实际是"不知道"）。无 usage 就不报成本。
    """
    if not usage or not (usage.get("prompt_tokens") or usage.get("completion_tokens")):
        return
    c = cfg.pricing.estimate(model, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))
    if c is not None:
        resp["estimated_cost_cny"] = round(c, 6)


# ─────────────────────────────────────
# GET /models + GET /v1/models（P1-7 / P0-3）
# ─────────────────────────────────────
def _models_payload():
    cfg = _cfg()
    # 优化: 目录按配置 mtime 缓存（热重载改配置才重建；静默期每次请求直接命中，省掉
    #   all_models() 的重复遍历与对象构建）。cfg 引用同一 config 对象，
    #   但在 hot_reload 时 mtime 会变 → 缓存自然失效。
    mtime = _state.get("mtime")
    _cached = _state.get("_models_cache")
    if _cached is not None and _cached[0] == mtime:
        return _cached[1]
    # 第六轮: 统一 data 里的 object 取值（一律 "model"），并补齐 OpenAI 标准的
    #   created / owned_by 字段 —— 严格按 `object == "model"` 过滤的客户端才认。
    #   第二十七轮: 路由别名（type="route"）条目随路由功能整体移除，这里只剩真实模型。
    created = int(_state.get("mtime") or 0)
    data = []
    for m in cfg.all_models():
        data.append({"id": m["id"], "object": "model", "created": created,
                     "owned_by": m["provider"],
                     "display_name": m["display_name"],
                     "type": m["type"], "provider": m["provider"], "provider_name": m["provider_name"],
                     # 第十三轮: 暴露「是否支持图像输入」，前端据此标注/告警（对话页带图时提示换模型）
                     "vision": bool(m.get("vision")),
                     # 第二十五轮: 暴露模型级强制参数（如 {"temperature": 1}）。前端据此标挂锁，
                     #   API 调用方也能提前知道自己传的采样参数会被上游要求覆盖。
                     "params": dict(m.get("params") or {}),
                    # 第二十七轮（三线直传）: 该模型能走哪几条协议线
                    #   （chat/completions | messages | responses）。前端据此打格式标签，
                    #   调用方据此决定把请求发到 /v1/chat/completions、/v1/messages 还是
                    #   /v1/responses —— 网关不做任何跨线翻译，写错线直接 400。
                    "protocols": list(m.get("protocols") or [PROTOCOL_CHAT])})
    # 第三十一轮: 虚拟模型 id（别名）也进目录 —— 客户端得能发现"有个叫 wjd 的 id 可以用"，
    #   否则这个入口只存在于口头约定里。但它是指针不是模型：
    #     · protocols 固定 chat/completions（别名只走 OpenAI 格式线，见 config.ALIAS_PROTOCOLS）；
    #     · vision / params / provider / type 全部**继承目标**（唯一事实来源，避免两份配置漂移）；
    #     · 多一个 alias_of（真实模型 id）与 alias_note —— 前端据此渲染「wjd → 目标」那一行
    #       （admin 的选择框默认选中项、member 的只读行都读它）。
    #   停用的别名、以及目标不可用的别名一律不出现（与"没配过"同义）。
    for name, spec in (cfg.aliases or {}).items():
        if not spec.enabled:
            continue
        tgt = cfg.find_provider_for_model(spec.target) if spec.target else None
        if tgt is None:
            continue      # 保存期已校验；这里只兜底，宁可不出条目也不给一个坏条目
        apkey, apconfig, aminfo = tgt
        data.append({"id": name, "object": "model", "created": created,
                     "owned_by": apkey,
                     "display_name": aminfo.display_name,
                     "type": aminfo.type, "provider": apkey, "provider_name": apconfig.name,
                     "vision": bool(aminfo.vision),
                     "params": dict(aminfo.params or {}),
                     "protocols": list(ALIAS_PROTOCOLS),
                     "alias_of": aminfo.id, "alias_note": spec.note})
    payload = {
        "object": "list",
        "data": data,
        # 本轮修复: 不再回显 base_url（内部拓扑信息，未开鉴权时会被任意访问者读到）
        "providers": {k: {"name": v.name, "model_count": len(v.models)}
                      for k, v in cfg.providers.items()},
    }
    _state["_models_cache"] = (mtime, payload)
    return payload


@app.get("/models")
async def list_models(request: Request, _auth=Depends(require_auth)):
    return _models_payload()


@app.get("/v1/models")
async def v1_models(request: Request, _auth=Depends(require_auth)):
    return _models_payload()


# ── 第二十七轮「三线直传」：/v1/messages 与 /v1/responses ──────────────
# 老板决策：**入站是什么格式，出站就是什么格式**；网关不翻译任何协议内容，只在
# 「供应商与模型」里声明每个模型能用哪几条协议线（models[].protocols）。
#
# 与 /v1/chat/completions 的关系：
#   chat/completions 线走**专用路径**（ChatRequest 校验 + 适配器按字段组装 payload），
#   这两条新线走**通用直传**（原始 JSON 原样进去、上游响应原样回来）—— 原因是
#   这两家协议的结构（内容块、tool 表达、SSE 事件类型）差别很大，重建一遍字段
#   就是"翻译"，会引入损耗，正好违背老板要的"不转"。
#
# 网关在直传里仍然做的（不算转格式，是网关的立身之本）：
#   ① 挑供应商与协议线：模型不支持这条线 → 400，绝不偷偷翻译；
#   ② 定点改写 model（别名 → 真实 id）；
#   ③ 盖模型级强制参数（models[].params，如 kimi-k2.7-code 只接受 temperature=1）；
#   ④ 注入该线的鉴权头 + x-opencode-session（入站 conversation_id 派生成按账号隔离的会话 id）；
#   ⑤ 熔断 / 限流 / 记账（usage 三种形状各解析一次，只读）/ 访问日志；
#   ⑥ 补网关壳字段（served_model/provider/provider_name/protocol/x_request_id）。
# 明确**不做**：字段改名、内容块互转、工具调用互转、SSE 事件互转、错误文案翻译。

def _protocol_mismatch(minfo, model_id: str, protocol: str) -> str:
    """模型不支持入站协议线时的 400 文案（说清"它支持什么"，别让用户猜）。"""
    supported = "、".join(minfo.protocols) or "（未声明）"
    others = "、".join(p for p in PROTOCOLS if p != protocol)
    return (f"模型 {model_id} 不支持 {protocol} 格式（它支持：{supported}）。"
            f"网关不做跨格式转换：请改用 {supported} 对应的端点，或换一个支持 {protocol} 的模型"
            f"（如 {others} 线的模型）。")


def _usage_counts(protocol: str, data: Any) -> dict[str, int]:
    """把三家协议的 usage 归一成 (prompt_tokens, completion_tokens) 供记账。

    这是**只读解析**：上游响应体原样交给客户端，这里只是为了指标/成本不丢
    —— chats 用 prompt_tokens/completion_tokens，messages 与 responses 都用
    input_tokens/output_tokens（不解析的话，两条新线的调用在统计里会全是 0）。
    """
    if not isinstance(data, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0}
    u = data.get("usage")
    if not isinstance(u, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0}
    if protocol in (PROTOCOL_MESSAGES, PROTOCOL_RESPONSES):
        return {"prompt_tokens": int(u.get("input_tokens") or 0),
                "completion_tokens": int(u.get("output_tokens") or 0)}
    return {"prompt_tokens": int(u.get("prompt_tokens") or 0),
            "completion_tokens": int(u.get("completion_tokens") or 0)}


class _UsageScanner:
    """流式直传的 usage 扫描器（只读，不改一个字节）。

    流式响应的 usage 藏在 SSE 事件里，且三家位置不同：
      chat/completions → `data: {"usage": {...}}`（需 include_usage）
      messages         → event `message_start`（input_tokens）+ `message_delta`（output_tokens）
      responses        → event `response.completed`（response.usage）
    这里按行喂入，识别 data: 行里的 JSON，并把常见的三种嵌套位置都看一眼，
    取到就更新（不依赖事件顺序 —— 两个方向各自覆盖，缺一个也不影响另一个）。
    """

    def __init__(self, protocol: str):
        self.protocol = protocol
        self.prompt = 0
        self.completion = 0
        self.seen = False
        self._buf = ""

    def feed(self, chunk: bytes) -> None:
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            return          # 跨 chunk 的多字节字符会在下一个 chunk 里补齐（这里丢掉半截即可）
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._line(line.strip())

    def _line(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return
        try:
            obj = json.loads(payload)
        except (ValueError, json.JSONDecodeError):
            return
        if not isinstance(obj, dict):
            return
        for cand in (obj, obj.get("message"), obj.get("response")):
            if not isinstance(cand, dict):
                continue
            u = cand.get("usage")
            if not isinstance(u, dict):
                continue
            inp = u.get("input_tokens", u.get("prompt_tokens"))
            out = u.get("output_tokens", u.get("completion_tokens"))
            if inp is not None:
                self.prompt = int(inp)
                self.seen = True
            if out is not None:
                self.completion = int(out)
                self.seen = True

    def counts(self) -> dict[str, int]:
        return {"prompt_tokens": self.prompt, "completion_tokens": self.completion}


def _resolve_passthrough(cfg: GatewayConfig, body: dict, protocol: str):
    """直传入口的公共解析：取 model + 校验协议线 + 抽 conversation_id。

    返回 (model_id, pkey, pconfig, minfo, conversation_id, clean_body)；
    clean_body 已摘掉网关内部字段（conversation_id），其余原样。
    """
    model_in = str(body.get("model") or "").strip()
    if not model_in:
        raise HTTPException(400, "缺少 model 字段")
    clean = dict(body)
    try:
        conv = normalize_conversation_id(clean.pop("conversation_id", None))
    except ValueError as e:
        raise HTTPException(400, f"conversation_id {e}")
    # 第三十一轮: 别名只走 chat/completions 线 —— 拿它打 messages / responses 一律入口 400，
    #   且**在查模型之前**先判（否则用户会收到"未知模型: wjd"，而 wjd 明明在 /v1/models 里，
    #   那种报错会把人带偏到"是不是我权限不够"上去）。
    aspec = cfg.alias_of(model_in)
    if aspec is not None:
        raise HTTPException(400, _alias_line_mismatch(aspec, model_in, protocol))
    found = cfg.find_provider_for_model(model_in)
    if found is None:
        raise HTTPException(400, f"未知模型: {model_in}")
    pkey, pconfig, minfo = found
    if not minfo.supports(protocol):
        raise HTTPException(400, _protocol_mismatch(minfo, model_in, protocol))
    if not pconfig.supports_protocol(protocol):
        raise HTTPException(400, f"供应商 {pconfig.name} 未配置 {protocol} 线")
    return model_in, pkey, pconfig, minfo, conv, clean


async def _passthrough(request: Request, protocol: str):
    """三线直传的通用实现（当前服务 /v1/messages 与 /v1/responses）。"""
    cfg = _cfg()
    rid = getattr(request.state, "rid", "-")
    caller = getattr(request.state, "caller", "anonymous")
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "请求体为空")
    try:
        body = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(400, "请求体不是合法 JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")

    model_in, pkey, pconfig, _minfo, conv, clean = _resolve_passthrough(cfg, body, protocol)
    stream = bool(clean.get("stream"))

    try:
        adapter, pkey, pconfig = _get_adapter(model_in, cfg.server.timeout, cfg)
    except (ModelNotFoundError, AuthConfigError) as e:
        raise HTTPException(502, str(e))
    cb = _circuits.get(pkey)
    if not cb.allow():
        raise HTTPException(503, f"供应商 {pconfig.name} 熔断中，请稍后重试")
    try:
        _enforce_rate_limit(cfg, request, pkey, model_in)
    except HTTPException:
        cb.release_probe()   # 请求没发出去 → 归还半开试探令牌
        raise

    # 定点改写：model 换成真实 id（别名场景）+ 盖模型级强制参数；其余字段一个不动
    payload = adapter.prepare_payload(model_in, clean)
    session = _upstream_session(conv, caller, getattr(request.state, "key_token", None))

    if stream:
        return await _passthrough_stream(request, rid, caller, protocol, adapter, cb,
                                         pkey, pconfig, model_in, payload, session, cfg)

    t0 = time.time()
    try:
        status, data = await adapter.raw_json(protocol, payload, session)
    except Exception as e:
        cb.record(False)
        _metrics.record_call(model_in, pkey, ok=False)
        _metrics.record_latency(model_in, time.time() - t0)
        log.error("[rid=%s] %s 直传失败 [%s]: %s", rid, protocol, model_in, e)
        raise HTTPException(502, f"上游调用失败: {e}")

    ok = 200 <= status < 300
    # 熔断口径（与真实链路一致）：4xx 是**客户端 body 的问题**（直传下尤其如此，
    #   body 就是客户端自己写的），不该把供应商判成坏了；只有 5xx 才算上游故障。
    if ok or status >= 500:
        cb.record(ok)
    else:
        cb.release_probe()
    _metrics.record_call(model_in, pkey, ok=ok)
    _metrics.record_latency(model_in, time.time() - t0)
    usage = _usage_counts(protocol, data)
    if ok:
        _metrics.record_tokens(usage["prompt_tokens"], usage["completion_tokens"])
    _access.add({"method": "POST", "kind": "chat", "path": f"/v1/{protocol}", "rid": rid,
                 "model": model_in, "served": model_in, "provider": pkey,
                 "caller": caller, "ok": ok, "stream": False,
                 "response": _logsafe(_resp_text(data) if ok else _upstream_detail(
                     json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)), 2000)})

    if not ok:
        # 直传口径：上游的错误体**原样**回给客户端（错误文案也不翻译），只加一个
        #   网关请求号，方便拿它去日志里对账。
        content = data if isinstance(data, dict) else {"error": {"message": str(data)[:1000]}}
        return JSONResponse(status_code=status, content=content,
                            headers={"x-request-id": rid})
    if not isinstance(data, dict):
        return Response(content=str(data), media_type="application/json",
                        headers={"x-request-id": rid})
    resp = dict(data)
    if usage["prompt_tokens"] or usage["completion_tokens"]:
        _attach_cost(resp, model_in, usage, cfg)
    resp["served_model"] = model_in
    resp["provider"] = pkey
    resp["provider_name"] = pconfig.name
    resp["protocol"] = protocol
    resp["x_request_id"] = rid
    return JSONResponse(status_code=status, content=resp, headers={"x-request-id": rid})


async def _passthrough_stream(request, rid, caller, protocol, adapter, cb,
                              pkey, pconfig, model_in, payload, session, cfg):
    """直传的流式分支：**原样转发上游 SSE 字节**（不解析事件、不重组帧）。

    为什么要先 `__aenter__` 再建 StreamingResponse：客户端需要拿到**真实状态码**。
    上游拒绝一个 stream 请求（400/401）时，若把它塞进 200 的 SSE 流里，SDK 会以为
    请求成功了、只是没内容 —— 这与直传"如实反映上游"的初衷相反。
    """
    cm = adapter.raw_stream(protocol, payload, session)
    resp = await cm.__aenter__()
    headers = {"x-request-id": rid}
    if resp.status_code >= 400:
        raw = await resp.aread()
        ctype = resp.headers.get("content-type", "application/json")
        await cm.__aexit__(None, None, None)
        if resp.status_code >= 500:
            cb.record(False)
        else:
            cb.release_probe()
        _metrics.record_call(model_in, pkey, ok=False)
        _access.add({"method": "POST", "kind": "chat", "path": f"/v1/{protocol}", "rid": rid,
                     "model": model_in, "served": model_in, "provider": pkey, "caller": caller,
                     "ok": False, "stream": True,
                     "response": _logsafe(_upstream_detail(raw.decode("utf-8", "replace")), 2000)})
        return Response(content=raw, status_code=resp.status_code, media_type=ctype, headers=headers)

    scanner = _UsageScanner(protocol)
    t0 = time.time()
    media_type = resp.headers.get("content-type", "text/event-stream")

    async def event_stream():
        ok = True
        try:
            async for chunk in resp.aiter_bytes():
                scanner.feed(chunk)      # 只读扫描 usage；字节原样 YIELD
                yield chunk
        except Exception as e:
            ok = False
            log.error("[rid=%s] %s 直传流中断 [%s]: %s", rid, protocol, model_in, e)
            raise
        finally:
            await cm.__aexit__(None, None, None)
            cb.record(ok)
            _metrics.record_call(model_in, pkey, ok=ok)
            _metrics.record_latency(model_in, time.time() - t0)
            counts = scanner.counts()
            if ok and scanner.seen:
                _metrics.record_tokens(counts["prompt_tokens"], counts["completion_tokens"])
            _access.add({"method": "POST", "kind": "chat", "path": f"/v1/{protocol}", "rid": rid,
                         "model": model_in, "served": model_in, "provider": pkey,
                         "caller": caller, "ok": ok, "stream": True, "response": ""})

    return StreamingResponse(event_stream(), media_type=media_type, headers=headers)


@app.post("/v1/messages")
async def anthropic_messages(request: Request, _auth=Depends(require_auth)):
    """Anthropic Messages 线（Claude Code / anthropic-sdk 的默认端点）。

    入站 = 出站 = `messages`：body 原样转发到该供应商的 messages 线（鉴权头按该线
    配置，OpenCode Go 实测认 `x-api-key` + `anthropic-version`），响应（含 SSE）原样回。
    鉴权：`Authorization: Bearer <网关 Key>` 与 `x-api-key: <网关 Key>` 都认（见 require_auth）。
    """
    return await _passthrough(request, PROTOCOL_MESSAGES)


@app.post("/v1/responses")
async def openai_responses(request: Request, _auth=Depends(require_auth)):
    """OpenAI Responses 线（Codex / openai-sdk 的 responses 端点）。

    入站 = 出站 = `responses`：body 原样转发到该供应商的 responses 线（Bearer 鉴权），
    响应（含 SSE）原样回。模型不支持该线时 400（不做跨格式翻译）。
    """
    return await _passthrough(request, PROTOCOL_RESPONSES)


# ── 一键测速（第二十七轮: 主动探测全部文本模型延迟）───────────────────
# 复用主调用链的 _get_adapter / adapter.chat（同一适配器、同一供应商 Key），
# 对每个文本模型发一条 1-token 的 "hi" 探针并计时，返回「延迟升序」结果。
# 注意：只读展示，不把探针写进 _metrics，避免一条小请求反过来改生产路由排序。
def _probe_protocol(minfo) -> str:
    """探针走哪条线：优先 chat/completions，其次 messages，最后 responses。

    第二十七轮: 不是每个模型都能走 chat/completions（实测 grok-4.6 / gpt-5.6-luna
    只认 responses；minimax-m2.7 只认 messages）。一键测速要对**每个**模型都能出结果，
    所以按模型自己声明的线挑一条 —— 挑不到就说明配置有问题（返回空串，调用方给文案）。
    """
    for p in (PROTOCOL_CHAT, PROTOCOL_MESSAGES, PROTOCOL_RESPONSES):
        if minfo.supports(p):
            return p
    return ""


def _probe_payload(protocol: str, model_id: str) -> dict:
    """探针请求体：每条线一份**最小合法**结构（不翻译，只是各写各的）。

    - chat/completions: messages + max_tokens
    - messages:         max_tokens 必填 + messages
    - responses:        input + max_output_tokens
    max_tokens 固定用 16：部分上游对 max_tokens<=2 回 400（"must be greater than 2"），
    而 16 足够便宜（探针成本可忽略），也够推理模型吐出一段内容。
    """
    if protocol == PROTOCOL_MESSAGES:
        return {"model": model_id, "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}]}
    if protocol == PROTOCOL_RESPONSES:
        return {"model": model_id, "input": "hi", "max_output_tokens": 16}
    return {"model": model_id, "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16, "stream": False}


@app.post("/v1/models/speedtest")
async def models_speedtest(request: Request, _auth=Depends(require_auth)):
    cfg = _cfg()
    # 可选 body.model：只测指定模型（前端「逐个测速」用——测一个显示一个、再测下一个）；不传则一次性全测。
    try:
        _body = await request.json()
    except Exception:
        _body = {}
    only = _body.get("model") if isinstance(_body, dict) else None
    # 第三十一轮: 别名不单独测速。它没有"自己的"延迟（跑的就是目标模型的延迟），
    #   而目标模型那一行本来就能测 —— 返回一句能直接照做的说明，
    #   比落到下面的"模型不存在或非文本模型"（wjd 明明在 /v1/models 里）清楚得多。
    _aspec = cfg.alias_of(str(only)) if only else None
    if _aspec is not None:
        return {"object": "list", "total": 1, "results": [{
            "model": str(only), "display_name": str(only), "provider": "—",
            "protocol": ALIAS_PROTOCOLS[0], "ok": False, "latency_ms": None,
            "error": f"{only} 是虚拟模型 id（别名），不单独测速 —— 请测它指向的模型 {_aspec.target}"}]}
    # 探针超时（第二十五轮: 15s → 30s）：
    #   15s 对"慢的推理模型"太紧 —— 实测 mimo-v2.5 过网关 2.9s~8.4s、agnes-2.5-flash 11s，
    #   上游一排队就冲破 15s，被记成「调用失败」，和「模型真的坏了」完全分不清。
    #   30s ≈ 实测最慢值的 3 倍，仍远小于真实对话链路用的 server.timeout。
    #   注意：该 timeout 只作用于**本探针自己创建的 adapter**（适配器缓存 key 含 timeout），
    #   改它不影响用户对话的超时行为。
    probe_timeout = 30.0
    results: list[dict] = []
    for m in cfg.all_models():
        if m.get("type") != "text":
            continue
        if only and m["id"] != only:
            continue
        row: dict = {"model": m["id"], "display_name": m.get("display_name"),
                     "provider": m.get("provider_name") or m.get("provider") or "—",
                     # 第二十七轮: 探针走哪条线也回给前端（模型列表里能对照格式标签）
                     "protocol": ""}
        # 第二十七轮: 按模型自己声明的协议线探测 —— 三条线共用同一个探针实现，
        #   否则 responses-only / messages-only 的模型会被误报成"调用失败"。
        found = cfg.find_provider_for_model(m["id"])
        protocol = _probe_protocol(found[2]) if found else ""
        if not protocol:
            row.update({"ok": False, "latency_ms": None, "error": "该模型未声明任何协议线"})
            results.append(row)
            continue
        row["protocol"] = protocol
        try:
            adapter, pkey, pconfig = _get_adapter(m["id"], int(probe_timeout), cfg)
        except ModelNotFoundError as e:
            row.update({"ok": False, "latency_ms": None, "error": "模型不存在: " + str(e)})
            results.append(row)
            continue
        except AuthConfigError as e:
            row.update({"ok": False, "latency_ms": None, "error": str(e)})
            results.append(row)
            continue
        cb = _circuits.get(pkey)
        if cb is not None and not cb.allow():
            row.update({"ok": False, "latency_ms": None, "error": f"供应商 {pconfig.name} 熔断中"})
            results.append(row)
            continue
        t0 = time.time()
        ok, err = False, ""
        # 探针重试：上游偶发瞬时 429/5xx（免费档限流、抖动等）会让单次探针误报成「模型坏了」。
        #   对 429/5xx 重试（退避同真实链路），4xx/超时不重试。
        for attempt in range(1, 4):   # 最多 3 次（含首次）
            try:
                # 直传原语：入站 body 原样上去、上游响应原样回来。探针只关心"通不通"，
                #   所以不解析响应体，只看状态码（4xx/5xx 都算失败并带上游原文）。
                #
                # 第三十轮（注明有意，不是漏改）：这里**故意不传 session_id** —— 探针要量的是
                #   "这个模型现在真实响不响应、要多久"，若复用真实对话用的固定会话值，
                #   上游前缀缓存会把结果拉成假快（命中缓存 → 延迟低得不像真调用），
                #   测速就失去意义。所以适配器回退到每请求随机值（对 opencode 即每次一个新会话）。
                status, data = await adapter.raw_json(protocol, _probe_payload(protocol, m["id"]))
                if 200 <= status < 300:
                    ok = True
                    break
                err = _upstream_detail(json.dumps(data, ensure_ascii=False)
                                       if isinstance(data, dict) else str(data)) or f"上游返回 HTTP {status}"
                if attempt < 3 and (status == 429 or status >= 500):
                    await asyncio.sleep(0.3 * attempt)
                else:
                    break
            except Exception as e:
                err = _speedtest_error_text(e, probe_timeout)
                if attempt < 3 and _speedtest_retryable(e):
                    await asyncio.sleep(0.3 * attempt)
                else:
                    break
        row.update({"ok": ok, "latency_ms": round((time.time() - t0) * 1000),
                    "error": "" if ok else (err or "调用失败")})
        results.append(row)
    # 逐个测（传了 model）时不排序；一次性全测时可用按延迟升序、失败排最后
    if not only:
        results.sort(key=lambda x: (x["ok"] is not True, x.get("latency_ms") is None, x.get("latency_ms") or 0))
    elif not results:
        results.append({"model": only, "display_name": only, "provider": "—",
                        "ok": False, "latency_ms": None, "error": "模型不存在或非文本模型"})
    return {"object": "list", "results": results, "total": len(results)}


# ─────────────────────────────────────
# POST /v1/chat/completions — 主接口（含熔断 P0-1）
# ─────────────────────────────────────
@app.post("/v1/chat/completions")
async def chat_completions(request: Request, req: ChatRequest, _auth=Depends(require_auth)):
    cfg = _cfg()
    rid = getattr(request.state, "rid", "-")
    caller = getattr(request.state, "caller", "anonymous")
    # 本轮修复: 未声明的字段不再被静默丢弃 —— 记一条日志，便于排查"客户端参数没生效"。
    unknown = req.unknown_fields()
    if unknown:
        log.info("[rid=%s] 未声明字段将原样透传上游: %s", rid, unknown)
    # 第六轮: 用 to_upstream() 透传（保留 tool_calls / tool_call_id / name 等），
    #   此前手工组 {"role","content"} 会把 function calling 的多轮历史丢干净。
    messages = [m.to_upstream() for m in req.messages]
    tools = [t.model_dump(exclude_none=True) for t in req.tools] if req.tools else None

    # Bug 15 修复: 未知模型应作为客户端错误 400 直接拒绝，
    #   而不是等到调用失败后才以 502（上游错误）误导性地返回。
    # 第二十七轮（三线直传）: 这条入口就是 chat/completions 线 —— 模型必须声明该线，
    #   否则 400（不把请求发去上游吃 500）。路由别名 / fallback 链已整体移除。
    model_id = _require_protocol_model(cfg, req.model, PROTOCOL_CHAT)

    # 第十三轮: 带图请求打到没标视觉能力的模型上，上游要么忽略图片、要么报参数错 ——
    #   记一条日志便于排查。第二十七轮: 不再重排模型（只有一个模型，重排=替用户换模型）。
    if messages_have_images(messages) and not cfg.find_provider_for_model(model_id)[2].vision:
        log.info("[rid=%s] 带图请求，但模型 %s 未标记视觉能力", rid, model_id)

    if req.stream:
        return _stream_response(request, req, model_id, messages, tools)
    return await _chat_once(cfg, rid, caller, request, req, model_id, messages, tools)


def _upstream_session(conversation_id: str | None, account: str, key_token: str | None = None) -> str:
    """把调用的标识派生成**稳定**的上游 session id（第二十六轮 + 第二十九轮强化）。

    上游（OpenCode Go）的提示词缓存以 `x-opencode-session` 为作用域 —— 实测：同一 session
    的第二次请求 `cached_tokens` 全量命中，换成另一个 session 则恒为 0（换 session = 完全不
    吃缓存）。所以这个值**必须在一次最近的重复调用间恒定**。取值优先级：

    ① 客户端带了 `conversation_id`（第二十六轮）：first 「账号:会话」后哈希 —— 同一段对话
       恒定 → 该对话内反复出现的前缀命中；不同账号天然隔离。
    ② 没带（QwenPaw / 多数 agent 不发这个字段，第二十九轮老板决策）：用**这个 API Key
       自己**派生 —— 一个 Key 一个稳定会话，比"每调随机"命中率高得多。但**先哈希再传**：
       直接把 `wjd-` 后面那串原样发给上游等于泄露 Key 凭证内容给第三方（见 _key_token）。
    ③ 连 Key 都没有（登录页面会话，kind=session）：按账号稳定兜底，同样不再随机。

    这三级都**不再回退成每个调用一个随机 UUID** —— 随机=缓存命中率恒为 0。上游只看到
    32 位十六进制串，不暴露账号名、业务命名或 Key 明文。
    """
    if conversation_id:
        return hashlib.sha256(f"{account}:{conversation_id}".encode("utf-8")).hexdigest()[:32]
    if key_token:
        return hashlib.sha256(f"k:{key_token}".encode("utf-8")).hexdigest()[:32]
    return hashlib.sha256(f"u:{account}".encode("utf-8")).hexdigest()[:32]


async def _chat_once(cfg, rid, caller, request, req, model_id, messages, tools):
    """非流式：单个模型的调用 + 熔断 + 重试。

    第二十七轮: 路由别名 / fallback 链已整体移除 —— 这里只服务一个模型，失败不再"换一个
    再试"，而是把上游错误如实报给客户端（同模型重试仍受 server.retry 控制）。
    """
    tool_choice = req.tool_choice
    timeout = cfg.server.timeout
    try:
        adapter, pkey, pconfig = _get_adapter(model_id, timeout, cfg)
    except ModelNotFoundError as e:
        raise HTTPException(400, f"未知模型: {model_id}") from e
    except AuthConfigError as e:
        # 供应商没配 Key 是网关侧配置问题：503（别报 502 让用户去查上游）
        raise HTTPException(503, f"供应商 {e.provider_key} 未配置可用的 API Key（{e.detail}）") from e

    # P0-1: 熔断检查
    cb = _circuits.get(pkey)
    if not cb.allow():
        log.warning("[rid=%s] 供应商 %s 熔断中，拒绝 [%s]", rid, pkey, model_id)
        raise HTTPException(503, f"供应商 {pconfig.name} 熔断中，请稍后重试")

    # 供应商级/全局级限流（Bug 1；路由级限流随路由功能一并移除）
    try:
        _enforce_rate_limit(cfg, request, pkey, model_id)
    except HTTPException:
        # 第六轮: 刚才是 cb.allow() 放行的（可能消耗了 HALF_OPEN 试探令牌），
        #   但请求根本没发出去，必须把令牌还回去，否则该供应商会被半开状态永久拒绝。
        cb.release_probe()
        raise

    ok = False
    result = None
    last_err = None
    t0 = time.time()
    retry = max(1, cfg.server.retry)
    for attempt in range(1, retry + 1):
        try:
            result = await adapter.chat(
                model=model_id, messages=messages,
                temperature=req.temperature, top_p=req.top_p,
                max_tokens=req.max_tokens, stop=req.stop,
                tools=tools, tool_choice=tool_choice,
                extra=req.upstream_extra() or None,
                session_id=_upstream_session(req.conversation_id, caller, getattr(request.state, "key_token", None)),
            )
            ok = True
            break
        except Exception as e:
            last_err = e
            log.error("[rid=%s] 上游失败 [%s] attempt=%d/%d: %s", rid, model_id, attempt, retry, e)
            if attempt < retry:
                await asyncio.sleep(0.3 * attempt)

    cb.record(ok)
    if not ok:
        _metrics.record_call(model_id, pkey, ok=False)
        _metrics.record_latency(model_id, time.time() - t0)  # Bug 5: 失败也算耗时
        # 第六轮: 键名从 `error` 改为 `message` —— OpenAI 规范要求 error.message，
        #   此前客户端拿到的是 {"error":{"error": ...}}，SDK 解析不到 message。
        raise HTTPException(502, {"message": f"模型 {model_id} 调用失败",
                                  "errors": [{"model": model_id, "error": str(last_err)}]})

    usage = result.get("usage") or {}
    _metrics.record_call(model_id, pkey, ok=True)
    _metrics.record_tokens(int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))
    _metrics.record_latency(model_id, time.time() - t0)
    _access.add({"method": "POST", "kind": "chat", "path": "/v1/chat/completions", "rid": rid,
                 "model": req.model, "served": model_id, "provider": pkey,
                 "caller": caller, "ok": True, "stream": False,
                 "response": _logsafe(_resp_text(result), 2000)})
    resp = {
        "id": result.get("id", "") or f"chatcmpl-{rid}", "object": "chat.completion",
        "created": int(time.time()), "model": req.model,
        "system_fingerprint": f"gw-api-{pkey}",
        "served_model": model_id, "provider": pkey, "provider_name": pconfig.name,
        "choices": result.get("choices", []), "usage": usage, "x_request_id": rid,
    }
    _attach_cost(resp, model_id, usage, cfg)
    return resp


# ── 流式 fallback（元信息 + 熔断 + 统一 [DONE]） ──
def _sse_error(rid, caller, req, model_id, detail, provider=None) -> str:
    """流式路径的统一错误帧（error 帧 + [DONE] 帧），并按失败记一条访问日志。

    第二十七轮: 不再有"这个模型失败就换下一个"的余地（路由/fallback 链已移除），
    所以错误帧在这里一次成型，调用方 yield 一下就收尾。
    """
    _access.add({"method": "POST", "kind": "chat", "path": "/v1/chat/completions", "rid": rid,
                 "model": req.model, "served": model_id, "provider": provider,
                 "caller": caller, "ok": False, "stream": True, "error": str(detail)[:200],
                 "response": ""})
    err = {"error": detail, "errors": [{"model": model_id, "error": detail}]}
    return (f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
            f"data: [DONE]\n\n")


def _stream_response(request, req, model_id, messages, tools):
    cfg = _cfg()
    rid = getattr(request.state, "rid", "-")
    caller = getattr(request.state, "caller", "anonymous")

    async def event_stream():
        # 本轮修复: 日志累加器必须在 try 外初始化。此前它写在链循环体 try 内，
        #   如果模型在 _get_adapter / 熔断 / 限流阶段就被拒，那些赋值永远不会执行 →
        #   失败分支引用它会 NameError，SSE 变成 500 而不是给客户端错误帧。
        resp_parts: list[str] = []     # 流式输出正文累积（供访问日志）
        reason_parts: list[str] = []   # 流式推理段累积（正文为空时回落，供访问日志）
        timeout = cfg.server.timeout
        try:
            try:
                adapter, pkey, pconfig = _get_adapter(model_id, timeout, cfg)
            except ModelNotFoundError as e:
                yield _sse_error(rid, caller, req, model_id, str(e))
                return
            except AuthConfigError as e:
                yield _sse_error(rid, caller, req, model_id,
                                 f"供应商 {e.provider_key} 未配置可用的 API Key（{e.detail}）")
                return

            cb = _circuits.get(pkey)
            if not cb.allow():
                yield _sse_error(rid, caller, req, model_id, f"供应商 {pconfig.name} 熔断中，请稍后重试", pkey)
                return

            # 供应商级/全局级限流（Bug 1；路由级限流随路由功能一并移除）
            try:
                _enforce_rate_limit(cfg, request, pkey, model_id)
            except HTTPException as e:
                cb.release_probe()   # 第六轮: 归还试探令牌（请求没发出去）
                yield _sse_error(rid, caller, req, model_id, f"限流: {e.detail}", pkey)
                return

            t0 = time.time()
            stream_gen = None       # 第六轮: 显式持有生成器，收尾时 aclose()
            try:
                emitted_meta = False
                stream_usage: dict = {}
                # 第六轮: 用 stream_gen 承接后再 async for，配合下面的 finally aclose()。
                #   上游 chat_stream 内部用 `async with self._lease()` 持有 client 占用，
                #   若只 break 不 aclose，生成器要等 GC 才释放 —— 热重载的延迟关闭会
                #   一直等不到 _active==0，连接池迟迟不关。
                stream_gen = adapter.chat_stream(
                    model=model_id, messages=messages,
                    temperature=req.temperature, top_p=req.top_p,
                    max_tokens=req.max_tokens, stop=req.stop,
                    tools=tools, tool_choice=req.tool_choice,
                    extra=req.upstream_extra() or None,
                    session_id=_upstream_session(req.conversation_id, caller, getattr(request.state, "key_token", None)),
                )
                async for chunk in stream_gen:
                    if chunk.get("done"):
                        break
                    # 功能 8: 累计上游 chunk 的 usage（若有）
                    if chunk.get("usage"):
                        u = chunk["usage"]
                        stream_usage["prompt_tokens"] = stream_usage.get("prompt_tokens", 0) + int(u.get("prompt_tokens", 0))
                        stream_usage["completion_tokens"] = stream_usage.get("completion_tokens", 0) + int(u.get("completion_tokens", 0))
                        stream_usage["total_tokens"] = stream_usage.get("total_tokens", 0) + int(u.get("total_tokens", 0))
                    # Bug 5 修复: meta 帧在首个 content 到达时才发送 —— 第六轮补齐为合法 chunk
                    #   （缺 id/created/choices 的"元信息帧"会被严格校验的 SDK 直接拒掉）。
                    if chunk.get("choices") and not emitted_meta:
                        meta = {"id": f"chatcmpl-{rid}", "object": "chat.completion.chunk",
                                "created": int(time.time()), "model": req.model,
                                "choices": [],
                                "served_model": model_id, "provider": pkey,
                                "provider_name": pconfig.name, "x_request_id": rid}
                        yield f"data: {json.dumps(meta, ensure_ascii=False)}\n\n"
                        emitted_meta = True
                    # M4: 累积流式输出正文到日志（推理段单独攒，正文为空时才回落）
                    for _c in chunk.get("choices", []):
                        _d = _c.get("delta") or {}
                        if _d.get("content"):
                            resp_parts.append(_d["content"])
                        if _d.get("reasoning_content"):
                            reason_parts.append(_d["reasoning_content"])
                    # 第三十一轮（别名调用）: 上游 chunk 里带的是**真实模型 id**，而元信息帧
                    #   报的是客户端请求的名字（req.model）。同一条流里出现两个名字，客户端会
                    #   以为网关中途换过模型 —— 所以别名调用时把 chunk 的 model 也统一成请求名，
                    #   真实目标由 served_model 表达（与非流式响应壳同一口径）。
                    #   非别名调用时 req.model == model_id，这个分支恒不成立，一个字节都不动。
                    if req.model != model_id:
                        chunk["model"] = req.model
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                cb.record(True)
                _metrics.record_call(model_id, pkey, ok=True)
                _metrics.record_latency(model_id, time.time() - t0)
                _access.add({"method": "POST", "kind": "chat", "path": "/v1/chat/completions", "rid": rid,
                             "model": req.model, "served": model_id, "provider": pkey,
                             "caller": caller, "ok": True, "stream": True,
                             "response": _logsafe(_stream_log_text(resp_parts, reason_parts), 2000)})
                # 功能 8: 流式末尾发一个 usage 块（累计自上游 chunks 的 usage，若有）
                if stream_usage:
                    uf = {'id': f'chatcmpl-{rid}', 'object': 'chat.completion.chunk', 'created': int(time.time()),
                          'model': req.model, 'choices': [], 'usage': stream_usage,
                          'served_model': model_id, 'provider': pkey}
                    # M2: 流式 usage 帧同样带上 token 成本估算，前端能显示费用
                    _attach_cost(uf, model_id, stream_usage, cfg)
                    yield f"data: {json.dumps(uf, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return
            except Exception as e:
                cb.record(False)
                _metrics.record_call(model_id, pkey, ok=False)
                _metrics.record_latency(model_id, time.time() - t0)  # Bug 5: 失败也算耗时
                _access.add({"method": "POST", "kind": "chat", "path": "/v1/chat/completions", "rid": rid,
                             "model": req.model, "served": model_id, "provider": pkey,
                             "caller": caller, "ok": False, "stream": True, "error": str(e)[:200],
                             "response": _logsafe(_stream_log_text(resp_parts, reason_parts), 2000)})
                # 第六轮: 已向客户端输出过 content 时本来就无法回退（第二十七轮起更没有下一个
                #   模型可退），统一发错误帧 + [DONE] 收尾，避免破坏已输出的 SSE 流。
                yield f"data: {json.dumps({'error': str(e), 'served_model': model_id}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return
            finally:
                # 第六轮: 无论正常收尾 / break / 异常 / 取消，都立刻关掉上游生成器，
                #   让 lease 马上归还（不依赖 GC）。已在结束时 aclose 是幂等的。
                if stream_gen is not None:
                    try:
                        await stream_gen.aclose()
                    except Exception:
                        pass
        finally:
            # 第六轮: in_flight 已改为在 middleware 的 finally 中统一递减（见 in_flight_tracker），
            #   此处不再递减 —— 此前流式请求会被双重递减（详见那边的说明）。
            pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-ID": rid},
    )


# ─────────────────────────────────────
# POST /v1/chat/completions/batch — 并发多 prompt（P1-14）
# ─────────────────────────────────────
@app.post("/v1/chat/completions/batch")
async def chat_batch(request: Request, req: BatchRequest, _auth=Depends(require_auth)):
    cfg = _cfg()
    rid = getattr(request.state, "rid", "-")
    # Bug 15: 批量入口同样对未知模型直接 400（第二十七轮: 这条入口也是 chat/completions 线）
    model_id = _require_protocol_model(cfg, req.model, PROTOCOL_CHAT)
    # 本轮修复: 批量条数上限（此前只受请求体 2MB 限制，可一次塞进上千条 → 自伤型 DoS）
    if not req.items:
        raise HTTPException(400, "items 不能为空")
    if len(req.items) > cfg.server.batch_max_items:
        raise HTTPException(400, f"items 数量 {len(req.items)} 超过上限 {cfg.server.batch_max_items}")
    # 本轮修复: 并发上限钳制（此前完全由客户端 max_concurrency 决定，可传 1e5 打爆上游/自身）
    limit = min(max(1, req.max_concurrency), cfg.server.batch_max_concurrency)
    sem = asyncio.Semaphore(limit)
    # 本轮修复: BatchRequest.extra 此前被完全丢弃 → 无法透传供应商参数（如 chat_template_kwargs）
    passthrough = dict(req.extra or {})

    async def run_one(i: int, item) -> dict:
        async with sem:
            messages = [m.to_upstream() for m in item.messages]
            try:
                sub = ChatRequest(
                    model=req.model, messages=item.messages,
                    temperature=item.temperature, top_p=item.top_p,
                    max_tokens=item.max_tokens, stream=False,
                    extra=passthrough,
                )
                r = await _chat_once(cfg, rid, "batch", request, sub, model_id, messages, None)
                return {"index": i, "label": item.label, "ok": True, "result": r}
            except HTTPException as e:
                # 第六轮: detail 是结构化 dict（如全部模型失败）时，str() 会产出 Python repr，
                #   客户端拿到 "{'message': ..., 'errors': [...]}" 这种不可解析的字符串。
                detail = e.detail
                err = detail if isinstance(detail, (dict, list)) else str(detail)
                return {"index": i, "label": item.label, "ok": False, "status": e.status_code, "error": err}
            except Exception as e:
                return {"index": i, "label": item.label, "ok": False, "error": f"internal: {str(e)}"}

    results = await asyncio.gather(*[run_one(i, it) for i, it in enumerate(req.items)])
    _access.add({"method": "POST", "kind": "chat", "path": "/v1/chat/completions/batch", "rid": rid,
                 "model": req.model, "caller": getattr(request.state, "caller", "?"),
                 "ok": True, "batch": len(results)})
    return {
        "object": "batch.result", "model": req.model, "x_request_id": rid,
        "count": len(results), "max_concurrency": limit,
        "results": [r if isinstance(r, dict) else {"error": str(r)} for r in results],
    }


# ─────────────────────────────────────
# POST /v1/images/generations — 图像
# ─────────────────────────────────────
@app.post("/v1/images/generations")
async def images_generations(request: Request, req: ImageRequest, _auth=Depends(require_auth)):
    cfg = _cfg()
    rid = getattr(request.state, "rid", "-")
    caller = getattr(request.state, "caller", "anonymous")
    resolved = _resolve_media_model(cfg, req.model, "image")
    if resolved is None:
        raise HTTPException(404, f"未知图像模型: {req.model}（没有可用的 image 类型模型）")
    pkey, pconfig, minfo, token, real_model = resolved
    # 顺序修复（本轮）：先做能力检查（501）、再限流 —— 否则"不支持图像的供应商"也会白扣一个限流 token。
    if not pconfig.media.supports_image:
        raise HTTPException(501, f"供应商 {pconfig.name} 不支持图像生成")

    # 第十三轮: Agnes 图像接口一次只出 1 张（实测 n=2 → 上游 400「n 必须为 1」）。
    #   此前该请求会白打上游、被包装成 502，还会记一次媒体熔断失败 —— 现在入站就挡下，
    #   给一句能照着做的 400（要多张就连续调用；管理台已改成逐张调用）。
    # 第十八轮: 挪到**限流/熔断检查之前** —— 纯参数错误不该白扣限流 token，更不该消费掉
    #   熔断器的 HALF_OPEN 试探令牌（此前 raise 时没 release_probe，令牌泄漏 → 供应商
    #   卡在半开态直到探测超时，与 circuit.py 里"全部调用点都满足归还约束"的注释矛盾）。
    if int(req.n or 1) != 1:
        raise HTTPException(
            400,
            f"图像接口一次只生成 1 张（n 必须为 1，收到 {req.n}）—— 需要多张请连续调用；管理台已自动逐张生成",
        )

    # Bug 1 修复: 图像是高成本资源，按供应商桶限流（配置 providers.<key>.rate_limit_rps 时生效）
    _enforce_media_rate_limit(cfg, pkey)
    # 本轮：媒体独立熔断桶（此前媒体调用完全不喂熔断器）
    cb = _media_circuit(pkey)
    _media_circuit_check(cb, pkey, pconfig)


    extra = dict(req.extra or {})
    # 第六轮: 未显式声明的图像参数（quality/style/response_format/…）不再被静默丢弃
    extra.update(req.model_extra or {})
    # 第七轮: 用图像专用窄集 —— 此前用宽集会连带删掉 response_format / user
    #   （与 ImageRequest 的 docstring 自相矛盾）
    extra = strip_protected(extra, PROTECTED_IMAGE_KEYS)
    if req.return_base64:
        extra["return_base64"] = True
    t0 = time.time()
    try:
        # 第六轮: _get_adapter 移进 try —— 供应商没配 token 时它抛 AuthConfigError，
        #   此前未捕获 → 500（纯文本），与 chat 路径的 502/503 不一致。
        adapter, _, _ = _get_adapter(real_model, cfg.server.timeout, cfg)
        result = await adapter.generate_image(
            model=real_model, prompt=req.prompt, size=req.size, ratio=req.ratio,
            image=req.image, n=req.n, extra=extra or None,
        )
        cb.record(True)
        _metrics.record_call(real_model, pkey, ok=True)
        _metrics.record_latency(real_model, time.time() - t0)
        # 第十轮: 日志补「响应数据」—— 图像响应可能带整段 base64，这里只留可读摘要
        _imgs = result.get("data") or []
        _first = (_imgs[0] or {}) if _imgs else {}
        _b64 = _first.get("b64_json")
        _resp_sum = {
            "served_model": real_model, "provider": pkey, "n": len(_imgs),
            "elapsed_s": round(time.time() - t0, 1),
            "first": _first.get("url") or (f"b64_json:{_b64[:24]}…" if _b64 else ""),
        }
        _access.add({"method": "POST", "kind": "image", "path": "/v1/images/generations", "rid": rid,
                     "model": req.model, "served": real_model, "provider": pkey, "caller": caller, "ok": True,
                     "response": json.dumps(_resp_sum, ensure_ascii=False)[:1500]})
        return {"created": int(t0),      # 第六轮: 补 OpenAI 标准的 created 字段
                "data": result.get("data", []), "model": req.model, "served_model": real_model,
                "provider": pkey, "provider_name": pconfig.name, "x_request_id": rid}
    except AuthConfigError as e:
        # 网关自身配置问题（不是上游故障）→ 不计入熔断，返回 503 并点明原因
        cb.release_probe()
        _access.add({"method": "POST", "kind": "image", "path": "/v1/images/generations", "rid": rid,
                     "model": req.model, "served": real_model, "provider": pkey, "caller": caller,
                     "ok": False, "error": str(e)[:200]})
        raise HTTPException(503, f"供应商 {pconfig.name} 未配置可用凭证: {e}")
    except ModelNotFoundError as e:
        cb.release_probe()
        raise HTTPException(404, f"图像模型不可用: {e}")
    except httpx.HTTPStatusError as e:
        # 第十三轮: 与视频创建对齐 —— 把"上游 4xx（客户端参数不被接受：不支持的尺寸/比例/字段）"
        #   和"上游 5xx（真故障）"分开。此前图像路径所有上游错误一律 502 且都记进媒体熔断，
        #   于是「选了个上游不认的尺寸」戳三次就能把媒体服务整体熔断 30 秒（视频页/图像页一起挂）。
        cb.release_probe()
        st = e.response.status_code
        detail = _upstream_detail(e.response.text) or f"HTTP {st}"
        _access.add({"method": "POST", "kind": "image", "path": "/v1/images/generations", "rid": rid,
                     "model": req.model, "served": real_model, "provider": pkey, "caller": caller,
                     "ok": False, "error": f"upstream {st} {detail}"})
        _metrics.record_call(real_model, pkey, ok=False)
        if st < 500:
            raise HTTPException(400, f"上游拒绝请求 (HTTP {st})：{detail}")
        cb.record(False)
        raise HTTPException(502, f"图像生成失败：上游 {st}: {detail}")
    except Exception as e:
        cb.record(False)
        _metrics.record_call(real_model, pkey, ok=False)
        _access.add({"method": "POST", "kind": "image", "path": "/v1/images/generations", "rid": rid,
                     "model": req.model, "served": real_model, "provider": pkey, "caller": caller, "ok": False, "error": str(e)[:200]})
        raise HTTPException(502, f"图像生成失败: {e}")


# ─────────────────────────────────────
# POST /v1/videos — 视频创建（等待模式 + Webhook，P1-6/7）
# ─────────────────────────────────────
_MAX_WAIT_TIMEOUT = 600  # Bug 9 修复: 媒体等待模式的上限（秒），防止客户端传超大值长期占住请求


async def _video_wait_and_callback(cfg, pkey, pconfig, pmodel_id, video_id, wait, wait_timeout, callback_url, rid):
    """wait=True 时内部轮询到完成/失败/超时；完成后若有 callback_url 则 POST 通知。"""
    if not wait:
        return None
    # Bug 9 修复: 钳制等待上限，避免客户端传超大 wait_timeout 长期占用连接与轮询上游
    wait_timeout = max(1, min(int(wait_timeout or cfg.server.timeout), _MAX_WAIT_TIMEOUT))
    # 第六轮: 这里可能在**后台任务**里执行，取不到 adapter（凭证缺失）不能让任务静默炸掉，
    #   记日志后返回 None（调用方按"未拿到最终状态"处理）。
    try:
        adapter, _, _ = _get_adapter(pmodel_id, cfg.server.timeout, cfg)
    except (AuthConfigError, ModelNotFoundError) as e:
        log.warning("[rid=%s] 无法获取适配器，跳过视频等待/回调 %s: %s", rid, _logsafe(video_id), e)
        return None
    # Bug 10 修复: 等待期间的轮询也尊重熔断，避免对坏供应商长时间空转
    # 本轮修正: 用**媒体独立桶**（此前查的是 chat 桶，而媒体从不喂 chat 桶 → 检查形同虚设）
    cb = _media_circuit(pkey)
    extra = {}
    if pconfig.media.video_model_name:
        extra["model_name"] = pconfig.media.video_model_name
    deadline = time.time() + wait_timeout
    final = None
    while time.time() < deadline:
        if not cb.allow():
            log.warning("[rid=%s] 供应商 %s 熔断中，停止等待视频 %s", rid, pkey, _logsafe(video_id))
            break
        try:
            final = await adapter.poll_video(video_id, model=pmodel_id, extra=extra or None)
            cb.record(True)
        except httpx.HTTPStatusError as e:
            # 第十三轮: 上游轮询限流（实测每 3 秒一次会被 429「查询过于频繁」）不是供应商故障，
            #   不能记熔断（连记三次会把整个媒体服务打开），也不该中断等待 —— 拉长间隔继续等。
            if e.response.status_code == 429:
                await asyncio.sleep(4)
                continue
            if e.response.status_code == 404:
                cb.release_probe()
                break          # 任务确实不存在，继续等到超时没有意义
            cb.record(False)
            await asyncio.sleep(2)
            continue
        except Exception:
            cb.record(False)
            await asyncio.sleep(2)
            continue
        raw = final.get("raw") or {}
        status = _video_status(raw)
        if _video_terminal(status):
            break
        await asyncio.sleep(2)
    # Webhook 回调
    if callback_url:
        # 第六轮: 回调前**再校验一次**。创建请求与真正回调之间可能隔了几分钟（视频生成慢），
        #   期间域名可能被重新解析到内网（DNS rebinding / TOCTOU）。复检把窗口压到最小。
        try:
            await _validate_callback_url(callback_url, cfg.server.webhook_allow_private)
        except HTTPException as e:
            log.warning("[rid=%s] webhook 地址复检未通过，放弃回调 %s: %s", rid, callback_url, e.detail)
            return final
        try:
            # Bug 2 修复: 按真实状态上报，failed 不再误标成 completed
            raw = (final.get("raw") if final else None) or {}
            status = _video_status(raw)
            if not _video_terminal(status):
                status = "timeout"
            payload = {"video_id": video_id, "provider": pkey, "status": status,
                      "result": raw, "x_request_id": rid}
            # 显式 follow_redirects=False：绝不让对方用 302 把我们导向内网地址（SSRF 绕过）
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as wc:
                await wc.post(callback_url, json=payload)
        except Exception as e:
            log.warning("[rid=%s] webhook 回调失败 %s: %s", rid, callback_url, e)
    return final


_VIDEO_MODES = ("text", "keyframe", "reference")
# Agnes Video 2.5 Flash: size 固定 720P；画幅白名单见官方文档「视频尺寸与画幅」
_VIDEO_SIZES = ("720P",)
_VIDEO_ASPECTS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")


def _video_status(raw: dict | None) -> str:
    """视频原始返回的状态取法（统一小写）。

    Agnes 轮询返回用 status / state；保留 task_status 兼容（未来其它视频供应商可能用到）。
    """
    if not raw:
        return ""
    return str(raw.get("status") or raw.get("state") or raw.get("task_status") or "").lower()


def _video_terminal(status: str) -> bool:
    """是否已到终态（成功或失败），用于结束轮询。"""
    s = status.lower()
    return s in ("success", "succeeded", "completed", "done", "finished",
                 "failed", "fail", "error", "cancelled", "canceled")


def _validate_video_request(req) -> None:
    """第十一轮 #3：按 Agnes Video 2.5 契约做**入站校验**，把明显不合法的 mode/媒体/时长
       组合在网关挡下并回**干净 400**（不再白发上游浪费限流额度，也不进熔断）。
       第十三轮补齐 size / aspect_ratio / n / videos 四项（文档「Flash 专属错误」与
       「生成模式规则」里明确列出的限制），并保留原有 mode/媒体/时长校验。
       目前只有 Agnes 配了视频，字段语义按它的文档来。"""
    mode = req.mode or "text"
    if mode not in _VIDEO_MODES:
        raise HTTPException(400, f"视频 mode 仅支持 {'/'.join(_VIDEO_MODES)}，收到 {mode!r}")
    try:
        sec = int(str(req.seconds))
    except (TypeError, ValueError):
        raise HTTPException(400, f"seconds 需为 4~12 的整数（字符串），收到 {req.seconds!r}")
    if not 4 <= sec <= 12:
        raise HTTPException(400, f"seconds 仅支持 4~12 秒，收到 {sec}")
    if (req.size or "720P") not in _VIDEO_SIZES:
        raise HTTPException(400, f"size 仅支持 {'/'.join(_VIDEO_SIZES)}，收到 {req.size!r}")
    if (req.aspect_ratio or "16:9") not in _VIDEO_ASPECTS:
        raise HTTPException(400, f"aspect_ratio 仅支持 {'/'.join(_VIDEO_ASPECTS)}，收到 {req.aspect_ratio!r}")
    if int(req.n or 1) != 1:
        raise HTTPException(400, f"视频接口一次只提交 1 个任务（n 必须为 1，收到 {req.n}）")
    if req.videos:
        raise HTTPException(400, "该视频模型不支持 videos（参考视频）输入 —— 只能传图片/音频参考")
    if mode == "text":
        if req.first_frame or req.last_frame or req.images or req.audios:
            raise HTTPException(400, "text 模式不接受 first_frame/last_frame/images/audios —— 要图生视频请用 keyframe（首尾帧）或 reference（参考图/音频）模式")
    elif mode == "keyframe":
        if req.images or req.audios:
            raise HTTPException(400, "keyframe 模式不接受 images/audios —— 那是 reference 模式的字段")
        if not (req.first_frame or req.last_frame):
            raise HTTPException(400, "keyframe 模式需要 first_frame 或 last_frame（首/尾帧图 URL）至少一个")
    elif mode == "reference":
        if req.first_frame or req.last_frame:
            raise HTTPException(400, "reference 模式不接受 first_frame/last_frame —— 那是 keyframe 模式的字段")
        if req.images and len(req.images) > 5:
            raise HTTPException(400, "reference 模式 images 最多 5 张")
        if req.audios and len(req.audios) > 3:
            raise HTTPException(400, "reference 模式 audios 最多 3 段")
        if not (req.images or req.audios):
            raise HTTPException(400, "reference 模式需要 images 或 audios 至少一类非空")


@app.post("/v1/videos")
async def videos_create(request: Request, req: VideoCreateRequestExtended, _auth=Depends(require_auth)):
    cfg = _cfg()
    rid = getattr(request.state, "rid", "-")
    caller = getattr(request.state, "caller", "anonymous")
    _validate_video_request(req)          # 第十一轮 #3：入站按 Agnes 契约校验 mode/媒体/时长
    resolved = _resolve_media_model(cfg, req.model, "video")
    if resolved is None:
        raise HTTPException(404, f"未知视频模型: {req.model}（没有可用的 video 类型模型）")
    pkey, pconfig, minfo, token, real_model = resolved
    # 顺序修复（本轮）：先能力检查（501），再校验回调地址（400），最后限流 —— 避免无效请求白扣 token。
    if not pconfig.media.supports_video:
        raise HTTPException(501, f"供应商 {pconfig.name} 不支持视频创建")

    # 第十八轮备注: 视频的 n≠1 已由最前面的 `_validate_video_request()` 挡下（那比本函数里
    #   任何限流/熔断检查都早），所以这条路径本来就没有"参数错误吃掉试探令牌"的问题 ——
    #   此前这里其实没有重复检查，只在 images 里漏了，不要再往这里补第二份。

    # 本轮新增：webhook 回调地址 SSRF 校验（默认禁止内网/回环，可用 server.webhook_allow_private 放行）
    await _validate_callback_url(req.callback_url, cfg.server.webhook_allow_private)
    # Bug 1 修复: 视频是高成本资源，按供应商桶限流（配置 providers.<key>.rate_limit_rps 时生效）
    _enforce_media_rate_limit(cfg, pkey)
    cb = _media_circuit(pkey)
    _media_circuit_check(cb, pkey, pconfig)

    extra: dict = {}
    if req.seed is not None:
        extra["seed"] = req.seed
    if req.first_frame:
        extra["first_frame"] = req.first_frame
    if req.last_frame:
        extra["last_frame"] = req.last_frame
    if req.images:
        extra["images"] = req.images
    if req.audios:
        extra["audios"] = req.audios
    if req.extra:
        extra.update(req.extra)
    # 第六轮: 核心键（model/prompt/seconds/mode/size/aspect_ratio）不接受客户端 extra 覆盖；
    #   first_frame/last_frame/images/audios/seed 等模式专用字段照旧透传。
    extra = strip_protected(extra, PROTECTED_VIDEO_KEYS)

    t0 = time.time()
    try:
        adapter, _, _ = _get_adapter(real_model, cfg.server.timeout, cfg)
        result = await adapter.create_video(
            model=real_model, prompt=req.prompt, seconds=req.seconds,
            mode=req.mode, aspect_ratio=req.aspect_ratio, size=req.size, extra=extra or None,
        )
        video_id = result.get("video_id")
        # 第三十轮: 登记视频归属 —— 轮询端点据此判权（见 app/video_owners.py 顶部的口径说明）。
        #   放在这里是因为这是"已知归属"的唯一时刻：上游刚把 video_id 交给我们，且 caller 明确。
        if video_id:
            await _offload(_owners.record, video_id, request.state.account, pkey)
        cb.record(True)
        _metrics.record_call(real_model, pkey, ok=True)
        _metrics.record_latency(real_model, time.time() - t0)
        resp: dict = {"video_id": video_id, "model": req.model, "served_model": real_model,
                "provider": pkey, "provider_name": pconfig.name,
                "raw": result.get("raw"), "x_request_id": rid, "async": not req.wait}
        if req.wait:
            final = await _video_wait_and_callback(
                cfg, pkey, pconfig, pmodel_id=real_model, video_id=video_id,
                wait=True, wait_timeout=req.wait_timeout, callback_url=req.callback_url, rid=rid,
            )
            resp["final"] = final.get("raw") if final else None
            # 本轮修复: status 同时看 status/state（此前只看 status，而上游若用 state
            #   会被显示成 "pending" —— 即使其实已完成，与等待循环的判定不一致）
            raw_final = (final.get("raw") or {}) if final else {}
            resp["status"] = (_video_status(raw_final) or "pending") if final else "submitted"
        elif req.callback_url:
            # 第六轮修复: 此前只传 callback_url（不传 wait）时，_video_wait_and_callback 第一行
            #   就 `if not wait: return None` → **webhook 被静默丢弃、永不回调**，而 README 却把
            #   callback_url 描述成"异步通知"。现在丢到后台任务里轮询并在完成后回调，
            #   请求立即返回（async=true），语义与文档一致。
            _spawn_bg(
                _video_wait_and_callback(
                    cfg, pkey, pconfig, pmodel_id=real_model, video_id=video_id,
                    wait=True, wait_timeout=req.wait_timeout,
                    callback_url=req.callback_url, rid=rid,
                ),
                name=f"video-callback-{video_id}",
            )
            resp["status"] = "submitted"
            resp["callback_scheduled"] = True
        # 第十轮: 视频日志带 video_id 与响应摘要；轮询数据由 /logs 按 video_id 合成进来
        _access.add({"method": "POST", "kind": "video", "path": "/v1/videos", "rid": rid,
                     "model": req.model, "served": real_model, "provider": pkey, "caller": caller,
                     "ok": True, "video_id": video_id,
                     "response": json.dumps({k: resp.get(k) for k in (
                         "video_id", "model", "served_model", "provider", "provider_name",
                         "status", "async", "callback_scheduled", "x_request_id")},
                         ensure_ascii=False)[:1500]})
        return resp
    except HTTPException:
        # 第六轮: 明确是 HTTPException（已是结构化错误）就原样抛出，别再包一层 502。
        cb.release_probe()
        raise
    except AuthConfigError as e:
        cb.release_probe()
        _access.add({"method": "POST", "kind": "video", "path": "/v1/videos", "rid": rid,
                     "model": req.model, "served": real_model, "provider": pkey, "caller": caller,
                     "ok": False, "error": str(e)[:200]})
        raise HTTPException(503, f"供应商 {pconfig.name} 未配置可用凭证: {e}")
    except ModelNotFoundError as e:
        cb.release_probe()
        raise HTTPException(404, f"视频模型不可用: {e}")
    except httpx.HTTPStatusError as e:
        # 第九轮: 把"上游 4xx（客户端参数不被接受）"和"上游 5xx（真的故障）"分开。
        # 之前一律记进熔断 —— 于是 UI 里选了个上游不支持的 1080P/某时长 → 400 → 计 1 次失败，
        # 戳到阈值就把整个媒体服务熔断 30s，之后所有视频请求都回 503，看起来像"视频坏了"。
        cb.release_probe()
        st = e.response.status_code
        detail = _upstream_detail(e.response.text) or f"HTTP {st}"
        _access.add({"method": "POST", "kind": "video", "path": "/v1/videos", "rid": rid,
                     "model": req.model, "served": real_model, "provider": pkey, "caller": caller,
                     "ok": False, "error": f"upstream {st} {detail}"})
        _metrics.record_call(real_model, pkey, ok=False)
        if st < 500:
            # 4xx = 请求参数问题（尺寸/时长/字段上游不认），不是供应商故障 → 不记熔断，
            # 直接以 400 把上游原因回给调用方，前端就能显示"size 必须是 720P 之一"这类人话。
            raise HTTPException(400, f"上游拒绝请求 (HTTP {st})：{detail}")
        # 5xx/429 等算真故障 → 记熔断，维持 502
        cb.record(False)
        raise HTTPException(502, f"视频创建失败：上游 {st}: {detail}")
    except Exception as e:
        cb.record(False)
        _metrics.record_call(real_model, pkey, ok=False)
        _access.add({"method": "POST", "kind": "video", "path": "/v1/videos", "rid": rid,
                     "model": req.model, "served": real_model, "provider": pkey, "caller": caller, "ok": False, "error": str(e)[:200]})
        raise HTTPException(502, f"视频创建失败: {e}")


@app.get("/v1/videos/{video_id}")
async def videos_poll(request: Request, video_id: str, model: str = "", wait: bool = False,
                      wait_timeout: int = 120, _auth=Depends(require_auth)):
    cfg = _cfg()
    rid = getattr(request.state, "rid", "-")
    # 说明：轮询端点不再往 /logs 记访问行（第十轮起改为按 video_id 累积到「创建」那条），
    # 故这里不取 caller 做日志归属。
    # 第三十轮: 但**归属校验**必须有 —— 此前任何登录账号拿到 video_id 就能读走别人视频的
    #   状态与产物 URL。创建时登记过归属的按登记判；无登记（本次改动前的老任务）放行，
    #   用 404 而不是 403 回绝，避免把 id 变成"存在性探针"。口径见 app/video_owners.py。
    if not await _offload(_owners.allows, video_id, request.state.account,
                          request.state.role == "admin"):
        raise HTTPException(404, f"视频任务不存在: {_logsafe(video_id)}")

    # 本轮重写：供应商定位不再"盲猜"。
    #   视频任务 ID 是**供应商私有**的，查错供应商必然 404/502，且静默误导。
    #   策略：给了 model → 用与创建端点相同的解析逻辑（_resolve_media_model）；
    #         没给 model → 仅当全站只有一家支持视频的供应商时才推断，否则要求显式指定。
    if model:
        resolved = _resolve_media_model(cfg, model, "video")
        if resolved is None:
            raise HTTPException(404, f"未知视频模型: {model}（没有可用的 video 类型模型）")
        pkey, pconfig, _minfo, _token, pmodel_id = resolved
    else:
        video_providers = [k for k, v in cfg.providers.items() if v.media.supports_video]
        if not video_providers:
            raise HTTPException(501, "无可用视频供应商")
        if len(video_providers) > 1:
            raise HTTPException(
                400,
                f"存在多个视频供应商（{', '.join(video_providers)}），查询任务时必须显式指定 model",
            )
        pkey = video_providers[0]
        pconfig = cfg.providers[pkey]
        pmodel_id = pconfig.models[0].id if pconfig.models else ""
        if not pmodel_id:
            raise HTTPException(501, f"供应商 {pkey} 未配置任何可用视频模型")

    extra = {}
    if pconfig.media.video_model_name:
        extra["model_name"] = pconfig.media.video_model_name
    cb = _media_circuit(pkey)
    # 第五轮: 查询前统一做熔断检查（与非等待路径、图像/视频创建保持一致）。
    #   此前仅在 wait=true 的循环里查，wait=false 时对已熔断的供应商照样发请求。
    _media_circuit_check(cb, pkey, pconfig)
    t0 = time.time()
    try:
        # 第六轮: 同 images —— _get_adapter 进 try，凭证缺失不再漏出 500
        adapter, _, _ = _get_adapter(pmodel_id, cfg.server.timeout, cfg)
        if wait:
            # Bug 9 修复: 钳制等待上限；Bug 10 修复: 轮询尊重熔断
            wt = max(1, min(int(wait_timeout or cfg.server.timeout), _MAX_WAIT_TIMEOUT))
            deadline = time.time() + wt
            result = None
            circuit_blocked = False
            # 第十八轮修复（HALF_OPEN 双取令牌）: 上面 _media_circuit_check 已经消费过一次
            #   `cb.allow()` —— 供应商刚从熔断恢复时，那一次拿到的就是**唯一**试探令牌。
            #   循环里再 allow() 会被自己持有的令牌挡住 → 第一次轮询必判"熔断中"→ 503，
            #   且令牌一直不归还，供应商要卡到探测超时（≥30s）才能再被试探。
            #   现在用 probe_held 记录"当前是否已持有许可"，只在需要时重新取。
            probe_held = True
            while time.time() < deadline:
                if not probe_held and not cb.allow():
                    log.warning("[rid=%s] 供应商 %s 熔断中，停止等待视频 %s", rid, pkey, _logsafe(video_id))
                    circuit_blocked = True
                    break
                probe_held = False
                # 本轮修复: 单次轮询失败不再让整段等待以 502 崩掉（与 _video_wait_and_callback 对齐），
                #   瞬时网络抖动只记一次失败并继续等，直到 deadline。
                try:
                    result = await adapter.poll_video(video_id, model=pmodel_id, extra=extra or None)
                    cb.record(True)
                except httpx.HTTPStatusError as e:
                    # 第五轮: 上游 404 = 任务确实不存在，继续轮询到超时毫无意义，立即终止
                    if e.response.status_code == 404:
                        cb.release_probe()   # 第六轮: 请求已发出但按"任务不存在"退出，令牌要归还
                        raise HTTPException(404, f"视频任务不存在: {_logsafe(video_id)}（供应商 {pconfig.name}）")
                    # 第十三轮: 429（查询过于频繁）是限流不是故障 → 不记熔断，拉长间隔继续等
                    if e.response.status_code == 429:
                        # 第十八轮: 这一问不记熔断（record 不会被调用），令牌必须主动归还，
                        #   否则半开态被自己卡住；下一轮循环会重新 allow() 取一个新的。
                        cb.release_probe()
                        probe_held = False
                        log.info("[rid=%s] 轮询视频 %s 被上游限流(429)，稍后重试", rid, _logsafe(video_id))
                        await asyncio.sleep(4)
                        continue
                    cb.record(False)     # record 同时会清掉 HALF_OPEN 的试探令牌
                    log.warning("[rid=%s] 轮询视频 %s 失败(%s): %s", rid, _logsafe(video_id),
                                e.response.status_code, e)
                    await asyncio.sleep(2)
                    continue
                except Exception as e:
                    cb.record(False)
                    log.warning("[rid=%s] 轮询视频 %s 瞬时失败: %s", rid, _logsafe(video_id), e)
                    await asyncio.sleep(2)
                    continue
                status = _video_status(result.get("raw"))
                if _video_terminal(status):
                    break
                await asyncio.sleep(2)
            if result is None:
                if circuit_blocked:
                    raise HTTPException(503, f"供应商 {pconfig.name} 媒体服务熔断中，暂时无法等待视频 {video_id}")
                raise HTTPException(504, f"视频 {video_id} 等待超时（{wt}s）内未取到状态")
        else:
            result = await adapter.poll_video(video_id, model=pmodel_id, extra=extra or None)
            cb.record(True)
        raw = result.get("raw") or {}
        _metrics.record_call(pmodel_id, pkey, ok=True)
        _metrics.record_latency(pmodel_id, time.time() - t0)
        # 第十轮: 轮询不单独占一行日志，改为按 video_id 快照累积，由 /logs 合成到创建那条里
        video_poll_push(video_id, {
            "ts": round(time.time(), 3), "ok": True,
            "status": _video_status(raw),
            "progress": raw.get("progress"),
            "raw": json.dumps(raw, ensure_ascii=False)[:1200],
        })
        return {"video_id": video_id, "provider": pkey, "served_model": pmodel_id, "raw": raw,
                "status": _video_status(raw), "x_request_id": rid}
    except AuthConfigError as e:
        cb.release_probe()
        raise HTTPException(503, f"供应商 {pconfig.name} 未配置可用凭证: {e}")
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        # 第五轮: 上游 404 不该被包装成 502（那会让客户端以为网关/上游故障而重试）；
        #   401/403 属网关侧上游凭证问题，保留 502 但在消息里点明。
        code = e.response.status_code
        if code == 429:
            # 第十三轮: 上游对轮询限流（实测 3 秒一次的频率就会 429「查询过于频繁」）。
            #   这不是故障：既不能记熔断（三次就把媒体桶打开 → 视频页/图像页一起 503），
            #   也不该把 429 抛给前端显示成"查询失败"。这里回 200 + throttled 标记，
            #   让客户端按 retry_after 继续轮询（任务仍在跑，只是这一问被挡了）。
            #   指标/日志都不记（否则 429 会污染调用成功率与日志时间线）。
            cb.release_probe()
            log.info("[rid=%s] 轮询视频 %s 被上游限流(429)，已回 throttled 让客户端稍后重试",
                     rid, _logsafe(video_id))
            return {"video_id": video_id, "provider": pkey, "served_model": pmodel_id,
                    "raw": None, "status": None, "throttled": True, "retry_after": 3,
                    "x_request_id": rid}
        _metrics.record_call(pmodel_id, pkey, ok=False)
        video_poll_push(video_id, {"ts": round(time.time(), 3), "ok": False,
                                   "status": f"HTTP {code}", "error": str(e)[:200]})
        if code == 404:
            # 第六轮: 404 是"客户端给了不存在的任务 id"，属请求错误**不是供应商故障** ——
            #   计入熔断会让反复查询无效 id 的客户端把媒体桶打开，连带挡住正常的视频生成。
            cb.release_probe()
            raise HTTPException(404, f"视频任务不存在: {_logsafe(video_id)}（供应商 {pconfig.name}）")
        cb.record(False)
        if code in (401, 403):
            raise HTTPException(502, f"视频查询失败：供应商 {pconfig.name} 凭证被拒（{code}）")
        raise HTTPException(502, f"视频查询失败: {e}")
    except Exception as e:
        cb.record(False)
        _metrics.record_call(pmodel_id, pkey, ok=False)
        video_poll_push(video_id, {"ts": round(time.time(), 3), "ok": False,
                                   "status": "error", "error": str(e)[:200]})
        raise HTTPException(502, f"视频查询失败: {e}")


# ─────────────────────────────────────
# GET /v1/media/download（第十七轮：生成图片/视频的"下载"按钮）
# ─────────────────────────────────────
# 为什么要绕服务端一圈：生成出来的图/视频挂在第三方 CDN（如
#   https://cos-platform-outputs.agnes-ai.cn/...），该 CDN **不返回 CORS 头**。
#   浏览器里直链下载会碰到两个死结：
#     1) fetch(url) → blob 被 CORS 拦（拿不到字节，也就没法自己造 Blob 下载）；
#     2) <a href=跨域 url download>：HTML 规范里 download 属性对跨域 URL 无效 ——
#        点下去只会"新标签页打开"，而不是存文件（手机端尤其明显）。
#   所以让网关代拉一次、补上 Content-Disposition: attachment 再回给前端，
#   下载就一定落在本地文件里（同源请求，前端用 fetch + Authorization 头拿 blob）。
#
# 安全（全网关唯一一个"服务端主动出网"的接口，必须锁死，否则等于送人一个开放代理）：
#   - 只允许 download.allowed_hosts 白名单里的域名（含其子域），空列表 = 功能关闭；
#   - 命中的域名还要 DNS 解析，解析出的每个 IP 都必须是公网地址（挡 DNS 指内网）；
#   - 只认 http/https，不跟随重定向（挡"302 到内网"的绕法）；
#   - 流式转发不落盘、限制单文件字节数，超限直接掐掉。
_DL_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-m4v": ".m4v",
}
_DL_HAS_EXT = re.compile(r"\.(png|jpe?g|webp|gif|avif|bmp|mp4|webm|mov|m4v)$", re.IGNORECASE)


def _dl_host_allowed(host: str) -> bool:
    """host 是否命中下载白名单（精确或子域匹配；大小写/末尾点无关）。"""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    for allow in _cfg().download.allowed_hosts:
        # 用 "." + allow 做子域匹配，避免 "evilcos-platform-outputs.agnes-ai.cn" 这种
        #   「后缀撞车」的域名被误放行；白名单项本身已在 config 里去掉了前导点。
        if h == allow or h.endswith("." + allow):
            return True
    return False


def _dl_host_is_public(host: str) -> bool:
    """域名解析结果必须全是公网 IP（挡 DNS 指向 127.0.0.1 / 内网段的 SSRF）。

    注意：这里解析一次、httpx 连接时还会再解析一次，理论上存在 DNS rebinding 的
    时间窗；配合"只允许白名单域名 + 不跟随重定向"，残余风险可接受（要更严就得上
    IP 直连 + SNI 校验，收益不大，先不引入复杂度）。
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 这类要按内网拦
        if not ip.is_global:
            return False
    return True


def _dl_safe_filename(name: str) -> str:
    """文件名清洗：抹掉路径成分 / 控制符 / 危险字符，限长。空则返回空串（由调用方兜底）。"""
    n = (name or "").replace("\\", "/").split("/")[-1]
    n = re.sub(r'[\x00-\x1f\x7f"*?<>|:]+', "_", n).strip().strip(".")
    return n[:120]


def _dl_filename(url: str, ctype: str, want: str) -> str:
    """定下载文件名：优先用前端传的（干净名），否则取 URL 末段；都缺扩展名时按类型补。"""
    base = _dl_safe_filename(want) or _dl_safe_filename(urlparse(url).path.rsplit("/", 1)[-1]) or "download"
    if not _DL_HAS_EXT.search(base):
        # Agnes 的图片地址形如 /images/t2i/task_xxx/output_80c0b9a66f944 —— 没有后缀，
        #   浏览器只能靠 Content-Type 猜；这里显式补上，省得下载下来是个无后缀文件。
        ext = _DL_EXT_BY_TYPE.get((ctype or "").split(";")[0].strip().lower(), "")
        if ext:
            base += ext
    return base


async def _dl_open_upstream(url: str):
    """打开上游流（单独抽出来是为了单测能替换掉真实网络调用）。

    返回 (client, response)，两者都由调用方负责关闭。不跟随重定向。
    """
    client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, read=180.0), follow_redirects=False)
    try:
        resp = await client.send(client.build_request("GET", url), stream=True)
    except Exception:
        await client.aclose()
        raise
    return client, resp


@app.get("/v1/media/download")
async def media_download(request: Request, url: str = "", filename: str = "",
                        _auth=Depends(require_auth)):
    """代下载生成出来的图片/视频（第十七轮，登录会话或 API Key 都可调）。

    入参：`url`（产物地址，必须命中 download.allowed_hosts 白名单）
          `filename`（可选：前端给的友好文件名，仅作附议，会做清洗）
    出参：媒体字节流 + `Content-Disposition: attachment`。
    """
    rid = getattr(request.state, "rid", "-") or "-"
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(400, "url 必须是 http/https 的完整地址")
    if not _dl_host_allowed(parsed.hostname):
        # 不泄露白名单内容，但把话说明白（否则用户只会看到"下载失败"）
        log.warning("[rid=%s] 下载被拒：域名 %s 不在白名单", rid, _logsafe(parsed.hostname))
        raise HTTPException(400, f"该地址不在允许下载的域名白名单内：{_logsafe(parsed.hostname)}")
    if not _dl_host_is_public(parsed.hostname):
        log.warning("[rid=%s] 下载被拒：域名 %s 解析到非公网地址", rid, _logsafe(parsed.hostname))
        raise HTTPException(400, "该地址解析到非公网地址，已拒绝")

    limit = _cfg().download.max_bytes
    try:
        client, resp = await _dl_open_upstream(url)
    except Exception as e:
        log.warning("[rid=%s] 下载代拉失败: %s", rid, type(e).__name__)
        raise HTTPException(502, f"取回源文件失败：{type(e).__name__}")

    if resp.status_code >= 400:
        code = resp.status_code
        await resp.aclose()
        await client.aclose()
        # 与图像/视频生成的错误口径保持一致：4xx 是"这个地址有问题"，
        #   5xx 才算源站故障（媒体都 502）。
        raise HTTPException(400 if code < 500 else 502,
                            f"源站返回 {code}（素材可能已过期或被清理）")

    ctype = (resp.headers.get("content-type") or "application/octet-stream").strip()
    name = _dl_filename(url, ctype, filename)
    clen = 0
    try:
        clen = int(resp.headers.get("content-length") or 0)
    except ValueError:
        clen = 0
    if clen and clen > limit:
        await resp.aclose()
        await client.aclose()
        raise HTTPException(413, f"文件超过下载上限（{limit // (1024 * 1024)}MB）")

    async def _stream():
        sent = 0
        try:
            async for chunk in resp.aiter_bytes(65536):
                if not chunk:
                    continue
                sent += len(chunk)
                if sent > limit:
                    # 上游没给 Content-Length 时的兜底：超限就断在这（客户端会看到
                    #   不完整文件，但不会把网关内存/带宽吃穿）。
                    log.warning("[rid=%s] 下载中断：超过上限 %d 字节", rid, limit)
                    break
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    _access.add({"method": "GET", "kind": "media", "path": "/v1/media/download", "rid": rid,
                 "model": "-", "served": filename or name, "provider": urlparse(url).hostname or "-",
                 "caller": getattr(request.state, "account", "-"), "ok": True,
                 "response": json.dumps({"file": name, "type": ctype,
                                         "bytes": clen or None}, ensure_ascii=False)})
    headers = {
        # filename= 给老浏览器（ASCII 兜底），filename*= 给现代浏览器（UTF-8 中文名）
        "Content-Disposition": 'attachment; filename="%s"; filename*=UTF-8\'\'%s'
                               % (re.sub(r"[^\x20-\x7e]", "_", name), quote(name, safe="")),
        "Cache-Control": "private, max-age=600",
        "X-Content-Type-Options": "nosniff",
    }
    if clen:
        headers["Content-Length"] = str(clen)  # 已知且未超限，让浏览器显示进度
    return StreamingResponse(_stream(), media_type=ctype, headers=headers)


# ─────────────────────────────────────
# GET /metrics + /metrics/prometheus（P1-3/8）
# ─────────────────────────────────────
@app.get("/metrics")
async def metrics(request: Request, _auth=Depends(require_auth)):
    if not _cfg().server.metrics_enabled:
        raise HTTPException(404, "metrics 已禁用")
    # M5+: 普通账号只看自己名下调用产生的聚合，admin 看全局 + 熔断状态。
    if request.state.role == "admin":
        snap = _metrics.snapshot()
        snap["circuits"] = _circuits.statuses()  # P2-16 熔断状态
    else:
        snap = _metrics.snapshot(request.state.account)
        snap["circuits"] = []
    return snap


def _prom_label(v) -> str:
    """Prometheus 标签值转义（反斜杠 / 引号 / 换行）。

    修复：模型名/供应商名若含 `"` 或 `\\`，直接拼进指标文本会产生**非法 Prometheus 格式**，
    导致抓取端整段解析失败。
    """
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@app.get("/metrics/prometheus", response_class=PlainTextResponse)
async def metrics_prometheus(request: Request, _auth=Depends(require_auth)):
    # M5+: Prometheus 抓取暴露全局用量，仅管理员可见（避免普通账号看到全站明细）。
    if request.state.role != "admin":
        raise HTTPException(403, "仅管理员可抓取指标")
    if not _cfg().server.metrics_enabled:
        raise HTTPException(404, "metrics 已禁用")
    snap = _metrics.snapshot()
    lines = [
        "# API Gateway Prometheus 指标",
        f'api_gateway_uptime_seconds {snap["uptime_seconds"]}',
        # 第二十七轮: api_gateway_fallback_total 随「路由别名 / fallback 链」一起删除 ——
        #   快照里已没有 fallback_total 字段，这里再读就是 KeyError（曾被 live_check 抓到 500）。
        f'api_gateway_tokens_prompt_total {snap["tokens"]["prompt"]}',
        f'api_gateway_tokens_completion_total {snap["tokens"]["completion"]}',
        # 功能 11: 在途请求 gauge
        f'api_gateway_in_flight {snap.get("in_flight", 0)}',
        f'api_gateway_in_flight_peak {snap.get("in_flight_peak", 0)}',
    ]
    for m, st in snap.get("calls", {}).items():
        ml = _prom_label(m)
        lines.append(f'api_gateway_calls_total{{model="{ml}"}} {sum(st.values())}')
        lines.append(f'api_gateway_calls_ok_total{{model="{ml}"}} {st.get("ok", 0)}')
        lines.append(f'api_gateway_calls_fail_total{{model="{ml}"}} {st.get("fail", 0)}')
    for m, lat in snap.get("latency", {}).items():
        ml = _prom_label(m)
        lines.append(f'api_gateway_latency_p50_seconds{{model="{ml}"}} {lat.get("p50", 0)}')
        lines.append(f'api_gateway_latency_p95_seconds{{model="{ml}"}} {lat.get("p95", 0)}')
    # 熔断状态 gauge（P2-16）
    for c in _circuits.statuses():
        state_val = {"closed": 0, "open": 1, "half_open": 2}.get(c["state"], 0)
        pl = _prom_label(c["provider"])
        lines.append(f'api_gateway_circuit_state{{provider="{pl}"}} {state_val}')
        lines.append(f'api_gateway_circuit_recent_failures{{provider="{pl}"}} {c["recent_failures"]}')
    # 第六轮: 补 HELP/TYPE（promtool / Grafana 的规范要求；此前只有裸指标行）
    # 第二十七轮: fallback_total 的 HELP/TYPE 已随路由/fallback 链删除（连同上面的指标行）。
    #   注意别把"已删除"写成裸注释行塞进导出的正文里：正文里只放真正存在的指标的
    #   HELP/TYPE，说明留在代码注释里（测试会校验声明与导出的一一对应）。
    help_lines = [
        "# HELP api_gateway_uptime_seconds 网关运行时长（秒）",
        "# TYPE api_gateway_uptime_seconds gauge",
        "# HELP api_gateway_tokens_prompt_total 上游 prompt token 累计",
        "# TYPE api_gateway_tokens_prompt_total counter",
        "# HELP api_gateway_tokens_completion_total 上游 completion token 累计",
        "# TYPE api_gateway_tokens_completion_total counter",
        "# HELP api_gateway_in_flight 在途请求数（进入 → 响应头生成完毕）",
        "# TYPE api_gateway_in_flight gauge",
        "# HELP api_gateway_in_flight_peak 在途请求峰值",
        "# TYPE api_gateway_in_flight_peak gauge",
        "# HELP api_gateway_calls_total 按模型统计的调用数",
        "# TYPE api_gateway_calls_total counter",
        "# HELP api_gateway_calls_ok_total 按模型统计的成功调用数",
        "# TYPE api_gateway_calls_ok_total counter",
        "# HELP api_gateway_calls_fail_total 按模型统计的失败调用数",
        "# TYPE api_gateway_calls_fail_total counter",
        "# HELP api_gateway_latency_p50_seconds 延迟 P50（秒）",
        "# TYPE api_gateway_latency_p50_seconds gauge",
        "# HELP api_gateway_latency_p95_seconds 延迟 P95（秒）",
        "# TYPE api_gateway_latency_p95_seconds gauge",
        "# HELP api_gateway_circuit_state 熔断状态（0=closed 1=open 2=half_open）",
        "# TYPE api_gateway_circuit_state gauge",
        "# HELP api_gateway_circuit_recent_failures 熔断窗口内近期失败数",
        "# TYPE api_gateway_circuit_recent_failures gauge",
    ]
    # "# API Gateway Prometheus 指标" 是第一行注释，保留在 HELP 之前
    head, body = lines[0], lines[1:]
    return "\n".join([head] + help_lines + body) + "\n"


# ─────────────────────────────────────
# GET /logs（P1-4）
# ─────────────────────────────────────
def _attach_polls(entries: list[dict]) -> list[dict]:
    """给带 video_id 的创建日志按需附加内存里累积的轮询快照（第十一轮：轮询合成一条）。"""
    for e in entries:
        vid = e.get("video_id")
        if vid:
            e["video_id"] = vid
            p = video_polls(vid)
            if p:
                e["polls"] = p
                e.setdefault("poll_last", p[-1].get("status"))
    return entries


def _logs_for_account(account: str, n: int = 200) -> list[dict]:
    """取某账号产生的访问日志（含轮询合成）。第十一轮 #2：admin 也走这里看别人。"""
    tail = _access.tail(max(1, n))
    rows = [e for e in tail
            if e.get("account") == account or e.get("caller") == account]
    return _attach_polls(rows)


@app.get("/logs")
async def logs(request: Request, n: int = 100, _auth=Depends(require_auth)):
    # 第十一轮 #2：日志页对**所有人（含 admin）都只显示自己的**。
    # 要看别人的调用日志/资产，走用户管理页的 admin 专属端点（/auth/users/{name}/logs）。
    return {"entries": _logs_for_account(request.state.account, n)}


# ─────────────────────────────────────
# GET / — 端点导航（功能 7）
# ─────────────────────────────────────
@app.get("/", include_in_schema=False)
async def root(request: Request, _auth=Depends(require_auth_or_open)):
    # 第六轮: / 也纳入 health_auth 保护（会列出供应商/端点清单）；_auth 未使用。
    if "text/html" in (request.headers.get("accept") or ""):
        # 浏览器访问根路径 → 前端登录页；API 调用（Accept 不含 text/html）仍返回 JSON 导航
        return RedirectResponse("/web/")
    cfg = _cfg()
    return {
        "name": "API Gateway",
        "version": VERSION,
        "endpoints": {
            "chat": "POST /v1/chat/completions（OpenAI Chat Completions 线）",
            # 第二十七轮「三线直传」: 入站是什么格式，出站就是什么格式 —— 网关不翻译协议，
            #   只在模型声明支持该线时原样转发（不支持则 400 说清它支持哪几条线）。
            "messages": "POST /v1/messages（Anthropic Messages 线，鉴权也认 x-api-key）",
            "responses": "POST /v1/responses（OpenAI Responses 线）",
            "batch": "POST /v1/chat/completions/batch（并发多 prompt）",
            "images": "POST /v1/images/generations",
            "videos_create": "POST /v1/videos（异步，?wait 或 callback_url）",
            "videos_poll": "GET /v1/videos/{video_id}?wait=true",
            "models": "GET /models 或 GET /v1/models",
            "metrics": "GET /metrics（JSON）/ GET /metrics/prometheus",
            "logs": "GET /logs?n=100",
            "health": 'GET /health（运维看板；health_auth=false 时匿名只回 {"status":"ok"}）'
                      " / GET /health/{provider}（**始终需鉴权**）",
            # 第三十轮: /docs 只在 docs_enabled 打开时才存在（关掉时路由就没注册）——
            #   导航里还列着它，照着敲会吃 404。这里跟真实路由表对齐：关了就整条不列。
            **({"docs": "GET /docs（Swagger UI）"} if _docs_on else {}),
        },
        "providers": list(cfg.providers.keys()),
    }


# ─────────────────────────────────────
# GET /health — 运维看板（功能 9：聚合失败率/耗时/熔断状态）
# ─────────────────────────────────────
@app.get("/healthz")
async def healthz():
    """最小探活端点：始终只返回 {"status":"ok"}，供负载均衡/监控探活，不泄露任何内部信息。"""
    return {"status": "ok"}


# ── 前端登录 / 登出（账号密码 → short-lived sess- token；API Key 由各账号在设置页自建）──
async def _offload(fn, *args):
    """把同步的 CPU 重活挪到线程池执行，别堵事件循环。

    第十八轮（性能）: 密码哈希是 PBKDF2-HMAC-SHA256 × 200_000，实测 ~31ms/次 ——
    登录、改密（改密要验两次）、建号、管理员重置都在 async handler 里**同步**跑，
    期间整个进程（所有模型的流式转发、其它请求）都停住。挪进线程池后 31ms 只占一个线程，
    事件循环继续服务。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args))


async def _json_object(request: Request) -> dict:
    """读请求体，并要求它是一个 JSON **对象**（dict）。

    第十八轮: 此前各端点直接 `body = await request.json()` 再 `body.get(...)` ——
    传 `[1,2]` / `"x"` / `1` 这类**合法但非对象**的 JSON 会抛 AttributeError，
    被兜底成 500「网关内部错误」（实测 /auth/users、/auth/password、/auth/apikeys 全能复现，
    /auth/login、/history/{kind} 同理）。现在统一回 400，并说清哪儿不对。
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体必须是合法 JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象（如 {\"k\": \"v\"}）")
    return body


def _body_bool(v: Any, field: str) -> bool:
    """请求体里的布尔字段：真布尔 / 0-1 / "true"-"false" 都认，其余一律 400。

    为什么不用 bool(v)：`bool("false") is True` —— 字符串形式的假值会被静默当成真，
    第二十三轮配置面板就栽在这个坑里（app/config.py 的 _opt_bool 同理）。这里宁可回
    400 说清楚，也不替调用方猜他想表达什么。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in ("true", "false", "1", "0"):
        return v.strip().lower() in ("true", "1")
    raise HTTPException(400, f"{field} 需为布尔值（true / false）")


@app.post("/auth/login")
async def auth_login(request: Request):
    body = await _json_object(request)
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if not username or not await _offload(_users.verify, username, password):
        if _record_auth_fail(request, _cfg().server.auth_fail_limit_per_min):
            raise HTTPException(429, "登录失败次数过多，已在 60 秒内触发限流，请稍后重试")
        raise HTTPException(401, "账号或密码错误", headers={"WWW-Authenticate": "Bearer"})
    # 第二十四轮: 账号被管理员停用 → 密码对也不发会话，并明确告知原因（让人知道去找管理员，
    # 而不是反复试密码）。校验顺序在口令之后：不知道密码的人依旧只看到 401，探不出账号状态。
    if _users.is_disabled(username):
        log.info("已停用账号 %s 登录被拒（%s）", username, _client_ip(request))
        raise HTTPException(403, "账号已被停用，请联系管理员")
    _clear_auth_fails(request)      # 登对了一次就不该再被前人的失败连坐
    _sess.set_ttl(_cfg().auth.session_ttl_minutes * 60)
    tok = _sess.create(_client_ip(request), username)
    return {"access_token": tok, "token_type": "Bearer",
            "expires_in": _cfg().auth.session_ttl_minutes * 60,
            "user": username,
            "role": _users.role(username),
            "gateway_version": VERSION}


@app.post("/auth/logout")
async def auth_logout(credentials: HTTPAuthorizationCredentials | None = Security(_bearer)):
    if credentials:
        _sess.revoke(credentials.credentials)
    return {"status": "ok"}


@app.post("/auth/password")
async def auth_change_password(request: Request, _auth=Depends(require_auth)):
    """修改自己的密码（只认会话凭证；API Key 不知道当前密码，也不该能改）。

    第十五轮（老板要求）：已经用网页登录了就不再要求填「当前密码」—— 会话本身就是身份证明，
    少一次输入。安全边界没变：仍然必须持**会话**（API Key 调这里照样 403），改完立刻吊销该账号
    全部会话强制重新登录；仍兼容老客户端，传了 current_password 就照旧校验（传错必须报错）。
    """
    account = _require_session(request)
    body = await _json_object(request)
    current = str(body.get("current_password") or "")
    new = str(body.get("new_password") or "")
    # 第十八轮: 每次 PBKDF2 ≈31ms，改密要验两次 + 写一次 —— 全部挪到线程池，别堵事件循环。
    if current and not await _offload(_users.verify, account, current):
        raise HTTPException(400, "当前密码不正确")
    # 不填当前密码时，用「新密码能否校验通过」来判断是否与原密码相同（等价于原来的 new == current）
    if new and await _offload(_users.verify, account, new):
        raise HTTPException(400, "新密码不能与当前密码相同")
    try:
        await _offload(_users.change_password, account, new)
    except UserError as e:
        raise HTTPException(400, str(e))
    # 改密后使该账号所有会话失效，强制重新登录（避免旧会话残留）
    n = _sess.revoke_account(account)
    return {"status": "ok", "revoked_sessions": n, "relogin_required": True}


# ── 管理员：账号管理（admin 专属） ──
def _require_admin(request: Request) -> str:
    account = request.state.account
    role = request.state.role
    if role != "admin":
        raise HTTPException(403, "仅管理员可执行此操作")
    return account


@app.get("/auth/users")
async def auth_list_users(request: Request, _auth=Depends(require_auth)):
    _require_admin(request)
    return {"users": _users.list_users()}


@app.post("/auth/users")
async def auth_create_user(request: Request, _auth=Depends(require_auth)):
    _require_admin(request)
    _require_session(request)
    body = await _json_object(request)
    username = str(body.get("username") or "").strip()
    password = body.get("password")
    try:
        # 默认密码 = 账号名（管理员可传 password 覆盖）
        await _offload(_users.create_user, username, password if password is not None else None)
    except UserError as e:
        raise HTTPException(400, str(e))
    return {"status": "ok", "username": username}


@app.delete("/auth/users/{username}")
async def auth_delete_user(username: str, request: Request, _auth=Depends(require_auth)):
    me = _require_admin(request)
    _require_session(request)
    if username == me:
        raise HTTPException(400, "不能删除当前登录的管理员账号")
    try:
        deleted = _users.delete_user(username)
    except UserError as e:
        raise HTTPException(400, str(e))
    if not deleted:
        raise HTTPException(404, f"账号 {username} 不存在")
    # 第八轮审计: 删号必须同时吊销会话，否则被删账号最长一个 TTL 内仍能调用上游额度。
    revoked = _sess.revoke_account(username)
    log.info("账号 %s 已删除，吊销会话 %d 个", username, revoked)
    return {"status": "ok", "revoked_sessions": revoked}


@app.put("/auth/users/{username}/disabled")
async def auth_set_user_disabled(username: str, request: Request, _auth=Depends(require_auth)):
    """停用 / 启用账号（admin 专属，第二十四轮）。

    停用效果：①不能登录（密码正确也拒，回 403 说明原因）；②名下 API Key 立刻失效
    （Key 记录**不删**，重新启用即恢复 —— 停用是「暂时关掉」，不是「吊销凭证」，
    真要作废用 DELETE /auth/users/{u}/apikeys）；③停用瞬间踢掉其全部网页会话。
    """
    me = _require_admin(request)
    _require_session(request)
    body = await _json_object(request)
    disabled = _body_bool(body.get("disabled", True), "disabled")
    try:
        # 「admin 不可停用」「不能停用自己」两条规则都在 store 里，接口层不重复判一遍
        found = _users.set_disabled(username, disabled, by=me)
    except UserError as e:
        raise HTTPException(400, str(e))
    if not found:
        raise HTTPException(404, f"账号 {username} 不存在")
    # 与删号/改密同一止血逻辑：停用必须同时踢会话，否则旧 token 最长一个 TTL 内照样能用。
    revoked = _sess.revoke_account(username) if disabled else 0
    log.info("管理员 %s %s账号 %s（吊销会话 %d 个）", me, "停用" if disabled else "启用", username, revoked)
    return {"status": "ok", "username": username, "disabled": disabled,
            "revoked_sessions": revoked}


@app.post("/auth/users/{username}/password")
async def auth_reset_password(username: str, request: Request, _auth=Depends(require_auth)):
    _require_admin(request)
    _require_session(request)
    body = await _json_object(request)
    pw = body.get("password") or username  # 默认重置为账号名
    try:
        await _offload(_users.change_password, username, str(pw))
    except UserError as e:
        raise HTTPException(400, str(e))
    # 重置密码即为止损手段：把该账号已登录的会话全部踢掉，要求用新密码重登。
    revoked = _sess.revoke_account(username)
    return {"status": "ok", "revoked_sessions": revoked}


@app.delete("/auth/users/{username}/apikeys")
async def auth_revoke_user_keys(username: str, request: Request, _auth=Depends(require_auth)):
    """一键吊销该账号的全部 API Key（长期凭证与密码无关，改密救不了泄漏的 Key）。"""
    _require_admin(request)
    _require_session(request)
    try:
        n = _users.clear_api_keys(username)
    except UserError as e:
        raise HTTPException(400, str(e))
    log.info("管理员 %s 吊销账号 %s 的全部 API Key（%d 把）",
             request.state.account, username, n)
    return {"status": "ok", "revoked_keys": n}


# ── 第十一轮 #2：管理员在「用户管理」里看某个账号的调用日志 + 生成的资产 ──
@app.get("/auth/users/{username}/logs")
async def auth_user_logs(username: str, request: Request, n: int = 200,
                         _auth=Depends(require_auth)):
    """某账号的调用日志（含视频轮询合成）。仅管理员；只读，不需会话凭证。"""
    _require_admin(request)
    if _users.find(username) is None:
        raise HTTPException(404, f"账号 {username} 不存在")
    return {"account": username, "entries": _logs_for_account(username, n)}


@app.get("/auth/users/{username}/history/{kind}")
async def auth_user_history(username: str, kind: str, request: Request,
                            _auth=Depends(require_auth)):
    """某账号生成的资产历史（kind ∈ images / videos / chat）。仅管理员；只读。"""
    _require_admin(request)
    if _users.find(username) is None:
        raise HTTPException(404, f"账号 {username} 不存在")
    try:
        kind = HistoryStore._check_kind(kind)
    except HistoryError as e:
        raise HTTPException(400, str(e))
    return {"account": username, "kind": kind,
            "items": await _offload(_history.get, username, kind)}


def _require_session(request: Request) -> str:
    """「动凭证」类操作（改密 / 铸删 API Key / 管账号）只接受密码登录换来的会话。

    第八轮审计: 此前泄漏的 API Key 可以直接调 POST /auth/apikeys 再铸一把、或删掉别人的
    Key —— 等于长期凭证可以自我繁殖，被盗后无从根除。API Key 的定位是「调模型」，
    管理账号请回网页用账号密码登录。
    """
    if getattr(request.state, "credential", "") != "session":
        raise HTTPException(403, "该操作需用账号密码登录的网页会话（API Key 仅用于调用模型）")
    return request.state.account


# ── 各账号自管 API Key（默认无，页面自建） ──
@app.get("/auth/apikeys")
async def auth_list_apikeys(request: Request, _auth=Depends(require_auth)):
    """列出本账号 Key（含明文，便于反复复制）。

    第八轮补: 因为现在回显**明文**，这个只读接口也只认密码登录换来的会话 ——
    否则泄漏一把 Key 就能顺走同账号其它 Key 的明文，等于无限续命。
    """
    _require_session(request)
    acc = request.state.account
    return {"keys": _users.keys(acc),
            "key_limit": key_limit_for_role(_users.role(acc))}


@app.post("/auth/apikeys")
async def auth_create_apikey(request: Request, _auth=Depends(require_auth)):
    _require_session(request)
    body = await _json_object(request)
    label = str((body.get("label") or "")[:40])
    try:
        key = _users.add_api_key(request.state.account, label)
    except UserError as e:
        raise HTTPException(400, str(e))
    return {"status": "ok", "key": key, "label": label,
            "key_limit": key_limit_for_role(request.state.role)}


@app.delete("/auth/apikeys/{keyhint}")
async def auth_delete_apikey(keyhint: str, request: Request, _auth=Depends(require_auth)):
    _require_session(request)
    ok = _users.remove_api_key(request.state.account, keyhint)
    if not ok:
        raise HTTPException(404, "未找到该 API Key")
    return {"status": "ok"}


# ── 前端历史（图像 / 视频 / 对话）：服务端落盘 + 按账号隔离 ──
# 写入是按 id upsert（见 app/history.py），所以前端既能"提交新增/变更"也能整份重推，
# 两台设备各生成一条也不会互相覆盖。读接口允许会话或 API Key；写接口只认会话。
@app.get("/history/{kind}")
async def history_list(request: Request, kind: str, _auth=Depends(require_auth)):
    # 第三十轮（性能）: 历史落盘/读取整体挪到线程池 —— HistoryStore 每次都是**整份**读写
    #   （上限 4MB），同步跑在 async handler 里会把事件循环按住，所有并发请求一起等。
    #   users/feedback/config 早就走 _offload 了，这里此前是唯一漏网的。
    try:
        items = await _offload(_history.get, request.state.account, kind)
    except HistoryError as e:
        raise HTTPException(400, str(e))
    return {"kind": kind, "items": items, "cap": HISTORY_CAP}


@app.post("/history/{kind}")
async def history_save(request: Request, kind: str, _auth=Depends(require_auth)):
    _require_session(request)
    try:
        HistoryStore._check_kind(kind)
    except HistoryError as e:
        raise HTTPException(400, str(e))
    # 第十八轮: 用统一的对象校验 —— 此前传 `[1,2]` 会走到 (body or {}).get 抛 AttributeError → 500
    body = await _json_object(request)
    items = body.get("items")
    if not isinstance(items, list):
        raise HTTPException(400, "items 必须是数组")
    try:
        merged = await _offload(_history.upsert, request.state.account, kind, items)
    except HistoryError as e:
        raise HTTPException(400, str(e))
    return {"status": "ok", "count": len(merged), "cap": HISTORY_CAP}


@app.delete("/history/{kind}")
async def history_clear(request: Request, kind: str, _auth=Depends(require_auth)):
    _require_session(request)
    try:
        n = await _offload(_history.clear, request.state.account, kind)
    except HistoryError as e:
        raise HTTPException(400, str(e))
    return {"status": "ok", "cleared": n}


@app.delete("/history/{kind}/{item_id}")
async def history_delete_one(request: Request, kind: str, item_id: str, _auth=Depends(require_auth)):
    _require_session(request)
    try:
        ok = await _offload(_history.delete_item, request.state.account, kind, item_id)
    except HistoryError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, "未找到该历史记录")
    return {"status": "ok"}


# ── 意见反馈（第二十二轮）：普通账号提交（可附图），管理员在「用户反馈」页看全部 ──
# 与历史写接口同一口径：**写只认网页会话**（API Key 的定位是调模型，不是操作界面）。
# 图片沿用「浏览器端压缩 → data URI 上送」这条既有路子：不新增上传端点、不引入图片处理
# 依赖（Pillow 不在 requirements.txt 里，不能拿它缩图 —— 缩略图由页面端 canvas 生成）。
@app.post("/feedback")
async def feedback_add(request: Request, _auth=Depends(require_auth)):
    _require_session(request)
    # 第二十二轮定稿：**管理员只处理、不提**。界面上不给 admin 这个菜单，
    # 服务端也堵住 —— 否则"只能看没法提"就成了一句只在 UI 上成立的约定。
    if request.state.role == "admin":
        raise HTTPException(403, "管理员账号只处理反馈，不提交反馈")
    body = await _json_object(request)
    try:
        item = await _offload(_feedback.add, request.state.account,
                              body.get("content"), body.get("images") or [])
    except FeedbackError as e:
        raise HTTPException(400, str(e))
    return {"status": "ok", "item": item, "cap": FEEDBACK_CAP}


@app.get("/feedback/mine")
async def feedback_mine(request: Request, _auth=Depends(require_auth)):
    """本人提交过的（新 → 旧）。"""
    return {"items": _feedback.mine(request.state.account), "cap": FEEDBACK_CAP}


@app.get("/feedback")
async def feedback_all(request: Request, _auth=Depends(require_auth)):
    """全部账号的反馈（admin 专属）。"""
    _require_admin(request)
    return {"items": _feedback.all(), "cap": FEEDBACK_CAP}


@app.patch("/feedback/{fid}/archive")
async def feedback_archive(request: Request, fid: str, _auth=Depends(require_auth)):
    """归档/取消归档（admin 收件箱整理用）。只打标记：不动数据、不动处理状态。"""
    _require_admin(request)
    _require_session(request)
    body = await _json_object(request)
    archived = _body_bool(body.get("archived"), "archived")
    item = await _offload(_feedback.set_archived, fid, archived)
    if not item:
        raise HTTPException(404, "未找到该反馈")
    return {"status": "ok", "archived": item.get("archived", False), "item": item}


@app.patch("/feedback/{fid}")
async def feedback_update(request: Request, fid: str, _auth=Depends(require_auth)):
    """管理员更新处理进度（status: pending/doing/done + 可选处理说明）。

    每一次改都会往该条的 history 里追加一条记录（谁改的 / 改到哪 / 说了什么），
    提交人在「我提交过的」里逐步都能看到。
    """
    _require_admin(request)
    body = await _json_object(request)
    try:
        item = await _offload(_feedback.update, fid, body.get("status"),
                              body.get("note") or "", request.state.account)
    except FeedbackError as e:
        raise HTTPException(400, str(e))
    if not item:
        raise HTTPException(404, "未找到该反馈")
    return {"status": "ok", "item": item}


@app.delete("/feedback/{fid}")
async def feedback_withdraw(request: Request, fid: str, _auth=Depends(require_auth)):
    """反馈删除：管理员真删任意一条（admin_delete，含图片）；非管理员只撤自己待处理的单子。

    非管理员只允许撤**待处理**的单子：管理员一动它（处理中/已处理）就不能撤了。
    """
    me = _require_session(request)
    # 管理员可永久删除任意一条（含图片）；非管理员仍是「本人撤回待处理的单子」。
    if _users.role(me) == "admin":
        item = await _offload(_feedback.admin_delete, fid)
        if not item:
            raise HTTPException(404, "未找到该反馈")
        return {"status": "ok", "deleted": True}
    r = await _offload(_feedback.withdraw, me, fid)
    if not r.get("ok"):
        reason = r.get("reason")
        if reason == "notfound":
            raise HTTPException(404, "未找到该反馈")
        if reason == "forbidden":
            raise HTTPException(403, "只能撤回自己提交的反馈")
        raise HTTPException(400, "管理员已开始处理，这条不能撤回了")
    return {"status": "ok", "item": r["item"]}


@app.get("/feedback/media/{fid}/{idx}")
async def feedback_media(request: Request, fid: str, idx: int, _auth=Depends(require_auth)):
    """取某条反馈的第 idx 张原图（返回 data URI）。

    权限：本人或 admin。fid 必须先存在于库里才继续 —— 路径只用「库里的 id + 下标」拼，
    不接受客户端直接给文件名，杜绝 ../ 之类的路径穿越。
    """
    owner = _feedback.owner_of(fid)
    if owner is None:
        raise HTTPException(404, "未找到该反馈")
    if owner != request.state.account and request.state.role != "admin":
        raise HTTPException(403, "只能查看自己反馈里的图片")
    uri = await _offload(_feedback.media_uri, fid, idx)
    if uri is None:
        raise HTTPException(404, "未找到该图片")
    return {"uri": uri}


# ── API Key 环境变量（data/.env）读取/写入（网关配置页「环境变量」卡）────────
def _env_file() -> Path:
    # 第三十轮: 路径可被 GATEWAY_ENV_FILE 覆盖 —— 与 users/history/feedback 同一套路，
    #   否则测试一旦覆盖到写路径就会**直接改掉生产 data/.env**（这是本轮补测试时发现的隐患）。
    return Path(os.environ.get("GATEWAY_ENV_FILE", str(_PROJECT_ROOT / "data" / ".env")))

def _env_snapshot() -> dict[str, dict]:
    """当前用 token_env 的供应商 → 环境变量名 + 是否配置 + 运行时值（以 os.environ 为准）。"""
    cfg = _cfg()
    out: dict[str, dict] = {}
    for pkey, p in cfg.providers.items():
        te = p.auth.token_env
        if te:
            out[te] = {
                "provider": pkey,
                "provider_name": p.name,
                "set": bool(os.environ.get(te)),
                "value": os.environ.get(te, ""),
            }
    return dict(sorted(out.items()))

def _write_env_values(updates: dict[str, str]) -> int:
    """把 API Key 写进 data/.env（原子替换 + 0600）并即时注入 os.environ 生效。"""
    path = _env_file()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        m = re.match(r"^(export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", stripped) if stripped else None
        if m:
            prefix, ename = m.group(1) or "", m.group(2)
            if ename in updates:
                out.append(f"{prefix}{ename}={updates[ename]}")
                seen.add(ename)
                continue
        out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    text = "\n".join(out).rstrip("\n") + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    for k, v in updates.items():
        os.environ[k] = v
    return len(updates)


@app.get("/config/env")
async def config_env_get(request: Request, _auth=Depends(require_auth)):
    """读取全部 API Key 环境变量（导出也走这里）。管理员 + 登录会话。"""
    _require_admin(request)
    _require_session(request)
    return {"envs": _env_snapshot()}


@app.post("/config/env")
async def config_env_post(request: Request, _auth=Depends(require_auth)):
    """设置/导入 API Key 环境变量。body: {"envs": {环境变量名: 值}}。管理员 + 登录会话。"""
    _require_admin(request)
    _require_session(request)
    body = await _json_object(request)
    envs = body.get("envs")
    if not isinstance(envs, dict):
        raise HTTPException(400, "envs 必须是 {环境变量名: 值} 对象")
    allowed = {p.auth.token_env for p in _cfg().providers.values() if p.auth.token_env}
    updates: dict[str, str] = {}
    for k, v in envs.items():
        name = (k or "").strip()
        if not name:
            continue
        if name not in allowed:
            raise HTTPException(400, f"环境变量 {name} 未被任何供应商用作 API Key")
        val = v if isinstance(v, str) else ""
        # 第三十轮: 值必须先校验再落盘。写入是**逐行重建 .env**，一个带 \n 的值
        #   （页面上粘贴多了个换行、或刻意构造）就能在文件里**凭空多出几行**，
        #   把同一份 .env 里的其它变量改掉/加进去；值里的换行同时会让"一行一变量"的文件
        #   在下次读取时被截断成错误的值。Key 也不该有控制字符与超长内容。
        if any(c in val for c in ("\n", "\r", "\x00")) or len(val) > 4096:
            raise HTTPException(400, f"环境变量 {name} 的值非法（不得含换行/控制字符，长度 ≤4096）")
        updates[name] = val
    if updates:
        await _offload(_write_env_values, updates)
    return {"status": "ok", "applied": len(updates), "envs": _env_snapshot()}


@app.get("/config")
async def config_get(request: Request, _auth=Depends(require_auth)):
    """读取当前配置（面板视图）+ 文件原文 + 告警/提示。仅管理员。

    - `config`：面板表单用的值（结构与 gateway.yaml 一一对应；密钥只回报"配没配"）
    - `yaml`：**文件当前原文**（页面上可展开看，不做二次加工 —— 你看到的就是盘上的）
    - `warnings`：配置体检结论（通配 CORS、缺 Key 等）
    - `restart_keys`：改了必须重启进程才生效的键（页面按此在输入框旁标注）
    - `model_ids`：给模型、定价的下拉用（不用前端再猜）
    """
    _require_admin(request)
    cfg = _cfg()
    raw_text = ""
    try:
        raw_text = CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        pass
    return {
        "config": gw_settings.config_to_view(cfg),
        "yaml": raw_text,
        "env": _env_snapshot(),
        "meta": {
            "path": str(CONFIG_PATH),
            "restart_keys": list(gw_settings.RESTART_KEYS),
            "warnings": validate_warnings(cfg),
            "model_ids": [m["id"] for m in cfg.all_models()],
            "model_types": list(gw_settings.MODEL_TYPES),
            "media_keys": list(gw_settings.MEDIA_KEYS),
            "mtime": _config_mtime(),
        },
    }


@app.put("/config")
async def config_put(request: Request, _auth=Depends(require_auth)):
    """保存配置（管理员 + 网页会话），**保存即生效**。

    顺序刻意如此：解析校验 → 渲染 → 写盘（备份 + 原子替换）→ 切运行态。
    校验不通过就直接 400，**不写盘、不动运行态** —— 半个坏配置比不改更糟。

    写盘后把 `_state["mtime"]` 一起更新，免得热重载循环看到 mtime 又白重载一遍。
    需重启才生效的键（host/port/…）在 `restart_required` 里回给页面提示。
    """
    _require_admin(request)
    _require_session(request)      # 会话专属：API Key 的定位是调模型，不是改网关配置
    body = await _json_object(request)

    # 全文编辑器路径：直接收 gateway.yaml 原文，校验后按用户原文写盘（保留注释/格式）。
    yaml_text = body.get("yaml") if isinstance(body.get("yaml"), str) else None
    if yaml_text is not None:
        if not yaml_text.strip():
            raise HTTPException(400, "配置文件内容为空")
        try:
            raw_dict = yaml.safe_load(yaml_text)
        except yaml.YAMLError as e:
            raise HTTPException(400, f"YAML 解析失败: {e}")
        if not isinstance(raw_dict, dict):
            raise HTTPException(400, "gateway.yaml 顶层必须是映射（key: value）")
        old_cfg = _cfg()
        try:
            new_cfg, warns = gw_settings.parse_and_validate(raw_dict)
        except ConfigError as e:
            raise HTTPException(400, str(e))
        text = yaml_text if yaml_text.endswith("\n") else yaml_text + "\n"
        info = await _offload(gw_settings.write_config, CONFIG_PATH, text)
        restart = gw_settings.restart_keys_changed(old_cfg, new_cfg)
        await _apply_config(new_cfg, source=f"页面保存YAML（{request.state.account}）")
        _state["mtime"] = _config_mtime()
        log.warning("配置已由 %s 通过管理页面以 YAML 原文保存并生效（%d 供应商）",
                    request.state.account, len(new_cfg.providers))
        return {"status": "ok", "applied": True, "restart_required": restart,
                "warnings": warns, "path": info.get("path", str(CONFIG_PATH)),
                "backup": info.get("backup", ""), "yaml": text, "mtime": _state["mtime"]}

    view = body.get("config") if isinstance(body.get("config"), dict) else body

    old_cfg = _cfg()
    raw_old = await _offload(gw_settings.read_raw, CONFIG_PATH)
    try:
        raw_new = gw_settings.view_to_raw(old_cfg, raw_old, view)
        new_cfg, warns = gw_settings.parse_and_validate(raw_new)
    except ConfigError as e:
        raise HTTPException(400, str(e))
    text = gw_settings.render_yaml(raw_new)
    info = await _offload(gw_settings.write_config, CONFIG_PATH, text)
    restart = gw_settings.restart_keys_changed(old_cfg, new_cfg)
    await _apply_config(new_cfg, source=f"页面保存（{request.state.account}）")
    _state["mtime"] = _config_mtime()
    log.warning("配置已由 %s 通过管理页面保存并生效（%d 供应商 / %d 字节）",
                request.state.account, len(new_cfg.providers), info.get("bytes", 0))
    return {
        "status": "ok",
        "applied": True,
        "restart_required": restart,
        "warnings": warns,
        "path": info.get("path", str(CONFIG_PATH)),
        "backup": info.get("backup", ""),
        "yaml": text,
        "mtime": _state["mtime"],
    }


@app.get("/health")
async def health(request: Request, _auth=Depends(require_auth_or_open)):
    # 安全报告：未鉴权访问只回 {"status":"ok"}；带有效 Bearer 才给完整看板
    if not getattr(request.state, "health_authed", False):
        return {"status": "ok"}
    cfg = _cfg()
    snap = _metrics.snapshot() if cfg.server.metrics_enabled else {}
    calls = snap.get("calls", {})
    # 每模型失败率 + 平均耗时
    model_health = {}
    for mid, st in calls.items():
        ok, fail = st.get("ok", 0), st.get("fail", 0)
        total = ok + fail
        lat = snap.get("latency", {}).get(mid, {})
        model_health[mid] = {
            "calls": total, "ok": ok, "fail": fail,
            "fail_rate": round(fail / total, 3) if total else 0.0,
            "latency_p50": lat.get("p50"), "latency_p95": lat.get("p95"),
        }
    # 熔断状态
    circuit_states = {c["provider"]: c["state"] for c in _circuits.statuses()}
    return {
        "status": "ok",
        "version": VERSION,
        "uptime_seconds": snap.get("uptime_seconds"),
        "in_flight": snap.get("in_flight", 0),
        "in_flight_peak": snap.get("in_flight_peak", 0),
        # 第二十七轮: fallback_total 已随路由/fallback 链删除（这里原本恒为 0 的死字段）
        "tokens": snap.get("tokens", {}),
        "providers": {
            pkey: {
                "name": pconfig.name,
                # 本轮修复: 不再回显 base_url —— /health 默认无需鉴权，回显上游内网/私有地址
                #   属信息泄露（内部拓扑）。需要地址时看配置文件或走鉴权端点。
                "api_key_set": bool(pconfig.auth.resolve_token()),
                "models": len(pconfig.models),
                "circuit": circuit_states.get(pkey, "not_tracked"),
                "media_circuit": circuit_states.get(f"{pkey}#media", "not_tracked"),
            }
            for pkey, pconfig in cfg.providers.items()
        },
        "models": model_health,
    }


@app.get("/health/{provider_key}")
async def provider_health(request: Request, provider_key: str, _auth=Depends(require_auth)):
    # 第六轮安全审计: 本端点会用**网关持有的上游 Key** 去 GET {base_url}/models，
    #   等同于"未授权者借网关的凭证触发上游请求并读回结果"。
    # 第三十轮更正: 上面这行的注释此前写着"与 /health 同受 health_auth 保护（此前它已经是
    #   require_auth_or_open）"—— 与代码不符：**它用的是 require_auth，恒定要鉴权**，
    #   并不随 health_auth 放宽。这个"更严"的行为是刻意的（理由就是上一段：它会花掉上游额度），
    #   所以保留代码、把注释改对：**本端点不受 health_auth 影响，永远需要凭证**；
    #   想让探活工具不带头也能用，用 /healthz 或 GET /health（匿名只回 {"status":"ok"}）。
    cfg = _cfg()
    if provider_key not in cfg.providers:
        raise HTTPException(404, f"未知供应商: {provider_key}")
    pconfig = cfg.providers[provider_key]
    token = pconfig.auth.resolve_token()
    if not token:
        return {"provider": provider_key, "reachable": False, "reason": "未配置 API Key"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{pconfig.base_url}/models",
                                   headers={"Authorization": f"Bearer {token}"})
        # Bug 11 修复: reachable = 上游返回 2xx（此前只要有响应就报 True，401/500 也误报可达）
        ok = 200 <= resp.status_code < 300
        result = {"provider": provider_key, "name": pconfig.name,
                  "reachable": ok, "ok": ok, "status_code": resp.status_code}
        if ok:
            try:
                data = resp.json()
                result["models_remote"] = len(data.get("data", [])) if isinstance(data, dict) else None
            except Exception:
                pass
        else:
            result["reason"] = f"上游返回 HTTP {resp.status_code}"
        return result
    except Exception as e:
        return {"provider": provider_key, "reachable": False, "ok": False, "reason": str(e)}


# ── 前端托管（M1）：Vue 构建产物挂到 /web（同源，免 CORS）。 ├──
_WEB_DIST = Path(__file__).parent.parent / "frontend" / "dist"
try:
    from fastapi.staticfiles import StaticFiles  # noqa: F811
except Exception:
    StaticFiles = None

if StaticFiles is not None and _WEB_DIST.is_dir():
    app.mount("/web", StaticFiles(directory=str(_WEB_DIST), html=True), name="web")
    _WEBRAISED = True
else:
    _WEBRAISED = False
    log.info("未找到前端构建产物 %s —— /web 未挂载（npm run build 后再重启生效）", _WEB_DIST)


# M5+ 修复: /web 缓存策略。Vite 产物 asset 文件名带内容 hash（天生不可变），可给一年长缓存；
#   但 index.html 每次部署都会换 hash 引用，若它被浏览器/代理缓存，旧页面会引已被删的旧
#   asset 而 404 白屏（本地复现：前端多次 build 后公网域名白屏）。这里让 index.html 发
#   no-cache（每次重新校验、内容变了就重取），带 hash 的 asset 发 `immutable` 长缓存。
@app.middleware("http")
async def web_cache_policy(request: Request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if request.method == "GET" and p.startswith("/web"):
        name = p.rsplit("/", 1)[-1]
        if not name or name == "index.html":
            resp.headers["Cache-Control"] = "no-cache"
        else:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp
