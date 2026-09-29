"""智能体（Agent）定义：存储 + 校验（第三十八轮，路线 A）。

背景与设计见 `docs/agents-dev-plan.md`。要点：

- **路线 A**：界面在网关，引擎是 QwenPaw（独立 profile `ai-gateway`）。
- **甲案**：模型是 profile 级的（请求级指定无效，已实测），而所有智能体共用
  `ai-gateway` 一个 profile ⇒ **所有智能体共用同一个模型**。所以这里**不存 model**。
- 智能体之间的区别只剩：名称/描述 + 参数（temperature/max_tokens/top_p/system_suffix）
  + 各自的会话历史。
- 提示词归 QwenPaw 管（决策②），这里**不存 system_prompt** 主提示词；
  `params.system_suffix` 只是一小条角色补充（网关侧注入）。

存储：`data/agents.yaml`（600、原子写、mtime 热载）。**配置无秘密，可入库**。
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ── 常量 ──────────────────────────────────────────────────────────────
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "agents.yaml"

#: slug 规则：小写字母开头，允许小写字母/数字/连字符，2~32 位
_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")

#: 智能体级参数白名单（决策 ㉗）。
#: 只放"采样/长度类"和一小条角色后缀 —— 结构性键（model/messages/stream/tools）刻意排除，
#: 防止用户从这儿绕过网关往上游塞结构。
AGENT_PARAM_KEYS: frozenset[str] = frozenset({
    "temperature", "max_tokens", "top_p", "system_suffix",
})

#: system_suffix 长度上限（字符）。它是"角色微调"，不是主提示词通道 ——
#: 要写长提示词请去 QwenPaw 建 profile（决策 ㉖ 的边界）。
SYSTEM_SUFFIX_MAX = 2000

#: 未归档智能体的数量上限（决策 ⑫，软上限）。
DEFAULT_MAX_AGENTS = 5

#: 允许的访问角色（二期下发用；一期固定 admin）。
ALLOWED_ROLES = frozenset({"admin", "member"})

#: 允许的状态。
ALLOWED_STATUS = frozenset({"draft", "published", "retired"})

#: 引擎类型（一期只有 qwenpaw 一种）。
ENGINE_TYPES = frozenset({"qwenpaw"})

#: 默认引擎地址（QwenPaw 本机，免鉴权）。
DEFAULT_ENGINE_URL = "http://127.0.0.1:8088"

#: 默认引擎 profile id。
DEFAULT_ENGINE_AGENT = "ai-gateway"


class AgentsError(ValueError):
    """智能体配置校验失败（映射为 400）。"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ── 校验辅助 ──────────────────────────────────────────────────────────
def _require_str(value: Any, where: str, *, maxlen: int = 200) -> str:
    if not isinstance(value, str):
        raise AgentsError(f"{where} 必须是字符串")
    v = value.strip()
    if not v:
        raise AgentsError(f"{where} 不能为空")
    if len(v) > maxlen:
        raise AgentsError(f"{where} 过长（上限 {maxlen} 字符）")
    return v


def validate_slug(slug: Any) -> str:
    """校验 slug 形态（唯一性由调用方另行校验）。"""
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise AgentsError(
            "slug 只能是小写字母开头、由小写字母/数字/连字符组成，长度 2~32（如 code-review）"
        )
    return slug


def validate_params(params: Any) -> dict[str, Any]:
    """校验智能体级参数：只认白名单，类型不对就报错（不静默忽略）。"""
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise AgentsError("params 必须是映射（键值对）")
    out: dict[str, Any] = {}
    for k, v in params.items():
        if k not in AGENT_PARAM_KEYS:
            raise AgentsError(
                f"params.{k} 不是允许的智能体参数（允许：{', '.join(sorted(AGENT_PARAM_KEYS))}）"
            )
        if k == "system_suffix":
            if not isinstance(v, str):
                raise AgentsError("params.system_suffix 必须是字符串")
            if len(v) > SYSTEM_SUFFIX_MAX:
                raise AgentsError(
                    f"params.system_suffix 过长（上限 {SYSTEM_SUFFIX_MAX} 字符）——"
                    "它是角色补充，主提示词请到 QwenPaw 的 profile 里改"
                )
            out[k] = v.strip()
            continue
        # temperature / top_p：浮点；max_tokens：整数
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise AgentsError(f"params.{k} 必须是数字")
        if k == "max_tokens":
            iv = int(v)
            if iv <= 0:
                raise AgentsError("params.max_tokens 必须大于 0")
            out[k] = iv
        else:
            fv = float(v)
            if k == "temperature" and not (0.0 <= fv <= 2.0):
                raise AgentsError("params.temperature 取值范围 0~2")
            if k == "top_p" and not (0.0 < fv <= 1.0):
                raise AgentsError("params.top_p 取值范围 (0, 1]")
            out[k] = fv
    return out


