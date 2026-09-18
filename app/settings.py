"""网关配置的面板化管理（第二十三轮）。

老板要求：`data/gateway.yaml` 里的**每一项**都能在管理员账号的页面上改，**保存后实时生效**。

这个模块只干"配置文本 ↔ 面板 JSON"这一件事，不碰运行时对象：

  - `config_to_view(cfg)`   运行中的配置 → 面板 JSON（密钥只回报"配没配"，不回显内容）
  - `view_to_raw(cfg, raw, view)` 面板提交 → 完整 yaml 字典（以**文件原文为底**，只覆盖面板管得着的键）
  - `parse_and_validate(raw)` 先在内存里解析 + 校验，**通过了才写盘**（绝不出现"写坏文件 → 重启起不来"）
  - `render_yaml(raw)`      生成带注释的 yaml 文本（注释由本文件维护，见下方各 section 抬头）
  - `write_config(path, text)` 滚动备份 + 原子替换（tmp + os.replace，热重载循环读不到半个文件）

"实时生效"由 main.py 负责：它拿到新 `GatewayConfig` 后就地更新指标/日志/熔断/限流并丢弃
adapter 缓存，与 mtime 热重载走**同一条** `_apply_config` 路径（两处逻辑不可能再跑偏）。

哪些键改了需要重启进程（启动时定死的东西）见 `RESTART_KEYS`；其余都是每请求读配置，
保存即生效。这个清单也回给前端，页面会在对应输入框旁标出来 —— 不标就是骗人。
"""

from __future__ import annotations

import copy
import os
import shutil
import time
from pathlib import Path
from typing import Any

import yaml

from app.config import (
    ConfigError,
    GatewayConfig,
    PROTOCOLS,
    _default_endpoints,
    parse_config,
    validate_config,
    validate_warnings,
)

# ── 改了必须重启进程才生效的键（其余都是"每请求读配置"，保存即生效） ──
#   host / port：uvicorn 在进程启动时绑定，改文件不会重绑。
#   graceful_shutdown：run.py 启动时读一次，传给 uvicorn 的 timeout_graceful_shutdown。
#   docs_enabled：FastAPI 实例在 import 期就按它决定 docs_url / openapi_url。
#   hot_reload：热重载任务本身在 lifespan 里创建/取消 —— 关掉能立刻停（循环每轮检查），
#     但从「关」改成「开」不会凭空长出任务来，所以按"需重启"标注。
RESTART_KEYS = (
    "server.host",
    "server.port",
    "server.graceful_shutdown",
    "server.docs_enabled",
    "server.hot_reload",
)

# 时间戳备份最多留几份（滚动 .bak 之外）
BACKUP_KEEP = 5

# 供应商 media 段的字段名（面板逐项编辑，顺序即展示顺序）
MEDIA_KEYS = (
    "image_endpoint",
    "image_default_size",
    "video_create_endpoint",
    "video_poll_base",
    "video_poll_path",
    "video_model_name",
)

MODEL_TYPES = ("text", "image", "video")


