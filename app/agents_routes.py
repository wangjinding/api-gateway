"""智能体路由 + 运行记录（第三十八轮 · 路线 A）。

设计见 `docs/agents-dev-plan.md`；SSE 协议见 `docs/qwenpaw-agent-api-sse.md`。

**为什么单开一个文件**：`main.py` 已经 3800+ 行。智能体是一块相对独立的特性，
把端点实现放这里、由 `main.py` 装配（`install(app, deps)`），比继续往 main 里塞更清楚。

会话列表**复用** `/history/{kind}`（`kind = agent_<slug>`），见 `app/history.py`。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Awaitable

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.agents import (
    DEFAULT_ENGINE_AGENT,
    DEFAULT_ENGINE_URL,
    Agent,
    AgentStore,
    AgentsError,
)
from app.adapters.qwenpaw_engine import check_engine, stream_chat

log = logging.getLogger("api-gateway.agents")

QWENPAW_CONFIG = Path("/root/.qwenpaw/config.json")


# ── 运行记录（data/agent_runs.json）────────────────────────────────────
class AgentRuns:
    """每轮对话一条记录 + 每智能体的汇总位。

    结构：`{"<slug>": {"last_used_at": str, "total": int, "items": [...]}}`
    条目：`{at, account, rounds, tools, tokens, ms, status}`（新→旧）

    写入失败只告警 —— 记录不该影响对话本身。
    """

    MAX_ITEMS = 200

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else \
            Path(__file__).resolve().parent.parent / "data" / "agent_runs.json"
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            log.warning("读 agent_runs.json 失败，按空处理")
        return {}

    def _write(self, data: dict) -> None:
        try:
            tmp = self.path.with_name(self.path.name + f".tmp{os.getpid()}")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001
            log.warning("写 agent_runs.json 失败（忽略）")

    def load(self) -> dict:
        with self._lock:
            return self._read()

    def record(self, slug: str, *, account: str, rounds: int, tools: int,
               tokens: int, ms: int, status: str) -> None:
        with self._lock:
            data = self._read()
            bucket = data.setdefault(slug, {"last_used_at": "", "total": 0, "items": []})
            bucket["last_used_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            bucket["total"] = int(bucket.get("total") or 0) + 1
            items = bucket.setdefault("items", [])
            items.insert(0, {
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "account": account, "rounds": rounds, "tools": tools,
                "tokens": tokens, "ms": ms, "status": status,
            })
            del items[self.MAX_ITEMS:]
            self._write(data)


#: 模块级单例（main.py 装配时复用）
agent_runs = AgentRuns()

#: 引擎实况缓存：{model, ready, checked_at}。列表页顺带刷新，避免每请求都打引擎。
engine_live: dict[str, Any] = {"model": "", "ready": False, "checked_at": 0.0}
ENGINE_LIVE_TTL = 20.0     # 秒；这段时间内不重复探


# ── 小工具 ────────────────────────────────────────────────────────────
def _qwenpaw_profile_ids() -> list[str]:
    """QwenPaw 的 profile 清单（slug 不能与它们撞）。读不到就返回空（降级不阻塞）。"""
    try:
        if QWENPAW_CONFIG.exists():
            d = json.loads(QWENPAW_CONFIG.read_text(encoding="utf-8"))
            return list(((d.get("agents") or {}).get("profiles") or {}).keys())
    except Exception:  # noqa: BLE001
        log.debug("读 QwenPaw profile 清单失败（slug 查重降级）")
    return []


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


async def refresh_engine_live(store: AgentStore, *, force: bool = False) -> None:
    """刷新"引擎当前模型 + 是否就绪"（带 TTL，避免每请求都打引擎）。"""
    now = time.time()
    if not force and (now - float(engine_live.get("checked_at") or 0)) < ENGINE_LIVE_TTL:
        return
    agents = store.list()
    base = agents[0].engine_base_url if agents else DEFAULT_ENGINE_URL
    agent_id = agents[0].engine_agent_id if agents else DEFAULT_ENGINE_AGENT
    ok, _why = await check_engine(base)
    model = ""
    if ok:
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(
                    base.rstrip("/") + "/api/providers/active",
                    params={"scope": "agent", "agent_id": agent_id},
                    headers={"X-Agent-Id": agent_id},
                )
                if r.status_code < 400:
                    slot = (r.json() or {}).get("active_llm") or {}
                    model = slot.get("model") or ""
        except Exception:  # noqa: BLE001
            log.debug("读引擎 active_model 失败")
    engine_live.update({"model": model, "ready": ok, "checked_at": now})


def can_use_agent(a: Agent, role: str) -> bool:
    """能否看/用这个智能体 —— 细粒度判定的**唯一入口**（二期下发靠它）。"""
    if role == "admin":
        return True
    if "admin" in a.access_roles and not a.access_users:
        return False          # 标了"仅管理员"的，member 不可见
    return role in a.access_roles


def install(app: FastAPI, *, store: AgentStore,
            require_auth: Callable[..., Any],
            require_admin: Callable[[Request], str],
            json_body: Callable[[Request], Awaitable[dict]],
            model_index: Callable[[], tuple[list[str], list[str]]],
            history_get: Callable[[str, str], list[dict]],
            ) -> None:
    """把智能体端点挂到 app 上（依赖由 main.py 注入，避免循环 import）。"""

    def _agent_or_404(slug: str) -> Agent:
        store.reload_if_changed()
        a = store.get(slug)
        if a is None or a.status == "retired":
            raise HTTPException(404, f"智能体「{slug}」不存在")
        return a

    def _view(a: Agent) -> dict:
        d = a.public_dict()
        bucket = agent_runs.load().get(a.slug) or {}
        d["last_used_at"] = bucket.get("last_used_at") or ""
        d["run_total"] = int(bucket.get("total") or 0)
        d["session_count"] = 0        # 按需再算（避免列表页为每个智能体读一次 history）
        d["engine_model"] = engine_live.get("model", "")
        d["engine_ready"] = bool(engine_live.get("ready"))
        return d

    # ── 列表 ──
    @app.get("/agents")
    async def agents_list(request: Request, include_retired: bool = False,
                          _auth=Depends(require_auth)):
        store.reload_if_changed()
        role = request.state.role
        if role == "admin":
            await refresh_engine_live(store)
            items = [_view(a) for a in store.list(include_retired=include_retired)]
        else:
            # member 只见 4 个字段（决策 ⑦）
            items = [{"slug": a.slug, "display_name": a.display_name,
                      "description": a.description, "status": a.status}
                     for a in store.list(include_retired=include_retired)
                     if can_use_agent(a, role)]
        agents = store.list()
        return {
            "agents": items,
            "max_agents": store.max_agents,
            "active": store.active_count(),
            "engine": {
                "model": engine_live.get("model", ""),
                "ready": bool(engine_live.get("ready")),
                "base_url": agents[0].engine_base_url if agents else DEFAULT_ENGINE_URL,
                "agent_id": agents[0].engine_agent_id if agents else DEFAULT_ENGINE_AGENT,
            },
        }

    # ── 新建 / 修改 / 归档 / 恢复 ──
    @app.post("/agents")
    async def agents_create(request: Request, _auth=Depends(require_auth)):
        require_admin(request)
        body = await json_body(request)
        ids, aliases = model_index()
        try:
            a = store.create(
                slug=body.get("slug", ""),
                display_name=body.get("display_name", ""),
                description=body.get("description", ""),
                params=body.get("params") or {},
                model_ids=ids, alias_names=aliases, profile_ids=_qwenpaw_profile_ids(),
            )
        except AgentsError as e:
            raise HTTPException(400, str(e))
        return {"status": "ok", "agent": _view(a)}

    @app.put("/agents/{slug}")
    async def agents_update(request: Request, slug: str, _auth=Depends(require_auth)):
        require_admin(request)
        body = await json_body(request)
        try:
            a = store.update(slug, body)
        except AgentsError as e:
            raise HTTPException(400, str(e))
        return {"status": "ok", "agent": _view(a)}

    @app.delete("/agents/{slug}")
    async def agents_retire(request: Request, slug: str, _auth=Depends(require_auth)):
        require_admin(request)
        try:
            a = store.retire(slug)
        except AgentsError as e:
            raise HTTPException(400, str(e))
        return {"status": "ok", "retired": True, "slug": a.slug}

    @app.post("/agents/{slug}/restore")
    async def agents_restore(request: Request, slug: str, _auth=Depends(require_auth)):
        require_admin(request)
        try:
            a = store.restore(slug)
        except AgentsError as e:
            raise HTTPException(400, str(e))
        return {"status": "ok", "agent": _view(a)}

    # ── 运行记录 ──
    @app.get("/agents/{slug}/runs")
    async def agents_runs(request: Request, slug: str, _auth=Depends(require_auth)):
        require_admin(request)
        _agent_or_404(slug)
        bucket = agent_runs.load().get(slug) or {}
        return {
            "slug": slug,
            "total": int(bucket.get("total") or 0),
            "last_used_at": bucket.get("last_used_at") or "",
            "items": bucket.get("items") or [],
        }

    # ── 会话列表（复用 history 存储；agent_<slug>）──
    @app.get("/agents/{slug}/sessions")
    async def agents_sessions(request: Request, slug: str, _auth=Depends(require_auth)):
        a = _agent_or_404(slug)
        if not can_use_agent(a, request.state.role):
            raise HTTPException(403, "无权使用该智能体")
        try:
            items = history_get(request.state.account, f"agent_{slug}")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))
        return {"slug": slug, "kind": f"agent_{slug}", "items": items}

    # ── 试跑（配置页那颗按钮）──
    @app.post("/agents/{slug}/test")
    async def agents_test(request: Request, slug: str, _auth=Depends(require_auth)):
        require_admin(request)
        a = _agent_or_404(slug)
        started = time.time()
        ok, detail, done = True, "", False
        try:
            async for msg in stream_chat(
                a.engine_base_url, a.engine_agent_id,
                [{"role": "user", "content": "回复：ok"}],
                f"selftest-{a.slug}-{int(started)}",
                system_suffix="只回复 ok，不要任何多余内容。",
                timeout=90.0,
            ):
                if msg.get("type") == "error":
                    ok, detail = False, str(msg.get("message"))[:200]
                    break
                if msg.get("type") == "done":
                    done = True
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, str(exc)[:200]
        if ok and not done:
            ok, detail = False, "引擎无响应"
        return {"ok": ok, "ms": int((time.time() - started) * 1000), "detail": detail}

    # ── 停止 ──
    @app.post("/agents/{slug}/stop")
    async def agents_stop(request: Request, slug: str, chat_id: str = "",
                          _auth=Depends(require_auth)):
        require_admin(request)
        a = _agent_or_404(slug)
        if not chat_id:
            raise HTTPException(400, "缺少 chat_id")
        url = a.engine_base_url.rstrip("/") + "/api/console/chat/stop"
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.post(url, params={"chat_id": chat_id},
                                 headers={"X-Agent-Id": a.engine_agent_id})
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"停止失败：{exc}")
        return {"status": "ok", "engine_http": r.status_code}

    # ── 对话（SSE）──
    @app.post("/agents/{slug}/chat")
    async def agents_chat(request: Request, slug: str, _auth=Depends(require_auth)):
        a = _agent_or_404(slug)
        if not can_use_agent(a, request.state.role):
            raise HTTPException(403, "无权使用该智能体")
        body = await json_body(request)
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise HTTPException(400, "messages 不能为空")
        session_id = str(body.get("session_id") or "").strip() \
            or f"agent-{slug}-{int(time.time())}"
        account = request.state.account

        # 防自指：引擎的模型不能指向 agent/*（否则引擎→智能体→引擎 死循环）
        await refresh_engine_live(store)
        model_now = str(engine_live.get("model") or "")
        if model_now == "agent" or model_now.startswith("agent/"):
            raise HTTPException(
                400, "引擎当前模型指向了智能体自身（agent/*），会造成死循环，已拒绝"
            )

        p = a.params or {}
        suffix = str(p.get("system_suffix") or "")
        started = time.time()
        rounds = len([m for m in messages if isinstance(m, dict) and m.get("role") == "user"])
        # 计量打标：让日志能一眼看出"这次调用是网关的智能体发出的"，
        #   与管理员在「对话」页直接聊区分开。明细（谁/哪个智能体/多少 token）落 agent_runs。
        # 注意：这行必须在 rounds 赋值**之后**（曾因前置引用 rounds 导致 500）。
        log.info("agent chat: slug=%s account=%s session=%s rounds=%d",
                 slug, account, session_id, rounds)
        tool_count = 0
        total_tokens = 0
        status = "ok"

        async def event_stream():
            nonlocal tool_count, total_tokens, status
            yield _sse({"type": "start", "slug": slug, "session_id": session_id})
            try:
                async for msg in stream_chat(
                    a.engine_base_url, a.engine_agent_id, messages, session_id,
                    system_suffix=suffix,
                ):
                    t = msg.get("type")
                    if t == "step" and msg.get("status") == "done":
                        tool_count += 1
                    if t == "usage":
                        total_tokens += int(msg.get("prompt") or 0) \
                            + int(msg.get("completion") or 0)
                    if t == "error":
                        status = "error"
                    yield _sse(msg)
            finally:
                agent_runs.record(
                    slug, account=account, rounds=rounds, tools=tool_count,
                    tokens=total_tokens, ms=int((time.time() - started) * 1000),
                    status=status,
                )

        return StreamingResponse(
            event_stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                     "X-Accel-Buffering": "no"},
        )

    # ── 身份 + 功能开关（灰度通道；前端唯一权威）──
    @app.get("/auth/me")
    async def auth_me(request: Request, _auth=Depends(require_auth)):
        store.reload_if_changed()
        role = request.state.role
        visible = [a.slug for a in store.list() if can_use_agent(a, role)]
        return {
            "account": request.state.account,
            "role": role,
            "features": {
                "agents": {
                    "enabled": bool(visible),
                    "can_manage": role == "admin",
                    "visible": visible,
                },
            },
            "engine": {
                "model": engine_live.get("model", ""),
                "ready": bool(engine_live.get("ready")),
            },
        }