# ── 数据模型 ──────────────────────────────────────────────────────────
@dataclass
class Agent:
    slug: str
    display_name: str
    description: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    engine_type: str = "qwenpaw"
    engine_base_url: str = DEFAULT_ENGINE_URL
    engine_agent_id: str = DEFAULT_ENGINE_AGENT
    access_roles: list[str] = field(default_factory=lambda: ["admin"])
    access_users: list[str] = field(default_factory=list)
    status: str = "published"
    created_at: str = ""
    updated_at: str = ""

    # ── 序列化 ──
    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "display_name": self.display_name,
            "description": self.description,
            "params": dict(self.params),
            "engine": {
                "type": self.engine_type,
                "base_url": self.engine_base_url,
                "agent_id": self.engine_agent_id,
            },
            "access": {"roles": list(self.access_roles), "users": list(self.access_users)},
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def public_dict(self) -> dict[str, Any]:
        """给前端列表/详情用的字段（含引擎实况位，由调用方补）。

        注意：**不含** system_suffix 之外的敏感内容 —— 由路由层按角色再裁。
        """
        d = self.to_dict()
        d["engine_type"] = self.engine_type
        d["access_scope"] = "admin" if self.access_roles == ["admin"] and not self.access_users else "custom"
        return d


def _parse_agent(slug: str, raw: Any) -> Agent:
    """从 YAML 的一段解析出一个 Agent（严格校验，报错就抛）。"""
    where = f"agents.{slug}"
    validate_slug(slug)
    if not isinstance(raw, dict):
        raise AgentsError(f"{where} 必须是一个映射")

    # 不接受别名之外的未知键（防拼错后静默失效）
    known = {"display_name", "name", "description", "params", "engine",
             "access", "status", "created_at", "updated_at"}
    unknown = set(raw) - known
    if unknown:
        raise AgentsError(f"{where} 含未知字段：{', '.join(sorted(unknown))}")

    display_name = _require_str(
        raw.get("display_name") or raw.get("name"), f"{where}.display_name", maxlen=60
    )
    description = raw.get("description") or ""
    if not isinstance(description, str):
        raise AgentsError(f"{where}.description 必须是字符串")
    description = description.strip()[:200]

    params = validate_params(raw.get("params"))

    eng = raw.get("engine") or {}
    if not isinstance(eng, dict):
        raise AgentsError(f"{where}.engine 必须是映射")
    etype = eng.get("type", "qwenpaw")
    if etype not in ENGINE_TYPES:
        raise AgentsError(
            f"{where}.engine.type 只支持 {'/'.join(sorted(ENGINE_TYPES))}（当前：{etype}）"
        )
    base_url = _require_str(eng.get("base_url") or DEFAULT_ENGINE_URL,
                            f"{where}.engine.base_url", maxlen=200)
    agent_id = _require_str(eng.get("agent_id") or DEFAULT_ENGINE_AGENT,
                            f"{where}.engine.agent_id", maxlen=64)

    acc = raw.get("access") or {}
    if not isinstance(acc, dict):
        raise AgentsError(f"{where}.access 必须是映射")
    roles = acc.get("roles") or ["admin"]
    users = acc.get("users") or []
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise AgentsError(f"{where}.access.roles 必须是字符串列表")
    bad = set(roles) - ALLOWED_ROLES
    if bad:
        raise AgentsError(
            f"{where}.access.roles 含非法角色：{', '.join(sorted(bad))}"
            f"（允许：{', '.join(sorted(ALLOWED_ROLES))}）"
        )
    if not isinstance(users, list) or not all(isinstance(u, str) for u in users):
        raise AgentsError(f"{where}.access.users 必须是字符串列表")

    status = raw.get("status", "published")
    if status not in ALLOWED_STATUS:
        raise AgentsError(
            f"{where}.status 非法：{status}（允许：{', '.join(sorted(ALLOWED_STATUS))}）"
        )

    return Agent(
        slug=slug,
        display_name=display_name,
        description=description,
        params=params,
        engine_type=etype,
        engine_base_url=base_url,
        engine_agent_id=agent_id,
        access_roles=list(roles),
        access_users=list(u.strip() for u in users if u.strip()),
        status=status,
        created_at=str(raw.get("created_at") or ""),
        updated_at=str(raw.get("updated_at") or ""),
    )