# ── 读取：运行中的配置 → 面板 JSON ─────────────────────────
def config_to_view(cfg: GatewayConfig) -> dict[str, Any]:
    """把运行中的配置摊平成面板能直接吃的 JSON（结构与 yaml 一一对应）。

    密钥只回报 `token_set`（明文 token 是否配了），**不回显 token 本身**；
    `token_env` 是环境变量名，不是秘密，照常回显。
    """
    providers: dict[str, Any] = {}
    for pkey, p in cfg.providers.items():
        providers[pkey] = {
            "name": p.name,
            "base_url": p.base_url,
            "auth": {
                "token_env": p.auth.token_env,
                # 面板里 token 输入框永远留空；placeholder 提示"留空 = 不修改"
                "token_set": bool(p.auth.token),
                "token_env_set": bool(p.auth.token_env and os.environ.get(p.auth.token_env)),
            },
            "extra_headers": dict(p.extra_headers or {}),
            "rate_limit_rps": p.rate_limit_rps,
            "requires_session_header": bool(p.requires_session_header),
            "media": {k: getattr(p.media, k) for k in MEDIA_KEYS},
            # 第二十七轮（三线直传）: 每条协议线的出站路径/鉴权/固定头。
            #   面板不提供编辑器（在 YAML 全文编辑器里改），但**必须一路带着走**：
            #   view_to_raw 是按固定键集重建 providers 的，少带这一段 = 管理员在页面上
            #   随手保存一次，OpenCode 的三条线配置就被静默清掉（模型全变"不支持该线"）。
            "endpoints": {
                name: {"path": ep.path, "auth": ep.auth, "headers": dict(ep.headers or {})}
                for name, ep in (p.endpoints or {}).items()
            },
            "models": [
                {"id": m.id, "display_name": m.display_name, "type": m.type, "vision": bool(m.vision),
                 # 第二十五轮: 回传模型级强制参数。面板不提供编辑入口（在 YAML 全文编辑器里改），
                 #   但必须一路带着走 —— 否则读回来再提交一次就会把它弄丢。
                 "params": dict(m.params or {}),
                 # 第二十七轮: 模型能走哪几条协议线。这一项**面板可编辑**
                 #   （「供应商与模型」页给每个模型打格式标签），所以两处都要认：
                 #   读回给页面显示、写盘时以页面提交的为准。
                 "protocols": list(m.protocols)}
                for m in p.models
            ],
        }
    s = cfg.server
    return {
        "providers": providers,
        "server": {
            "host": s.host,
            "port": s.port,
            "timeout": s.timeout,
            "retry": s.retry,
            "health_auth": s.health_auth,
            "hot_reload": s.hot_reload,
            "hot_reload_interval": s.hot_reload_interval,
            "metrics_enabled": s.metrics_enabled,
            "rate_limit_rps": s.rate_limit_rps,
            "max_body_size": s.max_body_size,
            "cors_origins": list(s.cors_origins or []),
            "graceful_shutdown": s.graceful_shutdown,
            "batch_max_concurrency": s.batch_max_concurrency,
            "batch_max_items": s.batch_max_items,
            "webhook_allow_private": s.webhook_allow_private,
            "docs_enabled": s.docs_enabled,
            "auth_fail_limit_per_min": s.auth_fail_limit_per_min,
        },
        "auth": {"session_ttl_minutes": cfg.auth.session_ttl_minutes},
        "circuit": {
            "enabled": cfg.circuit.enabled,
            "failure_threshold": cfg.circuit.failure_threshold,
            "cooldown": cfg.circuit.cooldown,
            "window": cfg.circuit.window,
        },
        "observability": {
            "log_level": cfg.observability.log_level,
            "log_json": cfg.observability.log_json,
            "access_log_maxlen": cfg.observability.access_log_maxlen,
        },
        "pricing": {
            "per_model": {
                name: {"input": v.get("input", 0.0), "output": v.get("output", 0.0)}
                for name, v in (cfg.pricing.per_model or {}).items()
            }
        },
        "download": {
            "allowed_hosts": list(cfg.download.allowed_hosts or []),
            "max_bytes": cfg.download.max_bytes,
        },
    }


# ── 写入：面板 JSON → 完整 yaml 字典 ───────────────────────
def _parse_protocols_view(v: Any, where: str) -> list[str]:
    """面板提交的模型协议线（第二十七轮三线直传）→ 写盘用的标准名列表。

    页面上的格式标签就是这几个标准名（chat/completions | messages | responses），
    写错名字在写盘前就报错（不能等运行时才发现"这个模型永远 400"）。
    列表去重保序；空列表报错（一个模型一条线都不支持多半是手滑）。
    """
    if isinstance(v, str):
        raw_items: list[Any] = [v]
    elif isinstance(v, (list, tuple)):
        raw_items = list(v)
    else:
        raise ConfigError(f"{where} 必须是列表（可选：{'/'.join(PROTOCOLS)}）")
    out: list[str] = []
    for item in raw_items:
        name = str(item or "").strip()
        if name not in PROTOCOLS:
            raise ConfigError(f"{where} 里的 {name!r} 不是标准协议名（可选：{'/'.join(PROTOCOLS)}）")
        if name not in out:
            out.append(name)
    if not out:
        raise ConfigError(f"{where} 不能为空 —— 每个模型至少要声明一条协议线")
    return out


