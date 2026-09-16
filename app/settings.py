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
    "video_style",
    "video_default_size",
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
            "media": {k: getattr(p.media, k) for k in MEDIA_KEYS},
            "models": [
                {"id": m.id, "display_name": m.display_name, "type": m.type, "vision": bool(m.vision)}
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
        "routes": {
            rname: {
                "models": list(r.models or []),
                "timeout": r.timeout,
                "retry": r.retry,
                "rate_limit_rps": r.rate_limit_rps,
            }
            for rname, r in cfg.routes.items()
        },
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
            for i, m in enumerate(pv.get("models") or []):
                m = _as_dict(m, f"providers.{key}.models[{i}]")
                models.append({
                    "id": str(m.get("id") or "").strip(),
                    "display_name": str(m.get("display_name") or "").strip(),
                    "type": str(m.get("type") or "text").strip(),
                    "vision": bool(m.get("vision", False)),
                })
            providers[key] = {
                "name": str(pv.get("name") or key).strip(),
                "base_url": str(pv.get("base_url") or "").strip(),
                "auth": {
                    "token_env": str(auth_in.get("token_env") or "").strip(),
                    "token": token,
                },
                "extra_headers": _str_map(pv.get("extra_headers"), f"providers.{key}.extra_headers"),
                "rate_limit_rps": pv.get("rate_limit_rps"),
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

    # ── routes（整段以面板为准：删掉的路由别名就是删掉）──
    if "routes" in view:
        rv = _as_dict(view.get("routes"), "routes")
        routes: dict[str, Any] = {}
        for rname, rv1 in rv.items():
            name = str(rname).strip()
            if not name:
                raise ConfigError("路由别名的名字不能为空")
            rv1 = _as_dict(rv1, f"routes.{name}")
            routes[name] = {
                "models": _str_list(rv1.get("models"), f"routes.{name}.models"),
                "timeout": rv1.get("timeout"),
                "retry": rv1.get("retry"),
                "rate_limit_rps": rv1.get("rate_limit_rps"),
            }
        out["routes"] = routes

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
    "routes": """\
# ── 路由别名 + 按优先级 fallback ────────────────────────
# model 填别名（如 wjd）时按 models 数组顺序逐个尝试：第 1 个失败/超时/重试耗尽 → 第 2 个 …
# 全失败才 502。timeout = 单模型超时秒（null 用 server.timeout），retry = 切下一个前该模型重试次数。
# 数组顺序即优先级（第 0 个最高）。可用模型 = 上面 providers 里定义的全部模型 id。
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

_SECTION_ORDER = ("providers", "server", "circuit", "observability", "pricing", "download", "auth", "routes")


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
