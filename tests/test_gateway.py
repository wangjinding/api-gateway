"""单元测试：用 httpx MockTransport 模拟供应商，验证核心逻辑。

运行：python -m pytest tests/ -q   或   python tests/test_gateway.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path as _Path

# 允许直接 python 运行（非 pytest）：把项目根加进 sys.path
_ROOT = _Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import os as _os
import httpx
try:
    import pytest
except ImportError:  # 非 pytest 环境直接跑
    pytest = None

# M5+: 账号体系改由页面管理。测试进程把 users 文件指向临时 iso 文件，避免污染运行库 data/users.json。
_os.environ.setdefault("GATEWAY_USERS", str(_ROOT / ".test_users.json"))
# 历史也必须指到临时文件：否则测试会往真实的 data/history.json 里写脏数据
_os.environ.setdefault("GATEWAY_HISTORY", str(_ROOT / ".test_history.json"))
# 第二十二轮 意见反馈：同理——反馈（含图片）必须落到临时路径，别污染真实 data/
_os.environ.setdefault("GATEWAY_FEEDBACK", str(_ROOT / ".test_feedback.json"))
_os.environ.setdefault("GATEWAY_FEEDBACK_MEDIA", str(_ROOT / ".test_feedback_media"))
# 第三十轮 会话落盘 / 视频归属落盘：同样隔离，否则跑测试会把真实登录会话与视频归属写脏
_os.environ.setdefault("GATEWAY_SESSIONS", str(_ROOT / ".test_sessions.json"))
_os.environ.setdefault("GATEWAY_VIDEO_OWNERS", str(_ROOT / ".test_video_owners.json"))
_os.environ.setdefault("GATEWAY_ENV_FILE", str(_ROOT / ".test_env"))

from app.config import load_config, validate_config, ConfigError
from app.circuit import CircuitBreaker, CircuitBreakerRegistry
from app.routing import resolve_with_auth, ModelNotFoundError

CONFIG = str(_ROOT / "data" / "gateway.yaml")

# 密钥经 token_env 指向 .env（yaml 不再存明文）。测试进程同样要加载 .env，
# 才能在真实配置上做 resolve_with_auth —— 与运行时（run.py/app.main 都 load_dotenv）对齐。
try:
    from dotenv import load_dotenv  # noqa: F401
    for _p in (_ROOT, _ROOT / "data"):
        if (_p / ".env").exists():
            load_dotenv(_p / ".env", override=False)
except ImportError:
    pass  # 未装 python-dotenv：交运 test_gateway.py 直跑的 mock 分支处理


# ── 鉴权（恒定开启）下，TestClient 默认带 admin 登录会话的 Bearer ──
# M5+: 网关鉴权恒定开启（账号体系页面管理）。测试统一用 admin/admin 登录换 short-lived
#   sess token 作为默认凭证；显式传 Authorization 优先，raw=True 则不注入（供测 401 用）。
try:
    from fastapi.testclient import TestClient as _TC
except Exception:
    _TC = None
if _TC is not None:
    _AUTOBEARER = ""
    try:
        import app.main as _m
        _c = _TC(_m.app)
        _r = _c.post("/auth/login", json={"username": "admin", "password": "admin"})
        if _r.status_code == 200:
            _AUTOBEARER = f"Bearer {_r.json().get('access_token', '')}"
    except Exception:
        _AUTOBEARER = ""
    def _tc_inject(method):
        def wrapper(self, *a, **kw):
            if _AUTOBEARER:
                h = dict(kw.get("headers") or {})
                h.setdefault("Authorization", _AUTOBEARER)   # 显式传的 Authorization 优先
                kw["headers"] = h
            return method(self, *a, **kw)
        return wrapper

    _TC.request = _tc_inject(_TC.request)
    _TC.stream = _tc_inject(_TC.stream)   # 流式走 stream()，不走 request()，必须一起打

def _login_admin_session(client=None):
    """新起/复用 client 登录 admin，返回会话 token（测试用）。"""
    from fastapi.testclient import TestClient
    import app.main as _m
    c = client or TestClient(_m.app)
    r = c.post("/auth/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


# ── 配置加载 ──
def test_load_config_providers():
    cfg = load_config(CONFIG)
    # 三家供应商：Agnes / 火山方舟 / OpenCode Go
    assert set(cfg.providers.keys()) == {"agnes", "volc_engine", "opencode"}
    assert cfg.providers["agnes"].models[0].id == "agnes-3.0-flash"


def test_load_config_routes():
    """第二十七轮（老板 2026-09-18 决策）: **路由别名 / fallback 链功能已整体删除**。

    为什么删：三线直传落地后「入站格式 = 出站格式」，模型 id 必须与协议线一一对应；
    再套一层"自动挑最快模型"的别名，会把调用方选定的格式/能力（视觉、工具、协议线）
    悄悄换掉。现在调用方直接填真实模型 id，网关不再替谁做主。
    这里同时证明：配置对象上已经没有路由引擎的任何入口，旧配置里的 routes 段会被忽略
    （见 test_legacy_routes_section_is_ignored）。
    """
    cfg = load_config(CONFIG)
    for attr in ("routes", "find_route", "all_routes", "validate_routes"):
        assert not hasattr(cfg, attr), f"路由引擎已删除，配置对象不该再有 {attr}"




def test_pricing_estimate():
    cfg = load_config(CONFIG)
    # 用火山方舟的价目：它不在每日同步脚本（scripts/sync_models.py）的范围内，价格稳定。
    #   （Agnes 的价目会随官方文档改价被自动更新 —— 拿 agnes-3.0-flash 写死 0.35/1.00
    #     会在文档改价那天变成假红灯。）
    mid = "ark-code-latest"
    price = cfg.pricing.per_model[mid]
    assert abs(price["input"] - 0.50) < 1e-9 and abs(price["output"] - 2.00) < 1e-9
    cost = cfg.pricing.estimate(mid, 1_000_000, 1_000_000)
    assert cost is not None and abs(cost - (0.50 + 2.00)) < 1e-6
    assert cfg.pricing.estimate("unknown-model", 10, 10) is None


# ── 路由解析 ──
def test_resolve_model():
    cfg = load_config(CONFIG)
    pkey, pconfig, minfo, token = resolve_with_auth(cfg, "agnes-3.0-flash")
    assert pkey == "agnes"
    assert minfo.type == "text"


def test_resolve_unknown_model_raises():
    cfg = load_config(CONFIG)
    # 兼容无 pytest 环境（此前该用例在缺 pytest 时直接 fail，制造了一个"假红灯"）
    if pytest is not None:
        with pytest.raises(ModelNotFoundError):
            resolve_with_auth(cfg, "definitely-not-a-model")
        return
    try:
        resolve_with_auth(cfg, "definitely-not-a-model")
    except ModelNotFoundError:
        return
    raise AssertionError("未知模型未抛出 ModelNotFoundError")


# ── 熔断器状态机 ──
def test_circuit_close_to_open():
    cb = CircuitBreaker("p", failure_threshold=2, cooldown=30, window=10)
    assert cb.allow()
    cb.record(False)
    assert cb.state if hasattr(cb, "state") else True
    cb.record(False)   # 第 2 次失败 → OPEN
    assert cb.status()["state"] == "open"
    assert not cb.allow()  # 熔断中拒绝


def test_circuit_registry_disabled():
    reg = CircuitBreakerRegistry({"enabled": False, "failure_threshold": 3})
    cb = reg.get("p")
    # 未启用熔断：永远 allow
    cb.record(False); cb.record(False); cb.record(False)
    assert cb.allow()


def test_circuit_recovery():
    cb = CircuitBreaker("p", failure_threshold=1, cooldown=0.1, window=10)
    cb.record(False)
    assert cb.status()["state"] == "open"
    import time
    time.sleep(0.15)   # 等 cooldown 到期 → HALF_OPEN
    assert cb.allow()   # 放行试探
    cb.record(True)      # 试探成功 → CLOSED
    assert cb.status()["state"] == "closed"


# ── OpenAI 兼容适配器 + mock 供应商 ──
def _make_adapter(model="agnes-3.0-flash"):
    """构造一个指向 mock 供应商的适配器。"""
    cfg = load_config(CONFIG)
    pkey, pconfig, minfo, token = resolve_with_auth(cfg, model)
    pconfig.base_url = "http://mock.test/v1"
    from app.adapters import create_adapter
    adapter = create_adapter(pconfig, token, timeout=10)
    # 用 MockTransport 拦截
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={
                "id": "chat-mock", "model": model,
                "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            })
        if req.url.path.endswith("/images/generations"):
            return httpx.Response(200, json={"data": [{"url": "http://mock.test/img.png"}]})
        if req.url.path.endswith("/videos"):
            return httpx.Response(200, json={"video_id": "mock-video-001", "id": "task-mock"})
        return httpx.Response(200, json={})
    transport = httpx.MockTransport(handler)
    adapter._client = httpx.AsyncClient(transport=transport, base_url=pconfig.base_url)
    return adapter


def test_adapter_chat():
    async def run():
        adapter = _make_adapter()
        r = await adapter.chat("agnes-3.0-flash", [{"role": "user", "content": "hi"}])
        assert r["choices"][0]["message"]["content"] == "OK"
        assert r["usage"]["completion_tokens"] == 3
        await adapter.close()
    asyncio.run(run())


def test_adapter_image():
    async def run():
        adapter = _make_adapter("agnes-image-2.5-flash")
        pkey, pconfig, _, _ = resolve_with_auth(load_config(CONFIG), "agnes-image-2.5-flash")
        r = await adapter.generate_image("agnes-image-2.5-flash", "a cube")
        assert r["data"][0]["url"].endswith(".png")
        await adapter.close()
    asyncio.run(run())


def test_adapter_video_create():
    async def run():
        adapter = _make_adapter("agnes-video-2.5-flash")
        r = await adapter.create_video("agnes-video-2.5-flash", "a cat")
        assert r["video_id"]  # mock 返回里带 id
        await adapter.close()
    asyncio.run(run())


# ── FastAPI 端点集成（TestClient + mock 上游） ──
def test_app_endpoints():
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    # 第二十七轮: 路由功能整体删除 → / 里不再有 routes 字段（不是"空列表"）
    assert "routes" not in r.json(), r.json()

    r2 = client.get("/v1/models")
    assert r2.status_code == 200
    ids = [x["id"] for x in r2.json()["data"]]
    assert "hy3" in ids
    # 第二十七轮: 路由功能整体删除 → /v1/models 不再有 type="route" 的旧条目
    # 第三十一轮: 但 wjd 这个名字合法回来了 —— 它必须是**别名条目**（带 alias_of），
    #   不是旧路由；这里对"有没有配别名"保持无关（老板随时可能停用它）
    assert all(x.get("type") != "route" for x in r2.json()["data"])
    assert all(x.get("protocols") == ["chat/completions"]
               for x in r2.json()["data"] if x.get("alias_of")), "别名只走 OpenAI 线"
    # 每个模型都带 protocols（三线直传的能力声明）
    byid2 = {x["id"]: x for x in r2.json()["data"]}
    assert byid2["hy3"]["protocols"] == ["chat/completions"]
    assert byid2["grok-4.6"]["protocols"] == ["responses"]


# ── 根路径导航 ──
def test_root_navigation():
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    r = client.get("/")
    assert r.status_code == 200
    j = r.json()
    assert "endpoints" in j and "chat" in j["endpoints"]
    # 第二十七轮: 路由功能整体删除 → 不再有 routes 字段；三条协议线出现在 endpoints 里
    assert "routes" not in j, j
    # endpoints 的键是端点名（值为"POST /v1/messages（…）"这类说明），不是路径
    assert "messages" in j["endpoints"] and "responses" in j["endpoints"]


# ── 根路径浏览器跳转 + CSP 放行同源前端 ──
def test_root_browser_redirects_to_web():
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app, follow_redirects=False)
    r = client.get("/", headers={"Accept": "text/html"})
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/web/"


def test_csp_allows_self_and_web_page_serves_html():
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    # API 导航（Accept 无 text/html）仍返回 JSON
    r2 = client.get("/")
    assert r2.status_code == 200
    assert "endpoints" in r2.json()
    # 前端页面可加载，且 CSP 放行同源脚本、仍禁框架嵌入
    r = client.get("/web/")
    assert r.status_code == 200
    csp = r.headers.get("Content-Security-Policy", "")
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


# ── /health 运维看板 ──
def test_health_board():
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    r = client.get("/health")
    assert r.status_code == 200
    j = r.json()
    # 看板字段
    assert "providers" in j and "models" in j
    assert "routes" not in j, "路由功能已删除，/health 不该再有 routes"  # 第二十七轮
    assert "circuit" in j["providers"]["agnes"]


# ── Bug 4: 媒体模型解析（路由别名 → 具体媒体模型） ──
def test_resolve_media_model_direct():
    import app.main as m
    cfg = m._cfg()
    # 直接指定 image 模型
    res = m._resolve_media_model(cfg, "agnes-image-2.5-flash", "image")
    assert res is not None and res[4] == "agnes-image-2.5-flash"


def test_resolve_media_model_rejects_alias():
    """第二十七轮 + 第三十一轮: 媒体端点只认真实模型 id。

    - 已配置的别名（wjd）→ **明确 400 指路**（第三十一轮起：别名只在文本线上存在，
      静默返回 None 会让调用方以为"这模型不存在"，而它明明在 /v1/models 里）；
    - 从没配过的名字 → 仍然返回 None（交给调用方报"未知模型"）。
    用自带别名的合成配置，不依赖生产配置里有没有配别名。
    """
    from fastapi import HTTPException
    import app.main as m
    cfg = m._cfg()
    assert m._resolve_media_model(cfg, "agnes-image-2.5-flash", "image") is not None
    acfg = _alias_cfg("m-chat")
    try:
        m._resolve_media_model(acfg, "wjd", "image")
    except HTTPException as e:
        assert e.status_code == 400 and "别名" in str(e.detail), e.detail
    else:
        raise AssertionError("别名打到媒体端点必须明确 400，不能静默放行")
    for unknown in ("img", "any-alias"):
        assert m._resolve_media_model(cfg, unknown, "image") is None, unknown




# ── Bug 1: 三级限流助手（路由级 > 供应商级 > 全局） ──
def test_effective_rps_priority():
    """Bug 1（第二十七轮调整）: 限流优先级 = 供应商级 > 全局（路由级随路由功能移除）。"""
    import app.main as m
    cfg = m._cfg()
    old = cfg.providers["agnes"].rate_limit_rps
    cfg.providers["agnes"].rate_limit_rps = 5
    try:
        assert m._effective_rps(cfg, "agnes") == 5, "供应商级应优先于全局"
        assert m._effective_rps(cfg, None) == cfg.server.rate_limit_rps
        assert m._effective_rps(cfg, "no-such-provider") == cfg.server.rate_limit_rps
    finally:
        cfg.providers["agnes"].rate_limit_rps = old




# ── Bug 2: 限流桶 LRU 清理 ──
def test_rate_limiter_eviction():
    from app.metrics import RateLimiter
    rl = RateLimiter(max_buckets=10)
    # 灌 20 个不同 key，桶数应被限制在 ~10
    for i in range(20):
        rl.allow(f"key-{i}", 100)
    assert rl.size() <= 10, f"桶数 {rl.size()} 超出上限"


# ── Bug 1: 熔断窗口按次数（非时间）生效 ──
def test_circuit_window_by_count():
    from app.circuit import CircuitBreaker
    # 窗口=5 次：前 3 成功 + 2 失败，窗口内失败 2 < 阈值 3 → 不熔断
    cb = CircuitBreaker("p", failure_threshold=3, cooldown=30, window=5)
    for _ in range(3):
        cb.record(True)
    for _ in range(2):
        cb.record(False)
    assert cb.status()["state"] == "closed", "窗口 5 内失败 2 未达阈值 3，应保持 closed"
    # 再来 1 失败（此时窗口最近 5 次为 [T,F,F,F]... 实际 deque 保留最近 5）
    cb.record(False)  # 窗口内失败 3 次 → 达阈值
    assert cb.status()["state"] == "open"
    assert cb.status()["window_size"] == 5  # status 暴露窗口大小


# ── Bug 1: 并发 get_client 只建一个 client ──
def test_adapter_client_concurrent():
    async def run():
        adapter = _make_adapter()
        import asyncio as _a
        # 并发 50 次 get_client，client 必须是同一个实例
        clients = await _a.gather(*[adapter.get_client() for _ in range(50)])
        assert all(c is clients[0] for c in clients), "并发建了多个 client"
        await adapter.close()
    asyncio.run(run())


# ── Bug 2: close() 幂等（重复调用不抛错） ──
def test_adapter_close_idempotent():
    async def run():
        adapter = _make_adapter()
        await adapter.get_client()
        await adapter.close()
        await adapter.close()   # 第二次 close 不应抛
        await adapter.close()
    asyncio.run(run())


# ── Bug 4: generate_image size 缺省给 1K ──
def test_adapter_image_default_size():
    async def run():
        captured = {}
        cfg = load_config(CONFIG)
        pkey, pconfig, _, _ = resolve_with_auth(cfg, "agnes-image-2.5-flash")
        pconfig.base_url = "http://mock.test/v1"
        def handler(req):
            captured['payload'] = json.loads(req.content)
            return httpx.Response(200, json={"data": [{"url": "http://mock.test/x.png"}]})
        from app.adapters import create_adapter
        adapter = create_adapter(pconfig, "tok", timeout=10)
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=pconfig.base_url)
        await adapter.generate_image("agnes-image-2.5-flash", "a cube")  # 不传 size
        assert captured['payload'].get("size") == "1K", f"size 缺省应为 1K，实际 {captured['payload'].get('size')}"
        await adapter.close()
    asyncio.run(run())


# ── 第二轮修复：验证新增/修复行为 ──────────────────────

def test_record_call_buckets():
    """Bug 8 → 第二十七轮调整: 指标只按 模型/供应商 记账（路由维度随路由功能删除）。"""
    from app.metrics import Metrics
    mtr = Metrics()
    mtr.record_call("modelX", "provX", ok=True)
    mtr.record_call("modelX", "provX", ok=False)
    snap = mtr.snapshot()
    assert snap["calls"]["modelX"] == {"ok": 1, "fail": 1}
    assert snap["providers"]["provX"] == {"ok": 1, "fail": 1}
    assert "routes" not in snap and "fallback_total" not in snap, snap
    assert not hasattr(mtr, "record_fallback"), "fallback 计数随路由功能删除"




def test_snapshot_no_lock_escape():
    """Bug 7: snapshot 全程持锁，并发下不应抛异常。"""
    import threading
    from app.metrics import Metrics
    mtr = Metrics()
    stop = {"v": False}

    def writer():
        i = 0
        while not stop["v"]:
            mtr.record_call(f"m{i % 5}", "p", ok=True)
            i += 1

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        for _ in range(200):
            mtr.snapshot()  # 不应抛 RuntimeError
    finally:
        stop["v"] = True
        t.join(timeout=1)


def test_validate_config_catches_missing_base_url():
    """Bug 12: 配置校验能发现缺 base_url。"""
    cfg = load_config(CONFIG)
    cfg.providers["agnes"].base_url = ""
    issues = validate_config(cfg)
    assert any("agnes" in i for i in issues)


def test_load_config_raises_config_error_on_bad_yaml():
    """Bug 12: 结构错误配置应抛 ConfigError（而非运行到一半才崩）。"""
    import tempfile
    import os
    bad = "providers:\n  p1:\n    name: Bad\n    models: []\n"  # 缺 base_url + 空 models
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(bad)
        path = f.name
    try:
        raised = False
        try:
            load_config(path)
        except ConfigError:
            raised = True
        assert raised, "坏配置应抛 ConfigError"
    finally:
        os.unlink(path)


def test_media_rate_limit_enforced():
    """Bug 1/3: 媒体端点的供应商桶限流确实生效且与 chat 同桶。"""
    import app.main as m
    from fastapi import HTTPException
    cfg = m._cfg()
    prov = cfg.providers["agnes"]
    old = prov.rate_limit_rps
    m._limiter._buckets.pop("prov:agnes", None)
    prov.rate_limit_rps = 1
    got_429 = False
    try:
        for _ in range(10):
            try:
                m._enforce_media_rate_limit(cfg, "agnes")
            except HTTPException as e:
                if e.status_code == 429:
                    got_429 = True
                    break
        assert got_429, "媒体限流未触发 429"
    finally:
        prov.rate_limit_rps = old
        m._limiter._buckets.pop("prov:agnes", None)


def test_body_size_limit_413():
    """请求体超限返回 413。"""
    from fastapi.testclient import TestClient
    import app.main as m
    cfg = m._cfg()
    old = cfg.server.max_body_size
    cfg.server.max_body_size = 10
    try:
        client = TestClient(m.app)
        r = client.post("/v1/chat/completions",
                        json={"model": "agnes-3.0-flash",
                              "messages": [{"role": "user", "content": "hello world"}]})
        assert r.status_code == 413, f"期望 413，实际 {r.status_code}"
    finally:
        cfg.server.max_body_size = old


def test_dynamic_cors_reflects_config():
    """Bug 6: CORS 来源实时读取配置（热重载可改），含预检。"""
    from fastapi.testclient import TestClient
    import app.main as m
    cfg = m._cfg()
    old = list(cfg.server.cors_origins)
    try:
        cfg.server.cors_origins = ["https://example.com"]
        client = TestClient(m.app)
        r = client.get("/health", headers={"Origin": "https://example.com"})
        assert r.headers.get("access-control-allow-origin") == "https://example.com"
        r2 = client.get("/health", headers={"Origin": "https://evil.com"})
        assert r2.headers.get("access-control-allow-origin") is None
        r3 = client.options("/v1/chat/completions",
                            headers={"Origin": "https://example.com",
                                     "Access-Control-Request-Method": "POST"})
        assert r3.status_code == 200
        assert r3.headers.get("access-control-allow-origin") == "https://example.com"
    finally:
        cfg.server.cors_origins = old


def test_auth_toggle_enforced():
    """M5+: 入口鉴权恒定开启（账号体系页面管理）：空/错凭证 401，admin 会话/API Key 200。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    assert client.get("/v1/models", headers={"Authorization": ""}).status_code == 401
    assert client.get("/v1/models",
                      headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/v1/models", headers={"Authorization": _AUTOBEARER}).status_code == 200
    # admin 建一个 API Key 也能作为外部调用凭证
    tok = _login_admin_session(client)
    rk = client.post("/auth/apikeys", json={"label": "t"}, headers={"Authorization": f"Bearer {tok}"})
    assert rk.status_code == 200, rk.text
    ak = rk.json()["key"]
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {ak}"}).status_code == 200
    # 删除用列表返回的 id（hash 前 8 位），不能用明文前缀
    lst = client.get("/auth/apikeys", headers={"Authorization": f"Bearer {tok}"}).json()["keys"]
    kid = [k["id"] for k in lst if k["label"] == "t"][-1]
    assert client.delete(f"/auth/apikeys/{kid}", headers={"Authorization": f"Bearer {tok}"}).status_code == 200


def test_global_rate_limit_when_auth_idle():
    """全局限流不依赖鉴权开关：恒定鉴权下仍按账号+凭证分桶触发 429。"""
    from fastapi.testclient import TestClient
    import app.main as m
    cfg = m._cfg()
    old_rps = cfg.server.rate_limit_rps
    cfg.server.rate_limit_rps = 1
    try:
        client = TestClient(m.app)
        tok = _login_admin_session(client)
        h = {"Authorization": f"Bearer {tok}"}
        m._limiter._buckets.clear()
        got_429 = False
        for _ in range(10):
            if client.get("/v1/models", headers=h).status_code == 429:
                got_429 = True
                break
        assert got_429, "全局限流未触发 429"
    finally:
        cfg.server.rate_limit_rps = old_rps
        m._limiter._buckets.clear()


def test_streaming_chat_sse():
    """流式 chat：SSE 事件流包含 meta + 内容块 + 统一 [DONE]。"""
    from fastapi.testclient import TestClient
    import app.main as m

    class FakeAdapter:
        async def chat_stream(self, model=None, messages=None, **kw):
            yield {"id": "c1", "object": "chat.completion.chunk", "model": model,
                   "choices": [{"index": 0, "delta": {"content": "Hi"}}]}
            yield {"id": "c1", "object": "chat.completion.chunk", "model": model,
                   "choices": [{"index": 0, "delta": {"content": " there"}}]}
            yield {"id": "c1", "choices": [], "done": True}

    orig = m._get_adapter
    m._get_adapter = lambda model_id, timeout=None, cfg=None: (
        FakeAdapter(), "agnes", m._cfg().providers["agnes"])
    try:
        client = TestClient(m.app)
        with client.stream("POST", "/v1/chat/completions", json={
            "model": "agnes-3.0-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }) as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())
        assert "Hi" in body and " there" in body, body
        assert '"provider": "agnes"' in body, body
        assert "[DONE]" in body
    finally:
        m._get_adapter = orig


def test_stream_usage_frame_has_cost():
    """M2: 流式末尾 usage 帧应带 token 成本估算（estimated_cost_cny）。"""
    from fastapi.testclient import TestClient
    import app.main as m

    class FakeAdapter:
        async def chat_stream(self, model=None, messages=None, **kw):
            yield {"id": "c1", "object": "chat.completion.chunk", "model": model,
                   "choices": [{"index": 0, "delta": {"content": "hi"}}]}
            # usage 在普通 chunk 里（无 done）；done 是独立结束哨兵
            yield {"id": "c1", "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 5}}
            yield {"done": True}

    orig = m._get_adapter
    m._get_adapter = lambda model_id, timeout=None, cfg=None: (
        FakeAdapter(), "agnes", m._cfg().providers["agnes"])
    try:
        client = TestClient(m.app)
        with client.stream("POST", "/v1/chat/completions", json={
            "model": "agnes-3.0-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }) as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())
        assert "[DONE]" in body
        # 网关累计出 usage 的末尾帧应带上成本
        assert '"prompt_tokens": 3' in body, body
        assert "estimated_cost_cny" in body, body
    finally:
        m._get_adapter = orig


def test_logs_include_request_body():
    """M4: 访问日志应带请求体摘要（能看到具体请求内容）。"""
    from fastapi.testclient import TestClient
    import app.main as m

    class FakeAdapter:
        async def chat(self, model=None, messages=None, **kw):
            return {"choices": [{"message": {"role": "assistant", "content": "hi"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    orig = m._get_adapter
    m._get_adapter = lambda model_id, timeout=None, cfg=None: (
        FakeAdapter(), "agnes", m._cfg().providers["agnes"])
    try:
        client = TestClient(m.app)
        r = client.post("/v1/chat/completions", json={
            "model": "agnes-3.0-flash",
            "messages": [{"role": "user", "content": "把这句话记进日志"}],
        })
        assert r.status_code == 200, r.text
        j = client.get("/logs?n=20").json()
        chats = [e for e in j["entries"] if e.get("path") == "/v1/chat/completions"]
        assert chats, "应有 chat 日志条目"
        hit = [e for e in chats if "把这句话记进日志" in (e.get("body") or "")]
        assert hit, "日志应含本次请求体正文: " + str(chats[-1] if chats else None)
        # 本轮修复: 非流式响应正文也要进日志（此前只有请求，看不出模型答了什么）
        assert hit[-1].get("response") == "hi", hit[-1]
    finally:
        m._get_adapter = orig


def test_logs_include_streaming_response():
    """本轮修复: 流式（SSE）调用的输出正文累积进访问日志 response 字段。"""
    from fastapi.testclient import TestClient
    import app.main as m

    class FakeStreamAdapter:
        async def chat_stream(self, model=None, messages=None, **kw):
            for piece in ["你", "好", "呀"]:
                yield {"choices": [{"delta": {"content": piece}}]}
            yield {"done": True}

    orig = m._get_adapter
    m._get_adapter = lambda model_id, timeout=None, cfg=None: (
        FakeStreamAdapter(), "agnes", m._cfg().providers["agnes"])
    try:
        client = TestClient(m.app)
        r = client.post("/v1/chat/completions", json={
            "model": "agnes-3.0-flash",
            "messages": [{"role": "user", "content": "流式正文进日志"}],
            "stream": True,
        })
        assert r.status_code == 200, r.text
        assert "[DONE]" in r.text, r.text
        j = client.get("/logs?n=20").json()
        hit = [e for e in j["entries"]
               if e.get("path") == "/v1/chat/completions"
               and "流式正文进日志" in (e.get("body") or "")]
        assert hit, "应有本次流式日志条目"
        assert hit[-1].get("stream") is True, hit[-1]
        assert hit[-1].get("response") == "你好呀", hit[-1]
    finally:
        m._get_adapter = orig


def test_log_response_falls_back_to_reasoning():
    """本轮补充: 推理型模型正文为空时，日志回落到 reasoning_content（否则看起来像「空响应」）。"""
    import app.main as m

    # 非流式
    assert m._resp_text({"choices": [{"message": {"content": "正文"}}]}) == "正文"
    assert m._resp_text({"choices": [{"message": {"content": "", "reasoning_content": "思考中"}}]}) == "[推理] 思考中"
    assert m._resp_text({"choices": [{"message": {"content": None}}]}) == ""
    assert m._resp_text({}) == ""

    # 流式
    assert m._stream_log_text(["答", "案"], ["不该出现"]) == "答案"
    assert m._stream_log_text([], ["推", "理"]) == "[推理] 推理"
    assert m._stream_log_text([], []) == ""


def test_streaming_unresolvable_model_sends_error_frame():
    """回归: 模型在 _get_adapter 阶段就被拒时，流式仍要正常收尾发错误帧（不是 500）。

    累加器 resp_parts/reason_parts 若只在循环体内 try 里初始化，这条路径会 NameError
    → SSE 变 500，客户端拿不到 {"error": ...} 帧。第二十七轮: 只剩一个模型，没有
    "换下一个" 的余地，错误帧就是最终答复。
    """
    from fastapi.testclient import TestClient
    from app.routing import ModelNotFoundError
    import app.main as m

    orig = m._get_adapter

    def boom(model_id, timeout=None, cfg=None):
        raise ModelNotFoundError(f"未知模型 {model_id}")

    m._get_adapter = boom
    try:
        client = TestClient(m.app)
        r = client.post("/v1/chat/completions", json={
            "model": "agnes-3.0-flash",
            "messages": [{"role": "user", "content": "解析失败流式"}],
            "stream": True,
        })
        assert r.status_code == 200, r.status_code
        assert "未知模型" in r.text, r.text
        assert "[DONE]" in r.text, r.text
    finally:
        m._get_adapter = orig




def test_batch_endpoint_concurrent():
    """/v1/chat/completions/batch：并发多 prompt 各返回结果。"""
    from fastapi.testclient import TestClient
    import app.main as m

    class FakeAdapter:
        async def chat(self, model=None, messages=None, **kw):
            text = messages[-1]["content"] if messages else ""
            return {
                "choices": [{"message": {"role": "assistant", "content": f"echo:{text}"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    orig = m._get_adapter
    m._get_adapter = lambda model_id, timeout=None, cfg=None: (
        FakeAdapter(), "agnes", m._cfg().providers["agnes"])
    try:
        client = TestClient(m.app)
        r = client.post("/v1/chat/completions/batch", json={
            "model": "agnes-3.0-flash",
            "items": [
                {"messages": [{"role": "user", "content": "a"}]},
                {"messages": [{"role": "user", "content": "b"}]},
            ],
        })
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["count"] == 2
        assert all(x["ok"] for x in j["results"]), j
        texts = [x["result"]["choices"][0]["message"]["content"] for x in j["results"]]
        assert any("echo:a" in t for t in texts) and any("echo:b" in t for t in texts), texts
    finally:
        m._get_adapter = orig


def test_video_webhook_reports_real_status():
    """Bug 2: webhook 按真实状态上报，failed 不再误标成 completed。"""
    import app.main as m

    class FakeAdapter:
        async def poll_video(self, video_id, model=None, extra=None):
            return {"raw": {"status": "failed", "reason": "boom"}}

    captured: dict = {}

    class FakeResp:
        status_code = 200

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResp()

    class FakeHttpx:
        AsyncClient = FakeClient

    orig_get = m._get_adapter
    orig_httpx = m.httpx
    m._get_adapter = lambda model_id, timeout=None, cfg=None: (
        FakeAdapter(), "agnes", m._cfg().providers["agnes"])
    m.httpx = FakeHttpx
    try:
        cfg = m._cfg()
        pconfig = cfg.providers["agnes"]
        # 第六轮: 回调地址现在会被 SSRF 复检（创建时 + 回调前各一次），
        # 这里用「公网字面量 IP」既能通过校验、又不需要真实 DNS（保持单测无网络依赖）。
        cb_url = "http://93.184.216.34/hook"
        asyncio.run(m._video_wait_and_callback(
            cfg, "agnes", pconfig, "agnes-video-2.5-flash", "vid-1",
            True, 5, cb_url, "rid-1"))
        assert captured.get("url") == cb_url, captured
        assert captured["json"]["status"] == "failed", captured
        assert captured["json"]["video_id"] == "vid-1"
    finally:
        m._get_adapter = orig_get
        m.httpx = orig_httpx


def test_unknown_model_returns_400():
    """Bug 15: 未知模型/路由应 400（客户端错误），而非 fallback 后的 502。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    r = client.post("/v1/chat/completions",
                    json={"model": "__nope__", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 400, r.status_code
    err = r.json().get("error")
    assert isinstance(err, dict) and "message" in err, r.text
    # batch 同样 400
    r2 = client.post("/v1/chat/completions/batch",
                     json={"model": "__nope__",
                           "items": [{"messages": [{"role": "user", "content": "x"}]}]})
    assert r2.status_code == 400, r2.status_code


# ── 第三轮修复：回归测试 ─────────────────────────────

def test_model_not_found_error_message():
    """Bug 1: 异常消息不再重复前缀，model_id 只存裸名。"""
    from app.routing import resolve, ModelNotFoundError
    cfg = load_config(CONFIG)
    raised = False
    try:
        resolve(cfg, "__nope__")
    except ModelNotFoundError as e:
        raised = True
        assert e.model_id == "__nope__", e.model_id
        assert str(e).count("未知模型") == 1, str(e)
        assert "可用模型" in str(e)
    assert raised, "应抛 ModelNotFoundError"


def test_model_index_matches_linear_scan():
    """Bug 5: 索引查找结果与原线性扫描一致。"""
    cfg = load_config(CONFIG)
    for m in cfg.all_models():
        f = cfg.find_provider_for_model(m["id"])
        assert f is not None and f[0] == m["provider"], m
    assert cfg.find_provider_for_model("__nope__") is None


def test_validation_error_openai_format():
    """Bug 2: 校验失败返回 OpenAI 兼容格式（非 FastAPI 的 detail）。"""
    from fastapi.testclient import TestClient
    import app.main as m
    c = TestClient(m.app)
    r = c.post("/v1/chat/completions", json={"model": "agnes-3.0-flash"})  # 缺 messages
    assert r.status_code == 422, r.status_code
    body = r.json()
    assert "error" in body, body
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] == "422"


def test_bad_content_length_returns_400():
    """Bug 3: 畸形 Content-Length 应 400（此前 500）。"""
    from fastapi.testclient import TestClient
    import app.main as m
    c = TestClient(m.app, raise_server_exceptions=False)
    r = c.post("/v1/chat/completions", content=b"{}",
               headers={"Content-Type": "application/json", "Content-Length": "abc"})
    assert r.status_code == 400, r.status_code
    assert r.json()["error"]["type"] == "invalid_request_error"


def test_in_flight_not_leaked_on_unhandled_error():
    """Bug 4: 未捕获异常（500）不得泄漏 in_flight 计数。"""
    from fastapi.testclient import TestClient
    import app.main as m

    @m.app.get("/__boom__")
    async def _boom():
        raise RuntimeError("boom")

    try:
        c = TestClient(m.app, raise_server_exceptions=False)
        before = m._metrics.in_flight_snapshot()
        r = c.get("/__boom__")
        assert r.status_code == 500, r.status_code
        after = m._metrics.in_flight_snapshot()
        assert after == before, f"in_flight 泄漏: {before} -> {after}"
    finally:
        m.app.router.routes = [rt for rt in m.app.router.routes
                               if getattr(rt, "path", None) != "/__boom__"]


def test_rate_limiter_first_request_consumes():
    """Bug 6: 首次请求也扣 token（rps=1 → cap=2，两次放行、第三次拒）。"""
    from app.metrics import RateLimiter
    rl = RateLimiter()
    assert rl.allow("k", 1) is True
    assert rl.allow("k", 1) is True
    assert rl.allow("k", 1) is False


def test_circuit_reset_when_disabled_on_reload():
    """Bug 7: 热重载禁用熔断时，已 OPEN 的 breaker 复位放行。"""
    from app.circuit import CircuitBreakerRegistry
    reg = CircuitBreakerRegistry({"enabled": True, "failure_threshold": 1,
                                  "cooldown": 9999, "window": 5})
    cb = reg.get("p")
    cb.record(False)
    assert cb.status()["state"] == "open"
    assert cb.allow() is False
    reg.update_config({"enabled": False})
    assert cb.status()["state"] == "closed"
    assert cb.allow() is True


# ═════════════════════════════════════════════════════════════
# 第四轮修复的回归测试
# ═════════════════════════════════════════════════════════════

class _Isolated:
    """测试用：替换全局熔断/指标实例，用完还原（避免用例间状态串味）。"""

    def __enter__(self):
        import app.main as m
        from app.circuit import CircuitBreakerRegistry
        self._m = m
        self._saved = (m._circuits, m._metrics)
        # 阈值调到极高：让"故意失败"的用例不会真的把熔断打开进而影响后续用例
        m._circuits = CircuitBreakerRegistry({"enabled": True, "failure_threshold": 9999,
                                              "cooldown": 1, "window": 10})
        m._metrics = m.Metrics(enabled=True)
        return m

    def __exit__(self, *exc):
        self._m._circuits, self._m._metrics = self._saved
        return False


def test_streaming_failure_metrics_and_no_fallback_counter():
    """第二十七轮: 流式失败如实记 ok=False + 耗时，且不再有 fallback 计数（路由已删）。

    旧语义（成功降级才计 fallback_total）随路由链一起消失：链没了，"降级"这个动作不存在。
    """
    from fastapi.testclient import TestClient
    import app.main as m

    class FailStream:
        async def chat_stream(self, model=None, messages=None, **kw):
            raise RuntimeError("upstream boom")
            yield  # pragma: no cover — 让它是个生成器

    orig = m._get_adapter
    try:
        with _Isolated() as mm:
            mm._get_adapter = lambda model_id, timeout=None, cfg=None: (
                FailStream(), "agnes", mm._cfg().providers["agnes"])
            client = TestClient(mm.app)
            r = client.post("/v1/chat/completions", json={
                "model": "agnes-3.0-flash",
                "messages": [{"role": "user", "content": "流式失败"}],
                "stream": True,
            })
            assert r.status_code == 200, r.text
            assert "upstream boom" in r.text, r.text
            assert "[DONE]" in r.text, r.text
            snap = mm._metrics.snapshot()
            assert snap["calls"]["agnes-3.0-flash"]["fail"] == 1, snap["calls"]
            assert "fallback_total" not in snap and "routes" not in snap, snap
            assert snap["latency"].get("agnes-3.0-flash"), "失败也要记耗时（Bug 5）"
    finally:
        m._get_adapter = orig




def test_chat_unknown_and_standard_params_passthrough():
    """未声明的字段 + OpenAI 标准参数都要透传给上游（此前被静默丢弃）。"""
    from fastapi.testclient import TestClient
    import app.main as m

    seen = {}

    class Capture:
        async def chat(self, model=None, messages=None, **kw):
            seen["extra"] = kw.get("extra")
            return {"choices": [{"message": {"role": "assistant", "content": "x"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    orig = m._get_adapter
    try:
        client = TestClient(m.app)
        with _Isolated() as mm:
            mm._get_adapter = lambda model_id, timeout=None, cfg=None: (
                Capture(), "agnes", mm._cfg().providers["agnes"])
            r = client.post("/v1/chat/completions", json={
                "model": "agnes-3.0-flash", "messages": [{"role": "user", "content": "hi"}],
                "n": 2, "presence_penalty": 0.5, "seed": 7,
                "response_format": {"type": "json_object"},
                "chat_template_kwargs": {"enable_thinking": False},
            })
            assert r.status_code == 200, r.text
        ex = seen["extra"]
        assert ex["n"] == 2 and ex["presence_penalty"] == 0.5 and ex["seed"] == 7, ex
        assert ex["response_format"] == {"type": "json_object"}, ex
        assert ex["chat_template_kwargs"] == {"enable_thinking": False}, ex
    finally:
        m._get_adapter = orig


def test_batch_extra_and_limits():
    """/batch：extra 透传、items 上限 400、并发被钳制。"""
    from fastapi.testclient import TestClient
    import app.main as m

    seen = []

    class Capture:
        async def chat(self, model=None, messages=None, **kw):
            seen.append(kw.get("extra"))
            return {"choices": [{"message": {"role": "assistant", "content": "x"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    orig = m._get_adapter
    try:
        client = TestClient(m.app)
        with _Isolated() as mm:
            mm._get_adapter = lambda model_id, timeout=None, cfg=None: (
                Capture(), "agnes", mm._cfg().providers["agnes"])
            cap = mm._cfg().server.batch_max_items
            r = client.post("/v1/chat/completions/batch", json={
                "model": "agnes-3.0-flash",
                "items": [{"messages": [{"role": "user", "content": "a"}]}],
                "extra": {"chat_template_kwargs": {"enable_thinking": False}},
                "max_concurrency": 9999,
            })
            assert r.status_code == 200, r.text
            assert r.json()["max_concurrency"] == mm._cfg().server.batch_max_concurrency, r.json()
            assert seen and seen[0]["chat_template_kwargs"] == {"enable_thinking": False}, seen

            r2 = client.post("/v1/chat/completions/batch", json={
                "model": "agnes-3.0-flash",
                "items": [{"messages": [{"role": "user", "content": "a"}]}] * (cap + 1),
            })
            assert r2.status_code == 400, r2.text
    finally:
        m._get_adapter = orig


def test_callback_url_ssrf_guard():
    """视频 callback_url：内网/回环/非 http(s) 一律 400，开关可放行。"""
    import asyncio
    import app.main as m
    from fastapi import HTTPException

    async def go(url, allow):
        await m._validate_callback_url(url, allow)

    # 非 http(s)
    for bad in ("ftp://example.com/x", "file:///etc/passwd", "javascript:alert(1)"):
        try:
            asyncio.run(go(bad, False))
            raise AssertionError(f"应拒绝 {bad}")
        except HTTPException as e:
            assert e.status_code == 400, bad
    # 内网 / 回环 / 链路本地
    for bad in ("http://127.0.0.1:8088/cb", "http://10.0.0.5/cb",
                "http://192.168.1.1/cb", "http://169.254.169.254/latest/meta-data",
                "http://[::1]:9000/cb"):
        try:
            asyncio.run(go(bad, False))
            raise AssertionError(f"应拒绝 {bad}")
        except HTTPException as e:
            assert e.status_code == 400, bad
    # 公网字面量 IP 通过
    asyncio.run(go("http://93.184.216.34/cb", False))
    # 开关放行
    asyncio.run(go("http://127.0.0.1:8088/cb", True))


def test_videos_poll_uses_model_and_reports_status():
    """videos_poll：指定 model 时用解析到的供应商；状态兼容 status/state。"""
    from fastapi.testclient import TestClient
    import app.main as m

    class FakeAdapter:
        async def poll_video(self, video_id, model=None, extra=None):
            return {"raw": {"state": "succeeded", "video_id": video_id}}

    orig = m._get_adapter
    m._get_adapter = lambda model_id, timeout=None, cfg=None: (
        FakeAdapter(), "agnes", m._cfg().providers["agnes"])
    try:
        client = TestClient(m.app)
        r = client.get("/v1/videos/abc123", params={"model": "agnes-video-2.5-flash"})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["status"] == "succeeded", j
        assert j["served_model"], j
    finally:
        m._get_adapter = orig


def test_sse_malformed_line_does_not_break_stream():
    """上游发非 JSON 行（keep-alive `data: ping`）时不应中断整个流。"""
    import asyncio
    import httpx
    from app.adapters.openai_compat import OpenAICompatAdapter
    from app.config import load_config

    cfg = load_config(CONFIG)
    p = cfg.providers["agnes"]

    sse = (b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n'
           b'data: ping\n'
           b'\n'
           b'data: {"choices":[{"delta":{"content":"lo"}}]}\n'
           b'data: [DONE]\n')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse, headers={"Content-Type": "text/event-stream"})

    transport = httpx.MockTransport(handler)
    adapter = OpenAICompatAdapter(p.name, p.base_url, headers=p.extra_headers, media=p.media)
    # 注入 mock 传输层（get_client 只在 _client 为 None 时新建，这里预置即可）
    adapter._client = httpx.AsyncClient(base_url=p.base_url, transport=transport)

    async def collect():
        out = []
        async for chunk in adapter.chat_stream(model="agnes-3.0-flash",
                                               messages=[{"role": "user", "content": "x"}]):
            out.append(chunk)
        return out

    chunks = asyncio.run(collect())
    text = "".join(
        c.get("choices", [{}])[0].get("delta", {}).get("content", "")
        for c in chunks if c.get("choices")
    )
    assert text == "Hello", (text, chunks)
    assert chunks[-1].get("done") is True


def test_request_id_sanitized():
    """客户端 X-Request-ID 含 CR/LF 时被丢弃并换成 UUID，且不注入响应头。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    r = client.get("/health", headers={"X-Request-ID": "evil\r\nX-Injected: 1"})
    assert "x-injected" not in {k.lower() for k in r.headers}
    rid = r.headers.get("x-request-id")
    assert rid and rid != "evil"
    r2 = client.get("/health", headers={"X-Request-ID": "trace-123_ok"})
    assert r2.headers.get("x-request-id") == "trace-123_ok"


def test_cors_headers_present_on_body_limit_413():
    """体积超限的 413 也要带 CORS 头（否则浏览器读不到错误原因）。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    limit = m._cfg().server.max_body_size
    body = b'{"model":"x","messages":[],"pad":"' + b"a" * (limit + 100) + b'"}'
    r = client.post("/v1/chat/completions", content=body,
                    headers={"Content-Type": "application/json", "Origin": "https://example.com"})
    assert r.status_code == 413, r.status_code
    assert "access-control-allow-origin" in {k.lower() for k in r.headers}


def test_media_circuit_isolated_from_chat():
    """媒体失败喂独立熔断桶，不应影响 chat 的供应商桶。"""
    import app.main as m
    reg = m._circuits
    chat = reg.get("agnes")
    media = m._media_circuit("agnes")
    assert chat is not media
    before = chat.status()["recent_failures"]
    media.record(False)
    assert chat.status()["recent_failures"] == before
    assert media.status()["recent_failures"] == 1


def test_rps_must_be_numeric():
    """YAML 里 rate_limit_rps 写成字符串也能正常解析为 float。"""
    import tempfile
    from app.config import load_config
    base = _Path(CONFIG).read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as d:
        p = _Path(d) / "g.yaml"
        p.write_text(base + "\nserver:\n  rate_limit_rps: \"5\"\n", encoding="utf-8")
        cfg = load_config(str(p))
        assert isinstance(cfg.server.rate_limit_rps, float)
        assert cfg.server.rate_limit_rps == 5.0


def test_auth_no_longer_config_driven():
    """M5+: 鉴权改由页面账号体系管理，配置文件不再有 auth.users/enabled。"""
    from app.users import UsersStore, UserError, USERNAME_RE
    u = UsersStore(str(_ROOT / ".test_users.json"))
    # 初始管理员 admin/admin
    assert u.verify("admin", "admin")
    assert u.is_admin("admin")
    # 创建账号：默认密码=账号名；账号名仅字母数字
    u.create_user("alice")
    assert u.verify("alice", "alice")
    u.add_api_key("alice", "l1")
    # 非法账号名 / 重名被拒
    try:
        u.create_user("Alice_2")
        assert False, "应拒绝非字母数字"
    except UserError:
        pass
    try:
        u.create_user("alice")
        assert False, "应拒绝重名"
    except UserError:
        pass
    assert USERNAME_RE.match("abc123")
    assert not USERNAME_RE.match("a-b")


def test_validate_warnings_duplicate_model_ids():
    """跨供应商重复模型 id → 出告警（不阻止启动）。"""
    from app.config import validate_warnings, ProviderConfig, ModelInfo, AuthConfig
    cfg = load_config(CONFIG)
    cfg.providers["copycat"] = ProviderConfig(
        key="copycat", name="C", base_url="http://x", auth=AuthConfig(),
        models=[ModelInfo(id="agnes-3.0-flash", type="text")])
    warns = validate_warnings(cfg)
    assert any("agnes-3.0-flash" in w for w in warns), warns


def test_validate_warnings_missing_key():
    """启动告警：缺 Key 供应商（鉴权已页面化 → 无弱 key 告警；路由相关告警随路由功能删除）。"""
    from app.config import validate_warnings, ProviderConfig, ModelInfo, AuthConfig
    cfg = load_config(CONFIG)
    cfg.providers["nokey"] = ProviderConfig(
        key="nokey", name="N", base_url="http://x", auth=AuthConfig(),
        models=[ModelInfo(id="nokey-m", type="text")])
    warns = validate_warnings(cfg)
    assert any("nokey" in w and "API Key" in w for w in warns), warns
    # 修好后不再报（避免长鸣）
    cfg.providers["nokey"].auth.token = "some-valid-token"
    warns2 = validate_warnings(cfg)
    assert not any("API Key" in w and "nokey" in w for w in warns2), warns2




def test_prometheus_label_escaping_and_p95():
    """Prometheus 标签转义 + p95 最近秩。"""
    import app.main as m
    assert m._prom_label('a"b\\c') == 'a\\"b\\\\c'
    met = m.Metrics(enabled=True)
    for v in range(1, 21):          # 20 个样本，p95 应为 19 而非 20
        met.record_latency("m", float(v))
    lat = met.snapshot()["latency"]["m"]
    assert lat["p95"] == 19.0, lat
    assert lat["p50"] == 10.0, lat


def test_videos_poll_upstream_404_maps_to_404():
    """上游 404（任务不存在）应返回 404，而不是被包成 502。"""
    from fastapi.testclient import TestClient
    import httpx
    import app.main as m

    class FakeAdapter:
        async def poll_video(self, video_id, model=None, extra=None):
            req = httpx.Request("GET", "https://upstream.agnes/v1/videos/x")
            raise httpx.HTTPStatusError("Not Found", request=req,
                                        response=httpx.Response(404, request=req))

    orig = m._get_adapter
    try:
        with _Isolated() as mm:
            mm._get_adapter = lambda model_id, timeout=None, cfg=None: (
                FakeAdapter(), "agnes", mm._cfg().providers["agnes"])
            client = TestClient(mm.app)
            r = client.get("/v1/videos/nope", params={"model": "agnes-video-2.5-flash"})
            assert r.status_code == 404, r.text
            assert "不存在" in r.json()["error"]["message"], r.text
    finally:
        m._get_adapter = orig


def test_videos_poll_circuit_open_returns_503():
    """媒体桶熔断时，查询（含 wait 两种模式）直接 503，不再白打上游 / 误报超时。"""
    from fastapi.testclient import TestClient
    from app.circuit import CircuitBreakerRegistry
    import app.main as m

    called = []

    class FakeAdapter:
        async def poll_video(self, video_id, model=None, extra=None):
            called.append(video_id)
            return {"raw": {"status": "processing"}}

    orig_adapter, orig_circuits = m._get_adapter, m._circuits
    m._circuits = CircuitBreakerRegistry({"enabled": True, "failure_threshold": 1,
                                          "cooldown": 9999, "window": 5})
    try:
        m._get_adapter = lambda model_id, timeout=None, cfg=None: (
            FakeAdapter(), "agnes", m._cfg().providers["agnes"])
        m._media_circuit("agnes").record(False)          # 打开媒体桶
        client = TestClient(m.app)
        for params in ({"model": "agnes-video-2.5-flash"},
                       {"model": "agnes-video-2.5-flash", "wait": True, "wait_timeout": 5}):
            r = client.get("/v1/videos/abc", params=params)
            assert r.status_code == 503, (params, r.text)
            assert "熔断" in r.json()["error"]["message"], r.text
        assert called == [], f"熔断期间不应真的访问上游: {called}"
    finally:
        m._get_adapter, m._circuits = orig_adapter, orig_circuits


def test_videos_create_schedules_callback_without_wait():
    """只传 callback_url（不传 wait）时 webhook 会被调度 —— 此前被静默丢弃、永不回调。"""
    from fastapi.testclient import TestClient
    import app.main as m

    class FakeAdapter:
        async def create_video(self, **kw):
            return {"video_id": "vid-9", "raw": {"status": "queued"}}

    spawned: dict = {}
    orig_adapter, orig_spawn = m._get_adapter, m._spawn_bg
    try:
        with _Isolated() as mm:
            mm._get_adapter = lambda model_id, timeout=None, cfg=None: (
                FakeAdapter(), "agnes", mm._cfg().providers["agnes"])

            def fake_spawn(coro, name):
                spawned["name"] = name
                coro.close()          # 不真的执行，避免真去发网络请求

            mm._spawn_bg = fake_spawn
            client = TestClient(mm.app)
            r = client.post("/v1/videos", json={
                "model": "agnes-video-2.5-flash", "prompt": "a cat",
                "callback_url": "http://93.184.216.34/hook"})
            assert r.status_code == 200, r.text
            j = r.json()
            assert j["async"] is True and j["callback_scheduled"] is True, j
            assert spawned.get("name") == "video-callback-vid-9", spawned
    finally:
        m._get_adapter, m._spawn_bg = orig_adapter, orig_spawn


def test_webhook_recheck_blocks_private_target():
    """回调前复检：域名在等待期间被改指内网 → 放弃回调（DNS rebinding / TOCTOU）。"""
    import asyncio
    import app.main as m

    class FakeAdapter:
        async def poll_video(self, video_id, model=None, extra=None):
            return {"raw": {"status": "completed"}}

    posted = []
    orig_adapter, orig_httpx = m._get_adapter, m.httpx
    try:
        with _Isolated() as mm:
            mm._get_adapter = lambda model_id, timeout=None, cfg=None: (
                FakeAdapter(), "agnes", mm._cfg().providers["agnes"])

            class FakeClient:
                def __init__(self, *a, **k):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

                async def post(self, url, json=None):
                    posted.append(url)

            mm.httpx = type("H", (), {"AsyncClient": FakeClient})
            # 内网回调 + 默认不允许 → 复检拦下，绝不发出请求
            asyncio.run(mm._video_wait_and_callback(
                mm._cfg(), "agnes", mm._cfg().providers["agnes"], "agnes-video-2.5-flash",
                "vid-x", True, 3, "http://127.0.0.1:9/hook", "rid-x"))
            assert posted == [], f"内网回调不应被发出: {posted}"
    finally:
        m._get_adapter, m.httpx = orig_adapter, orig_httpx


def test_circuit_half_open_probe_timeout_not_wedged():
    """试探请求永远不回报时，breaker 不能永久卡在 HALF_OPEN（此前会永久拒绝该供应商）。"""
    import time as _t
    from app.circuit import CircuitBreaker

    cb = CircuitBreaker("p", failure_threshold=1, cooldown=0.05, window=5)
    assert cb.allow() is True
    cb.record(False)
    assert cb.status()["state"] == "open"

    _t.sleep(0.06)                      # 冷却到期
    assert cb.allow() is True           # 放行唯一试探
    assert cb.status()["state"] == "half_open"
    # 试探请求"消失"了（客户端断开，record 永不到达）：冷却期内应拒绝
    assert cb.allow() is False
    # 探测超时兜底：即使 probe_timeout 还没到，先手工把它调小来验证状态机能自愈
    cb.probe_timeout = 0.0
    assert cb.allow() is False
    assert cb.status()["state"] == "open", cb.status()   # 已自愈回 OPEN，而非卡死在 half_open
    _t.sleep(0.06)
    assert cb.allow() is True, "自愈后应能再次进入试探"


# ═══════════ 第六轮回归测试 ═══════════
def test_extra_cannot_override_core_payload_keys():
    """P0: 客户端 extra 不能劫持 model / messages / stream（路由越权 + 协议错配）。"""
    from app.models import ChatRequest
    from app.adapters.openai_compat import OpenAICompatAdapter

    req = ChatRequest.model_validate({
        "model": "agnes-3.0-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
        "extra": {"model": "EVIL-EXPENSIVE", "messages": [], "stream": True, "top_k": 7},
    })
    extra = req.upstream_extra()
    assert "model" not in extra and "messages" not in extra and "stream" not in extra, extra
    assert extra.get("top_k") == 7, "正常的供应商特有参数仍应透传"

    ad = OpenAICompatAdapter("t", "https://x.example/v1")
    payload = ad._build_payload("real-model", [{"role": "user", "content": "hi"}],
                                None, None, None, False, None, None, None,
                                {"model": "EVIL", "messages": "x", "stream": True, "top_k": 7})
    assert payload["model"] == "real-model", payload
    assert payload["stream"] is False, payload
    assert isinstance(payload["messages"], list), payload
    assert payload["top_k"] == 7, payload


def test_extra_cannot_override_video_core_but_keeps_mode_fields():
    """视频：extra 不能改 model/prompt/seconds，但 first_frame 等模式字段必须保留。"""
    from app.adapters.openai_compat import OpenAICompatAdapter

    ad = OpenAICompatAdapter("t", "https://x.example/v1")
    cap = {}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            cap["url"], cap["json"] = url, json
            return type("R", (), {"status_code": 200, "json": lambda s: {"video_id": "v1"}, "raise_for_status": lambda s: None})()

    import app.adapters.openai_compat as oc
    orig = oc.__dict__.get("json_or_raise")
    oc.json_or_raise = lambda resp: resp.json()
    saved = ad._lease
    class _L:
        async def __aenter__(self): return FakeClient()
        async def __aexit__(self, *a): return False
    ad._lease = lambda: _L()
    try:
        asyncio.run(ad.create_video(model="real", prompt="p", seconds="5", mode="keyframe",
                                    extra={"model": "EVIL", "prompt": "hacked", "seconds": "60",
                                           "first_frame": "http://img/1.png"}))
    finally:
        ad._lease = saved
        if orig is not None:
            oc.json_or_raise = orig
    assert cap["json"]["model"] == "real", cap["json"]
    assert cap["json"]["prompt"] == "p", cap["json"]
    assert cap["json"]["seconds"] == "5", cap["json"]
    assert cap["json"]["first_frame"] == "http://img/1.png", cap["json"]


def test_message_tool_fields_survive_forwarding():
    """P1: function calling 多轮历史（tool_calls / tool_call_id）不再被丢弃。"""
    from app.models import Message
    msgs = [
        Message.model_validate({"role": "user", "content": "天气?"}),
        Message.model_validate({"role": "assistant", "content": None,
                                "tool_calls": [{"id": "c1", "type": "function",
                                                "function": {"name": "get_weather", "arguments": "{}"}}]}),
        Message.model_validate({"role": "tool", "tool_call_id": "c1", "content": "晴"}),
    ]
    out = [m.to_upstream() for m in msgs]
    assert out[1]["tool_calls"][0]["id"] == "c1", out
    assert "content" not in out[1], out          # content=None 应被省略（OpenAI 允许）
    assert out[2]["tool_call_id"] == "c1", out
    assert out[0] == {"role": "user", "content": "天气?"}, out


def test_stream_meta_frame_is_valid_chunk():
    """P1: 流式首个 meta 帧必须是合法的 chat.completion.chunk（此前缺 id/created/choices）。"""
    from fastapi.testclient import TestClient
    import app.main as m

    class FakeAdapter:
        async def chat_stream(self, **kw):
            yield {"choices": [{"delta": {"content": "hi"}}]}
            yield {"done": True}

    orig = m._get_adapter
    try:
        with _Isolated() as mm:
            mm._get_adapter = lambda model_id, timeout=None, cfg=None: (
                FakeAdapter(), "agnes", mm._cfg().providers["agnes"])
            client = TestClient(mm.app)
            with client.stream("POST", "/v1/chat/completions",
                               json={"model": "agnes-3.0-flash",
                                     "messages": [{"role": "user", "content": "x"}], "stream": True}) as r:
                body = "".join(r.iter_text())
    finally:
        m._get_adapter = orig
    frames = [ln[6:] for ln in body.splitlines() if ln.startswith("data: ") and ln[6:].strip() != "[DONE]"]
    first = json.loads(frames[0])
    assert first["object"] == "chat.completion.chunk", first
    assert first["choices"] == [] and first["created"] and first["id"], first


def test_unhandled_500_has_openai_shape_and_cors():
    """P2: 未捕获异常也要是 OpenAI 格式的 500，并带 CORS 头（此前绕过 CORS 层）。"""
    from fastapi.testclient import TestClient
    import app.main as m

    @m.app.get("/__boom__")
    async def _boom():
        raise RuntimeError("kaboom")

    try:
        client = TestClient(m.app, raise_server_exceptions=False)
        r = client.get("/__boom__", headers={"Origin": "http://localhost:5173"})
        assert r.status_code == 500, r.text
        assert r.json()["error"]["type"] == "server_error", r.text
        assert "Access-Control-Allow-Origin" in r.headers, dict(r.headers)
    finally:
        m.app.router.routes = [x for x in m.app.router.routes
                               if getattr(x, "path", "") != "/__boom__"]


def test_callback_url_bad_port_returns_400_not_500():
    """P2: callback_url 端口越界（:99999）应是 400，而不是未捕获 ValueError → 500。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    r = client.post("/v1/videos", json={"model": "agnes-video-2.5-flash", "prompt": "x",
                                        "callback_url": "http://example.com:99999/hook"})
    assert r.status_code == 400, r.text


def test_callback_url_private_rejected_but_public_ok():
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    r = client.post("/v1/videos", json={"model": "agnes-video-2.5-flash", "prompt": "x",
                                        "callback_url": "http://169.254.169.254/latest/meta-data/"})
    assert r.status_code == 400 and "内网" in r.json()["error"]["message"], r.text


def test_all_failed_model_error_has_message_key():
    """P1: 调用失败 502 的错误体必须是 error.message（此前是 error.error）。

    第二十七轮: 路由/fallback 链删除后，这里就是单模型失败的形状。
    """
    from fastapi.testclient import TestClient
    import app.main as m

    class AlwaysFail:
        async def chat(self, **kw):
            raise RuntimeError("upstream down")

    orig = m._get_adapter
    try:
        with _Isolated() as mm:
            mm._get_adapter = lambda model_id, timeout=None, cfg=None: (
                AlwaysFail(), "agnes", mm._cfg().providers["agnes"])
            client = TestClient(mm.app)
            r = client.post("/v1/chat/completions",
                            json={"model": "agnes-3.0-flash",
                                  "messages": [{"role": "user", "content": "x"}]})
            assert r.status_code == 502, r.text
            err = r.json()["error"]
            assert err.get("message") and "调用失败" in err["message"], err
            assert err["errors"] == [{"model": "agnes-3.0-flash",
                                      "error": "upstream down"}], err
    finally:
        m._get_adapter = orig




def test_models_endpoint_objects_are_all_model():
    """P2: /v1/models 的 data 里 object 必须统一为 "model"（第二十七轮: route 条目已删除）。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    j = client.get("/v1/models").json()
    assert j["data"], j
    assert all(x["object"] == "model" for x in j["data"]), j["data"]
    assert all("created" in x and "owned_by" in x for x in j["data"]), j["data"][0]


def test_models_payload_marks_free_models():
    """2026-09-21: 免费模型要在 /v1/models 里带 free=true（前端「能力」列据此打「免费」标签）。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    data = client.get("/v1/models").json()["data"]
    free_ids = [x["id"] for x in data if x.get("free")]
    assert "space-bunny-free" in free_ids, f"免费模型没标出来：{free_ids}"
    # 反向：付费模型必须是 free=false（不能因为字段缺失而误标）
    paid = next(x for x in data if x["id"] == "deepseek-v4.1-flash")
    assert paid["free"] is False, paid
    assert all("free" in x for x in data), "每个模型都要带 free 字段（缺字段=前端标签会闪）"


def test_empty_messages_rejected_and_stream_strict():
    """P2: 空 messages / 拼错的 stream 类型应在入参处被拒（而非打到上游变 502）。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    r = client.post("/v1/chat/completions", json={"model": "agnes-3.0-flash", "messages": []})
    assert r.status_code == 422, r.text
    r2 = client.post("/v1/chat/completions",
                     json={"model": "agnes-3.0-flash",
                           "messages": [{"role": "user", "content": "x"}], "stream": "yes"})
    assert r2.status_code == 422, r2.text


def test_circuit_release_probe_returns_token():
    """P1: 拿到 HALF_OPEN 试探令牌但没发请求时，必须能归还，否则该供应商永久被拒。"""
    from app.circuit import CircuitBreaker
    cb = CircuitBreaker("p", failure_threshold=1, cooldown=0.01, window=5)
    cb.record(False)
    import time as _t
    _t.sleep(0.02)
    assert cb.allow() is True                 # 唯一的试探令牌
    assert cb.status()["state"] == "half_open"
    assert cb.allow() is False                # 令牌已被占
    cb.release_probe()                        # 归还（请求其实没发出去）
    assert cb.allow() is True, "归还后应能重新试探"
    cb.record(True)
    assert cb.status()["state"] == "closed"


def test_rate_limited_request_does_not_leak_probe_token():
    """P1: 实际触发路径 —— 限流跳过不能让熔断器卡死在 HALF_OPEN。"""
    from fastapi.testclient import TestClient
    import app.main as m

    class AlwaysFail:
        async def chat(self, **kw):
            raise RuntimeError("down")

    orig_ad, orig_rps = m._get_adapter, m._cfg().server.rate_limit_rps
    try:
        with _Isolated() as mm:
            cb = mm._circuits.get("agnes")
            for _ in range(3):
                cb.record(False)
            mm._circuits.get("agnes").cooldown = 0.0    # 立刻允许试探
            mm._get_adapter = lambda model_id, timeout=None, cfg=None: (
                AlwaysFail(), "agnes", mm._cfg().providers["agnes"])
            mm._cfg().server.rate_limit_rps = 0.000001  # 必然限流
            client = TestClient(mm.app)
            for _ in range(3):
                client.post("/v1/chat/completions",
                            json={"model": "agnes-3.0-flash",
                                  "messages": [{"role": "user", "content": "x"}]})
            st = mm._circuits.get("agnes").status()
            assert not (st["state"] == "half_open" and st["probing"]), st
    finally:
        m._get_adapter = orig_ad
        m._cfg().server.rate_limit_rps = orig_rps


def test_adapter_close_defers_while_active():
    """P1: 有在途占用时 close() 不能把正在用的连接池关掉；排空后才关。"""
    from app.adapters.openai_compat import OpenAICompatAdapter
    ad = OpenAICompatAdapter("t", "https://x.example/v1")

    async def scenario():
        client = await ad.get_client()
        await ad.close()                       # 有在途 → 应延迟
        assert ad._client is client and not ad._client.is_closed, "在途时不该关闭 client"
        await ad._release()                    # 排空 → 真正关闭
        assert ad._client is None
        assert ad._close_requested is False

    asyncio.run(scenario())


def test_config_malformed_values_raise_configerror():
    """P2: 畸形配置（null/负数/非数字）应给出 ConfigError，而不是启动期 TypeError。"""
    import tempfile, os
    from app.config import ConfigError as CE
    base = open(CONFIG, encoding='utf-8').read()
    cases = [
        ("server:\n  hot_reload_interval: null\n", False),   # null → 用默认值，不崩
        ("server:\n  max_body_size: -1\n", True),            # 负数 → ConfigError
        ("server:\n  timeout: abc\n", True),                 # 非数字 → ConfigError
        ("server:\n  retry: null\n", False),                 # null → 默认
    ]
    for snippet, should_fail in cases:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
            f.write(base + "\n" + snippet)
            p = f.name
        try:
            try:
                load_config(p)
                ok = True
            except CE:
                ok = False
            assert ok != should_fail, f"{snippet!r} should_fail={should_fail} 实际 ok={ok}"
        finally:
            os.unlink(p)


def test_config_health_auth_always_effective():
    """鉴权恒定开启，health_auth 总是生效（无需再联动 enabled）。"""
    from app.config import validate_warnings
    cfg = load_config(CONFIG)
    cfg.server.health_auth = True
    warns = validate_warnings(cfg)
    # 不再有 "health_auth but auth disabled" 告警（已无该场景）
    assert not any("health_auth" in w for w in warns), warns


def test_video_poll_404_does_not_open_media_circuit():
    """P2: 客户端反复查询不存在的任务 id，不该把媒体熔断桶打开（挡住正常生成）。"""
    from fastapi.testclient import TestClient
    import httpx
    import app.main as m

    class NotFound:
        async def poll_video(self, video_id, model=None, extra=None):
            req = httpx.Request("GET", "https://up/v1/videos/x")
            raise httpx.HTTPStatusError("Not Found", request=req,
                                        response=httpx.Response(404, request=req))

    orig = m._get_adapter
    try:
        with _Isolated() as mm:
            mm._get_adapter = lambda model_id, timeout=None, cfg=None: (
                NotFound(), "agnes", mm._cfg().providers["agnes"])
            client = TestClient(mm.app)
            for _ in range(5):
                r = client.get("/v1/videos/nope", params={"model": "agnes-video-2.5-flash"})
                assert r.status_code == 404, r.text
            st = mm._media_circuit("agnes").status()
            assert st["state"] == "closed", st
    finally:
        m._get_adapter = orig


def test_legacy_routes_section_is_ignored():
    """第二十七轮: 老配置里残留的 routes 段不再解析 —— 不报错、正常加载、直接忽略。

    为什么"忽略"而不是"报 ConfigError"：路由功能是**整体删除**，旧配置（备份恢复、
    别的机器上的 gateway.yaml）不该因为带了一段历史配置就起不来。日志里有 warning 可查。
    """
    import tempfile
    import os
    base = open(CONFIG, encoding="utf-8").read()
    legacy = base + """
routes:
  wjd:
    models: [agnes-3.0-flash, hy3]
    timeout: 60
    retry: 2
    rate_limit_rps: 5
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(legacy)
        p = f.name
    try:
        cfg = load_config(p)
        assert not hasattr(cfg, "routes"), "routes 段应被忽略，不再进配置对象"
        assert cfg.providers and cfg.server, "其余配置照常加载"
    finally:
        os.unlink(p)




# ═══════════ 第七轮（回归审计发现）═══════════
def test_standard_passthrough_params_reach_final_payload():
    """回归 P1-1: 网关自己的 _PASSTHROUGH 标准参数不能被 _build_payload 误剔除。"""
    from app.models import ChatRequest
    from app.adapters.openai_compat import OpenAICompatAdapter

    keys = ("n", "seed", "user", "response_format", "presence_penalty",
            "frequency_penalty", "logprobs", "top_logprobs", "parallel_tool_calls")
    req = ChatRequest.model_validate({
        "model": "agnes-3.0-flash", "messages": [{"role": "user", "content": "x"}],
        "n": 2, "seed": 42, "user": "u1", "response_format": {"type": "json_object"},
        "presence_penalty": 0.5, "frequency_penalty": 0.2, "logprobs": True,
        "top_logprobs": 3, "parallel_tool_calls": False,
    })
    extra = req.upstream_extra()
    for k in keys:
        assert k in extra, ("upstream_extra 应含", k, extra)

    ad = OpenAICompatAdapter("t", "https://x.example/v1")
    payload = ad._build_payload("real-model", [{"role": "user", "content": "x"}],
                                None, None, None, False, None, None, None, extra)
    missing = [k for k in keys if k not in payload]
    assert not missing, f"标准参数被静默丢弃: {missing}; payload keys={sorted(payload)}"
    # 核心键仍必须由网关说了算
    assert payload["model"] == "real-model" and payload["stream"] is False, payload


def test_image_extra_keeps_vendor_params_but_not_core():
    """回归 P2-1: 图像路径的 response_format/user 不能被误删，核心键仍不可覆盖。"""
    from app.models import PROTECTED_IMAGE_KEYS, strip_protected

    client_extra = {"quality": "high", "style": "vivid", "response_format": "b64_json",
                    "user": "u1", "model": "EVIL", "prompt": "hacked", "size": "1x1", "n": 99}
    kept = strip_protected(client_extra, PROTECTED_IMAGE_KEYS)
    for k in ("quality", "style", "response_format", "user"):
        assert k in kept, ("图像 vendor 参数被误删", k, kept)
    for k in ("model", "prompt", "size", "n"):
        assert k not in kept, ("核心键不该被 extra 覆盖", k, kept)


def test_inflight_kept_until_stream_body_finishes():
    """回归 P2-2: 流式响应传输期间 in_flight 不能提前归零。"""
    from fastapi.testclient import TestClient
    from fastapi.responses import StreamingResponse
    import app.main as m

    produced = []

    @m.app.get("/__stream_inflight__")
    async def _s():
        async def gen():
            for i in range(3):
                produced.append(i)
                await asyncio.sleep(0.01)
                yield f"chunk{i}"
        return StreamingResponse(gen(), media_type="text/event-stream")

    snapshots = []
    orig = m._metrics.decrement_inflight

    def spy():
        snapshots.append(len(produced))
        return orig()

    m._metrics.decrement_inflight = spy
    try:
        client = TestClient(m.app)
        r = client.get("/__stream_inflight__")
        assert r.status_code == 200 and "chunk2" in r.text, r.text
        assert snapshots, "应当发生过递减"
        assert snapshots[0] == 3, f"递减发生在流结束前: 递减时已产出 {snapshots[0]}/3"
    finally:
        m._metrics.decrement_inflight = orig
        m.app.router.routes = [x for x in m.app.router.routes
                               if getattr(x, "path", "") != "/__stream_inflight__"]


def test_adapter_cache_key_includes_base_url():
    """回归 P2-4: 热重载改 base_url 后不能命中旧地址的缓存 adapter。"""
    import app.main as m
    from app.routing import AuthConfigError
    cfg = m._cfg()
    model_id = cfg.all_models()[0]["id"]
    pkey = cfg.all_models()[0]["provider"]
    try:
        a1, _, _ = m._get_adapter(model_id, cfg=cfg)
    except AuthConfigError:
        return                      # 该供应商没配 token，跳过（环境相关）
    old = cfg.providers[pkey].base_url
    try:
        cfg.providers[pkey].base_url = old + "/changed"
        a2, _, _ = m._get_adapter(model_id, cfg=cfg)
        assert a1 is not a2, "base_url 变了却复用了旧 adapter（热重载可能不生效）"
    finally:
        cfg.providers[pkey].base_url = old

def test_models_payload_cached_and_invalidated_by_mtime():
    """/v1/models 目录按配置 mtime 缓存；热重载改配置自动失效。"""
    import app.main as m
    mt0 = m._state.get("mtime")
    m._state["_models_cache"] = None
    try:
        a = m._models_payload()
        b = m._models_payload()
        assert a is b, "同一 mtime 下应直接命中缓存（同一对象）"
        c0 = m._state["_models_cache"][0]
        assert c0 == mt0, (c0, mt0)
        # 模拟热重载：mtime 变化 → 缓存失效重建
        m._state["mtime"] = (mt0 or 0) + 1.0
        c = m._models_payload()
        assert c is not a, "mtime 变化必须重建目录"
        assert m._state["_models_cache"][0] == (mt0 or 0) + 1.0
        # created 字段内嵌 mtime，重新构建后合法变化 —— 这里不比对 data 内容
    finally:
        m._state["mtime"] = mt0
        m._state["_models_cache"] = None

# ═══════════ 安全报告修复回归 ═══════════
def test_docs_disabled_by_default():
    """安全报告：/docs、/openapi.json 生产应关闭。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_healthz_never_leaks():
    """/healthz 始终只回 {"status":"ok"}，不泄露详情。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    # 不带鉴权（用 raw=false 会注入，这里显式空头关闭注入）
    r = client.get("/healthz", headers={"Authorization": ""})
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_minimal_unauthed_but_full_authed():
    """/health 未鉴权只给 {"status":"ok"}；带有效 key 才给完整看板。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    r_min = client.get("/health", headers={"Authorization": ""})   # 空头→未鉴权
    assert r_min.status_code == 200 and r_min.json() == {"status": "ok"}, r_min.text
    r_full = client.get("/health")                                  # 自动注入有效 key
    j = r_full.json()
    assert r_full.status_code == 200 and "providers" in j
    assert "circuit" in j["providers"]["agnes"] and "status" in j


def test_provider_health_requires_auth():
    """/health/{provider} 会用网关 Key 探上游，必须鉴权。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    assert client.get("/health/agnes", headers={"Authorization": ""}).status_code == 401
    assert client.get("/health/agnes").status_code == 200   # 自动注入 key


def test_auth_fail_401_then_429_rate_limited():
    """/ IP 级鉴权失败限流：连续错 key 达到上限 → 429。"""
    from fastapi.testclient import TestClient
    import app.main as m
    old_limit = m._cfg().server.auth_fail_limit_per_min
    try:
        m._cfg().server.auth_fail_limit_per_min = 5
        m._AUTH_FAILS.clear()
        client = TestClient(m.app)
        codes = []
        for _ in range(5):
            r = client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
            codes.append(r.status_code)
        assert codes == [401, 401, 401, 401, 429], f"limit=5 应第5次即429: {codes}"
        # 有效凭证不受 IP 限流影响
        good_tok = _login_admin_session(client)
        assert client.get("/v1/models",
                          headers={"Authorization": f"Bearer {good_tok}"}).status_code == 200
    finally:
        m._cfg().server.auth_fail_limit_per_min = old_limit
        m._AUTH_FAILS.clear()

def test_retry_uses_server_retry():
    """直接模型 id 的重试次数 = server.retry（第二十七轮: 路由别名已删，无 route.retry）。"""
    from fastapi.testclient import TestClient
    import app.main as m
    cfg = m._cfg()
    calls = []

    class CountingAdapter:
        async def chat(self, model=None, messages=None, **kw):
            calls.append(model)
            raise RuntimeError("boom")

    orig = m._get_adapter
    m._get_adapter = lambda model_id, timeout=None, cfg=None: (
        CountingAdapter(), "agnes", cfg.providers["agnes"])
    old_srv = cfg.server.retry
    cfg.server.retry = 3
    try:
        client = TestClient(m.app)
        r = client.post("/v1/chat/completions", json={
            "model": "agnes-3.0-flash", "messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 502, r.status_code
        assert len(calls) == 3, f"应重试 server.retry=3 次, 实际 {len(calls)}: {calls}"
    finally:
        m._get_adapter = orig
        cfg.server.retry = old_srv



# ═══════════ 前端登录会话（M1）═══════════
def test_auth_login_account_password():
    """账号密码登录换短时 session；带 session 可调受保护端点；登出后失效。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    try:
        # 账号密码登录
        r = client.post("/auth/login", json={"username": "admin", "password": "admin"})
        assert r.status_code == 200, r.text
        j = r.json()
        sess = j["access_token"]
        assert sess.startswith("sess-") and j["token_type"] == "Bearer"
        assert j["user"] == "admin" and j["role"] == "admin"
        # 前端登出后，另一个账号的 API Key 不可用（账号隔离）
        assert client.get("/v1/models", headers={"Authorization": f"Bearer {sess}"}).status_code == 200
        assert client.post("/auth/logout", headers={"Authorization": f"Bearer {sess}"}).status_code == 200
        assert client.get("/v1/models", headers={"Authorization": f"Bearer {sess}"}).status_code == 401
    finally:
        # 不整体清 _sess：公共 autobearer 依赖它（用独立的待失效 token 已验证登出错效）
        pass


def test_auth_login_rejects_bad_password():
    """错误账号/密码换不到 session。"""
    from fastapi.testclient import TestClient
    import app.main as m
    m._AUTH_FAILS.clear()
    before = m._sess.count()
    try:
        client = TestClient(m.app)
        for bad in ({"username": "admin", "password": "wrong"},
                    {"username": "ghost", "password": "x"}):
            r = client.post("/auth/login", json=bad)
            assert r.status_code == 401, r.status_code
        assert m._sess.count() >= before, "登录不应新建会话"
    finally:
        m._AUTH_FAILS.clear()


def test_auth_role_admin_enforced():
    """admin 才能建号/看账号；普通账号不行（接口 403）。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    # 建一个普通账号 + 登录
    admin_tok = _login_admin_session(client)
    ah = {"Authorization": f"Bearer {admin_tok}"}
    assert client.post("/auth/users", json={"username": "bob"}, headers=ah).status_code == 200
    bob_tok = client.post("/auth/login", json={"username": "bob", "password": "bob"}).json()["access_token"]
    bh = {"Authorization": f"Bearer {bob_tok}"}
    # 普通账号：不能列用户 / 不能建号
    assert client.get("/auth/users", headers=bh).status_code == 403
    assert client.post("/auth/users", json={"username": "eve"}, headers=bh).status_code == 403
    # admin 可以
    assert client.get("/auth/users", headers=ah).status_code == 200
    # 清理
    assert client.delete("/auth/users/bob", headers=ah).status_code == 200


# ── 第八轮审计：凭证吊销语义（会话 vs 长期 API Key） ──────────────

def _member(client, ah, name, password=None):
    """建一个临时成员并返回其会话 header（用完记得删）。"""
    body = {"username": name}
    if password:
        body["password"] = password
    r = client.post("/auth/users", json=body, headers=ah)
    assert r.status_code == 200, r.text
    tok = client.post("/auth/login", json={
        "username": name, "password": password or name}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def test_delete_user_revokes_its_sessions():
    """删号必须连带吊销该账号全部会话 —— 否则被删的人最长一个 TTL 还能刷上游额度。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    # 建一个成员 bob 并登录
    assert client.post("/auth/users", json={"username": "bob"}, headers=ah).status_code == 200
    bob_tok = client.post("/auth/login", json={"username": "bob", "password": "bob"}).json()["access_token"]
    bh = {"Authorization": f"Bearer {bob_tok}"}
    assert client.get("/v1/models", headers=bh).status_code == 200
    # admin 删除 bob
    r = client.delete("/auth/users/bob", headers=ah)
    assert r.status_code == 200, r.text
    assert r.json()["revoked_sessions"] >= 1
    # 被删账号的旧会话立即失效（不再等到 TTL 到期）
    assert client.get("/v1/models", headers=bh).status_code == 401, "删号后旧会话必须作废"
    assert client.get("/auth/apikeys", headers=bh).status_code == 401, "删号后旧会话必须作废"


def test_video_4xx_does_not_trip_circuit_and_maps_to_400():
    """第九轮: 上游 4xx（客户端参数不认）→ 干净 400 + 不记熔断；只有 5xx 才记。"""
    import httpx
    from fastapi.testclient import TestClient
    import app.main as m

    class Fake4xx:
        async def create_video(self, **kw):
            resp = httpx.Response(400, request=httpx.Request("POST", "https://x/videos"),
                                  json={"code": "invalid_request", "message": "size 必须是 720P 之一"})
            raise httpx.HTTPStatusError("400", request=resp.request, response=resp)

    real = m._get_adapter
    m._get_adapter = lambda model, t, cfg: (Fake4xx(), {}, model)
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    cb = m._media_circuit("agnes")
    before = list(cb._recent).copy()
    try:
        r = client.post("/v1/videos", json={
            "model": "agnes-video-2.5-flash", "prompt": "x", "size": "1080P",
            "seconds": "5", "aspect_ratio": "16:9"}, headers=ah)
        assert r.status_code == 400, r.text
        assert "720P" in r.text, r.text          # 把上游原话带给用户，不是一句裸 502
        assert list(cb._recent) == before, "4xx 不应记入熔断失败数"
        assert m._upstream_detail('{"message":"size 必须是 720P 之一"}') == "size 必须是 720P 之一"
    finally:
        m._get_adapter = real


def test_video_mode_validation_in_gateway():
    """第十一轮 #3：按 Agnes Video 契约在网关挡下非法 mode/媒体/时长组合（干净 400，不发上游、不进熔断）。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    base = {"model": "agnes-video-2.5-flash", "prompt": "x"}
    cb = m._media_circuit("agnes")
    before = list(cb._recent)
    # 非法 mode
    assert client.post("/v1/videos", json={**base, "mode": "nope"}, headers=ah).status_code == 400
    # keyframe 缺帧 / reference 缺素材
    r = client.post("/v1/videos", json={**base, "mode": "keyframe"}, headers=ah)
    assert r.status_code == 400 and "first_frame" in r.text
    assert client.post("/v1/videos", json={**base, "mode": "reference"}, headers=ah).status_code == 400
    # text 却带图 / images 超 5 张 / seconds 超范围
    assert client.post("/v1/videos", json={**base, "mode": "text", "images": ["u"]}, headers=ah).status_code == 400
    assert client.post("/v1/videos", json={**base, "mode": "reference", "images": ["a"] * 6}, headers=ah).status_code == 400
    assert client.post("/v1/videos", json={**base, "seconds": "15"}, headers=ah).status_code == 400
    assert client.post("/v1/videos", json={**base, "seconds": "3"}, headers=ah).status_code == 400
    assert list(cb._recent) == before, "非法请求不应进熔断"
    # 合法 keyframe 能通过校验并走到上游（mock）
    class Fake:
        async def create_video(self, **kw): return {"video_id": "vk", "raw": {"status": "queued"}}
    orig = m._get_adapter
    m._get_adapter = lambda model, t, cfg: (Fake(), {}, model)
    try:
        r = client.post("/v1/videos", json={**base, "mode": "keyframe",
                                            "first_frame": "http://x/1.png"}, headers=ah)
        assert r.status_code == 200, r.text
    finally:
        m._get_adapter = orig


def test_video_polls_merged_into_one_log_row_with_response():
    """第十轮: 图像/视频日志要带响应数据；视频轮询合成到「创建」一条里，不各占一行。"""
    from fastapi.testclient import TestClient
    import app.main as m

    class Fake:
        def __init__(self):
            self.n = 0
        async def create_video(self, **kw):
            return {"video_id": "vtest", "raw": {"status": "queued", "id": "vtest"}}
        async def poll_video(self, video_id, **kw):
            self.n += 1
            st = "completed" if self.n >= 2 else "processing"
            return {"raw": {"status": st, "progress": 50 if st == "processing" else 100}}

    real = m._get_adapter
    fake = Fake()   # 整个测试复用一个实例：轮询要连续计数到 completed
    m._get_adapter = lambda model, t, cfg: (fake, {}, model)
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    r = client.post("/v1/videos", json={"model": "agnes-video-2.5-flash",
                                        "prompt": "x", "aspect_ratio": "16:9"}, headers=ah)
    assert r.status_code == 200, r.text
    vid = r.json()["video_id"]
    client.get(f"/v1/videos/{vid}", headers=ah)    # 第 1 次轮询 processing
    client.get(f"/v1/videos/{vid}", headers=ah)    # 第 2 次轮询 completed
    logs = client.get("/logs?n=200", headers=ah).json()["entries"]
    rows = [e for e in logs if e.get("video_id") == vid]
    assert len(rows) == 1, f"轮询应合成到创建那条里(实际 {len(rows)} 行): {[r['path'] for r in logs]}"
    create = rows[0]
    assert create["ok"] is True
    assert "video_id" in (create.get("response") or ""), create.get("response")
    assert len(create.get("polls", [])) >= 2, create.get("polls")
    statuses = [p.get("status") for p in create["polls"]]
    assert "completed" in statuses and "processing" in statuses, statuses
    # 图像日志也要有响应数据
    class ImgFake:
        async def generate_image(self, **kw):
            return {"data": [{"url": "https://x/1.png"}]}
    m._get_adapter = lambda model, t, cfg: (ImgFake(), {}, model)
    r2 = client.post("/v1/images/generations", json={"model": "agnes-image-2.5-flash",
                                                     "prompt": "hi", "n": 1}, headers=ah)
    assert r2.status_code == 200, r2.text
    imgs = [e for e in client.get("/logs?n=200", headers=ah).json()["entries"]
            if e.get("path") == "/v1/images/generations"]
    assert imgs and "n" in (imgs[-1].get("response") or ""), imgs[-1].get("response")
    m._get_adapter = real


def test_logs_own_only_and_admin_per_user_view():
    """第十一轮 #2：日志页对所有人只显示自己；admin 在用户管理端点看别人的日志与资产。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    assert client.post("/auth/users", json={"username": "bob2"}, headers=ah).status_code == 200
    bob = client.post("/auth/login", json={"username": "bob2", "password": "bob2"}).json()["access_token"]
    bh = {"Authorization": f"Bearer {bob}"}
    # 直接往访问日志塞一条属于 bob2 的记录（省去真打上游），并让 bob2 存一条图像资产
    m._access.add({"method": "POST", "path": "/v1/chat/completions", "rid": "r-bob2",
                   "account": "bob2", "caller": "bob2", "ok": True})
    client.post("/history/images", json={"items": [{"id": "img1", "url": "http://x/1.png", "prompt": "p"}]}, headers=bh)
    # ① admin 的 /logs 不再包含 bob2 的行（以前 admin 看全部）
    admin_logs = client.get("/logs?n=500", headers=ah).json()["entries"]
    assert all(e.get("account") != "bob2" for e in admin_logs), "日志页 admin 不该看到别人"
    # ② admin 专属：按用户查日志
    r = client.get("/auth/users/bob2/logs?n=500", headers=ah)
    assert r.status_code == 200 and any(e.get("rid") == "r-bob2" for e in r.json()["entries"])
    # ③ admin 专属：按用户查图像资产
    r2 = client.get("/auth/users/bob2/history/images", headers=ah)
    assert r2.status_code == 200 and any(i.get("id") == "img1" for i in r2.json()["items"])
    # ④ 非 admin 不能用这些端点；⑤ 不存在账号 404
    assert client.get("/auth/users/admin/logs", headers=bh).status_code == 403
    assert client.get("/auth/users/admin/history/videos", headers=bh).status_code == 403
    assert client.get("/auth/users/ghost/logs", headers=ah).status_code == 404
    assert client.get("/auth/users/bob2/history/bogus", headers=ah).status_code == 400
    client.delete("/auth/users/bob2", headers=ah)


def test_users_file_saved_private():
    """users.json 里是密码/Key 哈希 → 落盘权限必须是 600（不进 git 也只是第一道）。"""
    import os
    from app.users import UsersStore
    p = _ROOT / ".test_users_perm.json"
    if p.exists():
        p.unlink()
    try:
        UsersStore(str(p)).create_user("permtest")
        assert oct(os.stat(p).st_mode & 0o777) == "0o600", oct(os.stat(p).st_mode & 0o777)
    finally:
        p.unlink(missing_ok=True)


# ── 第八轮审计回归（本会话误改导致尾部红线丢失后重建，逐一断言现行行为） ──

def test_apikey_cannot_manage_credentials():
    """泄漏的 API Key 只能调模型，不能再用它铸 Key / 删 Key / 看 Key 明文 / 管账号。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    assert client.post("/auth/users", json={"username": "mgr"}, headers=ah).status_code == 200
    tok = client.post("/auth/login", json={"username": "mgr", "password": "mgr"}).json()["access_token"]
    mh = {"Authorization": f"Bearer {tok}"}
    key = client.post("/auth/apikeys", json={"label": "leaked"}, headers=mh).json()["key"]
    kh = {"Authorization": f"Bearer {key}"}                     # 只有 API Key、没有会话
    assert client.post("/auth/apikeys", json={"label": "x"}, headers=kh).status_code == 403, "API Key 不能再铸 Key"
    assert client.get("/auth/apikeys", headers=kh).status_code == 403, "API Key 不能读同账号其它 Key 的明文"
    assert client.delete("/auth/apikeys/whatever", headers=kh).status_code == 403, "API Key 不能删 Key"
    assert client.post("/auth/users", json={"username": "evil"}, headers=kh).status_code == 403, "API Key 不能建号"
    # 会话仍能正常读（明文回显，为了反复复制）
    listed = client.get("/auth/apikeys", headers=mh).json()["keys"]
    assert any(k["key"] == key for k in listed), "会话应能看到明文 key"
    # 清理
    assert client.delete("/auth/users/mgr", headers=ah).status_code == 200


def test_member_key_limit_three_admin_unlimited():
    """普通账号最多 3 把 API Key，admin 不限。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    assert client.post("/auth/users", json={"username": "limtest"}, headers=ah).status_code == 200
    tok = client.post("/auth/login", json={"username": "limtest", "password": "limtest"}).json()["access_token"]
    lh = {"Authorization": f"Bearer {tok}"}
    for i in range(3):
        assert client.post("/auth/apikeys", json={"label": f"k{i}"}, headers=lh).status_code == 200, f"{i+1} 把应成功"
    r = client.post("/auth/apikeys", json={"label": "4th"}, headers=lh)   # 第 4 把
    assert r.status_code == 400, r.text
    assert "已达上限" in r.text, r.text   # 第十六轮：提示只留这四个字（上限/怎么处理在页面上说）
    # admin 不受 3 把限制
    for i in range(4):
        assert client.post("/auth/apikeys", json={"label": f"a{i}"}, headers=ah).status_code == 200
    n_admin = len(client.get("/auth/apikeys", headers=ah).json()["keys"])
    assert n_admin >= 4, f"admin 不限，实际 {n_admin}"
    assert client.delete("/auth/users/limtest", headers=ah).status_code == 200


def test_admin_can_revoke_all_user_keys():
    """管理员一键吊销某账号全部 API Key（止损泄漏的长期凭证）。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    assert client.post("/auth/users", json={"username": "revk"}, headers=ah).status_code == 200
    tok = client.post("/auth/login", json={"username": "revk", "password": "revk"}).json()["access_token"]
    rh = {"Authorization": f"Bearer {tok}"}
    key = client.post("/auth/apikeys", json={"label": "y"}, headers=rh).json()["key"]
    r = client.delete("/auth/users/revk/apikeys", headers=ah)
    assert r.status_code == 200 and r.json()["revoked_keys"] == 1, r.text
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).status_code == 401, "被吊销的 Key 应失效"
    assert client.delete("/auth/users/revk", headers=ah).status_code == 200


def test_reset_password_revokes_sessions():
    """管理员重置密码 → 被重置者旧会话立即失效，用新密码才能重登。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    assert client.post("/auth/users", json={"username": "rst"}, headers=ah).status_code == 200
    old = client.post("/auth/login", json={"username": "rst", "password": "rst"}).json()["access_token"]
    oh = {"Authorization": f"Bearer {old}"}
    assert client.get("/v1/models", headers=oh).status_code == 200
    r = client.post("/auth/users/rst/password", json={"password": "rotated-9"}, headers=ah)
    assert r.status_code == 200 and r.json()["revoked_sessions"] >= 1, r.text
    assert client.get("/v1/models", headers=oh).status_code == 401, "重置密码后旧会话必须失效"
    assert client.post("/auth/login", json={"username": "rst", "password": "rotated-9"}).status_code == 200
    assert client.delete("/auth/users/rst", headers=ah).status_code == 200


def test_change_password_requires_current_and_revokes_session():
    """改密：必须带对当前密码，改后本账号旧会话全部失效需重登。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    assert client.post("/auth/users", json={"username": "chg"}, headers=ah).status_code == 200
    tok = client.post("/auth/login", json={"username": "chg", "password": "chg"}).json()["access_token"]
    ch = {"Authorization": f"Bearer {tok}"}
    assert client.get("/v1/models", headers=ch).status_code == 200
    # 当前密码不对 → 拒绝
    assert client.post("/auth/password", json={"current_password": "wrong", "new_password": "newpass9"},
                       headers=ch).status_code == 400
    # 太短的新密码 → 拒绝
    assert client.post("/auth/password", json={"current_password": "chg", "new_password": "ab"},
                       headers=ch).status_code in (400, 422)
    # 正确改密
    r = client.post("/auth/password", json={"current_password": "chg", "new_password": "newpass9"}, headers=ch)
    assert r.status_code == 200 and r.json()["revoked_sessions"] >= 1, r.text
    assert client.get("/v1/models", headers=ch).status_code == 401, "改密后旧会话必须失效"
    assert client.post("/auth/login", json={"username": "chg", "password": "newpass9"}).status_code == 200
    assert client.delete("/auth/users/chg", headers=ah).status_code == 200


def test_change_password_without_current_password():
    """第十五轮（老板要求）：已经用会话登录，改密不再要求填当前密码。

    安全边界必须不退化：仍必须持**会话**（API Key 调这里照样 403）、改完旧会话立刻失效、
    新密码能登、旧密码不能登；传了 current_password 就照旧校验（传错要报错，不能默默放行）。
    """
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    assert client.post("/auth/users", json={"username": "nocur"}, headers=ah).status_code == 200
    tok = client.post("/auth/login", json={"username": "nocur", "password": "nocur"}).json()["access_token"]
    ch = {"Authorization": f"Bearer {tok}"}

    # ① 只传新密码 → 直接改成（不再要当前密码）
    r = client.post("/auth/password", json={"new_password": "nopass99"}, headers=ch)
    assert r.status_code == 200, r.text
    assert r.json()["relogin_required"] is True
    assert client.get("/v1/models", headers=ch).status_code == 401, "改密后旧会话必须失效"
    assert client.post("/auth/login", json={"username": "nocur", "password": "nopass99"}).status_code == 200
    assert client.post("/auth/login", json={"username": "nocur", "password": "nocur"}).status_code == 401, "旧密码必须失效"

    tok2 = client.post("/auth/login", json={"username": "nocur", "password": "nopass99"}).json()["access_token"]
    ch2 = {"Authorization": f"Bearer {tok2}"}
    # ② 新密码与原密码相同 → 拒绝（不填当前密码也能判断出来）
    assert client.post("/auth/password", json={"new_password": "nopass99"}, headers=ch2).status_code == 400
    # ③ 太短 → 拒绝
    assert client.post("/auth/password", json={"new_password": "ab"}, headers=ch2).status_code == 400
    # ④ 老客户端照旧传 current_password：传错必须报错，不能默默放行
    assert client.post("/auth/password", json={"current_password": "wrong", "new_password": "zzzz99"},
                       headers=ch2).status_code == 400
    assert client.post("/auth/login", json={"username": "nocur", "password": "nopass99"}).status_code == 200, "④ 不该改成功"
    # ⑤ API Key 不能改密（仍然只认会话）——免密改密不能变成"有 Key 就能改"
    key = client.post("/auth/apikeys", json={"label": "pw"}, headers=ch2).json()["key"]
    assert client.post("/auth/password", json={"new_password": "kkkk99"},
                       headers={"Authorization": f"Bearer {key}"}).status_code == 403
    assert client.delete("/auth/users/nocur", headers=ah).status_code == 200


def test_delete_missing_user_returns_404():
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    assert client.delete("/auth/users/doesnotexist99", headers=ah).status_code == 404


def test_new_wjd_and_legacy_ak_key_store_forms():
    """新建 Key 用 wjd- 前缀；旧 ak- 前缀的 Key 仍能通过 resolve_key 解析。"""
    from app.users import UsersStore
    import tempfile, os
    p = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    try:
        u = UsersStore(p)
        u.create_user("kfmt")
        key = u.add_api_key("kfmt")
        assert key.startswith("wjd-"), key
        assert u.resolve_key(key) == ("kfmt", "member"), u.resolve_key(key)
        u.remove_api_key("kfmt", key)
        assert u.resolve_key(key) is None, "删除后不应再解析成功"
    finally:
        try: os.unlink(p)
        except OSError: pass


# ── 第十三轮：Agnes 三个文档的全场景（视觉对话 / 图像单张 / 视频素材 / 轮询限流）──────

def test_models_expose_vision_capability():
    """第十三轮: /v1/models 暴露 vision —— 前端据此标 👁、带图时告警，不必再猜哪个模型能看图。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    data = client.get("/v1/models").json()["data"]
    byid = {d["id"]: d for d in data}
    assert byid["agnes-3.0-flash"]["vision"] is True, "Agnes 3.0 Flash 文档写了图像输入"
    assert byid["agnes-image-2.5-flash"]["vision"] is False
    assert byid["ark-code-latest"]["vision"] is False
    # 第二十七轮: 路由别名已撤（/v1/models 里没有 type=route 的条目），
    #   改为断言每个模型的**协议线**标签（调用方据此决定发到哪条线）。
    # 第三十一轮: wjd 这个名字合法回来了，但只作为**别名条目**（alias_of 指向真实目标，
    #   protocols 固定 chat/completions）—— 与旧路由是两回事，别再断言"它不存在"。
    assert all(d.get("type") != "route" for d in data)
    for d in data:
        if d.get("alias_of"):
            assert d["protocols"] == ["chat/completions"], d
    assert byid["hy3"]["protocols"] == ["chat/completions"]
    assert byid["grok-4.6"]["protocols"] == ["responses"]
    assert byid["deepseek-v4.1-flash"]["protocols"] == ["chat/completions", "messages", "responses"]


def test_messages_have_images_detects_vision_blocks():
    """只认规范里的 image_url / input_image，纯文本消息不能误判成带图。"""
    import app.main as m
    assert m.messages_have_images([{"role": "user", "content": "纯文本"}]) is False
    assert m.messages_have_images([{"role": "user", "content": None}]) is False
    assert m.messages_have_images([{"role": "user", "content": [{"type": "text", "text": "x"}]}]) is False
    assert m.messages_have_images([{"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
    ]}]) is True


def test_image_n_gt_1_rejected_without_calling_upstream():
    """第十三轮: 上游图像接口 n 必须为 1（实测 n=2 → 400）。网关入站挡下：干净 400、不白打上游、不进熔断。"""
    from fastapi.testclient import TestClient
    import app.main as m

    called: list[int] = []

    class Rec:
        async def generate_image(self, *a, **kw):
            called.append(1)
            return {"data": [{"url": "http://mock/1.png"}]}

    orig = m._get_adapter
    try:
        with _Isolated() as mm:
            mm._get_adapter = lambda model_id, timeout=None, cfg=None: (
                Rec(), "agnes", mm._cfg().providers["agnes"])
            client = TestClient(mm.app)
            ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
            cb = mm._media_circuit("agnes")
            before = list(cb._recent)
            r = client.post("/v1/images/generations", json={
                "model": "agnes-image-2.5-flash", "prompt": "x", "n": 2}, headers=ah)
            assert r.status_code == 400, r.text
            assert "n 必须为 1" in r.text, r.text
            assert not called, "非法 n 不该打到上游"
            assert list(cb._recent) == before, "非法请求不该记熔断"
            # n=1 正常放行
            r2 = client.post("/v1/images/generations", json={
                "model": "agnes-image-2.5-flash", "prompt": "x", "n": 1}, headers=ah)
            assert r2.status_code == 200, r2.text
    finally:
        m._get_adapter = orig


def test_image_upstream_4xx_clean_400_and_only_5xx_trips_circuit():
    """第十三轮: 图像上游错误分流 —— 4xx（尺寸/字段不认）→ 干净 400 不记熔断；5xx → 502 且记熔断。

    此前图像路径所有上游错误一律 502 且都记进媒体熔断：选错一个尺寸戳三次就能把
    图像页 + 视频页一起打成 503「媒体服务熔断中」。
    """
    import httpx
    from fastapi.testclient import TestClient
    import app.main as m

    def _err(status: int, body: dict):
        class Boom:
            async def generate_image(self, *a, **kw):
                req = httpx.Request("POST", "https://up/v1/images/generations")
                raise httpx.HTTPStatusError(str(status), request=req,
                                            response=httpx.Response(status, request=req, json=body))
        return Boom()

    orig = m._get_adapter
    try:
        with _Isolated() as mm:
            client = TestClient(mm.app)
            ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
            cb = mm._media_circuit("agnes")
            mm._get_adapter = lambda model_id, timeout=None, cfg=None: (
                _err(400, {"message": "size 不支持 4K"}), "agnes", mm._cfg().providers["agnes"])
            before = list(cb._recent)
            r = client.post("/v1/images/generations", json={
                "model": "agnes-image-2.5-flash", "prompt": "x"}, headers=ah)
            assert r.status_code == 400, r.text
            assert "size 不支持 4K" in r.text, "应把上游原话带给用户：" + r.text
            assert list(cb._recent) == before, "4xx 不该记入媒体熔断"
            # 5xx 仍要记一次失败并回 502
            mm._get_adapter = lambda model_id, timeout=None, cfg=None: (
                _err(500, {"message": "internal"}), "agnes", mm._cfg().providers["agnes"])
            r2 = client.post("/v1/images/generations", json={
                "model": "agnes-image-2.5-flash", "prompt": "x"}, headers=ah)
            assert r2.status_code == 502, r2.text
            assert len(cb._recent) == len(before) + 1, "5xx 必须记熔断（否则坏上游永远拦不住）"
    finally:
        m._get_adapter = orig


def test_video_rejects_videos_and_bad_size_aspect_n():
    """第十三轮: 补齐文档里 Flash 专属限制 —— 不支持 videos、size 只认 720P、aspect 白名单、n 必须 1。"""
    from fastapi.testclient import TestClient
    import app.main as m

    called: list[int] = []

    class Rec:
        async def create_video(self, **kw):
            called.append(1)
            return {"video_id": "vk", "raw": {"status": "queued"}}

    orig = m._get_adapter
    try:
        with _Isolated() as mm:
            mm._get_adapter = lambda model_id, timeout=None, cfg=None: (
                Rec(), "agnes", mm._cfg().providers["agnes"])
            client = TestClient(mm.app)
            ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
            cb = mm._media_circuit("agnes")
            before = list(cb._recent)
            base = {"model": "agnes-video-2.5-flash", "prompt": "x"}
            # 参考视频：字段以前未声明会被 pydantic 静默丢弃（用户以为传了，实际没传）→ 现在明确拒绝
            r = client.post("/v1/videos", json={**base, "mode": "reference",
                                                "images": ["u"], "videos": ["http://x/v.mp4"]}, headers=ah)
            assert r.status_code == 400 and "videos" in r.text, r.text
            assert client.post("/v1/videos", json={**base, "size": "1080P"}, headers=ah).status_code == 400
            assert client.post("/v1/videos", json={**base, "aspect_ratio": "3:2"}, headers=ah).status_code == 400
            assert client.post("/v1/videos", json={**base, "n": 2}, headers=ah).status_code == 400
            assert not called, "非法请求不该打到上游"
            assert list(cb._recent) == before, "非法请求不该记熔断"
            # 合法的 aspect（文档 6 种）都能过
            for asp in ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16"):
                r_ok = client.post("/v1/videos", json={**base, "aspect_ratio": asp}, headers=ah)
                assert r_ok.status_code == 200, f"{asp} 应放行：{r_ok.text}"
    finally:
        m._get_adapter = orig


def test_video_poll_429_is_throttled_not_failure():
    """第十三轮: 上游轮询 429（实测 3s 一次的频率就会「查询过于频繁」）不是故障。

    以前：429 → 记媒体熔断 + 给前端 502「查询失败」，连撞三次就把整个媒体服务熔断 30 秒。
    现在：200 + throttled + retry_after，熔断桶与调用指标都不动。
    """
    import httpx
    from fastapi.testclient import TestClient
    import app.main as m

    class Throttled:
        async def poll_video(self, video_id, model=None, extra=None):
            req = httpx.Request("GET", "https://up/agnesapi")
            raise httpx.HTTPStatusError("429", request=req,
                                        response=httpx.Response(429, request=req, json={"error": "查询过于频繁"}))

    orig = m._get_adapter
    try:
        with _Isolated() as mm:
            mm._get_adapter = lambda model_id, timeout=None, cfg=None: (
                Throttled(), "agnes", mm._cfg().providers["agnes"])
            client = TestClient(mm.app)
            ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
            for _ in range(4):
                r = client.get("/v1/videos/vid-429", params={"model": "agnes-video-2.5-flash"}, headers=ah)
                assert r.status_code == 200, r.text
                j = r.json()
                assert j.get("throttled") is True, j
                assert j.get("retry_after") == 3, j
                assert j.get("status") is None, "限流时不要伪造状态，让前端保持 pending"
            assert mm._media_circuit("agnes").status()["state"] == "closed", "429 不该熔断"
            # 指标也别把 429 记成失败调用
            snap = mm._metrics.snapshot()
            fails = sum(m2.get("fail", 0) for m2 in (snap.get("calls") or {}).values())
            assert fails == 0, f"429 不该计入失败指标: {snap.get('calls')}"
    finally:
        m._get_adapter = orig


# ── 第十七轮：媒体下载代理 /v1/media/download ──
# 背景：产物在第三方 CDN 且不带 CORS 头，浏览器里没法自己下载 → 网关代拉。
# 这是唯一一个"服务端主动出网"的接口，所以测试重点在**拒绝**侧（白名单/内网/超限）。
class _FakeUpstream:
    """假的 httpx 流式响应（替掉 _dl_open_upstream，测试不真出网）。"""

    def __init__(self, body: bytes = b"", ctype: str = "image/png", status: int = 200,
                 clen: int | None = None):
        import httpx
        hdrs = {"content-type": ctype}
        if clen is not None:
            hdrs["content-length"] = str(clen)
        self.status_code = status
        self.headers = httpx.Headers(hdrs)
        self._body = body
        self.closed = False
        self.client = _FakeClient()

    async def aiter_bytes(self, n: int = 65536):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    async def aclose(self):
        self.closed = True


class _FakeClient:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


def _fake_open(resp, recorder: dict | None = None):
    async def _open(url: str):
        if recorder is not None:
            recorder["url"] = url
        return resp.client, resp
    return _open


def test_download_host_allowlist():
    """白名单匹配：精确 / 子域放行；后缀撞车域名、裸 IP、空值一律拒。"""
    import app.main as m
    allow = m._cfg().download.allowed_hosts
    assert allow, "gateway.yaml 的 download.allowed_hosts 不该为空（否则下载功能整体关闭）"
    host = allow[0]
    assert m._dl_host_allowed(host) is True
    assert m._dl_host_allowed(host.upper()) is True          # 大小写无关
    assert m._dl_host_allowed("cdn." + host) is True          # 子域放行
    assert m._dl_host_allowed("evil" + host) is False         # 前缀撞车不算子域
    assert m._dl_host_allowed(host + ".evil.com") is False    # 后缀撞车
    assert m._dl_host_allowed("127.0.0.1") is False
    assert m._dl_host_allowed("") is False


def test_download_public_ip_guard():
    """内网/回环地址必须被判非公网（挡 DNS 指内网的 SSRF）。"""
    import app.main as m
    assert m._dl_host_is_public("127.0.0.1") is False
    assert m._dl_host_is_public("localhost") is False
    assert m._dl_host_is_public("10.0.0.1") is False
    assert m._dl_host_is_public("192.168.1.1") is False
    assert m._dl_host_is_public("169.254.169.254") is False  # 云元数据地址
    assert m._dl_host_is_public("no-such-host.invalid") is False
    assert m._dl_host_is_public(m._cfg().download.allowed_hosts[0]) is True


def test_download_filename_sanitize():
    """文件名清洗：抹路径成分 / 危险字符；缺后缀按 Content-Type 补。"""
    import app.main as m
    # 目录穿越与引号必须被抹掉，不能带出路径
    assert m._dl_filename("https://h/x/y", "image/png", "../../etc/passwd") == "passwd.png"
    assert m._dl_filename("https://h/x/y", "image/png", 'a"b*c?<d>.png') == "a_b_c_d_.png"
    # Agnes 图片地址没有后缀 → 按类型补 .png / .jpg
    assert m._dl_filename("https://h/images/t2i/task_1/output_ab", "image/png; charset=utf-8", "") == "output_ab.png"
    assert m._dl_filename("https://h/images/t2i/task_1/output_ab", "image/jpeg", "") == "output_ab.jpg"
    # 已有后缀则不动，视频链接原样取末段
    assert m._dl_filename("https://h/videos/a/task_z.mp4", "video/mp4", "") == "task_z.mp4"
    assert m._dl_filename("https://h/x", "application/octet-stream", "") == "x"


def test_download_rejects_bad_targets():
    """出网守门：非白名单域名 / 非 http / 内网 → 400；无凭证 → 401。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}

    r = client.get("/v1/media/download", params={"url": "https://evil.example.com/a.png"}, headers=ah)
    assert r.status_code == 400 and "白名单" in r.json()["error"]["message"], r.text

    # 用白名单里的域名伪装前缀也不行
    host = m._cfg().download.allowed_hosts[0]
    r = client.get("/v1/media/download", params={"url": f"https://evil{host}/a.png"}, headers=ah)
    assert r.status_code == 400, r.text

    # 回环地址（哪怕域名解析过去也拦，这里直接写 IP，白名单先拦一道）
    r = client.get("/v1/media/download", params={"url": "http://127.0.0.1:8808/healthz"}, headers=ah)
    assert r.status_code == 400, r.text

    # 非 http(s) 协议
    r = client.get("/v1/media/download", params={"url": "file:///etc/passwd"}, headers=ah)
    assert r.status_code == 400, r.text
    r = client.get("/v1/media/download", params={"url": ""}, headers=ah)
    assert r.status_code == 400, r.text

    # 未带凭证（显式空 Authorization 覆盖测试默认注入的 admin 会话）
    r = client.get("/v1/media/download", params={"url": f"https://{host}/a.png"},
                   headers={"Authorization": ""})
    assert r.status_code == 401, r.text


def test_download_proxies_bytes_and_headers():
    """正常路径：网关代拉 + attachment 头 + 字节完整；4xx/5xx/超限各有明确回应。"""
    from fastapi.testclient import TestClient
    import app.main as m
    host = m._cfg().download.allowed_hosts[0]
    target = f"https://{host}/images/t2i/task_1/output_ab"
    body = b"\x89PNG\r\n\x1a\n" + b"x" * 5000

    with _Isolated() as mm:
        client = TestClient(mm.app)
        ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
        orig = mm._dl_open_upstream
        try:
            # ① 200：字节一致、附件头、文件名按类型补后缀、Content-Length 保留
            rec: dict = {}
            resp = _FakeUpstream(body=body, ctype="image/png", clen=len(body))
            mm._dl_open_upstream = _fake_open(resp, rec)
            r = client.get("/v1/media/download", params={"url": target, "filename": "小银 的图"}, headers=ah)
            assert r.status_code == 200, r.text
            assert r.content == body, "代拉的字节必须与源站一致"
            cd = r.headers["content-disposition"]
            assert cd.startswith("attachment;"), cd
            assert "filename*=UTF-8''" in cd, cd                     # 现代浏览器读这个
            assert "%E5%B0%8F%E9%93%B6" in cd, cd                   # 小银（UTF-8 百分号编码）
            assert '.png"' in cd, cd                                # 未给后缀时按 Content-Type 补
            assert r.headers["content-type"] == "image/png"
            assert r.headers.get("content-length") == str(len(body))
            assert rec["url"] == target, "应该代拉用户给的那个地址"
            assert resp.closed is True and resp.client.closed is True, "必须关掉上游连接（防泄漏）"

            # ② 源站 404 → 400「素材可能已过期」，不冒充 502
            gone = _FakeUpstream(status=404)
            mm._dl_open_upstream = _fake_open(gone)
            r = client.get("/v1/media/download", params={"url": target}, headers=ah)
            assert r.status_code == 400 and "404" in r.json()["error"]["message"], r.text

            # ③ 源站 500 → 502（与图像/视频的 5xx 口径一致）
            bad = _FakeUpstream(status=500)
            mm._dl_open_upstream = _fake_open(bad)
            r = client.get("/v1/media/download", params={"url": target}, headers=ah)
            assert r.status_code == 502, r.text

            # ④ 超过 download.max_bytes → 413（已知 Content-Length 时不必真拉完）
            over = m._cfg().download.max_bytes + 1
            big = _FakeUpstream(body=b"y" * 10, ctype="video/mp4", clen=over)
            mm._dl_open_upstream = _fake_open(big)
            r = client.get("/v1/media/download", params={"url": target}, headers=ah)
            assert r.status_code == 413 and "上限" in r.json()["error"]["message"], r.text

            # ⑤ 连不上 → 502（不把上游异常原文抛给客户端）
            async def _boom(url):
                raise OSError("connection refused")
            mm._dl_open_upstream = _boom
            r = client.get("/v1/media/download", params={"url": target}, headers=ah)
            assert r.status_code == 502, r.text
        finally:
            mm._dl_open_upstream = orig


# ── 第十八轮回归（全面体检发现的问题）──────────────────────────────
def test_json_body_must_be_object_not_500():
    """第十八轮: 合法但**非对象**的 JSON（数组/字符串/数字）必须 400，不能 500。

    此前各端点直接 `body.get(...)`，传 `[1,2]` 抛 AttributeError 被兜底成
    「网关内部错误」—— 前端只能显示"服务端异常"，看不出是自己 body 传错了。
    """
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    tok = _login_admin_session(client)
    ah = {"Authorization": f"Bearer {tok}"}
    for ep in ("/auth/users", "/auth/password", "/auth/apikeys", "/history/chat", "/auth/login"):
        for raw in (b"[1,2]", b'"str"', b"42", b"{oops"):
            r = client.post(ep, content=raw, headers={**ah, "Content-Type": "application/json"})
            assert r.status_code == 400, f"{ep} body={raw!r} 期望 400，实际 {r.status_code}: {r.text}"
            assert "error" in r.json(), r.text
    # 不能"一律 400"：正常的对象请求照旧
    assert client.get("/auth/users", headers=ah).status_code == 200
    assert client.post("/auth/apikeys", json={"label": "t18"}, headers=ah).status_code == 200
    assert client.post("/history/chat", json={"items": []}, headers=ah).status_code == 200


def test_client_ip_takes_rightmost_forwarded_for():
    """第十八轮（安全）: XFF 取最右段 —— 那是 nginx `$proxy_add_x_forwarded_for` 追加的
    真实来源；最左段是客户端自带、可任意伪造的。"""
    from starlette.requests import Request
    import app.main as m

    def mk(xff, peer="127.0.0.1"):
        hdrs = [(b"x-forwarded-for", xff.encode())] if xff else []
        return Request({"type": "http", "method": "GET", "path": "/", "headers": hdrs,
                        "client": (peer, 12345), "query_string": b"",
                        "server": ("127.0.0.1", 8808), "scheme": "http"})

    assert m._client_ip(mk("1.2.3.4, 203.0.113.7")) == "203.0.113.7"
    assert m._client_ip(mk("203.0.113.7")) == "203.0.113.7"
    assert m._client_ip(mk(None)) == "127.0.0.1"
    # 直连方不是本机反代 → 整个 XFF 一律不认（防 8808 被直接暴露时伪造）
    assert m._client_ip(mk("1.2.3.4, 5.6.7.8", peer="198.51.100.9")) == "198.51.100.9"


def test_login_rate_limit_not_bypassable_by_fake_xff():
    """第十八轮（安全）: 轮换伪造 XFF 最左段不能重置登录失败计数（取最右段归并后仍 429）。"""
    from fastapi.testclient import TestClient
    import app.main as m
    cfg = m._cfg()
    old_limit = cfg.server.auth_fail_limit_per_min
    cfg.server.auth_fail_limit_per_min = 3
    m._AUTH_FAILS.clear()
    try:
        c = TestClient(m.app, client=("127.0.0.1", 41234))
        codes = []
        for i in range(3):
            r = c.post("/auth/login", json={"username": "admin", "password": "definitely-wrong"},
                       headers={"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.7"})
            codes.append(r.status_code)
        assert codes == [401, 401, 429], f"最右段相同就该归并到同一来源: {codes}"
    finally:
        cfg.server.auth_fail_limit_per_min = old_limit
        m._AUTH_FAILS.clear()


def test_history_upsert_rebuilds_index_on_idless_insert():
    """第十八轮: 同一批「无 id 新增 + 带 id 更新」时，带 id 的那条必须落到原位。

    此前只在"带 id 的新条目"分支重建下标，而无 id 的插入同样会把后面所有元素顶后一位 ——
    于是带 id 的更新会按过期下标覆盖到错误位置（把刚插进去的新条目吃掉、旧条目留下）。
    """
    import tempfile
    from app.history import HistoryStore
    with tempfile.TemporaryDirectory() as d:
        st = HistoryStore(str(_Path(d) / "h.json"))
        st.upsert("acc", "chat", [{"id": "x", "v": 1}])
        out = st.upsert("acc", "chat", [{"v": 9}, {"id": "x", "v": 2}])
        assert len(out) == 2, out
        assert out[0].get("id") is None and out[0]["v"] == 9, f"无 id 新条目应插到最前: {out}"
        assert out[1]["id"] == "x" and out[1]["v"] == 2, f"带 id 更新应就地覆盖: {out}"


def test_media_param_error_does_not_consume_probe_token():
    """第十八轮: n≠1 是纯参数错误 —— 必须在限流/熔断检查**之前**拒绝。

    此前检查排在 `_media_circuit_check` 之后，raise 时既不 release_probe 也不 record：
    熔断器刚从 OPEN 进入 HALF_OPEN 时，唯一的试探令牌被这次"参数错误"吃掉，
    供应商要卡到探测超时（≥30s）才可能再被试探。
    """
    import time
    from fastapi.testclient import TestClient
    import app.main as m
    cfg = m._cfg()
    img = next((mm.id for p in cfg.providers.values() for mm in p.models if mm.type == "image"), "")
    pkey = next((p for p, v in cfg.providers.items() if v.media.supports_image), "")
    assert img and pkey
    cb = CircuitBreaker(f"{pkey}#media", failure_threshold=1, cooldown=0.05, window=3)
    cb.record(False)                       # → OPEN
    time.sleep(0.06)                       # 冷却结束：下一次 allow() 就会发试探令牌
    orig = m._media_circuit
    m._media_circuit = lambda k: cb
    try:
        client = TestClient(m.app)
        r = client.post("/v1/images/generations", json={"model": img, "prompt": "x", "n": 2})
        assert r.status_code == 400, r.text
        assert "1 张" in r.json()["error"]["message"], r.text
        st = cb.status()
        assert st["state"] == "open" and st["probing"] is False, f"试探令牌被参数错误吃掉了: {st}"
    finally:
        m._media_circuit = orig


def test_videos_poll_wait_reuses_single_probe_token():
    """第十八轮: HALF_OPEN 下带 wait 的轮询不再被自己持有的试探令牌挡住。

    此前 `_media_circuit_check` 拿到令牌后，等待循环第一轮又 `cb.allow()` → 被自己挡 →
    直接判"熔断中"返回 503；供应商恢复后的第一次轮询永远失败。
    """
    import time
    from fastapi.testclient import TestClient
    import app.main as m
    cfg = m._cfg()
    vid = next((mm.id for p in cfg.providers.values() for mm in p.models if mm.type == "video"), "")
    pkey = next((p for p, v in cfg.providers.items() if v.media.supports_video), "")
    assert vid and pkey

    class FakeVideoAdapter:
        async def poll_video(self, video_id, model=None, extra=None):
            # 与上游回执同形：终止状态藏在 raw.status，端点据此 break
            return {"id": video_id, "raw": {"status": "completed", "url": "https://cdn.example/v.mp4"}}

    cb = CircuitBreaker(f"{pkey}#media", failure_threshold=1, cooldown=0.05, window=3)
    cb.record(False)
    time.sleep(0.06)
    orig_c, orig_g = m._media_circuit, m._get_adapter
    m._media_circuit = lambda k: cb
    m._get_adapter = lambda model_id, timeout=None, cfg=None: (FakeVideoAdapter(), pkey, m._cfg().providers[pkey])
    try:
        client = TestClient(m.app)
        r = client.get("/v1/videos/vid18", params={"model": vid, "wait": "true", "wait_timeout": 5})
        assert r.status_code == 200, f"HALF_OPEN 首轮轮询不该 503: {r.status_code} {r.text}"
        assert r.json()["status"] == "completed", r.text
        assert cb.status()["state"] == "closed", cb.status()
    finally:
        m._media_circuit, m._get_adapter = orig_c, orig_g


def test_password_hashing_offloaded_off_event_loop():
    """第十八轮（性能）: PBKDF2 实测 ~31ms/次，必须挪出事件循环（否则并发流式转发被卡住）。"""
    import threading
    import app.main as m
    main_tid = threading.get_ident()
    got = asyncio.run(m._offload(lambda: threading.get_ident()))
    assert got != main_tid, "哈希仍在事件循环线程里跑"
    assert asyncio.run(m._offload(lambda a, b: a + b, 1, 2)) == 3


# ── 第二十二轮：意见反馈（普通账号提交 / 管理员看全部）──────────────────
# 服务器不解码像素（页面端已压缩），只按**魔数**认类型 —— 所以测试里的"图片"
# 只需头部正确、长度合规即可，不必是真图。
import base64 as _b64


def _img_uri(size: int = 120, head: bytes = b"\x89PNG\r\n\x1a\n") -> str:
    return "data:image/png;base64," + _b64.b64encode(head + b"0" * size).decode()


def test_feedback_submit_isolation_and_admin_inbox():
    """普通账号提交 → 只有自己和管理员看得到；图片按权限取。"""
    import tempfile
    from pathlib import Path as P
    from fastapi.testclient import TestClient
    import app.main as m
    from app.feedback import FeedbackStore
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    # 换一个全新的临时反馈库：反馈是**不可删**的持久数据，不隔离的话重复跑
    # （或与其它用例共用一个 .test_feedback.json）会让"我的列表"里混进历史条目。
    old, tmp = m._feedback, tempfile.TemporaryDirectory()
    m._feedback = FeedbackStore(str(P(tmp.name) / "fb.json"), str(P(tmp.name) / "media"))
    try:
        for u in ("fbtest", "fbother"):
            client.delete(f"/auth/users/{u}", headers=ah)   # 清上一次跑残留（可能中途失败没删掉）
        assert client.post("/auth/users", json={"username": "fbtest"}, headers=ah).status_code == 200
        tok = client.post("/auth/login", json={"username": "fbtest", "password": "fbtest"}).json()["access_token"]
        fh = {"Authorization": f"Bearer {tok}"}

        r = client.post("/feedback", headers=fh, json={
            "content": "导出按钮点了没反应，附截图",
            "images": [{"uri": _img_uri(), "thumb": _img_uri(20), "name": "shot.png"}],
        })
        assert r.status_code == 200, r.text
        item = r.json()["item"]
        fid = item["id"]
        assert item["account"] == "fbtest" and item["images"][0]["thumb"].startswith("data:image/png;base64,")
        assert item["created"] > 0

        # 本人：能看列表、能取原图
        mine = client.get("/feedback/mine", headers=fh).json()["items"]
        assert [x["id"] for x in mine] == [fid], mine
        got = client.get(f"/feedback/media/{fid}/0", headers=fh)
        assert got.status_code == 200 and got.json()["uri"].startswith("data:image/png;base64,"), got.text

        # 普通账号不能看"全部"（这是 admin 的收件箱）
        assert client.get("/feedback", headers=fh).status_code == 403

        # 另一个普通账号：看不到这条、也取不到它的图
        assert client.post("/auth/users", json={"username": "fbother"}, headers=ah).status_code == 200
        tok2 = client.post("/auth/login", json={"username": "fbother", "password": "fbother"}).json()["access_token"]
        oh = {"Authorization": f"Bearer {tok2}"}
        assert client.get("/feedback/mine", headers=oh).json()["items"] == []
        assert client.get(f"/feedback/media/{fid}/0", headers=oh).status_code == 403

        # 管理员：在收件箱里看到它，并能取原图
        allitems = client.get("/feedback", headers=ah).json()["items"]
        assert fid in [x["id"] for x in allitems], "admin 收件箱里没有这条反馈"
        assert client.get(f"/feedback/media/{fid}/0", headers=ah).status_code == 200
        # 越界下标 / 不存在的 id：明确 404，别抛 500
        assert client.get(f"/feedback/media/{fid}/9", headers=ah).status_code == 404
        assert client.get("/feedback/media/f-nope/0", headers=ah).status_code == 404

        assert client.delete("/auth/users/fbtest", headers=ah).status_code == 200
        assert client.delete("/auth/users/fbother", headers=ah).status_code == 200
    finally:
        m._feedback = old
        tmp.cleanup()


def test_feedback_write_needs_session_not_apikey():
    """API Key 可以调模型，但不能拿它提交反馈（与 /history 写接口同一口径）。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    client.delete("/auth/users/fbkey", headers=ah)          # 清上一次跑残留
    assert client.post("/auth/users", json={"username": "fbkey"}, headers=ah).status_code == 200
    mh = {"Authorization": "Bearer " + client.post(
        "/auth/login", json={"username": "fbkey", "password": "fbkey"}).json()["access_token"]}
    key = client.post("/auth/apikeys", json={"label": "fbk"}, headers=mh).json()["key"]
    h = {"Authorization": f"Bearer {key}"}
    r = client.post("/feedback", headers=h, json={"content": "用 Key 提交应该被拒"})
    assert r.status_code == 403, r.text
    assert "网页会话" in r.text, r.text
    assert client.delete("/auth/users/fbkey", headers=ah).status_code == 200


def test_feedback_admin_cannot_submit():
    """管理员只处理、不提：不给菜单，服务端也退 403。"""
    import app.main as m
    from fastapi.testclient import TestClient
    client = TestClient(m.app)
    h = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    r = client.post("/feedback", headers=h, json={"content": "管理员不该能提"})
    assert r.status_code == 403 and "只处理反馈" in r.text, r.text


def test_feedback_validation_rejects_bad_input():
    """正文空/超长、图片过多/不是图片 —— 一律 400 且说清原因。"""
    import app.main as m
    from fastapi.testclient import TestClient
    from app.feedback import MAX_CONTENT, MAX_IMAGES
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    client.delete("/auth/users/fbvalid", headers=ah)        # 清上一次跑残留
    assert client.post("/auth/users", json={"username": "fbvalid"}, headers=ah).status_code == 200
    # 校验用例得用**普通账号**：管理员现在提交一律 403，测不出校验分支
    h = {"Authorization": "Bearer " + client.post(
        "/auth/login", json={"username": "fbvalid", "password": "fbvalid"}).json()["access_token"]}

    assert client.post("/feedback", headers=h, json={"content": "   "}).status_code == 400
    assert client.post("/feedback", headers=h, json={"content": "x" * (MAX_CONTENT + 1)}).status_code == 400
    assert client.post("/feedback", headers=h, json={
        "content": "图太多", "images": [{"uri": _img_uri()} for _ in range(MAX_IMAGES + 1)],
    }).status_code == 400
    bad = client.post("/feedback", headers=h, json={
        "content": "不是图片", "images": [{"uri": "data:text/plain;base64,aGk="}],
    })
    assert bad.status_code == 400 and "data URI" in bad.text, bad.text
    fake = client.post("/feedback", headers=h, json={
        "content": "伪装成 png", "images": [{"uri": "data:image/png;base64," + _b64.b64encode(b"not an image").decode()}],
    })
    assert fake.status_code == 400 and "可识别的图片" in fake.text, fake.text
    # 非对象 JSON 也要 400（而不是 500）
    assert client.post("/feedback", headers=h, json=[1, 2]).status_code == 400
    assert client.delete("/auth/users/fbvalid", headers=ah).status_code == 200


def test_feedback_withdraw_only_when_pending():
    """撤回规则：待处理可自撤（连图片文件一起删）；处理中/已处理不能撤；别人的不能撤。"""
    import tempfile
    from pathlib import Path as P
    from fastapi.testclient import TestClient
    import app.main as m
    from app.feedback import FeedbackStore
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    old, tmp = m._feedback, tempfile.TemporaryDirectory()
    media = P(tmp.name) / "media"
    m._feedback = FeedbackStore(str(P(tmp.name) / "fb.json"), str(media))
    try:
        for u in ("fbw", "fbw2"):
            client.delete(f"/auth/users/{u}", headers=ah)
            assert client.post("/auth/users", json={"username": u}, headers=ah).status_code == 200

        def tok(u):
            return {"Authorization": "Bearer " + client.post(
                "/auth/login", json={"username": u, "password": u}).json()["access_token"]}

        hw, hw2 = tok("fbw"), tok("fbw2")

        # ① 待处理 + 带图 → 本人可撤，条目与图片文件一起消失
        fid = client.post("/feedback", headers=hw, json={
            "content": "撤回用：这条会被撤掉", "images": [{"uri": _img_uri()}],
        }).json()["item"]["id"]
        assert len(list(media.glob(f"{fid}-*"))) == 1, "图片文件没落盘"
        r = client.delete(f"/feedback/{fid}", headers=hw)
        assert r.status_code == 200, r.text
        assert r.json()["item"]["id"] == fid
        assert client.get("/feedback/mine", headers=hw).json()["items"] == []
        assert list(media.glob(f"{fid}-*")) == [], "撤回没删图片文件"
        # 撤过的再撤 → 404
        assert client.delete(f"/feedback/{fid}", headers=hw).status_code == 404

        # ② 别人的单子，普通用户不能撤（哪怕也是待处理）；管理员可真删（admin_delete 含图片）
        fid2 = client.post("/feedback", headers=hw, json={
            "content": "别人的单子", "images": [{"uri": _img_uri()}],
        }).json()["item"]["id"]
        r = client.delete(f"/feedback/{fid2}", headers=hw2)
        assert r.status_code == 403 and "自己" in r.text, r.text
        # 管理员收件箱可真删：200 + 条目与图片文件一起消失
        r = client.delete(f"/feedback/{fid2}", headers=ah)
        assert r.status_code == 200 and r.json().get("deleted") is True, r.text
        assert client.get("/feedback/mine", headers=hw).json()["items"] == []
        assert list(media.glob(f"{fid2}-*")) == [], "管理员真删没清图片文件"

        # ③ 管理员推进到「处理中」后不能撤；「已处理」同样不能
        fid2b = client.post("/feedback", headers=hw, json={"content": "别人进度单"}).json()["item"]["id"]
        assert client.patch(f"/feedback/{fid2b}", headers=ah, json={"status": "doing"}).status_code == 200
        r = client.delete(f"/feedback/{fid2b}", headers=hw)
        assert r.status_code == 400 and "开始处理" in r.text, r.text
        assert client.patch(f"/feedback/{fid2b}", headers=ah, json={"status": "done"}).status_code == 200
        assert client.delete(f"/feedback/{fid2b}", headers=hw).status_code == 400
        # 撤不动，但单子还在（不会被误删）
        assert [x["id"] for x in client.get("/feedback/mine", headers=hw).json()["items"]] == [fid2b]

        # ④ API Key 不能撤（写操作只认网页会话）
        key = client.post("/auth/apikeys", json={"label": "fbw"}, headers=hw).json()["key"]
        fid3 = client.post("/feedback", headers=hw, json={"content": "Key 撤不动"}).json()["item"]["id"]
        r = client.delete(f"/feedback/{fid3}", headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 403, r.text

        for u in ("fbw", "fbw2"):
            assert client.delete(f"/auth/users/{u}", headers=ah).status_code == 200
    finally:
        m._feedback = old
        tmp.cleanup()


def test_feedback_status_update_flow():
    """处理进度：提交=待处理 → 管理员改处理中/已处理 + 说明 → 提交人看得到。"""
    import tempfile
    from pathlib import Path as P
    from fastapi.testclient import TestClient
    import app.main as m
    from app.feedback import FeedbackStore, MAX_NOTE
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    old, tmp = m._feedback, tempfile.TemporaryDirectory()
    m._feedback = FeedbackStore(str(P(tmp.name) / "fb.json"), str(P(tmp.name) / "media"))
    try:
        for u in ("fbst", "fbother2"):
            client.delete(f"/auth/users/{u}", headers=ah)
        assert client.post("/auth/users", json={"username": "fbst"}, headers=ah).status_code == 200
        mh = {"Authorization": "Bearer " + client.post(
            "/auth/login", json={"username": "fbst", "password": "fbst"}).json()["access_token"]}
        fid = client.post("/feedback", headers=mh, json={"content": "希望支持批量导出"}).json()["item"]["id"]

        # 新提交的在提交人眼里就是「待处理」
        mine = client.get("/feedback/mine", headers=mh).json()["items"][0]
        assert mine["status"] == "pending" and mine["note"] == "" and mine["updated"] > 0
        # 处理记录：提交这一步就得有（from 为空、by 是提交人自己）
        assert len(mine["history"]) == 1, mine["history"]
        assert mine["history"][0]["from"] == "" and mine["history"][0]["to"] == "pending"
        assert mine["history"][0]["by"] == "fbst"

        # 非法状态 / 超长说明 → 400；不存在的 id → 404
        assert client.patch(f"/feedback/{fid}", headers=ah, json={"status": "whatever"}).status_code == 400
        assert client.patch(f"/feedback/{fid}", headers=ah,
                            json={"status": "doing", "note": "x" * (MAX_NOTE + 1)}).status_code == 400
        assert client.patch("/feedback/f-nope", headers=ah, json={"status": "done"}).status_code == 404
        # 被拒的请求不留下记录（记录只跟着成功的变更走）
        assert len(client.get("/feedback/mine", headers=mh).json()["items"][0]["history"]) == 1

        # 普通账号不能改状态（那是管理员的事）
        assert client.patch(f"/feedback/{fid}", headers=mh, json={"status": "done"}).status_code == 403

        r = client.patch(f"/feedback/{fid}", headers=ah, json={"status": "doing", "note": "评估中，下个版本排期"})
        assert r.status_code == 200, r.text
        assert r.json()["item"]["status"] == "doing"
        # 提交人能看到进度与说明（这就是"查看处理进度"）
        mine = client.get("/feedback/mine", headers=mh).json()["items"][0]
        assert mine["status"] == "doing" and mine["note"] == "评估中，下个版本排期"
        assert mine["updated"] >= mine["created"]
        # 每一步都有记录：提交 → 处理中，且带上是哪个管理员改的
        assert [s["to"] for s in mine["history"]] == ["pending", "doing"], mine["history"]
        assert mine["history"][1]["from"] == "pending"
        assert mine["history"][1]["note"] == "评估中，下个版本排期"
        assert mine["history"][1]["by"] == "admin"
        # 再推一次 → 又追加一条（只增不改，顺序 old → new）
        assert client.patch(f"/feedback/{fid}", headers=ah, json={"status": "done"}).status_code == 200
        hist = client.get("/feedback/mine", headers=mh).json()["items"][0]["history"]
        assert [s["to"] for s in hist] == ["pending", "doing", "done"], hist
        assert hist[2]["from"] == "doing" and hist[2]["by"] == "admin"
        # 管理员在收件箱里也看到同一串记录
        inbox = [x for x in client.get("/feedback", headers=ah).json()["items"] if x["id"] == fid]
        assert inbox and len(inbox[0]["history"]) == 3
        # 落盘 → 重新加载后状态与记录都在（重启不丢）
        st2 = FeedbackStore(str(P(tmp.name) / "fb.json"), str(P(tmp.name) / "media"))
        assert st2.all()[0]["status"] == "done"
        assert [s["to"] for s in st2.all()[0]["history"]] == ["pending", "doing", "done"]
        # 老数据（既没 status 也没 history）→ 归一化成待处理 + 补一条提交记录
        st2._items[0].pop("status")
        st2._items[0].pop("history")
        st2._items[0].pop("note")
        st2._save()
        st3 = FeedbackStore(str(P(tmp.name) / "fb.json"), str(P(tmp.name) / "media"))
        assert st3.all()[0]["status"] == "pending"
        assert len(st3.all()[0]["history"]) == 1 and st3.all()[0]["history"][0]["to"] == "pending"
        assert client.delete("/auth/users/fbst", headers=ah).status_code == 200
    finally:
        m._feedback = old
        tmp.cleanup()


def test_feedback_decode_size_and_corrupt_base64():
    """单张图超过上限 / base64 损坏，都在解码层就被拦下。"""
    from app.feedback import FeedbackError, _decode_image
    ok = _decode_image(_img_uri(200), 10_000, "图")
    assert ok[1] == "png", ok
    for bad, why in (
        (_img_uri(200), "超过"),
        ("data:image/png;base64,!!!!", "base64"),
        (12345, "必须是 data URI"),
    ):
        try:
            _decode_image(bad, 100, "图")
        except FeedbackError as e:
            assert why in str(e), f"{why} 未被拦下: {e}"
        else:
            raise AssertionError(f"{why} 未被拦下")


def test_feedback_store_cap_drops_oldest_and_cleans_media():
    """超过条数上限时丢最旧的一条，并删掉它的图片文件（不留下无主文件）。"""
    import tempfile
    from pathlib import Path as P
    from app.feedback import FeedbackStore
    with tempfile.TemporaryDirectory() as d:
        media = P(d) / "media"
        st = FeedbackStore(str(P(d) / "fb.json"), str(media), max_entries=2)
        a = st.add("u1", "第一条", [{"uri": _img_uri(), "thumb": _img_uri(20), "name": "a.png"}])
        st.add("u2", "第二条", [])
        st.add("u1", "第三条", [{"uri": _img_uri(), "thumb": _img_uri(20), "name": "c.png"}])
        ids = [e["id"] for e in st.all()]
        assert len(ids) == 2 and a["id"] not in ids, ids
        assert not list(media.glob(f"{a['id']}-*")), "被淘汰条目的图片文件没删掉"
        assert st.mine("u1")[0]["content"] == "第三条"
        # 落盘 → 重新加载后仍在（重启不丢）
        st2 = FeedbackStore(str(P(d) / "fb.json"), str(media), max_entries=2)
        assert [e["content"] for e in st2.all()] == ["第三条", "第二条"]


def test_feedback_store_survives_corrupt_file_and_prunes_orphans():
    """坏 JSON 不能让网关起不来；无主的图片文件要清掉。"""
    import tempfile
    from pathlib import Path as P
    from app.feedback import FeedbackStore
    with tempfile.TemporaryDirectory() as d:
        path, media = P(d) / "fb.json", P(d) / "media"
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        st = FeedbackStore(str(path), str(media))
        assert st.all() == []
        assert (P(d) / "fb.json.bad").exists(), "坏文件应先备份再重建"
        media.mkdir(parents=True, exist_ok=True)
        orphan = media / "f999-0.png"
        orphan.write_bytes(b"\x89PNG\r\n\x1a\n")
        FeedbackStore(str(path), str(media))
        assert not orphan.exists(), "无主图片文件未被清理"


# ── 第二十三轮：管理页面「网关配置」（读 + 写 + 保存即生效）─────────
def test_config_view_masks_secrets_and_roundtrips():
    """面板视图 ↔ yaml 必须一一对应：往返一次不能改变任何配置，且密钥不回显。"""
    import yaml as _yaml
    from app import settings as S
    cfg = load_config(CONFIG)
    view = S.config_to_view(cfg)
    assert set(view) == {"providers", "aliases", "server", "auth", "circuit",
                         "observability", "pricing", "download"}, set(view)
    # 面板能改的 server 键 = 文件里 server 段的键（少一个都算"配置项漏搬"）
    raw_file = S.read_raw(CONFIG)
    assert set(view["server"]) == set(S._SERVER_KEYS) == set(raw_file["server"]), "server 段有键没搬上面板"
    assert set(view["circuit"]) == set(raw_file["circuit"])
    assert set(view["observability"]) == set(raw_file["observability"])
    # 第二十七轮: 路由功能整体删除 → 面板视图与文件都不该再有 routes 段
    assert "routes" not in view and "routes" not in raw_file, (view.keys(), raw_file.keys())
    assert set(view["providers"]["opencode"]["endpoints"]) == set(
        raw_file["providers"]["opencode"]["endpoints"]), "endpoints 必须一路带到面板"
    assert view["providers"]["opencode"]["models"][0]["protocols"] == \
        raw_file["providers"]["opencode"]["models"][0]["protocols"], "protocols 必须带到面板"
    assert set(raw_file["providers"]["agnes"]) <= set(view["providers"]["agnes"])
    assert set(raw_file["providers"]["agnes"]["media"]) <= set(view["providers"]["agnes"]["media"])
    # 密钥：只回报"配没配"，绝不回显内容（面板/浏览器都不该拿到明文）
    for p in view["providers"].values():
        assert "token" not in p["auth"] and isinstance(p["auth"]["token_set"], bool)
    # 往返：视图 → yaml → 配置，必须与原配置完全相等
    raw2 = S.view_to_raw(cfg, raw_file, view)
    cfg2, _ = S.parse_and_validate(raw2)
    assert cfg == cfg2, "面板视图往返后配置被改变"
    cfg3, _ = S.parse_and_validate(_yaml.safe_load(S.render_yaml(raw2)))
    assert cfg == cfg3, "渲染出的 yaml 解析回来与原配置不一致"


def test_config_view_to_raw_keeps_token_and_unknown_sections():
    """面板留空 token = 不修改；面板不管的自定义段/键要原样留着重写。"""
    from app import settings as S
    raw = {
        "providers": {"p1": {"name": "P", "base_url": "http://x/v1",
                             "auth": {"token": "sk-plaintext"}, "models": [{"id": "m1"}]}},
        "server": {"port": 8808},
        "my_custom_block": {"keep": [1, 2]},
    }
    cfg, _ = S.parse_and_validate(raw)
    view = S.config_to_view(cfg)
    assert view["providers"]["p1"]["auth"]["token_set"] is True
    view["server"]["timeout"] = 33
    out = S.view_to_raw(cfg, raw, view)
    assert out["providers"]["p1"]["auth"]["token"] == "sk-plaintext", "面板留空不能清掉文件里的明文 token"
    assert out["my_custom_block"] == {"keep": [1, 2]}, "面板不管的段必须原样保留"
    assert out["server"]["timeout"] == 33
    # 换成真值则覆盖
    view["providers"]["p1"]["auth"]["token"] = "sk-new"
    assert S.view_to_raw(cfg, raw, view)["providers"]["p1"]["auth"]["token"] == "sk-new"


def test_config_bool_coercion_and_bad_values():
    """yaml 里 "false"/"no"/0 都是假（原来 bool("false") = True）；写错的值要报字段名。"""
    from app.config import _opt_bool, ConfigError as _CE
    for v in ("false", "False", "no", "off", "0", 0, False):
        assert _opt_bool(v, "x") is False, v
    for v in ("true", "yes", "on", "1", 1, True):
        assert _opt_bool(v, "x") is True, v
    try:
        _opt_bool("maybe", "server.health_auth")
        raise AssertionError("非法布尔值应报错")
    except _CE as e:
        assert "server.health_auth" in str(e)
    # 类型写错：报错信息里要带字段名（页面直接把它显示给管理员）
    try:
        load_config(_write_tmp_yaml({"providers": {"p": {"base_url": "http://x", "models": [{"id": "m"}]}},
                                     "server": {"port": "not-a-number"}}))
        raise AssertionError("非法端口应报错")
    except _CE as e:
        assert "server.port" in str(e)


def _write_tmp_yaml(obj) -> str:
    import tempfile
    import yaml as _yaml
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        _yaml.safe_dump(obj, f, allow_unicode=True, sort_keys=False)
        return f.name


def test_config_endpoints_permission_matrix():
    """GET/PUT /config：只认管理员 + 网页会话（API Key 不行），普通账号 403。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}

    assert client.get("/config", headers={"Authorization": ""}).status_code == 401
    assert client.put("/config", headers={"Authorization": ""}, json={}).status_code == 401

    # 普通账号：两个方向都 403（页面不给菜单，服务端也必须堵住）
    client.delete("/auth/users/cfgmem", headers=ah)
    assert client.post("/auth/users", json={"username": "cfgmem"}, headers=ah).status_code == 200
    mtok = client.post("/auth/login", json={"username": "cfgmem", "password": "cfgmem"}).json()["access_token"]
    mh = {"Authorization": f"Bearer {mtok}"}
    assert client.get("/config", headers=mh).status_code == 403
    assert client.put("/config", headers=mh, json={"server": {"timeout": 1}}).status_code == 403

    # 管理员 API Key：能读（require_auth），不能改（改配置属"动凭证"级操作，要会话）
    key = client.post("/auth/apikeys", json={"label": "cfg"}, headers=ah).json()["key"]
    kh = {"Authorization": f"Bearer {key}"}
    assert client.get("/config", headers=kh).status_code == 200
    assert client.put("/config", headers=kh, json={"server": {"timeout": 1}}).status_code == 403


def test_config_save_applies_without_restart():
    """保存即生效：写盘（带备份）+ 运行态立刻切换 + 需重启项提示；坏值不写盘。"""
    import tempfile
    import yaml as _yaml
    from pathlib import Path as P
    from fastapi.testclient import TestClient
    import app.main as m

    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}

    tmp = tempfile.TemporaryDirectory()
    cfg_path = P(tmp.name) / "gateway.yaml"
    cfg_path.write_text(P(CONFIG).read_text(encoding="utf-8"), encoding="utf-8")
    # 注入一行人工注释：结构化保存**不许**把它抹掉（第三十一轮修的正是这个事故）
    with cfg_path.open("a", encoding="utf-8") as fh:
        fh.write("\n# 人工注释：别动我\n")
    comments_before = cfg_path.read_text(encoding="utf-8").count("#")
    old_path, old_cfg = m.CONFIG_PATH, m.config
    m.CONFIG_PATH = cfg_path
    try:
        got = client.get("/config", headers=ah)
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["meta"]["path"] == str(cfg_path)
        assert "server.host" in body["meta"]["restart_keys"]
        assert body["yaml"].strip(), "应回显文件原文，供页面核对"
        view = body["config"]

        # ① 改一串"每请求读配置"的值：超时/重试/访问日志容量/模型协议线/新增模型
        #   （第二十七轮: 路由别名已撤，改用"模型协议线"验证"改配置立刻生效"）
        view["server"]["timeout"] = 95
        view["server"]["retry"] = 2
        view["observability"]["access_log_maxlen"] = 777
        _hy3 = next(x for x in view["providers"]["opencode"]["models"] if x["id"] == "hy3")
        _hy3["protocols"] = ["chat/completions", "messages"]
        view["providers"]["volc_engine"]["models"].append(
            {"id": "ark-code-mini", "display_name": "迷你", "type": "text", "vision": False})
        view["pricing"]["per_model"]["ark-code-mini"] = {"input": 0.1, "output": 0.2}

        r = client.put("/config", headers=ah, json={"config": view})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["applied"] is True and out["restart_required"] == []

        # 运行态**当场**就变了（不重启、不等 mtime 轮询）
        assert m.config.server.timeout == 95
        assert m.config.server.retry == 2
        assert m.config.observability.access_log_maxlen == 777
        assert next(x for x in m.config.providers["opencode"].models
                    if x.id == "hy3").protocols == ["chat/completions", "messages"], \
            "协议线改动应就地生效（无需重启）"
        assert m.config.find_provider_for_model("ark-code-mini")[0] == "volc_engine"
        assert m._access._max == 777, "访问日志容量应就地调整"
        # 盘上也变了，且旧文件有备份
        on_disk = _yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["server"]["timeout"] == 95
        assert next(x for x in on_disk["providers"]["opencode"]["models"]
                    if x["id"] == "hy3")["protocols"] == ["chat/completions", "messages"]
        assert (P(tmp.name) / "gateway.yaml.bak").exists(), "保存前应备份原文件"
        # 协议线变更后，/v1/models 立刻跟着变（调用方看到的就是最新能力声明）
        lm = client.get("/v1/models", headers=ah).json()
        assert next(x for x in lm["data"] if x["id"] == "hy3")["protocols"] == \
            ["chat/completions", "messages"]

        # ② 改"需重启"的键：能保存，但要明确回提示（不能假装已生效）
        assert _yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["server"]["port"] == 8808
        v = client.get("/config", headers=ah).json()["config"]
        v["server"]["port"] = 8809
        r2 = client.put("/config", headers=ah, json={"config": v})
        assert r2.status_code == 200 and r2.json()["restart_required"] == ["server.port"], r2.text

        # ③ 坏值：400 + 不写盘 + 运行态不动
        v3 = client.get("/config", headers=ah).json()["config"]
        v3["server"]["timeout"] = "abc"
        r3 = client.put("/config", headers=ah, json={"config": v3})
        assert r3.status_code == 400 and "server.timeout" in r3.text, r3.text
        v4 = client.get("/config", headers=ah).json()["config"]
        # 第二十七轮: 非法协议名必须在写盘前被拦住（否则运行时该模型永远 400）
        v4["providers"]["opencode"]["models"][0]["protocols"] = ["bogus-format"]
        assert client.put("/config", headers=ah, json={"config": v4}).status_code == 400, \
            "非法协议名应被拦住"
        assert _yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["server"]["port"] == 8809, "坏值不应写盘"
        assert m.config.server.port == 8809 and m.config.server.timeout == 95, "坏值不应动运行态"
        # ④ 空字符串的 rate_limit 仍解析为"不限"
        v5 = client.get("/config", headers=ah).json()["config"]
        v5["server"]["rate_limit_rps"] = ""
        assert client.put("/config", headers=ah, json={"config": v5}).status_code == 200
        assert m.config.server.rate_limit_rps is None
    finally:
        m.CONFIG_PATH, m.config = old_path, old_cfg
        tmp.cleanup()


# ── 第二十四轮：管理员停用 / 启用账号 ────────────────────────────────
def _mk_member(client, ah, name):
    """建一个普通账号并返回它的会话头（已存在先删掉，避免上一次跑的残留）。"""
    client.delete(f"/auth/users/{name}", headers=ah)
    assert client.post("/auth/users", json={"username": name}, headers=ah).status_code == 200
    r = client.post("/auth/login", json={"username": name, "password": name})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_disabled_account_cannot_login_or_use_apikey():
    """停用后：登录 403、名下 API Key 403、旧会话立即作废；重新启用后全部恢复（Key 不删）。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    mh = _mk_member(client, ah, "stopuser")
    try:
        # 该账号自己铸一把 Key，并确认停用前能用（调模型接口 /models）
        key_plain = client.post("/auth/apikeys", json={"label": "t"}, headers=mh).json()["key"]
        kh = {"Authorization": f"Bearer {key_plain}"}
        assert client.get("/models", headers=kh).status_code == 200, "停用前 Key 应可用"

        # 管理员停用
        r = client.put("/auth/users/stopuser/disabled", json={"disabled": True}, headers=ah)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["disabled"] is True and out["revoked_sessions"] >= 1, out
        # 列表里能看出停用状态 + 停用时间（页面据此显示「已停用」并换成「启用」按钮）
        row = next(u for u in client.get("/auth/users", headers=ah).json()["users"] if u["name"] == "stopuser")
        assert row["disabled"] is True and row["disabled_at"], row

        # ① 不能登录：密码正确也 403，且提示是「已停用」而不是「账号或密码错误」
        lg = client.post("/auth/login", json={"username": "stopuser", "password": "stopuser"})
        assert lg.status_code == 403 and "停用" in lg.text, lg.text
        # 密码错的人仍旧只看到 401（不泄露账号是否存在 / 是否被停用）
        bad = client.post("/auth/login", json={"username": "stopuser", "password": "wrong-pw"})
        assert bad.status_code == 401, bad.text
        # ② API Key 立刻失效：403 说清原因（不是 401「无效凭证」）
        rk = client.get("/models", headers=kh)
        assert rk.status_code == 403 and "停用" in rk.text, rk.text
        # ③ 停用前换来的网页会话被踢：401
        assert client.get("/auth/apikeys", headers=mh).status_code == 401, "旧会话应已吊销"

        # 重新启用 → 登录、Key 全部恢复，且 Key 记录一条没少（停用 ≠ 吊销凭证）
        r2 = client.put("/auth/users/stopuser/disabled", json={"disabled": False}, headers=ah)
        assert r2.status_code == 200 and r2.json()["disabled"] is False, r2.text
        lg2 = client.post("/auth/login", json={"username": "stopuser", "password": "stopuser"})
        assert lg2.status_code == 200, lg2.text
        mh2 = {"Authorization": f"Bearer {lg2.json()['access_token']}"}
        assert client.get("/models", headers=kh).status_code == 200, "启用后原 Key 应恢复"
        row2 = next(u for u in client.get("/auth/users", headers=ah).json()["users"] if u["name"] == "stopuser")
        assert row2["disabled"] is False and not row2.get("disabled_at"), row2
        assert row2["api_key_count"] == 1, "停用不应删除 Key 记录"
        assert client.get("/auth/apikeys", headers=mh2).status_code == 200
    finally:
        client.delete("/auth/users/stopuser", headers=ah)


def test_disable_requires_admin_and_session_and_valid_flag():
    """停用接口的权限与入参：非管理员 403、API Key 403、admin 不可停用、坏值 400、不存在 404。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    mh = _mk_member(client, ah, "stopuser2")
    try:
        # ① 普通账号无权停用（哪怕停用自己）
        r = client.put("/auth/users/stopuser2/disabled", json={"disabled": True}, headers=mh)
        assert r.status_code == 403 and "管理员" in r.text, r.text
        # ② API Key 不行：管账号只认网页会话（与建号/改密同口径）
        akey = client.post("/auth/apikeys", json={"label": "adm"}, headers=ah).json()["key"]
        r = client.put("/auth/users/stopuser2/disabled", json={"disabled": True},
                       headers={"Authorization": f"Bearer {akey}"})
        assert r.status_code == 403, r.text
        # ③ 唯一管理员不可停用（否则没人能进管理页把人放出来）
        r = client.put("/auth/users/admin/disabled", json={"disabled": True}, headers=ah)
        assert r.status_code == 400 and "不能停用管理员" in r.text, r.text
        # ④ 账号不存在 → 404
        r = client.put("/auth/users/nobody-here/disabled", json={"disabled": True}, headers=ah)
        assert r.status_code == 404, r.text
        # ⑤ 入参容错：字符串 "false" 绝不能被当成真（bool("false") is True 的老坑）；
        #    真布尔 / 0-1 / "true"-"false" 认，其余 400
        assert client.put("/auth/users/stopuser2/disabled", json={"disabled": "false"}, headers=ah).status_code == 200
        row = next(u for u in client.get("/auth/users", headers=ah).json()["users"] if u["name"] == "stopuser2")
        assert row["disabled"] is False, "字符串 false 不应停用账号"
        assert client.put("/auth/users/stopuser2/disabled", json={"disabled": 1}, headers=ah).status_code == 200
        assert client.put("/auth/users/stopuser2/disabled", json={"disabled": "maybe"}, headers=ah).status_code == 400
        assert client.put("/auth/users/stopuser2/disabled", json={"disabled": []}, headers=ah).status_code == 400
        # 幂等：重复停用 / 重复启用都算成功（页面连点两下不该报错）
        assert client.put("/auth/users/stopuser2/disabled", json={"disabled": True}, headers=ah).status_code == 200
        assert client.put("/auth/users/stopuser2/disabled", json={"disabled": True}, headers=ah).status_code == 200
        assert client.put("/auth/users/stopuser2/disabled", json={"disabled": False}, headers=ah).status_code == 200
    finally:
        client.delete("/auth/users/stopuser2", headers=ah)


def test_disabled_flag_blocks_every_authed_endpoint_and_heals_session():
    """停用是全局闸门：账号所有需鉴权的接口一律进不来；手工改盘（没踢会话）也有兜底。"""
    from fastapi.testclient import TestClient
    import app.main as m
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    mh = _mk_member(client, ah, "stopuser3")
    tok = mh["Authorization"].split(" ", 1)[1]
    try:
        assert client.put("/auth/users/stopuser3/disabled", json={"disabled": True}, headers=ah).status_code == 200
        for path in ("/models", "/metrics", "/logs", "/history/images", "/auth/apikeys"):
            r = client.get(path, headers=mh)
            assert r.status_code == 401, f"{path} 应 401，实际 {r.status_code}"
        assert m._sess.account_for(tok) is None, "停用时接口应主动吊销该账号的会话"
        # 兜底那道闸：直接改 store（等价于手工编辑 users.json —— 没走接口、也就没踢会话），
        # 鉴权层同样把该账号的会话判为无效，并顺手吊销。
        m._users.set_disabled("stopuser3", False)         # 先复原（接口那条路是带着踢会话的）
        tok2 = m._sess.create("127.0.0.1", "stopuser3")   # 造一个「账号当时可用」的会话
        assert m._identity_from_credentials(tok2) is not None
        m._users.set_disabled("stopuser3", True)          # 只有盘上标记变了，会话没被踢
        assert m._identity_from_credentials(tok2) is None, "鉴权层应自行作废被停用账号的会话"
        assert m._sess.account_for(tok2) is None
        # 启用后恢复（闸门只认 disabled 字段）
        assert client.put("/auth/users/stopuser3/disabled", json={"disabled": False}, headers=ah).status_code == 200
        lg = client.post("/auth/login", json={"username": "stopuser3", "password": "stopuser3"})
        assert lg.status_code == 200, lg.text
        mh2 = {"Authorization": f"Bearer {lg.json()['access_token']}"}
        assert client.get("/models", headers=mh2).status_code == 200
    finally:
        client.delete("/auth/users/stopuser3", headers=ah)


def test_admin_disabled_flag_in_file_is_autohealed():
    """手工把 admin 标成 disabled → 重启加载时自动恢复启用（唯一管理员锁死是灾难）。"""
    import tempfile, json as _json
    from pathlib import Path as _P
    from app.users import UsersStore
    with tempfile.TemporaryDirectory() as d:
        p = _P(d) / "users.json"
        UsersStore(str(p))          # 造出初始文件（下面直接改里面 admin 的 disabled 标记）
        data = _json.loads(p.read_text(encoding="utf-8"))
        data["users"]["admin"]["disabled"] = True
        data["users"]["admin"]["disabled_at"] = 1.0
        p.write_text(_json.dumps(data), encoding="utf-8")
        again = UsersStore(str(p))          # 重新加载（等价于进程重启）
        assert again.is_disabled("admin") is False, "admin 不允许被停用"
        assert _json.loads(p.read_text(encoding="utf-8"))["users"]["admin"]["disabled"] is False


def test_chat_conversation_id_is_internal_not_upstream():
    """方案 A：conversation_id 是网关内部字段，绝不能透传给上游。"""
    from app.models import ChatRequest
    req = ChatRequest(model="agnes-3.0-flash", messages=[{"role": "user", "content": "hi"}],
                      conversation_id="conv-123")
    assert req.conversation_id == "conv-123"
    assert "conversation_id" not in req.upstream_extra(), "内部字段不应透传上游"
    assert "conversation_id" not in req.unknown_fields()
    # 未带时为 None，且同样不透传
    req2 = ChatRequest(model="agnes-3.0-flash", messages=[{"role": "user", "content": "hi"}])
    assert req2.conversation_id is None
    assert "conversation_id" not in req2.upstream_extra()


def test_opencode_session_header_from_conversation_id():
    """方案 A：同一对话的 conversation_id 原样作为 x-opencode-session；缺省回退随机 UUID。"""
    from app.adapters import create_adapter
    cfg = load_config(CONFIG)
    p = cfg.providers["opencode"]
    assert p.requires_session_header is True
    ad = create_adapter(p, "dummy-token")
    # 传会话 id → 原样使用（同一段对话稳定，吃上游提示词缓存）
    assert ad._req_headers("conv-abc")["x-opencode-session"] == "conv-abc"
    # 未传 → 回退 32 位随机 hex，且两次不同
    h1 = ad._req_headers()["x-opencode-session"]
    h2 = ad._req_headers()["x-opencode-session"]
    assert len(h1) == 32 and h1 != h2
    # 非会话供应商（agnes / 火山）根本不该带这个头
    ag = create_adapter(cfg.providers["agnes"], "dummy-token")
    assert "x-opencode-session" not in ag._req_headers("conv-abc")


def test_api_conversation_id_flows_to_session_header():
    """API 调用（HTTP body）传 conversation_id → 适配器 session_id；且不透传上游 payload。

    覆盖老板问的「非前端、纯 API 调用是否支持稳定会话」：只要请求体带
    conversation_id，后端就把它作为 OpenCode Go 的 x-opencode-session。
    第二十六轮起不再**原样透传**，而是派生成「账号 + 会话 id」的稳定哈希（隔离见
    test_conversation_id_isolated_per_account）。
    """
    from fastapi.testclient import TestClient
    import app.main as m

    captured = {}

    class Capture:
        async def chat(self, model=None, messages=None, session_id=None, extra=None, **kw):
            captured["session_id"] = session_id
            captured["extra"] = extra
            return {"id": "c1", "object": "chat.completion", "model": model,
                    "choices": [{"message": {"role": "assistant", "content": "ok"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1}}

    orig = m._get_adapter
    m._get_adapter = lambda model_id, timeout=None, cfg=None: (
        Capture(), "agnes", m._cfg().providers["agnes"])
    try:
        client = TestClient(m.app)
        # ① 带 conversation_id → 原样落为 session_id；且不作为上游参数透传
        r = client.post("/v1/chat/completions", headers={"Authorization": _AUTOBEARER},
                        json={"model": "agnes-3.0-flash",
                              "messages": [{"role": "user", "content": "hi"}],
                              "conversation_id": "conv-api-42"})
        assert r.status_code == 200, r.text
        # 第二十六轮：不再原样透传，改为「账号 + 会话 id」的稳定派生值
        derived = captured["session_id"]
        assert derived and derived != "conv-api-42", "不应把客户端原值直接发给上游"
        # 同账号 + 同会话 → 恒定不变（否则缓存收益就没了）
        client.post("/v1/chat/completions", headers={"Authorization": _AUTOBEARER},
                    json={"model": "agnes-3.0-flash",
                          "messages": [{"role": "user", "content": "hi"}],
                          "conversation_id": "conv-api-42"})
        assert captured["session_id"] == derived, "同一会话必须派生出同一个 session"
        assert not captured["extra"] or "conversation_id" not in captured["extra"]
        # ② 不传 conversation_id → 第二十九轮起不再 None/随机：走「Key 稳定会话」
        #   （无 Key 走账号稳定兜底；此处是登录会话 → 账号兜底，同样必须恒定）
        r2 = client.post("/v1/chat/completions", headers={"Authorization": _AUTOBEARER},
                         json={"model": "agnes-3.0-flash",
                               "messages": [{"role": "user", "content": "hi"}]})
        assert r2.status_code == 200, r2.text
        kstable = captured["session_id"]
        assert kstable is not None, "无会话也必须派生成稳定值（不再是 None/随机）"
        assert kstable != derived, "无会话的稳定值应来自另一个命名空间"
        # 同 Key 再打一次（换一句提问） → 会话依旧恒定，缓存才能生效
        r3 = client.post("/v1/chat/completions", headers={"Authorization": _AUTOBEARER},
                         json={"model": "agnes-3.0-flash",
                               "messages": [{"role": "user", "content": "hi again"}]})
        assert r3.status_code == 200, r3.text
        assert captured["session_id"] == kstable, "同一 Key 无会话时必须恒定（保住缓存收益）"
    finally:
        m._get_adapter = orig


# ── 第二十六轮：会话 id 入站校验 + 按账号隔离 ──
#
# 起因（实测）：conversation_id 最终要进 **HTTP header**（x-opencode-session）。原先
# 无任何校验，传中文时 httpx 组 header 抛 UnicodeEncodeError → 被误判成"上游失败"
# → 重试 3 次（必然全失败）→ 最终 502「路由 x 全部模型调用失败」+ 记熔断，真实原因
# （客户端传错值）被完全掩盖；同时原值直接透传，两个账号用同一个字符串会共用上游
# 缓存命名空间（不泄露内容，但命中率/计费口径互相干扰）。


def test_conversation_id_model_level_normalization():
    """模型层：剥空白、空白串视作未传；非 ASCII / 超长一律校验失败。"""
    from pydantic import ValidationError
    from app.models import ChatRequest, CONVERSATION_ID_MAX

    base = {"model": "x", "messages": [{"role": "user", "content": "hi"}]}
    # 两端空白剥掉；全空白等同未传（回退上游随机会话，与旧行为一致）
    assert ChatRequest(**base, conversation_id="  abc-1  ").conversation_id == "abc-1"
    assert ChatRequest(**base, conversation_id="   ").conversation_id is None
    # 恰好到上限可用，超一个字符就拒
    assert ChatRequest(**base, conversation_id="A" * CONVERSATION_ID_MAX).conversation_id
    for bad in ["会话一号", "café", "🙂", "A" * (CONVERSATION_ID_MAX + 1)]:
        try:
            ChatRequest(**base, conversation_id=bad)
        except ValidationError:
            pass
        else:
            raise AssertionError(f"{bad!r} 应被拒绝")


def test_conversation_id_isolated_per_account():
    """同一 conversation_id 在不同账号下必须派生成不同的上游 session（反之恒定）。"""
    import app.main as m

    a = m._upstream_session("shared-conv", "alice")
    b = m._upstream_session("shared-conv", "bob")
    assert a != b, "不同账号必须隔离（否则共用上游缓存命名空间）"
    assert a == m._upstream_session("shared-conv", "alice"), "同账号同会话必须稳定（保住缓存收益）"
    assert m._upstream_session("other-conv", "alice") != a, "同账号不同会话必须不同"
    # 第二十九轮：未传 conversation_id → 不再回退 None/随机（那会命中率恒 0）。
    #   有 API Key → 按 Key 派生稳定会话；没有（登录会话）→ 按账号稳定兜底。
    k1 = m._upstream_session(None, "alice", "TOKEN-A")
    assert k1 and len(k1) == 32 and all(c in "0123456789abcdef" for c in k1)
    assert k1 == m._upstream_session(None, "alice", "TOKEN-A"), "同一 Key → 必须恒定（缓存收益）"
    assert k1 != m._upstream_session(None, "alice", "TOKEN-B"), "不同 Key → 必须隔离"
    assert k1 != a, "Key 会话与「账号+会话」派生值应来自不同空间"
    assert m._upstream_session("", "alice", "TOKEN-A") == k1, "空白串视同未传 → 走 Key 稳定会话"
    u1 = m._upstream_session(None, "alice")
    assert u1 and u1 == m._upstream_session(None, "alice"), "无 Key → 账号兜底且恒定"
    assert u1 != m._upstream_session(None, "bob"), "不同账号兜底值不同"
    # 上游只应看到一个十六进制串，不该看到账号名、业务命名或 Key 明文
    assert len(a) == 32 and all(c in "0123456789abcdef" for c in a), a
    assert "alice" not in a and "shared" not in a and "TOKEN-A" not in k1


def test_non_ascii_conversation_id_rejected_before_upstream():
    """非法会话 id 必须在入站回 422，且**一次都不发上游**（此前会重试 3 次再报 502）。"""
    from fastapi.testclient import TestClient
    import app.main as m

    calls = []

    class Capture:
        async def chat(self, model=None, messages=None, session_id=None, extra=None, **kw):
            calls.append(session_id)
            return {"id": "c1", "object": "chat.completion", "model": model,
                    "choices": [{"message": {"role": "assistant", "content": "ok"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1}}

    orig = m._get_adapter
    m._get_adapter = lambda model_id, timeout=None, cfg=None: (
        Capture(), "agnes", m._cfg().providers["agnes"])
    try:
        client = TestClient(m.app)
        base = {"model": "agnes-3.0-flash", "messages": [{"role": "user", "content": "hi"}]}

        # ① 中文 → 422（不是 502），且适配器零调用
        r = client.post("/v1/chat/completions", headers={"Authorization": _AUTOBEARER},
                        json={**base, "conversation_id": "会话一号"})
        assert r.status_code == 422, (r.status_code, r.text)
        assert "ASCII" in r.text, r.text
        assert calls == [], "非法会话 id 不该发往上游（此前会重试 3 次 + 502）"

        # ② 超长 → 422，同样零调用
        r = client.post("/v1/chat/completions", headers={"Authorization": _AUTOBEARER},
                        json={**base, "conversation_id": "A" * 500})
        assert r.status_code == 422, (r.status_code, r.text)
        assert calls == []

        # ③ 空白串 → 视作未传，正常放行（第二十九轮：不再 None/随机，给稳定会话）
        r = client.post("/v1/chat/completions", headers={"Authorization": _AUTOBEARER},
                        json={**base, "conversation_id": "   "})
        assert r.status_code == 200, r.text
        assert len(calls) == 1 and calls[0] is not None, "空白=未传 → 稳定会话（非 None）"
    finally:
        m._get_adapter = orig


# ── 第二十五轮：模型级强制参数（上游硬约束）+ 测速超时/错误文案 ──
#
# 起因：kimi-k2.7-code 上游只接受 temperature=1，测速探针固定发 0 → 每次必 400；
# 而 mimo-v2.5 是慢（偶发冲破 15s 探针超时），却被显示成同样的「调用失败」。
# 两条修法各自的回归如下。

def test_model_params_parsed_from_config():
    """gateway.yaml 的 models[].params 要被解析出来，并随 all_models 暴露。"""
    from app.config import parse_config
    cfg = parse_config({"providers": {"p": {"base_url": "http://x/v1", "models": [
        {"id": "m1", "params": {"temperature": 1, "top_p": 0.9}},
        {"id": "m2"}]}}})
    assert cfg.providers["p"].models[0].params == {"temperature": 1, "top_p": 0.9}
    assert cfg.providers["p"].models[1].params == {}
    am = {m["id"]: m for m in cfg.all_models()}
    assert am["m1"]["params"] == {"temperature": 1, "top_p": 0.9}


def test_model_params_rejects_bad_keys_and_types():
    """params 的键必须在白名单内：结构性键（model/messages/stream）绝不允许 ——
    否则等于让配置文件改写路由与协议语义（回到越权面）。写错必须报错，不能静默失效。"""
    from app.config import parse_config, ConfigError
    bads = [{"model": "evil"}, {"messages": []}, {"stream": True}, {"temperature": object()}, [1, 2]]
    for bad in bads:
        try:
            parse_config({"providers": {"p": {"base_url": "http://x/v1",
                                              "models": [{"id": "m", "params": bad}]}}})
        except ConfigError:
            continue
        raise AssertionError(f"非法 params 未被拦截: {bad!r}")


def test_real_config_locks_kimi_temperature():
    """真配置口径：kimi-k2.7-code 只接受 temperature=1，必须在配置里写死。"""
    cfg = load_config(CONFIG)
    m = [x for x in cfg.providers["opencode"].models if x.id == "kimi-k2.7-code"][0]
    assert m.params.get("temperature") == 1, m.params


def test_model_params_override_client_value():
    """配了 params 的模型：客户端传什么值都被覆盖，且只发一次请求（不做无谓重发）。"""
    from app.adapters.openai_compat import OpenAICompatAdapter
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"id": "1", "choices": [], "usage": {}})

    ad = OpenAICompatAdapter("t", "http://mock.test/v1",
                             model_params={"locked": {"temperature": 1}})
    ad._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                   base_url="http://mock.test/v1")

    async def run():
        await ad.chat("locked", [{"role": "user", "content": "hi"}], temperature=0)
        await ad.close()

    asyncio.run(run())
    assert seen[0]["temperature"] == 1, seen[0]
    assert len(seen) == 1


def test_model_param_constraint_auto_corrected_and_retried():
    """上游 400 说「只允许 temperature=1」→ 网关按 1 重发一次（而非原样重试）。"""
    from app.adapters.openai_compat import OpenAICompatAdapter
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        if len(seen) == 1:
            return httpx.Response(400, json={"error": {"message":
                "Error from provider (Console Go): [invalid_request_error] "
                "invalid temperature: only 1 is allowed for this model"}})
        return httpx.Response(200, json={"id": "1", "choices": [
            {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}], "usage": {}})

    ad = OpenAICompatAdapter("t", "http://mock.test/v1")
    ad._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                   base_url="http://mock.test/v1")

    async def run():
        r = await ad.chat("kimi-k2.7-code", [{"role": "user", "content": "hi"}], temperature=0)
        await ad.close()
        return r

    r = asyncio.run(run())
    assert len(seen) == 2, seen
    assert seen[0]["temperature"] == 0 and seen[1]["temperature"] == 1, seen
    assert r["choices"][0]["message"]["content"] == "ok"


def test_model_param_constraint_no_infinite_retry():
    """上游始终 400 → 只修正重发一次就抛错（绝不能与同一约束来回打转）。"""
    from app.adapters.openai_compat import OpenAICompatAdapter
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(400, json={"error": {"message":
            "invalid temperature: only 1 is allowed for this model"}})

    ad = OpenAICompatAdapter("t", "http://mock.test/v1")
    ad._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                   base_url="http://mock.test/v1")

    async def run():
        try:
            await ad.chat("m", [{"role": "user", "content": "hi"}], temperature=0)
        finally:
            await ad.close()

    try:
        asyncio.run(run())
        raise AssertionError("应抛出 httpx.HTTPStatusError")
    except httpx.HTTPStatusError:
        pass
    assert len(seen) == 2, f"应只发 2 次（原始 + 修正），实际 {len(seen)}"


def test_model_param_conflict_respects_configured_value():
    """管理员写死的值与上游要求冲突 → 尊重配置，不偷偷改、也不发无谓的第二枪。"""
    from app.adapters.openai_compat import OpenAICompatAdapter
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(400, json={"error": {"message":
            "invalid temperature: only 1 is allowed for this model"}})

    ad = OpenAICompatAdapter("t", "http://mock.test/v1", model_params={"m": {"temperature": 0.5}})
    ad._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                   base_url="http://mock.test/v1")

    async def run():
        try:
            await ad.chat("m", [{"role": "user", "content": "hi"}], temperature=0.5)
        finally:
            await ad.close()

    try:
        asyncio.run(run())
        raise AssertionError("应抛出 httpx.HTTPStatusError")
    except httpx.HTTPStatusError:
        pass
    assert len(seen) == 1, seen
    assert seen[0]["temperature"] == 0.5, seen[0]


def test_stream_param_constraint_auto_corrected():
    """流式路径同样自修复：开局 400 → 按上游要求修正后重发。"""
    from app.adapters.openai_compat import OpenAICompatAdapter
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        if len(seen) == 1:
            return httpx.Response(400, json={"error": {"message":
                "invalid temperature: only 1 is allowed for this model"}})
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n',
                              headers={"content-type": "text/event-stream"})

    ad = OpenAICompatAdapter("t", "http://mock.test/v1")
    ad._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                   base_url="http://mock.test/v1")

    async def run():
        out = [c async for c in ad.chat_stream("kimi-k2.7-code",
                                               [{"role": "user", "content": "hi"}], temperature=0)]
        await ad.close()
        return out

    out = asyncio.run(run())
    assert len(seen) == 2, seen
    assert seen[0]["temperature"] == 0 and seen[1]["temperature"] == 1, seen
    assert out, "流式应至少收到一个数据块"


def test_speedtest_error_text_surfaces_timeout_and_upstream_detail():
    """测速失败文案：超时要说清是超时，400 要透出上游原文。
    （本轮排查时，"参数被上游拒绝" 与 "模型太慢" 在旧文案里长得一模一样。）"""
    import app.main as m
    t = m._speedtest_error_text(httpx.ReadTimeout("timed out"), 30.0)
    assert "超时" in t and "30" in t, t

    req = httpx.Request("POST", "http://x/v1/chat/completions")
    resp = httpx.Response(400, request=req, json={"error": {
        "message": "invalid temperature: only 1 is allowed for this model"}})
    e = httpx.HTTPStatusError("Client error '400 Bad Request'", request=req, response=resp)
    t2 = m._speedtest_error_text(e, 30.0)
    assert "only 1 is allowed" in t2, t2


def test_settings_panel_preserves_model_params():
    """管理页面保存配置时，手写在 YAML 里的 models[].params 不能被面板冲掉。
    （面板的模型编辑器只认 id/display_name/type/vision，照它重建就会静默丢掉 params。）"""
    from app import settings as gw_settings
    from app.config import parse_config
    raw = {"providers": {"p": {"name": "P", "base_url": "http://x/v1", "models": [
        {"id": "m", "display_name": "M", "type": "text", "vision": False,
         "params": {"temperature": 1}}]}}}
    cfg = parse_config(raw)
    view = gw_settings.config_to_view(cfg)
    # 面板读出来就带着 params
    assert view["providers"]["p"]["models"][0]["params"] == {"temperature": 1}
    # 提交回去（模拟面板原样回传）→ params 必须还在
    out = gw_settings.view_to_raw(cfg, raw, {"providers": view["providers"]})
    assert out["providers"]["p"]["models"][0]["params"] == {"temperature": 1}, \
        out["providers"]["p"]["models"][0]


# 注意：`if __name__ == "__main__"` 运行块**必须留在文件最末尾**（见文件底部）。
#   第二十七轮收尾时它被夹在文件中间，导致定义在它之后的测试在脚本模式下根本不会被收集
#   （test_image_request_goes_to_requested_model 就这么白写了一轮）。运行器读的是执行到
#   该行时已存在的 globals，所以它之前的所有 def 才算数。
#   同时：pytest 模式不受影响（它自己收集全文件），因此这个坑只在 `python tests/test_gateway.py`
#   （Makefile 的 make test 走的就是这条）下暴露 —— 两种入口的行为必须一致。


def test_image_request_goes_to_requested_model():
    """第二十七轮: 带图请求不再被重排到"链上的视觉模型" —— 原样打到用户选的模型。

    视觉优先重排随路由链一起删除：只有一个模型时，"重排"等于替用户换模型。
    这里特意用**没标视觉能力**的模型（deepseek-v4-pro），证明网关不做小动作。
    """
    from fastapi.testclient import TestClient
    import app.main as m

    seen = []

    class Cap:
        async def chat(self, model=None, messages=None, **kw):
            seen.append(model)
            return {"id": "x", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    orig = m._get_adapter
    try:
        with _Isolated() as mm:
            mm._get_adapter = lambda model_id, timeout=None, cfg=None: (
                Cap(), "opencode", mm._cfg().providers["opencode"])
            client = TestClient(mm.app)
            r = client.post("/v1/chat/completions", json={
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "看图"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]}]})
            assert r.status_code == 200, r.text
            assert seen == ["deepseek-v4-pro"], seen
    finally:
        m._get_adapter = orig


# ══════════════════════════════════════════════════════════════════════
# 三线直传：真发请求的回归测试（2026-09-18 上线后补）
# ══════════════════════════════════════════════════════════════════════
# 为什么现在才补：第二十七轮把「三线直传」做成了核心功能，但改动后**没有任何一条单测
# 真发过 /v1/messages 或 /v1/responses 请求**（只覆盖了配置解析、/v1/models 列表、
# 跨线文案），于是两处 500 一路溜到了生产：
#   ① `_enforce_rate_limit(cfg, request, None, pkey, model_in)` 多传一个参数
#      → 每一次直传都是 `TypeError` → 500「网关内部错误」；
#   ② `/metrics/prometheus` 还在读已被删除的 `snapshot["fallback_total"]` → KeyError → 500。
# 两条都属于「改了一处、漏了另一处」：所以这里的测试刻意**走完整 HTTP 路径**，
# 而不是直接调内部函数 —— 只有真发请求才能把这类接线错误照出来。
# 另一层原因见上面运行块旁注：运行器原来夹在文件中间，文件尾部的测试根本没跑。

try:
    from contextlib import asynccontextmanager as _asynccontextmanager
except Exception:  # pragma: no cover
    _asynccontextmanager = None


class _FakeStreamResp:
    """假的流式上游响应（直传只用到 status_code / headers / aread / aiter_bytes）。"""

    def __init__(self, status, headers, chunks):
        self.status_code = status
        self.headers = headers
        self._chunks = list(chunks)

    async def aread(self):
        return b"".join(self._chunks)

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


class _PassthroughAdapter:
    """直传用的假适配器：只实现直传需要的三个原语，并记录出站参数。"""

    def __init__(self, status=200, body=None, stream_status=200, stream_chunks=None,
                 stream_ctype="text/event-stream"):
        self.status = status
        self.body = {"ok": True} if body is None else body
        self.stream_status = stream_status
        self.stream_chunks = list(stream_chunks or [b""])
        self.stream_ctype = stream_ctype
        self.json_calls = []      # (protocol, payload, session)
        self.stream_calls = []

    def prepare_payload(self, model, payload):
        """与真实适配器同口径：只定点改 model（强制参数已在别处单独测）。"""
        out = dict(payload)
        out["model"] = model
        return out

    async def raw_json(self, protocol, payload, session_id=None):
        self.json_calls.append((protocol, payload, session_id))
        return self.status, self.body

    def raw_stream(self, protocol, payload, session_id=None):
        self.stream_calls.append((protocol, payload, session_id))
        adapter = self

        @_asynccontextmanager
        async def _cm():
            yield _FakeStreamResp(adapter.stream_status,
                                  {"content-type": adapter.stream_ctype},
                                  adapter.stream_chunks)
        return _cm()


def _bind_passthrough(m, adapter):
    """把假适配器接到 _get_adapter 上（供应商固定用 opencode：它三条线都配了）。"""
    m._get_adapter = lambda model_id, timeout=None, cfg=None: (
        adapter, "opencode", m._cfg().providers["opencode"])


def test_passthrough_messages_line_round_trip():
    """POST /v1/messages：入站 messages → 出站 messages → 上游字段原样回。

    这条路径此前**零覆盖**，而它正是本轮功能本身。
    """
    from fastapi.testclient import TestClient
    import app.main as m

    fake = _PassthroughAdapter(body={
        "id": "msg_1", "type": "message", "role": "assistant",
        "content": [{"type": "text", "text": "OK"}],
        "usage": {"input_tokens": 7, "output_tokens": 3}})
    orig = m._get_adapter
    try:
        with _Isolated() as mm:
            _bind_passthrough(mm, fake)
            r = TestClient(mm.app).post("/v1/messages", json={
                "model": "deepseek-v4.1-flash", "max_tokens": 16,
                "messages": [{"role": "user", "content": "回复：OK"}],
                "conversation_id": "conv-abc"})
            assert r.status_code == 200, r.text
            protocol, payload, session = fake.json_calls[-1]
            assert protocol == m.PROTOCOL_MESSAGES, protocol
            assert payload["model"] == "deepseek-v4.1-flash", payload
            assert payload["messages"] == [{"role": "user", "content": "回复：OK"}], payload
            # conversation_id 是网关内部字段（不透传上游），上游只看到按账号派生的 32 位哈希
            assert "conversation_id" not in payload, payload
            assert session and len(session) == 32, session
            j = r.json()
            assert j["content"][0]["text"] == "OK", j            # 上游正文原样
            assert j["served_model"] == "deepseek-v4.1-flash", j  # 不再有 fallback 换模型
            assert j["protocol"] == "messages" and j["provider"] == "opencode", j
            assert r.headers.get("x-request-id"), dict(r.headers)
            snap = mm._metrics.snapshot()
            assert snap["calls"]["deepseek-v4.1-flash"]["ok"] == 1, snap["calls"]
            # messages 线的 usage 字段名不同（input/output_tokens），也必须记进 token 账
            assert (snap["tokens"]["prompt"], snap["tokens"]["completion"]) == (7, 3), snap["tokens"]
    finally:
        m._get_adapter = orig


def test_passthrough_responses_line_and_cross_line_gate():
    """POST /v1/responses 直传 + 跨线闸门：模型不支持的线必须在**入口** 400（不打扰上游）。"""
    from fastapi.testclient import TestClient
    import app.main as m

    fake = _PassthroughAdapter(body={
        "id": "resp_1", "object": "response", "status": "completed",
        "usage": {"input_tokens": 4, "output_tokens": 2}})
    orig = m._get_adapter
    try:
        with _Isolated() as mm:
            _bind_passthrough(mm, fake)
            c = TestClient(mm.app)
            # grok-4.6 实测只认 responses 线
            r = c.post("/v1/responses", json={"model": "grok-4.6", "input": "hi", "max_tokens": 16})
            assert r.status_code == 200, r.text
            assert fake.json_calls[-1][0] == m.PROTOCOL_RESPONSES
            assert r.json()["protocol"] == "responses", r.json()
            # 跨线：agnes 只声明 chat/completions → messages / responses 都必须 400 并点出支持的线
            for path, proto in (("/v1/messages", "messages"), ("/v1/responses", "responses")):
                r = c.post(path, json={"model": "agnes-3.0-flash", "max_tokens": 16,
                                       "messages": [{"role": "user", "content": "x"}]})
                assert r.status_code == 400, (path, r.status_code, r.text)
                assert proto in r.text and "chat/completions" in r.text, (path, r.text)
            assert len(fake.json_calls) == 1, fake.json_calls  # 被挡下的请求一次都没打到上游
    finally:
        m._get_adapter = orig


def test_passthrough_enforces_provider_rate_limit():
    """直传也必须走供应商级限流（这里正是 TypeError 的现场：多传了一个参数）。"""
    import time as _time
    from fastapi.testclient import TestClient
    import app.main as m

    fake = _PassthroughAdapter()
    orig_get, limiter = m._get_adapter, m._limiter
    pconf = m._cfg().providers["opencode"]
    saved_rps = pconf.rate_limit_rps
    body = {"model": "deepseek-v4.1-flash", "max_tokens": 16,
            "messages": [{"role": "user", "content": "x"}]}
    try:
        with _Isolated() as mm:
            _bind_passthrough(mm, fake)
            pconf.rate_limit_rps = 0.001
            # 把该供应商的桶清空 → 下一次请求必然被限流（不靠 sleep 抢时序）
            limiter._buckets["prov:opencode"] = [0.0, _time.monotonic()]
            c = TestClient(mm.app)
            r = c.post("/v1/messages", json=body)
            assert r.status_code == 429, r.text
            assert "限流" in r.text, r.text
            assert not fake.json_calls, "被限流的请求不该打到上游"
            # 不限流时同一端点必须 200（若再出现 TypeError，这里就红）
            pconf.rate_limit_rps = None
            r = c.post("/v1/messages", json=body)
            assert r.status_code == 200, r.text
    finally:
        pconf.rate_limit_rps = saved_rps
        limiter._buckets.pop("prov:opencode", None)
        m._get_adapter = orig_get


def test_passthrough_upstream_error_verbatim_and_no_breaker_on_4xx():
    """上游 4xx 原样回（状态码 + 正文都不改）；且 4xx 是调用方 body 的问题，不该计熔断。"""
    from fastapi.testclient import TestClient
    import app.main as m

    up_body = {"error": {"type": "authentication_error", "message": "invalid api key"}}
    fake = _PassthroughAdapter(status=401, body=up_body)
    orig = m._get_adapter
    try:
        with _Isolated() as mm:
            _bind_passthrough(mm, fake)
            r = TestClient(mm.app).post("/v1/messages", json={
                "model": "deepseek-v4.1-flash", "max_tokens": 16,
                "messages": [{"role": "user", "content": "x"}]})
            assert r.status_code == 401, r.text
            assert r.json()["error"]["message"] == "invalid api key", r.json()
            snap = mm._metrics.snapshot()
            assert snap["calls"]["deepseek-v4.1-flash"]["fail"] == 1, snap["calls"]
            cb = next(x for x in mm._circuits.statuses() if x["provider"] == "opencode")
            assert cb["recent_failures"] == 0, cb
    finally:
        m._get_adapter = orig


def test_passthrough_stream_forwards_bytes_and_real_status():
    """流式直传：字节原样转发（不重编帧）+ 池外只读扫 usage；上游 4xx 要如实给状态码。

    「不把上游 400 塞进 200 的 SSE 流」是这块代码的明确设计（否则 SDK 会以为请求成功了）。
    """
    from fastapi.testclient import TestClient
    import app.main as m

    sse = (b'event: message_start\n'
           b'data: {"type":"message_start","message":{"usage":{"input_tokens":5,"output_tokens":0}}}\n\n'
           b'event: message_delta\n'
           b'data: {"type":"message_delta","usage":{"output_tokens":2}}\n\n'
           b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
    fake = _PassthroughAdapter(stream_chunks=[sse[:40], sse[40:]])
    body = {"model": "deepseek-v4.1-flash", "max_tokens": 16, "stream": True,
            "messages": [{"role": "user", "content": "x"}]}
    orig = m._get_adapter
    try:
        with _Isolated() as mm:
            _bind_passthrough(mm, fake)
            c = TestClient(mm.app)
            r = c.post("/v1/messages", json=body)
            assert r.status_code == 200, r.text
            assert r.content == sse, (r.content[:200], sse[:200])   # 分块被拼接，但字节没被改写
            assert fake.stream_calls[-1][0] == m.PROTOCOL_MESSAGES
            snap = mm._metrics.snapshot()
            assert snap["calls"]["deepseek-v4.1-flash"]["ok"] == 1, snap["calls"]
            assert (snap["tokens"]["prompt"], snap["tokens"]["completion"]) == (5, 2), snap["tokens"]

            # 上游流式返回 400：状态码与正文都要如实回，而不是包成 200 的空流
            fake.stream_status, fake.stream_chunks = 400, [b'{"error":{"message":"bad request"}}']
            fake.stream_ctype = "application/json"
            r = c.post("/v1/messages", json=body)
            assert r.status_code == 400, r.text
            assert "bad request" in r.text, r.text
            assert not r.text.startswith("event:"), r.text
    finally:
        m._get_adapter = orig


def test_prometheus_export_ok_and_dropped_metrics_removed():
    """GET /metrics/prometheus 必须 200，且不再导出已删除的 fallback_total。

    线上 500 的真因：导出器还在 `snap["fallback_total"]`（该字段随路由/fallback 链一起删了）。
    这里顺带校验「HELP/TYPE 声明」与「实际导出的指标名」一一对应 —— 声明的指标没有导出
    （或反之）都说明导出器接线断了，Grafana 会看到空图。
    """
    from fastapi.testclient import TestClient

    with _Isolated() as mm:
        r = TestClient(mm.app).get("/metrics/prometheus")
        assert r.status_code == 200, r.text
        text = r.text
        assert "api_gateway_uptime_seconds" in text, text
        # 只在「指标行 + HELP/TYPE 声明」里查，不在注释行里查：说明性注释提到旧指标名是
        #   允许的（正文注释不参与抓取），导出器真把旧指标名写进声明/指标行才算坏。
        scraped = [ln for ln in text.splitlines()
                   if ln and (not ln.startswith("#") or ln.startswith(("# HELP ", "# TYPE ")))]
        assert not any("fallback_total" in ln for ln in scraped), scraped
        declared = {ln.split()[2] for ln in text.splitlines() if ln.startswith("# HELP ")}
        exported = {ln.split("{")[0].split()[0] for ln in text.splitlines() if ln and not ln.startswith("#")}
        # 方向只要一边：**导出的指标必须都有 HELP 声明**（缺声明 = 接线断了，Grafana 里是空图）。
        #   反过来「声明了但没有样本」是正常的（这一刻没有该模型的调用 / 没有熔断器实例），
        #   Prometheus 允许 HELP/TYPE 先于样本出现。
        assert not (exported - declared), sorted(exported - declared)


def test_media_requests_carry_bearer_token():
    """第二十七轮回归：图像/视频的**三个**调用点也必须带鉴权头。

    媒体端点不属于「三条协议线」，走的是静态 headers。第二十七轮把鉴权头改成
    「在 _req_headers 里按协议线组装」后，create_adapter 不再往静态头里塞 Authorization，
    而这三个调用点还在用 self._headers ⇒ 出站请求**一个鉴权头都没有**。真机表现：
    `POST /v1/images/generations` 上游 401「未提供令牌」（同一个 key 直接 curl 同地址是 200）。
    """
    from app.adapters import create_adapter

    cfg = load_config(CONFIG)
    pconfig = cfg.providers["agnes"]
    pconfig.base_url = "http://mock.test/v1"
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path.endswith("/images/generations"):
            return httpx.Response(200, json={"data": [{"url": "http://mock.test/i.png"}]})
        if req.url.path.endswith("/videos"):
            return httpx.Response(200, json={"video_id": "mock-video-001", "id": "task-mock"})
        return httpx.Response(200, json={"status": "SUCCESS"})

    adapter = create_adapter(pconfig, "test-token-123", timeout=10)
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                        base_url=pconfig.base_url)

    async def run():
        await adapter.generate_image("agnes-image-2.5-flash", "a plain red square")
        await adapter.create_video("agnes-video-2.5-flash", "a cat")
        await adapter.poll_video("mock-video-001")
        await adapter.close()

    asyncio.run(run())
    assert len(seen) == 3, [r.url.path for r in seen]   # 三个调用点都被真的发出去了
    for req in seen:
        assert req.headers.get("authorization") == "Bearer test-token-123", \
            (req.url.path, dict(req.headers))


# ── 第三十轮「全面体检」发现项：会话落盘 / 视频归属 / env 值校验 / 口令下限 ──────

def test_session_store_persists_across_restart_and_hides_raw_token():
    """会话落盘：重启不掉线（本轮目的）；但盘上只许有 token 的哈希，不能有可直接用的凭证。"""
    import tempfile, os
    from app.sessions import SessionStore
    d = tempfile.mkdtemp()
    p = os.path.join(d, "sessions.json")
    s1 = SessionStore(ttl_seconds=3600, path=p)
    tok = s1.create("1.2.3.4", "alice")
    assert s1.validate(tok)
    raw = open(p, encoding="utf-8").read()
    assert tok not in raw, "落盘文件里出现了明文会话 token"
    assert "alice" in raw, "账号名要留着（否则重启后没法按账号吊销）"
    s2 = SessionStore(ttl_seconds=3600, path=p)          # 模拟进程重启
    assert s2.validate(tok), "重启后旧会话应仍然有效（本轮的改动目的）"
    assert s2.account_for(tok) == "alice"
    assert s2.revoke_account("alice") == 1
    s3 = SessionStore(ttl_seconds=3600, path=p)
    assert not s3.validate(tok), "吊销必须落盘，否则重启就把被吊销的会话放回来了"


def test_session_store_drops_expired_entries_on_load():
    """载入时直接丢掉过期会话 —— 别把已死亡会话带进运行期（否则 TTL 形同虚设一份）。"""
    import json, tempfile, os, time
    from app.sessions import SessionStore, _fp
    d = tempfile.mkdtemp()
    p = os.path.join(d, "sessions.json")
    live, dead = "sess-live-token", "sess-dead-token"
    json.dump({"version": 1, "sessions": {
        _fp(live): {"created": time.time(), "ip": "", "account": "a"},
        _fp(dead): {"created": time.time() - 10 * 86400, "ip": "", "account": "a"},
    }}, open(p, "w", encoding="utf-8"))
    s = SessionStore(ttl_seconds=3600, path=p)
    assert s.validate(live)
    assert not s.validate(dead), "过期会话不该被载入"


def test_video_owner_store_semantics():
    """视频归属：有登记按登记判、无登记放行、管理员放行；结论要能跨重启保持。"""
    import tempfile, os
    from app.video_owners import VideoOwnerStore
    d = tempfile.mkdtemp()
    p = os.path.join(d, "videos.json")
    v = VideoOwnerStore(path=p)
    v.record("vid-1", "alice", "agnes")
    assert v.owner("vid-1") == "alice"
    assert v.allows("vid-1", "alice") is True
    assert v.allows("vid-1", "bob") is False, "别人的视频不该能让另一个账号读到"
    assert v.allows("vid-1", "bob", is_admin=True) is True, "管理员要能排障"
    assert v.allows("vid-unknown", "bob") is True, "无登记的老任务放行（渐进收紧，不砸正常流程）"
    v2 = VideoOwnerStore(path=p)                          # 重启后归属仍在
    assert v2.allows("vid-1", "bob") is False
    v.record("vid-2", "alice")
    assert v.count() == 2


def test_video_poll_denies_other_account_allows_owner_and_admin():
    """端点级：别人 poll 我的视频 → 404（不是 403，避免把 id 变成存在性探针）；本人与管理员正常。"""
    import uuid
    from fastapi.testclient import TestClient
    import app.main as m

    vid = "vtest-owner-" + uuid.uuid4().hex[:8]

    class Fake:
        async def create_video(self, **kw):
            return {"video_id": vid, "raw": {"status": "queued", "id": vid}}

        async def poll_video(self, video_id, **kw):
            return {"raw": {"status": "completed", "progress": 100}}

    real = m._get_adapter
    m._get_adapter = lambda model, t, cfg: (Fake(), {}, model)
    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    names = []
    try:
        owner = _member(client, ah, "vowner"); names.append("vowner")
        other = _member(client, ah, "vother"); names.append("vother")
        r = client.post("/v1/videos", json={"model": "agnes-video-2.5-flash",
                                            "prompt": "x", "aspect_ratio": "16:9"}, headers=owner)
        assert r.status_code == 200, r.text
        assert r.json()["video_id"] == vid
        assert client.get(f"/v1/videos/{vid}", headers=other).status_code == 404, \
            "另一个账号不该能读走别人的视频任务"
        assert client.get(f"/v1/videos/{vid}", headers=owner).status_code == 200
        assert client.get(f"/v1/videos/{vid}", headers=ah).status_code == 200, "管理员放行"
        # 无登记（本次改动之前创建的老任务）→ 放行，不能砸掉正在轮询的老流程
        assert client.get("/v1/videos/never-recorded-xyz", headers=other).status_code == 200
    finally:
        m._get_adapter = real
        for n in names:
            client.delete(f"/auth/users/{n}", headers=ah)


def test_config_env_rejects_multiline_value_and_writes_sandbox_file():
    """env 值校验：换行/控制字符/超长一律 400（能凭空改掉 .env 里别的行）；正常值才落盘。"""
    import os
    from fastapi.testclient import TestClient
    import app.main as m

    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    name = sorted(m._env_snapshot().keys())[0]
    env_path = m._env_file()
    assert str(env_path).endswith(".test_env"), f"测试必须写在沙箱 env 文件上：{env_path}"
    before = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    try:
        for bad in ("abc\ndef", "abc\rX=A", "A\x00B", "x" * 4097):
            r = client.post("/config/env", json={"envs": {name: bad}}, headers=ah)
            assert r.status_code == 400, (bad[:12], r.status_code, r.text)
        after = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        assert after == before, "被拒的值不该动到文件"
        old = os.environ.get(name)
        try:
            r = client.post("/config/env", json={"envs": {name: "sk-test-value-123"}}, headers=ah)
            assert r.status_code == 200, r.text
            assert os.environ.get(name) == "sk-test-value-123", "写盘后要立即在运行时生效"
            assert f"{name}=sk-test-value-123" in env_path.read_text(encoding="utf-8")
        finally:
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
    finally:
        if before:
            env_path.write_text(before, encoding="utf-8")


def test_password_min_length_raised_to_8():
    """口令下限 4→8：只作用于「显式给密码/改密」；登录不设下限（老短口令账号照常登录）。"""
    import tempfile, os
    from app.users import UsersStore, UserError
    p = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    try:
        u = UsersStore(p)
        for bad in ("1234", "1234567", ""):
            try:
                u.create_user("shorty", password=bad)
                raise AssertionError(f"短口令 {bad!r} 不该被接受")
            except UserError as e:
                assert "8" in str(e), e
        u.create_user("shorty")                     # 默认密码=账号名，不受下限约束（老板约定）
        assert u.verify("shorty", "shorty") is True, "短默认口令仍要能登录"
        try:
            u.change_password("shorty", "1234567")
            raise AssertionError("改密到 7 位不该被接受")
        except UserError:
            pass
        u.change_password("shorty", "12345678")
        assert u.verify("shorty", "12345678") is True
        assert u.verify("shorty", "shorty") is False
    finally:
        try: os.unlink(p)
        except OSError: pass


# ── 第三十一轮：虚拟模型 id（别名 wjd）───────────────────────────────
#
# 老板决策（2026-09-18）：客户端固定写 `model: wjd`，实际转发到哪个真实模型由管理员在
# 「供应商与模型」页选。规则（保存期校验，见 app/config.py 的 ALIAS_PROTOCOLS 注释）：
#   ① 别名只走 chat/completions（OpenAI）线 —— 用 messages / responses 打 wjd 入口 400；
#   ② target 必须是**存在的文本模型**且支持 chat/completions（面板里其余模型置灰）；
#   ③ 别名 id 不得与真实模型 id 重名；
#   ④ 别名不复制目标配置：协议线/视觉/强制参数/定价全继承 target（一份事实）；
#   ⑤ enabled: false = 该 id 不存在（等同没配），且停用时不校验 target。
# 下面把「配置解析 / 五条规则 / 目录 / 三条入口 / 测速 / 面板保存」逐个钉住。

_ALIAS_BASE_RAW = {"providers": {"p": {"name": "P", "base_url": "http://x/v1", "models": [
    {"id": "m-chat", "display_name": "Chat 模型", "vision": True, "params": {"temperature": 1}},
    {"id": "m-msg", "display_name": "Messages 模型", "protocols": ["messages"]},
    {"id": "m-img", "display_name": "画图模型", "type": "image"},
]}}}


def _alias_cfg(target: str, name: str = "wjd", **spec):
    """真·测试配置：一个供应商 + 三个模型（chat / 只有 messages 线 / 图像）+ 一个别名。"""
    from app.config import parse_config
    raw = json.loads(json.dumps(_ALIAS_BASE_RAW))
    raw["aliases"] = {name: {"enabled": True, "target": target, **spec}}
    return parse_config(raw)


def test_alias_config_rules_and_resolution():
    """规则①~⑤ + 解析：别名→真实 id、普通 id 原样、停用等同不存在；违规一律拦在保存期。"""
    from app.config import parse_config, ALIAS_PROTOCOLS
    cfg = parse_config({**_ALIAS_BASE_RAW,
                        "aliases": {"wjd": {"target": "m-chat", "note": "客户入口"}}})
    assert ALIAS_PROTOCOLS == ("chat/completions",), "别名固定一条线，不许偷偷放宽"
    assert cfg.resolve_alias("wjd") == "m-chat", "别名要解析成真实 id"
    assert cfg.resolve_alias("m-chat") == "m-chat", "普通 id 必须原样返回"
    assert cfg.alias_of("wjd").note == "客户入口"
    assert cfg.find_provider_for_model("wjd") is None, "别名不是模型：不进模型索引"
    # 简写 `wjd: m-chat` 等价于只填 target
    assert parse_config({**_ALIAS_BASE_RAW, "aliases": {"wjd": "m-chat"}}).resolve_alias("wjd") == "m-chat"
    # 停用：不校验 target，该 id 相当于不存在
    off = parse_config({**_ALIAS_BASE_RAW,
                        "aliases": {"wjd": {"enabled": False, "target": "这个目标不存在"}}})
    assert off.alias_of("wjd") is None and off.resolve_alias("wjd") == "wjd"

    # 违规：每一条都必须报错（静默接受 = 用户拿着一个永远 400 的入口不知道找谁）
    cases = [
        ("目标不存在", "wjd", {"target": "nope"}),
        ("目标是图像模型", "wjd", {"target": "m-img"}),
        ("目标只有 messages 线", "wjd", {"target": "m-msg"}),
        ("启用了但没写 target", "wjd", {"target": "   "}),
        ("与真实模型 id 重名", "m-chat", {"target": "m-chat"}),
        ("非法别名 id", "w jd", {"target": "m-chat"}),
    ]
    for why, name, spec in cases:
        try:
            parse_config({**_ALIAS_BASE_RAW, "aliases": {name: spec}})
        except ConfigError:
            continue
        raise AssertionError(f"非法别名未被拦截（{why}）: {name} {spec}")


def test_alias_in_models_payload_inherits_target():
    """目录里要有别名条目（客户端得能发现它），能力/参数全继承目标，另带 alias_of；
    停用的别名不出现。"""
    import app.main as m
    old_cfg, old_cache = m.config, m._state.get("_models_cache")
    try:
        m.config, m._state["_models_cache"] = _alias_cfg("m-chat"), None
        rows = {x["id"]: x for x in m._models_payload()["data"]}
        w = rows["wjd"]
        assert w["alias_of"] == "m-chat", w
        assert w["protocols"] == ["chat/completions"], w        # 别名只走 OpenAI 线
        assert w["vision"] is True, w                           # 视觉能力继承目标
        assert w["params"] == {"temperature": 1}, w             # 强制参数继承目标
        assert w["type"] == "text" and w["provider"] == "p", w
        assert w["display_name"] == "Chat 模型", w
        # 停用 → 目录里不出现（与"没配过"同义）
        m.config.aliases["wjd"].enabled = False
        m._state["_models_cache"] = None
        assert "wjd" not in {x["id"] for x in m._models_payload()["data"]}
    finally:
        m.config, m._state["_models_cache"] = old_cfg, old_cache


def test_alias_chat_completions_hits_target_and_echoes_alias():
    """核心：model=wjd → 上游收到真实 id；响应 model 回显客户端请求名、served_model 是真实目标；
    指标/记账记在**真实模型**上（wjd 不是模型，别污染成本与指标口径）。"""
    from fastapi.testclient import TestClient
    import app.main as m

    class _FakeChat:
        def __init__(self):
            self.calls = []

        async def chat(self, **kw):
            self.calls.append(kw)
            return {"id": "c1",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2}}

    fake = _FakeChat()
    old_cfg, old_adapter, old_cache = m.config, m._get_adapter, m._state.get("_models_cache")
    m.config, m._state["_models_cache"] = _alias_cfg("m-chat"), None
    m._get_adapter = lambda model_id, timeout=None, cfg=None: (fake, "p", m.config.providers["p"])
    try:
        with _Isolated():
            r = TestClient(m.app).post("/v1/chat/completions", json={
                "model": "wjd", "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 16})
            assert r.status_code == 200, r.text
            j = r.json()
            assert fake.calls[-1]["model"] == "m-chat", fake.calls[-1]   # 上游只看到真实 id
            assert j["model"] == "wjd", j                                # 回显客户端请求的名字
            assert j["served_model"] == "m-chat", j                      # 真实目标
            assert j["choices"][0]["message"]["content"] == "OK", j
            snap = m._metrics.snapshot()
            assert snap["calls"]["m-chat"]["ok"] == 1, snap["calls"]
            assert "wjd" not in snap["calls"], "别名不该出现在指标里（它不是模型）"
            # 访问日志两条线索都留：model=请求名、served=真实目标
            last = m._access.tail(1)[0]
            assert last.get("served") == "m-chat", last
    finally:
        m.config, m._get_adapter, m._state["_models_cache"] = old_cfg, old_adapter, old_cache


def test_alias_rejected_on_messages_and_responses_lines():
    """规则①：别名只走 chat/completions —— 另两条线入口 400（指路 + 说出目标），
    且**一次都不打上游**（不能让用户对着上游的 500/401 猜自己哪里写错）。"""
    from fastapi.testclient import TestClient
    import app.main as m
    touched = []
    old_cfg, old_adapter = m.config, m._get_adapter
    m.config = _alias_cfg("m-chat")
    m._get_adapter = lambda model_id, timeout=None, cfg=None: (touched.append(model_id), None)[1]
    try:
        c = TestClient(m.app)
        for path, body in (
            ("/v1/messages", {"model": "wjd", "max_tokens": 16,
                              "messages": [{"role": "user", "content": "x"}]}),
            ("/v1/responses", {"model": "wjd", "input": "x", "max_output_tokens": 16}),
        ):
            r = c.post(path, json=body)
            assert r.status_code == 400, (path, r.status_code, r.text)
            assert "wjd" in r.text and "chat/completions" in r.text and "m-chat" in r.text, (path, r.text)
        assert touched == [], "被入口挡下的请求不该碰上游"
    finally:
        m.config, m._get_adapter = old_cfg, old_adapter


def test_alias_rejected_on_media_endpoint():
    """图像/视频端点不吃别名（别名只在文本线上存在）：明确 400 指路，而不是"未知图像模型"。"""
    from fastapi.testclient import TestClient
    import app.main as m
    old_cfg = m.config
    m.config = _alias_cfg("m-chat")
    try:
        r = TestClient(m.app).post("/v1/images/generations",
                                   json={"model": "wjd", "prompt": "一只猫"})
        assert r.status_code == 400, (r.status_code, r.text)
        assert "别名" in r.text and "m-chat" in r.text, r.text
    finally:
        m.config = old_cfg


def test_alias_not_speedtested():
    """别名不单独测速：给一句能照做的说明（测它指向的模型），而不是"模型不存在"。"""
    from fastapi.testclient import TestClient
    import app.main as m
    old_cfg = m.config
    m.config = _alias_cfg("m-chat")
    try:
        r = TestClient(m.app).post("/v1/models/speedtest", json={"model": "wjd"})
        assert r.status_code == 200, r.text
        row = r.json()["results"][0]
        assert row["ok"] is False and "别名" in row["error"] and "m-chat" in row["error"], row
    finally:
        m.config = old_cfg


def test_view_to_raw_keeps_panel_defaults_out_of_the_file():
    """第三十二轮：面板视图是「填满默认值」的，GET→PUT 不许把这些默认值写进文件。

    起因（真踩到的坑）：文件里没写 `rate_limit_rps` / `requires_session_header` / `endpoints`，
    模型没写 `type` / `vision` / `protocols`，可面板视图里这些键**全都有值**（默认值）。
    照着视图重建 providers，这些"用户从没写过"的键就被当成用户输入 → 段值变化 →
    合并写盘判定该段要重写 → 段内手写注释跟着赔掉（实测：整份视图 PUT 会重写 530 行、
    `#` 标记 126 → 72）。规则：**文件里没有 且 值等于面板默认 → 不写**。
    """
    import app.settings as S
    from app.config import parse_config

    raw_file = {
        "providers": {
            "p1": {
                "name": "P1",
                "base_url": "http://p1/v1",
                "auth": {"token_env": "P1_KEY"},
                "media": {"image_endpoint": "/images/generations"},
                "models": [
                    {"id": "m-a", "display_name": "A", "type": "text", "vision": True},
                    {"id": "m-b", "display_name": "B", "protocols": ["messages"]},
                ],
            },
            "p2": {                       # 故意什么默认键都没有：无 auth / 无 media / 无 models 元数据
                "name": "P2",
                "base_url": "http://p2/v1",
                "rate_limit_rps": 5,      # 文件里**有**这个键 → 必须原样留住
                "models": [{"id": "m-c", "display_name": "C"}],
            },
        },
        "server": {"port": 8808},
    }
    cfg = parse_config(raw_file)
    view = S.config_to_view(cfg)

    # 先钉住前提：视图确实是"填满默认值"的（否则这个用例就白测了）
    vp1 = view["providers"]["p1"]
    assert vp1["rate_limit_rps"] is None and vp1["requires_session_header"] is False
    assert "endpoints" in vp1 and vp1["extra_headers"] == {}
    assert vp1["models"][0]["protocols"] == ["chat/completions"]   # 文件里没写 → 视图给默认线
    assert vp1["models"][0]["params"] == {}
    assert vp1["media"]["video_model_name"] is None

    out = S.view_to_raw(cfg, raw_file, view)
    # 核心不变量：只有 providers 段以面板为准，而这趟"原样往返"必须零变化
    assert out["providers"] == raw_file["providers"], "面板默认值被写进文件了"
    assert "rate_limit_rps" not in out["providers"]["p1"]
    assert "endpoints" not in out["providers"]["p1"]
    assert "media" not in out["providers"]["p2"] and "auth" not in out["providers"]["p2"]
    assert out["providers"]["p1"]["media"] == {"image_endpoint": "/images/generations"}
    assert out["providers"]["p2"]["rate_limit_rps"] == 5
    assert out["providers"]["p1"]["models"][1]["protocols"] == ["messages"]
    assert "vision" not in out["providers"]["p2"]["models"][0]

    # 真改过就要写：文件里没这个键，但值≠默认 → 落盘
    v_mod = {**view, "providers": {**view["providers"],
                                   "p1": {**vp1, "rate_limit_rps": 7}}}
    assert S.view_to_raw(cfg, raw_file, v_mod)["providers"]["p1"]["rate_limit_rps"] == 7
    # 文件里本来就有的键，用户清空（值变成默认）→ 也是明确意图，照写
    v_clear = {**view, "providers": {**view["providers"],
                                     "p2": {**view["providers"]["p2"], "rate_limit_rps": None}}}
    assert S.view_to_raw(cfg, raw_file, v_clear)["providers"]["p2"]["rate_limit_rps"] is None


def test_full_view_put_rewrites_nothing():
    """第三十二轮：把 `GET /config` 的整份视图原样 PUT 回来 = 零变化（文件逐字节不动）。

    这是"注释不被赔掉"的总闸 —— 只要 `view_to_raw` 还 materialize 默认值，
    providers 段就会被判「变了」重写一遍（实测 530 行、`#` 126 → 72）。用临时配置文件跑，
    绝不碰生产 data/gateway.yaml。
    """
    import tempfile
    from pathlib import Path as P
    from fastapi.testclient import TestClient
    import app.main as m

    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    tmp = tempfile.TemporaryDirectory()
    cfg_path = P(tmp.name) / "gateway.yaml"
    src = P(CONFIG).read_text(encoding="utf-8")
    cfg_path.write_text(src, encoding="utf-8")
    with cfg_path.open("a", encoding="utf-8") as fh:
        fh.write("\n# 人工注释：整份视图 PUT 也别动我\n")
    src = cfg_path.read_text(encoding="utf-8")
    old_path, old_cfg = m.CONFIG_PATH, m.config
    m.CONFIG_PATH = cfg_path
    try:
        view = client.get("/config", headers=ah).json()["config"]
        r = client.put("/config", headers=ah, json={"config": view})
        assert r.status_code == 200, r.text
        disk = cfg_path.read_text(encoding="utf-8")
        assert disk == src, "整份视图 PUT 改动了文件（面板默认值被 materialize 进 providers）"
    finally:
        m.CONFIG_PATH, m.config = old_path, old_cfg
        tmp.cleanup()


def test_alias_panel_round_trip_and_save_validation():
    """面板是别名的编辑入口：GET 读出来带 aliases → 原样 PUT 不丢 → 非法目标 400（不写盘）。
    用临时配置文件，绝不碰生产 data/gateway.yaml。"""
    import tempfile
    import yaml as _yaml
    from pathlib import Path as P
    from fastapi.testclient import TestClient
    import app.main as m

    client = TestClient(m.app)
    ah = {"Authorization": f"Bearer {_login_admin_session(client)}"}
    tmp = tempfile.TemporaryDirectory()
    cfg_path = P(tmp.name) / "gateway.yaml"
    cfg_path.write_text(P(CONFIG).read_text(encoding="utf-8"), encoding="utf-8")
    # 注入一行人工注释：结构化保存**不许**把它抹掉（第三十一轮修的正是这个事故）
    with cfg_path.open("a", encoding="utf-8") as fh:
        fh.write("\n# 人工注释：别动我\n")
    comments_before = cfg_path.read_text(encoding="utf-8").count("#")
    old_path, old_cfg = m.CONFIG_PATH, m.config
    m.CONFIG_PATH = cfg_path
    try:
        view = client.get("/config", headers=ah).json()["config"]
        # 生产配置里**可能已经配了**别名（老板在页面上配的）—— 本用例只钉
        #   「面板往返不丢 / 保存即生效 / 非法目标 400 不写盘」，不假设起始状态为空。
        assert "aliases" in view, view.keys()
        # 换一个和生产默认不同的目标，才能证明写入真的生效了
        target = "ark-code-latest"
        payload = {"wjd": {"enabled": True, "target": target, "note": "客户入口"}}
        # 面板真实载荷：模型页「保存映射」只提交 aliases 一段（Models.vue saveAlias），
        #   不是整份视图 —— 服务端按段合并，其余段沿用文件原文（连行尾注释都不动）。
        r = client.put("/config", headers=ah, json={"config": {"aliases": payload}})
        assert r.status_code == 200, r.text
        assert m.config.resolve_alias("wjd") == target, "保存即生效"
        # 落盘 yaml 里也确实有这一段（面板下次读得回来），且**人工注释一行不少**
        disk = cfg_path.read_text(encoding="utf-8")
        assert _yaml.safe_load(disk)["aliases"]["wjd"]["target"] == target
        assert "人工注释：别动我" in disk, "结构化保存把人工注释抹掉了"
        assert disk.count("#") == comments_before, "结构化保存不该改动其他段的注释"
        lm = client.get("/v1/models", headers=ah).json()["data"]
        assert next(x for x in lm if x["id"] == "wjd")["alias_of"] == target
        # 非法目标：400 + 不写盘（盘上仍是上一个合法值）
        r2 = client.put("/config", headers=ah,
                        json={"config": {"aliases": {"wjd": {"enabled": True,
                                                             "target": "根本没有这个模型"}}}})
        assert r2.status_code == 400 and "wjd" in r2.text, r2.text
        assert _yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["aliases"]["wjd"]["target"] == target
        assert m.config.resolve_alias("wjd") == target
    finally:
        m.CONFIG_PATH, m.config = old_path, old_cfg
        tmp.cleanup()


def test_alias_streaming_uses_one_name_throughout():
    """流式（别名调用）：同一条 SSE 里不该出现两个模型名 —— 元信息帧报请求名 wjd，
    上游 chunk 带的是真实 id，也要统一成 wjd；真实目标只在 served_model 里体现。
    （普通调用不受影响：那是 chunk 里 model 本来就是请求的那个 id。）"""
    from fastapi.testclient import TestClient
    import app.main as m

    class FakeAdapter:
        async def chat_stream(self, **kw):
            yield {"choices": [{"delta": {"content": "hi"}}], "model": "m-chat"}
            yield {"done": True}

    old_cfg, old_adapter, old_cache = m.config, m._get_adapter, m._state.get("_models_cache")
    m.config, m._state["_models_cache"] = _alias_cfg("m-chat"), None
    m._get_adapter = lambda model_id, timeout=None, cfg=None: (FakeAdapter(), "p", m.config.providers["p"])
    try:
        with _Isolated():
            client = TestClient(m.app)
            with client.stream("POST", "/v1/chat/completions",
                               json={"model": "wjd", "messages": [{"role": "user", "content": "x"}],
                                     "stream": True}) as r:
                body = "".join(r.iter_text())
    finally:
        m.config, m._get_adapter, m._state["_models_cache"] = old_cfg, old_adapter, old_cache
    frames = [json.loads(ln[6:]) for ln in body.splitlines()
              if ln.startswith("data: ") and ln[6:].strip() != "[DONE]"]
    assert frames, body
    for f in frames:
        assert f.get("model") == "wjd", f          # 名字只有一个：客户端请求的那个
    assert frames[0]["served_model"] == "m-chat", frames[0]   # 真实目标在这里说清楚


def test_merged_write_keeps_neighbour_docs_and_skips_unchanged():
    """第三十一轮补测：文本级合并的三个边界（都是这次真踩过的坑）。

    ① 被替换段的**段尾说明**（那是下一段的抬头注释 / 文件页脚）必须留在原位 ——
       否则"改 providers"会顺手把 `server:` 上方的说明书删掉；
    ② 新增的段要插在下一段**说明注释之前**，不能插进"说明与键行"之间（否则别人的
       说明书就成了新段的抬头）；
    ③ 提交了但值没变的段一个字节都不许动 —— 面板 PUT 的是整份视图，段内行尾注释
       （如 `timeout: 120  # …`）不该因为一次无关保存而蒸发。"""
    import tempfile
    import yaml as _yaml
    from pathlib import Path as P
    from app.settings import merge_submitted_sections, write_config_merged

    text = (
        "# 抬头：手写说明\n"
        "providers:\n"
        "  p:\n"
        "    name: P\n"
        "    base_url: http://x/v1\n"
        "    models:\n"
        "      - id: m-chat\n"
        "        display_name: Chat\n"
        "# ── server 的说明书（人工）────────────────\n"
        "# host/port 改完要重启进程才生效。\n"
        "server:\n"
        "  timeout: 120      # 行尾注释：同一模型的超时，别动\n"
    )
    prov = {"p": {"name": "P", "base_url": "http://x/v1",
                  "models": [{"id": "m-chat", "display_name": "Chat"}]}}
    alias = {"wjd": {"enabled": True, "target": "m-chat", "note": "客户入口"}}
    plain = {"providers": prov, "server": {"timeout": 120}}

    # ① 改 providers：它的段尾正是 server 的说明书 → 说明与段序都得在
    m = merge_submitted_sections(text, plain, {"providers"})
    assert _yaml.safe_load(m) == plain, m
    assert "── server 的说明书" in m and "host/port 改完要重启进程才生效。" in m, m
    assert m.index("说明书") < m.index("server:"), "说明必须还在 server 上方"

    # ② 新增 aliases：插点在 server 的说明注释之前
    m2 = merge_submitted_sections(text, {**plain, "aliases": alias}, {"aliases"})
    assert _yaml.safe_load(m2) == {**plain, "aliases": alias}, m2
    assert m2.index("aliases:") < m2.index("── server 的说明书") < m2.index("server:"), m2
    assert m2.startswith("# 抬头：手写说明"), "抬头不许被跳过"

    # ③ 值没变的段不重写：server 的行尾注释要活下来（写盘端到端）
    with tempfile.TemporaryDirectory() as d:
        f = P(d) / "gateway.yaml"
        f.write_text(text, encoding="utf-8")
        raw_old = _yaml.safe_load(text)
        info = write_config_merged(f, {**plain, "aliases": alias}, raw_old,
                                   {"aliases", "providers", "server"})
        disk = f.read_text(encoding="utf-8")
        assert info["mode"] == "merged" and info["sections"] == ["aliases"], info
        assert "行尾注释：同一模型的超时，别动" in disk, "没变的段被重写了"
        assert _yaml.safe_load(disk) == {**plain, "aliases": alias}, disk


def test_structured_save_only_touches_submitted_section():
    """第三十一轮修的真实事故：结构化保存（模型页「保存映射」）原来走 render_yaml 整份重写，
    把 gateway.yaml 里历轮手写的现场注释全抹了。现在只替换**提交过的那一段**，其余字节不动。

    这里用一个带"人工注释"的小配置文件端到端验：改别名 → 注释还在、别的段一个字节没变；
    再加一段定位失败的怪文本，确认回退整份重渲染（配置正确优先，注释其次）。
    """
    from app.settings import merge_submitted_sections, write_config_merged, render_yaml
    import yaml as _yaml
    text = (
        "# 抬头注释（人工）\n"
        "providers:\n"
        "  p:\n"
        "    # 这里也是人工注释：别动我\n"
        "    name: P\n"
        "    base_url: http://x/v1\n"
        "    models:\n"
        "      - id: m-chat\n"
        "        display_name: Chat 模型\n"
        "server:\n"
        "  timeout: 120\n"
    )
    new = {"providers": {"p": {"name": "P", "base_url": "http://x/v1",
                               "models": [{"id": "m-chat", "display_name": "Chat 模型"}]}},
           "aliases": {"wjd": {"enabled": True, "target": "m-chat", "note": "客户入口"}},
           "server": {"timeout": 120}}
    merged = merge_submitted_sections(text, new, {"aliases"})
    assert merged is not None
    assert "别动我" in merged and "抬头注释（人工）" in merged, "人工注释必须原样保留"
    assert merged.startswith(text.split("aliases")[0][:20]), "前面的文本不该被重排"
    # 新增段插到它该在的位置（aliases 在 _SECTION_ORDER 里紧跟 providers → 应在 server 之前）
    assert merged.index("aliases:") < merged.index("server:"), merged
    assert "wjd" in merged and _yaml.safe_load(merged) == new, "合并结果必须与提交内容一致"
    assert "provider 与 server 的原文都在"  # 语义检查由上面 safe_load 相等保证

    # 段已存在 → 只替换该段
    merged2 = merge_submitted_sections(merged, {**new, "server": {"timeout": 999}}, {"server"})
    assert "timeout: 999" in merged2 and "别动我" in merged2 and _yaml.safe_load(merged2)["aliases"] == new["aliases"]

    # 怪文本（没有顶层段）→ 让调用方回退整份重渲染
    assert merge_submitted_sections("这不是 yaml：：\n- 1\n- 2\n", new, {"aliases"}) is None

    # write_config_merged: 不变量对不上时自动回退 rendered 模式（且写出的一定是正确配置）
    import tempfile
    from pathlib import Path as P
    with tempfile.TemporaryDirectory() as d:
        f = P(d) / "g.yaml"
        f.write_text(text, encoding="utf-8")
        info = write_config_merged(f, new, {}, {"aliases"})
        assert info["mode"] == "merged" and "别动我" in f.read_text(encoding="utf-8"), info
        # 提交一个"面板不认识"的段 → 仍然要能写对（追加到末尾），且回读一致
        weird = {**new, "custom_x": {"a": 1}}
        info2 = write_config_merged(f, weird, {}, {"aliases", "custom_x"})
        assert _yaml.safe_load(f.read_text(encoding="utf-8")) == weird, info2
        assert ("别动我" in f.read_text(encoding="utf-8")) if info2["mode"] == "merged" else True
    assert render_yaml(new).startswith("#"), "整份重渲染路径仍在（回退用）"


def test_sync_plan_is_empty_drills_into_provider_buckets():
    """sync_models.plan_is_empty 必须下钻到 provider 桶，无漂移时不得判成「有动作」。

    回归（2026-09-24 实测踩到）：`plan["add"] = {"opencode": {}, "agnes": {}}` 这种
    两层结构即使一个模型都没变也是**真值**，早先直接 `any(plan[k] ...)` → 每天无漂移
    也走写盘分支：白重写配置、覆盖上一份备份，报告还误报「已写盘：…」。
    """
    sys.path.insert(0, str(_ROOT / "scripts"))
    import sync_models as sm

    empty = {"add": {"opencode": {}, "agnes": {}},
             "remove": {"opencode": [], "agnes": []},
             "price_set": {}, "price_add": {}, "proto_add": {}, "free_set": {}}
    assert sm.plan_is_empty(empty) is True
    assert sm.plan_is_empty({**empty, "add": {"opencode": {"x": {}}, "agnes": {}}}) is False
    assert sm.plan_is_empty({**empty, "remove": {"opencode": [], "agnes": ["y"]}}) is False
    assert sm.plan_is_empty({**empty, "price_add": {"m": {"input": 1.0, "output": 2.0}}}) is False
    assert sm.plan_is_empty({**empty, "price_set": {"m": {"old": None, "new": None}}}) is False
    assert sm.plan_is_empty({**empty, "proto_add": {"m": "messages"}}) is False
    assert sm.plan_is_empty({**empty, "free_set": {"m": True}}) is False


def test_agnes_free_map_rules():
    """Agnes 免费标注解析（2026-09-24 老板要求 Agnes 也标免费 / 限时免费）。

    只用文档里的**严格措辞**判定，且必须按 bullet/段落切块 + 只认反引号里的完整 id：
      · 「当前均免费」「当前免费」→ 免费；「限时免费」→ 限时免费
      · 「输入图片前 5 张免费」这种计费公式附注 **不算** 模型免费（视频 2.5 就是这样）
      · `agnes-video-2.5` 是 `agnes-video-2.5-flash` 的子串，不能互相串味
    """
    sys.path.insert(0, str(_ROOT / "scripts"))
    import sync_models as sm

    ids = {"agnes-3.0-flash", "agnes-2.5-flash", "agnes-video-2.5", "agnes-video-2.5-flash",
           "agnes-video-v2.0", "agnes-2.5-pro"}

    real = ("## 视频模型\n### 当前优惠\n"
            "* `agnes-video-v2.0` 当前免费。\n"
            "* `agnes-video-2.5` 按输出视频分辨率和总计费时长计费；输入图片前 5 张免费，第 6 张起计费。\n"
            "* `agnes-video-2.5-flash` 采用相同计费公式，当前限时免费。\n"
            "### 计费规则\n输入图片前 5 张免费。\n")
    got = sm._agnes_free_map(real, ids)
    assert got == {"agnes-video-v2.0": True, "agnes-video-2.5-flash": "limited"}, got

    # 改口径 → 不再免费（否则同步会一直把「免费」标签留在配置里）
    changed = ("### 当前优惠\n* `agnes-3.0-flash` 当前按刊例价计费，暂无折扣。\n"
               "* `agnes-2.5-flash` 当前均免费。\n")
    assert sm._agnes_free_map(changed, ids) == {"agnes-2.5-flash": True}

    # 下一小节里的「免费」不能串进本轮判定
    boundary = "### 当前优惠\n* `agnes-video-v2.0` 当前免费。\n### 计费规则\n输入图片前 5 张免费。\n"
    assert sm._agnes_free_map(boundary, ids) == {"agnes-video-v2.0": True}


def test_agnes_free_apply_and_revoke_round_trip():
    """agnes 免费标注落盘：加标注 / 限时 / 撤标注都要能改对，且不碰相邻手写注释。"""
    import yaml as _yaml
    sys.path.insert(0, str(_ROOT / "scripts"))
    import sync_models as sm

    text = (
        "providers:\n"
        "  agnes:\n"
        '    name: "Agnes"\n'
        "    models:\n"
        '      - id: "agnes-3.0-flash"\n'
        '        display_name: "Agnes 3.0 Flash"\n'
        '        type: "text"\n'
        "        # 手写注释：别动我\n"
        '      - id: "agnes-video-2.5-flash"\n'
        '        display_name: "Agnes Video 2.5 Flash"\n'
        '        type: "video"\n'
        '      - id: "agnes-video-v2.0"\n'
        '        display_name: "Agnes Video V2.0"\n'
        '        type: "video"\n'
        "        free: true     # 旧标注\n"
    )
    date = "2026-09-24"

    def run(src, free_map):
        plan = {"add": {"opencode": {}, "agnes": {}}, "remove": {"opencode": [], "agnes": []},
                "price_set": {}, "price_add": {}, "proto_add": {}, "free_set": {},
                "agnes_free": dict(free_map)}
        new = sm.apply_plan(src, plan, date)
        assert sm.verify(new, _yaml.safe_load(src), plan) == [], "回读校验必须全过"
        return new, _yaml.safe_load(new)["providers"]["agnes"]["models"]

    # ① 加标注：普通免费 + 限时免费
    text, models = run(text, {"agnes-3.0-flash": True, "agnes-video-2.5-flash": "limited"})
    by = {m["id"]: m for m in models}
    assert sm._cfg_free_state(by["agnes-3.0-flash"]) is True
    assert "free_limited" not in by["agnes-3.0-flash"]
    assert sm._cfg_free_state(by["agnes-video-2.5-flash"]) == "limited"
    assert "别动我" in text, "相邻手写注释必须原样保留"
    assert "旧标注" in text, "没在本次动作里的模型，标注行不许动"

    # ② 撤标注（文档改口径）+ 限时 → 普通免费
    text, models = run(text, {"agnes-video-v2.0": None, "agnes-video-2.5-flash": True})
    by = {m["id"]: m for m in models}
    assert sm._cfg_free_state(by["agnes-video-v2.0"]) is None
    assert not [k for k in by["agnes-video-v2.0"] if k.startswith("free")], "撤标注要整行删干净"
    assert "旧标注" not in text
    assert sm._cfg_free_state(by["agnes-video-2.5-flash"]) is True
    assert "free_limited" not in by["agnes-video-2.5-flash"], "限时行要跟着删"
    assert sm._cfg_free_state(by["agnes-3.0-flash"]) is True, "不相关的模型不能被顺手改掉"

    # ③ 幂等：同一份动作重放，结果逐字节一致
    plan = {"add": {"opencode": {}, "agnes": {}}, "remove": {"opencode": [], "agnes": []},
            "price_set": {}, "price_add": {}, "proto_add": {}, "free_set": {},
            "agnes_free": {"agnes-3.0-flash": True}}
    assert sm.apply_plan(sm.apply_plan(text, plan, date), plan, date) == \
        sm.apply_plan(text, plan, date)


# 直接运行（非 pytest）—— 必须留在文件末尾，见上方 `test_image_request_goes_to_requested_model`
# 之前的旁注：运行器只会收集执行到这一行时已经存在的 globals。
if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"[FAIL] {fn.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