def _default_endpoints_view() -> dict[str, Any]:
    """供应商没在文件里写 endpoints 时，面板/写盘要用的默认三条线。"""
    return {name: {"path": ep.path, "auth": ep.auth, "headers": dict(ep.headers or {})}
            for name, ep in _default_endpoints().items()}


def _as_dict(v: Any, field_name: str) -> dict[str, Any]:
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise ConfigError(f"{field_name} 必须是对象（键值对），得到: {type(v).__name__}")
    return v


def _str_list(v: Any, field_name: str) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        # 容错：单个值写成字符串也认（前端偶尔只传一项）
        v = [v]
    if not isinstance(v, list):
        raise ConfigError(f"{field_name} 必须是数组，得到: {type(v).__name__}")
    out: list[str] = []
    for x in v:
        if x is None:
            continue
        if not isinstance(x, str):
            raise ConfigError(f"{field_name} 只能填字符串，得到: {x!r}")
        out.append(x.strip())
    return out


def _str_map(v: Any, field_name: str) -> dict[str, str]:
    d = _as_dict(v, field_name)
    out: dict[str, str] = {}
    for k, val in d.items():
        kk = str(k).strip()
        if not kk:
            continue
        out[kk] = "" if val is None else str(val)
    return out


def view_to_raw(cfg: GatewayConfig, raw: dict[str, Any], view: dict[str, Any]) -> dict[str, Any]:
    """面板提交 → 完整 yaml 字典。

    以**文件原文为底**（`raw`），只覆盖面板管得着的键：

      - 这样文件里将来多出来的自定义段/键不会因为页面保存一次就消失；
      - 供应商的明文 token 面板留空时也不会被清掉（原文里的值原样留着）；
      - 面板没传的段（局部提交/脚本调用）保持原文不动，不会"顺手清空"。

    校验不在这里做 —— 调用方拿到 raw 后交给 `parse_and_validate`，不通过就别写盘。
    """
    out = copy.deepcopy(raw) if isinstance(raw, dict) else {}

    # ── providers（整段以面板为准）──
    if "providers" in view:
        prov_in = _as_dict(view.get("providers"), "providers")
        base_prov = out.get("providers") if isinstance(out.get("providers"), dict) else {}
        providers: dict[str, Any] = {}
        for pkey, pv in prov_in.items():
            key = str(pkey).strip()
            if not key:
                raise ConfigError("供应商的 key 不能为空")
            pv = _as_dict(pv, f"providers.{key}")
            base_p = base_prov.get(key) if isinstance(base_prov.get(key), dict) else {}
            auth_in = _as_dict(pv.get("auth"), f"providers.{key}.auth")
            base_auth = _as_dict(base_p.get("auth"), f"providers.{key}.auth")
            token = auth_in.get("token")
            # 留空 = 不修改：沿用文件里已有的明文 token（面板从不回显它）
            if token is None or (isinstance(token, str) and not token.strip()):
                token = base_auth.get("token", "") or ""
            media_in = _as_dict(pv.get("media"), f"providers.{key}.media")
            base_media = _as_dict(base_p.get("media"), f"providers.{key}.media")
            models = []
            # 第二十五轮: 模型项要「以文件原文为底」再覆盖面板管得着的键。
            #   面板的模型编辑器只认 id/display_name/type/vision 四个字段，若照着它重建，
            #   手写在 YAML 里的 models[].params（模型级强制参数，如 temperature: 1）
            #   会被**静默清掉** —— 管理员只是在页面上改了别的东西，上游硬约束就没了，
            #   之后又变成"这个模型怎么老是 400"。按 id 对齐原文、保留其余键即可。
            base_models: dict[str, dict[str, Any]] = {}
            for bm in (base_p.get("models") or []):
                if isinstance(bm, dict) and str(bm.get("id") or "").strip():
                    base_models[str(bm["id"]).strip()] = bm
            for i, m in enumerate(pv.get("models") or []):
                m = _as_dict(m, f"providers.{key}.models[{i}]")
                mid = str(m.get("id") or "").strip()
                item = dict(base_models.get(mid) or {})
                item.update({
                    "id": mid,
                    "display_name": str(m.get("display_name") or "").strip(),
                    "type": str(m.get("type") or "text").strip(),
                    "vision": bool(m.get("vision", False)),
                })
                # 第二十七轮: 协议线由面板管理（每个模型的格式标签），提交了就以此为准；
                #   没提交则沿用文件原文里的值（避免旧前端/脚本提交时把 protocols 清空）。
                if m.get("protocols"):
                    item["protocols"] = _parse_protocols_view(
                        m.get("protocols"), f"providers.{key}.models[{i}].protocols")
                models.append(item)
            providers[key] = {
                "name": str(pv.get("name") or key).strip(),
                "base_url": str(pv.get("base_url") or "").strip(),
                "auth": {
                    "token_env": str(auth_in.get("token_env") or "").strip(),
                    "token": token,
                },
                "extra_headers": _str_map(pv.get("extra_headers"), f"providers.{key}.extra_headers"),
                "rate_limit_rps": pv.get("rate_limit_rps"),
                "requires_session_header": bool(pv.get("requires_session_header", False)),
                # 第二十七轮: 协议线的出站配置（路径/鉴权/固定头）。
                #   仍然以**文件原文为底**：面板没有这段的编辑器，而它一旦丢失，
                #   声明了 messages/responses 的模型会直接变成"供应商没配这条线"。
                "endpoints": base_p.get("endpoints", _default_endpoints_view()),
                # media 留空 = 该供应商不支持对应媒体（写回时保留原文里的空 {} 语义）
                "media": {
                    k: (media_in.get(k) if k in media_in else base_media.get(k))
                    for k in MEDIA_KEYS
                },
                "models": models,
            }
        out["providers"] = providers

    # ── server（逐键覆盖，缺的沿用原文）──
    if "server" in view:
        sv = _as_dict(view.get("server"), "server")
        base_s = _as_dict(out.get("server"), "server")
        out["server"] = {**base_s, **{k: v for k, v in sv.items() if k in _SERVER_KEYS}}

    # ── auth ──
    if "auth" in view:
        av = _as_dict(view.get("auth"), "auth")
        base_a = _as_dict(out.get("auth"), "auth")
        merged = {**base_a}
        if "session_ttl_minutes" in av:
            merged["session_ttl_minutes"] = av["session_ttl_minutes"]
        out["auth"] = merged

    # ── circuit / observability ──
    for sec, keys in (("circuit", _CIRCUIT_KEYS), ("observability", _OBS_KEYS)):
        if sec in view:
            vv = _as_dict(view.get(sec), sec)
            out[sec] = {k: vv[k] for k in keys if k in vv} or _as_dict(out.get(sec), sec)

    # ── pricing.per_model（整表以面板为准）──
    if "pricing" in view:
        pv = _as_dict(view.get("pricing"), "pricing")
        per_model: dict[str, Any] = {}
        for model, price in _as_dict(pv.get("per_model"), "pricing.per_model").items():
            name = str(model).strip()
            if not name:
                continue
            price = _as_dict(price, f"pricing.per_model.{name}")
            per_model[name] = {"input": price.get("input"), "output": price.get("output")}
        out["pricing"] = {"per_model": per_model}

    # ── download ──
    if "download" in view:
        dv = _as_dict(view.get("download"), "download")
        base_d = _as_dict(out.get("download"), "download")
        merged_d = {**base_d}
        if "allowed_hosts" in dv:
            merged_d["allowed_hosts"] = _str_list(dv.get("allowed_hosts"), "download.allowed_hosts")
        if "max_bytes" in dv:
            merged_d["max_bytes"] = dv["max_bytes"]
        out["download"] = merged_d

    return out