# ── 存储 ──────────────────────────────────────────────────────────────
class AgentStore:
    """`data/agents.yaml` 的读写与校验。

    - 原子写（临时文件 + rename）+ 备份（复用 settings.write_config 的语义）
    - mtime 热载：文件被改后自动重读，不必重启
    - 线程锁：读写都加锁，单进程够用
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_PATH
        self._lock = threading.RLock()
        self._agents: dict[str, Agent] = {}
        self._max_agents: int = DEFAULT_MAX_AGENTS
        self._mtime: float = 0.0
        self._load()

    # ── 读 ──
    def _load(self, *, force: bool = False) -> None:
        with self._lock:
            if not self.path.exists():
                self._agents, self._max_agents = {}, DEFAULT_MAX_AGENTS
                self._mtime = 0.0
                return
            mtime = self.path.stat().st_mtime
            if not force and mtime == self._mtime:
                return
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise AgentsError("agents.yaml 顶层必须是映射")
            unknown = set(raw) - {"max_agents", "agents", "version"}
            if unknown:
                raise AgentsError(f"agents.yaml 含未知顶层键：{', '.join(sorted(unknown))}")
            mx = raw.get("max_agents", DEFAULT_MAX_AGENTS)
            if isinstance(mx, bool) or not isinstance(mx, int) or mx < 1:
                raise AgentsError("max_agents 必须是 ≥1 的整数")
            items = raw.get("agents") or {}
            if not isinstance(items, dict):
                raise AgentsError("agents 段必须是映射（slug → 定义）")
            agents: dict[str, Agent] = {}
            for slug, body in items.items():
                agents[str(slug)] = _parse_agent(str(slug), body)
            self._agents = agents
            self._max_agents = int(mx)
            self._mtime = mtime

    def reload_if_changed(self) -> bool:
        """文件变了就重读（供每请求或热载循环调用）。返回是否发生了重载。"""
        try:
            if self.path.exists() and self.path.stat().st_mtime != self._mtime:
                self._load(force=True)
                return True
        except FileNotFoundError:
            pass
        return False

    # ── 查询 ──
    @property
    def max_agents(self) -> int:
        with self._lock:
            return self._max_agents

    def list(self, *, include_retired: bool = False) -> list[Agent]:
        with self._lock:
            out = [a for a in self._agents.values()
                   if include_retired or a.status != "retired"]
            out.sort(key=lambda a: (a.status == "retired", a.created_at or "", a.slug))
            return out

    def get(self, slug: str) -> Agent | None:
        with self._lock:
            return self._agents.get(slug)

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for a in self._agents.values() if a.status != "retired")

    def slug_conflicts(self, slug: str, *, model_ids: list[str] | None = None,
                       alias_names: list[str] | None = None,
                       profile_ids: list[str] | None = None) -> str | None:
        """返回冲突说明；无冲突返回 None。

        三重查重（计划 Step 1）：不能撞已有智能体 slug、模型 id、别名、QwenPaw profile id ——
        因为智能体以后可能以 `agent/<slug>` 形式暴露给客户端，撞名会混。
        """
        if slug in self._agents:
            return f"slug「{slug}」已被占用"
        if model_ids and slug in model_ids:
            return f"slug「{slug}」与模型 id 冲突"
        if alias_names and slug in alias_names:
            return f"slug「{slug}」与虚拟模型别名冲突"
        if profile_ids and slug in profile_ids:
            return f"slug「{slug}」与 QwenPaw 智能体 profile 冲突"
        return None

    # ── 写 ──
    def _dump(self) -> str:
        """渲染成 YAML 文本（顶层 max_agents + agents 段）。"""
        body: dict[str, Any] = {"max_agents": self._max_agents, "agents": {}}
        for slug in sorted(self._agents):
            a = self._agents[slug]
            body["agents"][slug] = {
                "display_name": a.display_name,
                "description": a.description,
                "params": dict(a.params),
                "engine": {
                    "type": a.engine_type,
                    "base_url": a.engine_base_url,
                    "agent_id": a.engine_agent_id,
                },
                "access": {"roles": list(a.access_roles), "users": list(a.access_users)},
                "status": a.status,
                "created_at": a.created_at,
                "updated_at": a.updated_at,
            }
        head = (
            "# 智能体定义（第三十八轮 · 路线 A：界面在网关，引擎是 QwenPaw）\n"
            "#\n"
            "# 说明：\n"
            "#   · 所有智能体共用 QwenPaw 的同一个 profile（engine.agent_id），\n"
            "#     也共用同一个模型 —— 模型是 profile 级设置（请求级指定无效，已实测）。\n"
            "#   · 提示词主档归 QwenPaw 管（改 SOUL.md / AGENTS.md）；\n"
            "#     这里的 params.system_suffix 只是一小条角色补充。\n"
            "#   · 本文件不含秘密，可入版本库。\n"
            "#   · 归档（status: retired）不计入 max_agents 上限。\n"
        )
        return head + yaml.safe_dump(body, allow_unicode=True, sort_keys=False, width=100)

    def _write(self, text: str) -> None:
        """原子写 + 备份（逐字节替换，权限 600）。"""
        p = self.path
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            try:
                import shutil
                shutil.copy2(p, p.with_name(p.name + ".bak"))
            except OSError:
                pass
        tmp = p.with_name(p.name + f".tmp{os.getpid()}")
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
        self._mtime = p.stat().st_mtime

    def _commit(self) -> None:
        self._write(self._dump())

    def create(self, *, slug: str, display_name: str, description: str = "",
               params: dict[str, Any] | None = None,
               access_roles: list[str] | None = None,
               access_users: list[str] | None = None,
               engine_base_url: str | None = None,
               engine_agent_id: str | None = None,
               status: str = "published",
               model_ids: list[str] | None = None,
               alias_names: list[str] | None = None,
               profile_ids: list[str] | None = None) -> Agent:
        with self._lock:
            self._load()   # 先取最新盘上状态
            validate_slug(slug)
            conflict = self.slug_conflicts(
                slug, model_ids=model_ids, alias_names=alias_names, profile_ids=profile_ids
            )
            if conflict:
                raise AgentsError(conflict)
            if self.active_count() >= self._max_agents:
                raise AgentsError(
                    f"智能体数量已达上限（{self._max_agents} 个）——请先归档不用的再新建"
                )
            a = _parse_agent(slug, {
                "display_name": display_name,
                "description": description,
                "params": params or {},
                "engine": {
                    "type": "qwenpaw",
                    "base_url": engine_base_url or DEFAULT_ENGINE_URL,
                    "agent_id": engine_agent_id or DEFAULT_ENGINE_AGENT,
                },
                "access": {"roles": access_roles or ["admin"], "users": access_users or []},
                "status": status,
                "created_at": _now(),
                "updated_at": _now(),
            })
            self._agents[slug] = a
            self._commit()
            return a

    #: 允许通过 update 改的字段（其余字段一期只读，见计划）
    UPDATABLE = frozenset({"display_name", "description", "params", "status"})

    def update(self, slug: str, patch: dict[str, Any]) -> Agent:
        with self._lock:
            self._load()
            a = self._agents.get(slug)
            if a is None:
                raise AgentsError(f"智能体「{slug}」不存在")
            unknown = set(patch) - self.UPDATABLE
            if unknown:
                raise AgentsError(
                    f"字段不可改：{', '.join(sorted(unknown))}"
                    f"（一期可改：{', '.join(sorted(self.UPDATABLE))}）"
                )
            # 逐项覆盖到"可被 _parse_agent 接受的形状"（不含 slug —— 它是键不是字段）
            merged: dict[str, Any] = {
                "display_name": patch.get("display_name", a.display_name),
                "description": patch.get("description", a.description),
                "params": patch.get("params", dict(a.params)),
                "engine": {
                    "type": a.engine_type,
                    "base_url": a.engine_base_url,
                    "agent_id": a.engine_agent_id,
                },
                "access": {"roles": list(a.access_roles), "users": list(a.access_users)},
                "status": patch.get("status", a.status),
                "created_at": a.created_at,
                "updated_at": _now(),
            }
            new = _parse_agent(slug, merged)
            self._agents[slug] = new
            self._commit()
            return new

    def retire(self, slug: str) -> Agent:
        """归档（不物理删 —— 项目惯例：归档优于删除）。"""
        with self._lock:
            self._load()
            a = self._agents.get(slug)
            if a is None:
                raise AgentsError(f"智能体「{slug}」不存在")
            a.status = "retired"
            a.updated_at = _now()
            self._commit()
            return a

    def restore(self, slug: str) -> Agent:
        """从归档恢复（决策 ⑰）。"""
        with self._lock:
            self._load()
            a = self._agents.get(slug)
            if a is None:
                raise AgentsError(f"智能体「{slug}」不存在")
            if a.status != "retired":
                raise AgentsError(f"智能体「{slug}」未归档，无需恢复")
            if self.active_count() >= self._max_agents:
                raise AgentsError(f"智能体数量已达上限（{self._max_agents}），无法恢复")
            a.status = "published"
            a.updated_at = _now()
            self._commit()
            return a


#: 进程级单例（延迟到首次用时构造，方便测试换路径）
_store: AgentStore | None = None
_store_lock = threading.Lock()


def get_store() -> AgentStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = AgentStore(os.environ.get("GATEWAY_AGENTS_PATH"))
        return _store


def set_store(store: AgentStore | None) -> None:
    """测试用：替换/重置单例。"""
    global _store
    with _store_lock:
        _store = store