_SERVER_KEYS = (
    "host", "port", "timeout", "retry", "health_auth", "hot_reload", "hot_reload_interval",
    "metrics_enabled", "rate_limit_rps", "max_body_size", "cors_origins", "graceful_shutdown",
    "batch_max_concurrency", "batch_max_items", "webhook_allow_private", "docs_enabled",
    "auth_fail_limit_per_min",
)
_CIRCUIT_KEYS = ("enabled", "failure_threshold", "cooldown", "window")
_OBS_KEYS = ("log_level", "log_json", "access_log_maxlen")


def parse_and_validate(raw: dict[str, Any]) -> tuple[GatewayConfig, list[str]]:
    """解析 + 结构校验 + 收集告警。返回 (配置, 告警列表)。

    校验不通过抛 `ConfigError`（调用方据此 400，**不写盘**）。
    告警不阻塞保存（比如 cors 还是通配），但会原样回给页面提示。
    """
    cfg = parse_config(raw)                 # 类型/必填/下限都在这里把关
    issues = validate_config(cfg)
    if issues:
        raise ConfigError("配置校验失败:\n  - " + "\n  - ".join(issues))
    return cfg, validate_warnings(cfg)


def restart_keys_changed(old: GatewayConfig, new: GatewayConfig) -> list[str]:
    """哪些"需重启"的键这次真的变了（页面据此提示，不变就不吓人）。"""
    changed: list[str] = []
    for key in RESTART_KEYS:
        _, field = key.split(".", 1)
        if getattr(old.server, field) != getattr(new.server, field):
            changed.append(key)
    return changed


# ── 渲染 yaml 文本 ────────────────────────────────────────
_HEADER = """\
# AI 网关配置文件（data/ 目录）
#
# ⚠ 本文件由管理页面「网关配置」生成（管理员账号），页面保存时会**整份重写**：
#   想要的历史决策/踩坑说明在 README.md（按轮次记录），这里只保留必要的现场注释。
#   手改本文件仍然有效（server.hot_reload=true 时按 mtime 自动重载），
#   但页面下次保存会以页面上的值为准 —— 改配置优先用页面。
#
# 端口默认 8808；密钥统一走 auth.token_env 指向的环境变量（本机见 .env），不在 yaml 存明文。
# 本文件是项目的唯一配置文件（run.py 默认读取；GATEWAY_CONFIG 可覆盖）。
"""

_SECTION_COMMENTS = {
    "providers": """\
# ── 供应商 + 模型清单 ───────────────────────────────────
# auth.token_env 指向存放密钥的环境变量；auth.token 只有在不用环境变量时才填（明文）。
# media 段留空 = 该供应商不支持图像/视频（Agnes 是自有非标准端点，故单独覆盖）。
# models.type: text | image | video；vision: 该模型是否支持图像输入（带图对话会被优先排到链首）。
""",
    "server": """\
# ── 服务与运维开关 ──────────────────────────────────────
# host/port/graceful_shutdown/docs_enabled/hot_reload 这几项**改完要重启进程**才生效，
# 其余每请求读取，页面保存即生效。
# 安全相关：health_auth（/health/* 是否鉴权）、webhook_allow_private（视频回调是否允许内网地址）、
#   docs_enabled（FastAPI 文档，默认关）、auth_fail_limit_per_min（同源 IP 每分钟鉴权失败上限）。
# cors_origins 写 ["*"] 表示任意站点都能跨域驱动本网关（会把上游额度暴露给陌生网页），
#   生产建议收敛成可信前端域名白名单。
""",
    "auth": """\
# ── 网关入口鉴权 ────────────────────────────────────────
# 账号与 API Key 由页面管理，存在 data/users.json（见 app/users.py）：
#   管理员在「用户管理」建账号，各账号在「设置 → 我的 API Key」自助铸 Key。
# 本文件里没有任何用户/密钥，只有会话时长这一个开关。
""",
    "circuit": """\
# ── 供应商熔断 ──────────────────────────────────────────
# 某供应商在 window 秒内失败达到 failure_threshold 次就熔断 cooldown 秒，
# 期间直接跳过它（不再白等），让 fallback 快速切到健康供应商。
""",
    "observability": """\
# ── 可观测性 ────────────────────────────────────────────
# log_level 支持 DEBUG/INFO/WARNING/ERROR；log_json=true 输出结构化 JSON 行（接 ELK）。
# access_log_maxlen = 访问日志环形缓冲长度（日志页「已加载 N 条」的上限）。
""",
    "pricing": """\
# ── 定价（人民币 / 百万 token，用于 /v1 响应里估算成本） ─
# 不配则该模型不返回 estimated_cost_cny（不影响调用）。
""",
    "download": """\
# ── 媒体下载代理白名单 ──────────────────────────────────
# 生成的图片/视频挂在第三方 CDN 上且不带 CORS 头，浏览器 fetch 不到 blob、跨域 <a download>
# 也不生效 —— 所以由网关代拉一次并加 Content-Disposition: attachment。
# 安全：这是全网关唯一一个服务端主动出网的接口，只有这里列出的域名（含子域）允许被代下载，
#   且解析出的 IP 必须是公网地址；allowed_hosts 为空 = 该功能整体关闭。
#   换了供应商/CDN 域名时，往这里加一条即可（如 volc 的 tos-*.volces.com）。
""",
}

_SECTION_ORDER = ("providers", "server", "circuit", "observability", "pricing", "download", "auth")


def _dump(obj: Any) -> str:
    """稳定的 block 风格 dump（不排序键，保留插入顺序 = 面板里的顺序）。"""
    return yaml.safe_dump(
        obj,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=1000,          # 别把长 base_url 折行（折了也能读，但难看）
        indent=2,
    )


def render_yaml(raw: dict[str, Any]) -> str:
    """生成带注释的 yaml 文本（注释见 _SECTION_COMMENTS，改注释改这里）。"""
    parts: list[str] = [_HEADER]
    known = set(_SECTION_ORDER)
    for sec in _SECTION_ORDER:
        if sec not in raw:
            continue
        parts.append(_SECTION_COMMENTS.get(sec, ""))
        parts.append(_dump({sec: raw[sec]}))
    # 文件里多出来的、面板不管的段（人工加的）原样保留在末尾，不静默丢掉
    extra = {k: v for k, v in raw.items() if k not in known}
    if extra:
        parts.append("# ── 以下是面板不管理的自定义配置（原样保留）──\n")
        parts.append(_dump(extra))
    text = "".join(p for p in parts if p)
    return text if text.endswith("\n") else text + "\n"


# ── 写盘（备份 + 原子替换）────────────────────────────────
def _prune_backups(path: Path) -> None:
    """只留最近 BACKUP_KEEP 份时间戳备份（滚动 .bak 不动）。"""
    pat = f"{path.name}.*.bak"
    cands = sorted(path.parent.glob(pat))
    for old in cands[:-BACKUP_KEEP] if len(cands) > BACKUP_KEEP else []:
        try:
            old.unlink()
        except OSError:
            pass


def write_config(path: str | Path, text: str) -> dict[str, Any]:
    """把配置文本写盘：先备份原文，再 tmp + os.replace 原子替换。

    原子替换是必须的：`server.hot_reload` 的循环按 mtime 触发重载，
    直接覆盖写会让它有机会读到半份文件 → 重载失败（旧配置继续跑，但白报一次错）。

    权限 600：配置里可能被填了明文 token（正常应走 token_env），别让它可读给同机其它用户。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    bak_path = path.with_name(path.name + ".bak")
    stamp_path = path.with_name(f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}.bak")
    if path.exists():
        shutil.copy2(path, bak_path)
        shutil.copy2(path, stamp_path)
        _prune_backups(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return {"path": str(path), "backup": str(bak_path) if bak_path.exists() else "", "bytes": len(text.encode("utf-8"))}


def read_raw(path: str | Path) -> dict[str, Any]:
    """读文件原文（缺失/空文件 → {}），供 view_to_raw 当"底"。"""
    path = Path(path)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}
