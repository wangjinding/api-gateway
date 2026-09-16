# API Gateway

统一多供应商 LLM 接入网关，纯后端 FastAPI，配置驱动。支持 **Agnes AI**、**b.ai**、**火山方舟 Coding Plan**、**智谱 GLM** 四个供应商，全部走 OpenAI 兼容协议。v2.4 已集成熔断、限流、图像/视频、指标、成本估算、热加载、测试等完整能力。

## 特性

- ✅ 多供应商统一路由（`model` 字段自动分发）
- ✅ 路由别名 `wjd`：运行时挑「当前可用 + 最近最快」的文本模型优先调用，其余按速度顺序 fallback + 熔断
- ✅ 同模型重试 + 路由级超时 + **供应商熔断器**（chat 与媒体各自独立桶）
- ✅ 图像 `/v1/images/generations` + 视频 `/v1/videos`（异步 + 等待模式 + Webhook）
- ✅ 页面账号体系鉴权（账号密码登录 + 每账号自建 API Key，三级限流**全局/路由/供应商**）
- ✅ 用量指标 `/metrics` + **Prometheus** `/metrics/prometheus` + 访问日志 `/logs`
- ✅ **Token 成本估算**（响应带 `estimated_cost_cny`）
- ✅ **Batch 并发调用** `/v1/chat/completions/batch`
- ✅ OpenAI 兼容：`/v1/models` 标准端点 + OpenAI 格式错误 + 标准可选参数全透传
- ✅ 配置热加载、`X-Request-ID` 贯穿、CORS（覆盖 413/400 短路响应）、gzip、请求体限制、优雅关闭
- ✅ 安全：恒定时间 key 比较、webhook 回调 SSRF 防护、开放代理启动告警、请求 ID 消毒
- ✅ 本地即开即用：`python run.py` 一键启动 + 配置热重载 + 单元测试

## 架构

```
api-gateway/
├── data/gateway.yaml       # 默认运行时配置（run.py 读取，hot_reload 开）
├── app/
│   ├── config.py           # YAML 加载 → dataclass（供应商/路由/鉴权/熔断/可观测/定价）
│   ├── models.py           # Pydantic 请求/响应模型（chat/image/video/batch）
│   ├── routing.py          # model_id → 供应商路由 + 认证解析
│   ├── metrics.py          # 用量指标 + 访问日志 + 限流（无外部依赖）
│   ├── circuit.py          # 供应商熔断器（CLOSED/OPEN/HALF_OPEN）
│   ├── main.py             # FastAPI 主入口（全部端点 + 中间件）
│   └── adapters/
│       ├── __init__.py     # 适配器工厂
│       ├── base.py         # 抽象基类（chat / chat_stream / image / video）
│       └── openai_compat.py # OpenAI 兼容适配器（四家通用 + 媒体端点）
├── tests/test_gateway.py   # 单元测试（mock 供应商，不依赖真实网络）
├── Makefile                # 快捷命令（install/run/test/lint/clean）
├── run.py                  # 启动入口
└── requirements.txt        # 锁定版本
```

## 供应商与模型

| 供应商 | Base URL | 认证 | 模型 |
|---|---|---|---|
| Agnes AI | `https://api.agnes-ai.cn/v1` | Bearer | `agnes-3.0-flash`(text)、`agnes-image-2.5-flash`(image)、`agnes-video-2.5-flash`(video) |
| b.ai | `https://api.b.ai/v1` | Bearer | `qwen3.8-flash`、`hy3`（text） |
| 火山方舟 Coding | `https://ark.cn-beijing.volces.com/api/coding/v3` | Bearer | `ark-code-latest`（text） |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | Bearer | 文本：`GLM-4.7-Flash`、`GLM-4-Flash-250414`、`GLM-Z1-Flash`、`GLM-4.6V-Flash`、`GLM-4V-Flash`、`GLM-4.1V-Thinking-Flash`（其中 3 个 V 型号支持图像输入）；图像：`cogview-3-flash`；视频：`cogvideox-flash`（文生视频，异步 `/videos/generations` + `/async-result/{id}` 轮询） |

> Agnes 的图像/视频走自有端点，通过供应商配置里的 `media` 段指定。

## 配置

所有数据从 yaml 读取，改供应商/模型/端点/熔断/限流只改配置文件，不改代码。

### 供应商（含 media 媒体端点）

```yaml
providers:
  agnes:
    name: "Agnes AI"
    base_url: "https://api.agnes-ai.cn/v1"
    auth: { token_env: "AGNES_API_KEY" }   # token 走 env/.env，yaml 不再存明文
    extra_headers: {}
    media:                          # 图像/视频端点（可选）
      image_endpoint: "/images/generations"
      video_create_endpoint: "/videos"
      video_poll_base: "https://api.agnes-ai.cn"
      video_poll_path: "/agnesapi"
      video_model_name: "agnes-video-2.5-flash"
    models:
      - { id: "agnes-3.0-flash", display_name: "Agnes 3.0 Flash", type: "text" }
```

认证优先级：**环境变量 > yaml 里的 `token` 字段**。

### 路由别名 + 按「可用 + 速度」动态排序 + 熔断
（第十一轮 #4：`wjd` 不再按配置里的数组优先级，改为运行时挑可用且最快的文本模型先调用；详见下方路由说明）

```yaml
routes:
  wjd:
    timeout: 60         # 单模型 60s 没出结果就切下一个（避免 6×120s 挂死）
    retry: 1           # 每个模型试 1 次，失败立即切下一个
    models:
      - "ark-code-latest"     # 1) 火山方舟（最高优先级）
      - "agnes-3.0-flash"     # 2) Agnes 兜底
      - "qwen3.8-flash"       # 3) b.ai qwen 兜底
      - "hy3"                 # 4) b.ai hy3 兜底

circuit:                 # 供应商熔断
  enabled: true
  failure_threshold: 3   # 窗口内失败 ≥3 次即熔断
  cooldown: 30           # 熔断持续 30 秒
  window: 10            # 滑动窗口 = 最近 N 次调用（按次数，非按时间）
```

> v2.3：`window` 现在真正按**最近 N 次调用**生效（之前是写死 60 秒、配置无效）。判定 = 最近 `window` 次里失败数 ≥ `failure_threshold`。
>
> **修复（v2.5）**：
> - 媒体端点限流真正接线：`/v1/images/generations`、`/v1/videos` 现在确实按 `providers.<key>.rate_limit_rps` 供应商桶限流（此前该逻辑只定义未调用，属死代码）。
> - 全局 `server.rate_limit_rps` 不再依赖鉴权开关：auth 关时统一按 anonymous 桶限流，全局 QPS 始终生效。
> - 流式 fallback 增强：模型在**发出任何 content 之前**失败（如首字前断连），会回退到链上下一个模型；一旦已输出 content 则无法回退（避免破坏 SSE 流）。
> - 视频 webhook 回调的 `status` 按真实状态上报，`failed` 不再误标为 `completed`。
> - 适配器增加在途占用计数：热重载/关闭适配器时会等待在途请求（含流式）排空后再真正关连接，避免掐断正在进行的请求。
> - 新增 `smoke_test.py`：对运行中的网关做健康/模型/chat/batch/错误格式冒烟检查（网关未启动或缺失真实 key 时优雅跳过）。
>
> **修复（v2.6）**：
> - 指标更真实：`fallback_total` 只统计**成功降级**，全模型失败（502）不再污染该计数；`/metrics` 的 `routes` 现在记录真实的 ok/fail（此前恒为 0），并新增按路由维度的成功率。
> - `route.retry` 整条链统一：路由存在时链首也使用 `route.retry`（此前链首误用全局 `server.retry`）。
> - 媒体限流与 chat **共用同一供应商桶** `prov:<pkey>`（此前媒体用独立桶，实际放行量翻倍，与文档不符）。
> - 热重载保留运行时状态：不再清空熔断状态 / 调用指标 / 访问日志 / 限流令牌桶（此前整体重建，刚熔断的坏供应商会被立刻恢复重试、指标与日志清零）；`hot_reload_interval` 改为每轮读取，改配置即时生效；重载后重新打印路由告警。
> - CORS 改为**动态**：每次请求实时读取 `cors_origins`，热重载改来源即时生效（含 OPTIONS 预检），不再需要重启。
> - 健壮性：`/metrics` 快照全程持锁（并发下不再抛 `dictionary changed size during iteration`）；`provider_health` 的 `reachable` 仅 2xx 时为真（此前 401/500 也误报可达）；视频等待模式的 `wait_timeout` 钳制上限 600s，轮询期间尊重熔断；适配器缓存 key 纳入 token 指纹（环境变量换 token 可刷新）。
> - 配置校验：`load_config` 对缺供应商 / 缺 `base_url` / 空模型链等结构错误抛 `ConfigError`（友好中文说明），启动与热重载均可拦下。
> - 错误语义：未知模型/路由改为直接 **400**（客户端错误），不再走到 fallback 尽头后以 502（上游错误）误导性返回（chat 与 batch 入口均生效）。
> - 启动脚本 `run.py` 单一配置来源（不再双重加载），并采用配置里的 `observability.log_level`。
> - 清理死代码（未使用的 `ChatResponse`/`Usage`/`Choice`、`RouteInfo.effective_timeout`、`AuthConfig.type`）；补齐测试（route 指标、快照并发、配置校验、媒体限流、413 体积限制、动态 CORS、鉴权开关、全局限流、流式 SSE、batch 并发、视频 webhook 真实状态、未知模型 400），测试数 24 → 37。
>
> **修复（v2.7）**：
> - `routing.ModelNotFoundError` 消息不再重复前缀，`model_id` 只存裸模型名（此前把整句当 `model_id`，报错形如「未知模型: 未知模型 '…'」）。
> - **请求体校验失败改为 OpenAI 兼容格式**：补 `RequestValidationError` 处理器，缺字段/类型错返回 `{"error":{"message","type":"invalid_request_error","code":"422"}}`（此前 FastAPI 默认 `{"detail":[...]}`，OpenAI SDK 无法解析）。
> - 畸形 `Content-Length` 请求头改为 **400**（此前进 `int()` 抛异常 → 500）；体积检查只对 POST/PUT/PATCH/DELETE 生效，GET/HEAD/OPTIONS 直接放行。
> - `in_flight` 中间件异常时归还计数（修复未捕获异常 → 500 后计数永久 +1 的泄漏）。
> - 性能：模型查找改为 O(1) 索引（`GatewayConfig._model_index`），不再每请求线性扫描。
> - 限流桶首次请求也消耗 token（消除等效 cap+1 的突发）。
> - 热重载禁用熔断时复位已 OPEN 的 breaker，立刻恢复放行。
> - 图像默认档位提为配置项 `media.image_default_size`（默认 1K 兜底，去掉硬编码）。
> - 工程化：`Makefile` 的 `2>nul` 改为 `2>/dev/null`（原为 Windows 语法，Linux 下会误建 `nul` 文件）；两份 yaml 补关系说明并删除已废弃的 `auth.type`。测试数 37 → 44。

> **修复（v2.8）**：
> - **【P0】无法停服**：lifespan 里的 `loop.add_signal_handler(SIGTERM/SIGINT)` 覆盖了 uvicorn 自己的信号处理器，导致 `supervisorctl stop` / `Ctrl+C` 停不下来（只能等超时被 `kill -9`，在途请求被硬切、连接池不优雅关闭）。已移除自注册，实测 SIGTERM → 0.5s 内优雅退出。
> - **流式 fallback 指标记反**（v2.6 修复的漏网）：流式路径**全失败也计 `fallback_total`、成功降级却完全不计**，与非流式语义相反。现已对齐：只有成功降级才计入。
> - **`/batch` 丢弃 `extra`**：`BatchRequest.extra` 从未传进单项请求 → 批量调用无法透传供应商参数（如 Agnes `chat_template_kwargs`），静默失效。已修复并写入 `max_concurrency` 实测值。
> - **`/batch` 防滥用**：`max_concurrency` 客户端可控（可传 1e5）、条目数只受 2MB 限制 → 自伤型 DoS。新增 `server.batch_max_concurrency`（钳制）与 `server.batch_max_items`（超出 400）。
> - **视频轮询**：① 无 `model` 时不再"猜第一个支持视频的供应商"（任务 ID 是供应商私有的，猜错必然 404/502）；改为单供应商才推断、多供应商必须显式指定，并统一走 `_resolve_media_model`（支持路由别名）；② `wait=true` 的轮询不再因一次瞬时抖动整段 502，改为记录失败后继续等到 deadline；③ 补上 `record_call`/`record_latency` 指标；④ 返回状态兼容 `state` 字段（此前只认 `status`，上游用 `state` 时会一直显示 `pending`）。
> - **媒体熔断桶独立**：图像/视频终于会喂熔断器（此前媒体调用完全不记录，而视频轮询却拿 chat 的桶做 `allow()` 检查 —— 一边永不计、一边永远查，检查形同虚设）。现用独立的 `<provider>#media` 桶，既拦住坏媒体供应商，又避免"客户端坏 prompt 导致上游 4xx"把 chat 熔断误打开。
> - **安全 · SSRF**：视频 `callback_url` 此前无任何校验，可让网关 POST 到任意地址（含 `169.254.169.254`、`127.0.0.1` 等）。现仅允许 http/https，默认拦截回环/私网/链路本地/保留/多播地址（域名会做异步 DNS 解析后校验，防"指向内网的域名"绕过），内网回调可用 `server.webhook_allow_private: true` 放行。
> - **安全 · 开放代理提示**：默认 `host=127.0.0.1`（只监听回环，安全）；一旦把 `host` 改成非回环地址且 `auth.enabled=false`，启动会打醒目 WARNING —— 此时同网段任何人都能白用你配置的上游 Key，`/metrics`、`/logs` 也会暴露。
> - **安全 · 其它**：API key 比较改用 `hmac.compare_digest`（恒定时间，防时序侧信道）；`auth.enabled=true` 但 `users` 为空时 `validate_config` 直接报错（此前会静默全站 401 自锁）；`X-Request-ID` 做字符/长度消毒（防响应头与日志注入）；`/health` 与 `/v1/models` 不再回显上游 `base_url`（内部拓扑）。
> - **OpenAI 兼容性**：`ChatRequest` 之前会**静默丢弃**未声明的标准参数（`n` / `presence_penalty` / `frequency_penalty` / `seed` / `user` / `response_format` / `logprobs` / `top_logprobs` / `parallel_tool_calls` / `stream_options`）。现已显式声明并透传，未知字段也不再丢弃（`extra="allow"` 原样透传 + 记日志）。
> - **CORS 覆盖短路响应**：`limit_body` 中间件在 CORS 外层，导致体积超限的 413 **不带 CORS 头**（浏览器只能看到 CORS 报错、读不到真实原因）。现把 CORS 注册到最外层，413/400 均带 CORS 头（已实测）。
> - **SSE 抗坏行**：上游发非 JSON 行（`data: ping` keep-alive、半截行）此前会抛异常**中断整个流**；现在跳过坏行继续。
> - **其它健壮性**：`rate_limit_rps` 写成字符串（`"5"`）不再在 `rps * 2` 处崩（配置层强转 `float`）；请求体分块读取出错时不再静默放行而是 400（避免 handler 拿到不完整 body 报出无关的 422）；Prometheus 标签值转义（模型名含 `"`/`\` 不再产出非法指标文本）；P95 改用最近秩（此前 `int(n*0.95)` 在 20 倍数时取到最大值，退化成 P100）；跨供应商重复模型 id 出启动告警。测试数 44 → 58（含本轮 14 项回归；顺带修掉"缺 pytest 时用例假红"的假红灯）。
> - 文档：修正 `media:prov:<key>` 旧桶名残留（与 v2.6 说明自相矛盾）。

> **修复（v2.9 · 第六轮复查）**：本轮由三路独立审计（并发/生命周期、安全与配置、接口契约）交叉复核，P0×1、P1×8、P2×20+ 已全部处理：
> - **【P0】`extra` 可劫持路由与协议**：`extra={"model": ...}` 会覆盖网关已解析的模型 → 用你的上游 Key 调**任意（更贵/未登记）模型**并绕过计费口径；`extra={"stream": ...}` 还能让非流式客户端收到 SSE（502）。现在 `model/messages/stream/tools/tool_choice` 等核心键一律剔除（`models.PROTECTED_PAYLOAD_KEYS` + 适配器侧兜底），视频路径用更窄的 `PROTECTED_VIDEO_KEYS`（保留 `first_frame` 等模式字段）。
> - **【P1】熔断器会被永久卡死**：HALF_OPEN 的试探令牌只发不收 —— 只要拿到令牌后没走到 `record()`（限流跳过、凭证缺失、客户端断连的 `CancelledError`），该供应商就**永远**被拒（只能重启）。现修三处：限流跳过前 `release_probe()` 主动归还、探测超时兜底（`probe_timeout`）、`allow()` 在 HALF_OPEN 下超时自愈回 OPEN。两位审计员用不同路径独立实测复现。
> - **【P1】`close()` 会关掉正在使用的连接池**：`close()` 先在锁内判定 `_active==0`、出锁后再关闭，两步之间的并发 `get_client()` 会把计数加到 1 并拿到那个 client，随后它被 `aclose()` → 在途请求 `RuntimeError: client has been closed`。现将"判定 + 关闭"合并进同一次持锁。
> - **【P1】`in_flight` 指标双缺陷互相掩盖**：① `except Exception` 抓不到 `CancelledError` → 客户端断连时计数永久 +1；② `isinstance(response, StreamingResponse)` 在 Starlette 1.6 下**恒为假**（`call_next` 返回的 `_StreamingResponse` 不是它的子类）→ 流式请求被"中间件 + 生成器"双重递减。现统一为 `try/finally` 只减一次，语义＝「进入 → 响应头生成完毕」（**v2.10 进一步修正为"响应体发送完毕"**，见下）。
> - **【P1】function calling 多轮对话直接坏掉**：`Message` 此前只有 `role/content`，assistant 的 `tool_calls`、tool 消息的 `tool_call_id` 被静默丢弃 → 能发起工具调用，但回传历史时报错。现补全字段并改用 `to_upstream()` 透传。
> - **【P1】媒体端点凭证缺失漏出 500**：images/videos 的 `_get_adapter` 在 `try` 之外，供应商没配 token 时抛 `AuthConfigError` → 500 纯文本（与 chat 的 502/503 不一致）。现已纳入 `try` 并区分映射（凭证缺失 503、模型不可用 404，且不计入熔断）。
> - **【P1】异步回调后台任务无人管理**：新增 `_spawn_bg` 后关停时任务被硬切、`callback_url` 静默丢通知；lifespan 现在会等待（最多 5s）再取消，并且热重载的 adapter 字典改为**先原子摘除再关闭**（此前"先关后置空"会让并发请求新建的连接池成为孤儿，永远不被 close）。
> - **契约修复**：全失败 502 错误体 `error.error` → `error.message`（OpenAI 规范）；流式首个 meta 帧与末尾 usage 帧补齐 `id/created/object=chat.completion.chunk/choices`（此前不是合法 chunk，严格校验的 SDK 直接报错）；`/v1/models` 的 `data` 统一 `object="model"` 并补 `created/owned_by`（此前混入 `object="route"`）；`/batch` 单项错误不再输出 Python dict repr；图像补 `created`；上游返回非 JSON/空 body 不再是 `Expecting value: line 1 column 1`。
> - **入参校验**：空 `messages` 与 `"stream": "yes"`（类型拼错）改为入参处 422，而不是打到上游变 502；图像请求未声明的标准参数（`quality/style/...`）不再静默丢弃。
> - **安全/健壮性**：`callback_url` 端口非法（`:99999`）→ 400（此前未捕获 `ValueError` → 500）；`video_id` 等客户端可控字段进日志前剥离控制字符（防 `%0a` 伪造日志行）；未捕获异常的 500 现在**带 CORS 头**（此前由最外层 `ServerErrorMiddleware` 生成，绕过 CORS，浏览器只看到 CORS 报错）；配置解析全面加固（`null`/负数/非数字一律给出带字段名的 `ConfigError`，不再启动期 `TypeError` 崩进程或运行期 `httpx.Timeout("abc")` 每次 500）；`health_auth=true` 但未开鉴权、以及"通配 CORS + 无鉴权"现在都会出启动告警；`/` 与 `/health/{provider}` 纳入 `health_auth` 保护（后者会用网关的 Key 主动探上游）。
> - **其它**：流式 `break` 后显式 `aclose()` 上游生成器（不再依赖 GC 来归还 lease）；`estimated_cost_cny` 不再在上游无 usage 时报假 `0.0`；Prometheus 输出补 `# HELP/# TYPE`。测试数 58 → 78。

> **修复（v2.10 · 第七轮回归复查）**：把 v2.9 的全部改动交给独立审计员做**回归专项**复核（逐行推演 + 80 用例 + 只读运行时实测），抓到 **1 个本轮新引入的高危倒退** ＋ 4 个中低风险项，已全部修复：
> - **【P1·新引入】标准参数被静默丢弃**：v2.9 给 `_build_payload` 加 `strip_protected(extra)` 防 `extra` 劫持，但用的是**宽集合** `PROTECTED_PAYLOAD_KEYS`，而它恰好包含 `_PASSTHROUGH` 里全部 10 个键（`n/seed/user/response_format/logprobs/top_logprobs/parallel_tool_calls/stream_options/presence_penalty/frequency_penalty`）—— 刚塞进 extra 又被删掉，`_PASSTHROUGH` 机制事实上被废掉。实测：带 `response_format`/`seed`/`n` 的请求到上游只剩 `model/messages/stream`。现拆成三套集合：适配器侧只剔**自己刚写入的核心键**（`models.CHAT_CORE_KEYS`），客户端 extra 禁写集仍是宽集合（`upstream_extra()` 已挡，放行无风险）。**触发面**：任何用 `response_format`（JSON 模式）、`seed`、`n`、`user`、penalty、`logprobs*` 的 chat/batch 请求。
> - **【P2】图像 `response_format`/`user` 被误删**：图像路径同样用了宽集合，与 `ImageRequest` docstring（明说这些走 `model_extra` 透传）自相矛盾。现改用图像专用窄集 `PROTECTED_IMAGE_KEYS`（只挡 `model/prompt/size/n/image/images`），`quality/style/response_format/user` 正常透传。
> - **【P2】流式期间 `in_flight` 提前归零**：`call_next` 在收到 `http.response.start` 就返回（body 由外层继续迭代），故 v2.9 的"响应头生成完毕即减"会让 SSE **传输期间**读数为 0（实测 `yield#1` 起就是 0）。现把递减挂到响应体迭代结束/中断（包一层 `body_iterator`），语义＝「进入 → 响应体发送完毕」。活体实测：877 帧的流式请求全程 `in_flight=2`（流本身 + 采样请求），结束后正确归位、`in_flight_peak` 无漂移。
> - **【P2】热重载后 `base_url` 改动可能不生效**：适配器缓存 key 不含 `base_url`，而本轮改成"先原子摘除字典再关闭"后，持有旧 cfg 的在途请求仍可能重建 adapter 并写回新字典 → 后续按新配置的请求命中旧地址。现把 `base_url` 指纹并入 `cache_key`。
> - **【P2】死代码**：`BaseAdapter._close_now()` 在 `close()` 改为持锁直调 `_close_now_locked()` 后已无调用点，删除。
> - **无可达风险（已加约束说明）**：`release_probe()` 不校验令牌归属，理论上可能误还并发请求的探测令牌；逐一核对全部调用点后**当前不可达**（同 tick 或 except 分支内无抛错源），已在 docstring 写明"仅令牌持有者、且放行与归还间不得有 await"的前置条件，并给出后续放宽方案（probe token 归属校验）。
> - 审计同时确认**无新问题**：`json_or_raise()` 空/非 JSON body 映射、`Message.to_upstream()` 多轮 function calling、`stream` 严格校验、`unhandled_error_guard` 位于最外层且 500 带 CORS、`release_probe` 常规路径与 `probe_timeout` 自愈、`videos_poll` 404 归还不计失败、`_get_adapter` 移入 try 后的 503/404 映射、`_spawn_bg` 强引用与 lifespan 等待、`_opt_int` 的 `default>=minimum` 逐处正确。测试数 78 → 84（补 4 项回归：标准参数最终 payload、图像额外参数、流式 `in_flight` 时机、`base_url` 缓存 key）。

调用时 `model` 填 `wjd`，网关按顺序尝试；某供应商连续失败会被熔断，期间快速跳过，让健康供应商顶上。

### 鉴权（页面账号体系 + 每账号 API Key）

鉴权**不写在配置文件里**：账号、密码、API Key 全部由管理台页面维护，持久化在 `data/users.json`
（密码 PBKDF2 哈希、Key SHA-256 哈希，均不落明文；文件权限 600 且已加入 `.gitignore`）。
`data/gateway.yaml` 只剩 `auth.session_ttl_minutes`（会话时长，默认 1440 分钟）一项。

```yaml
auth:
  session_ttl_minutes: 1440   # 网页登录会话有效期（每次登录重新计时）
```

**两类凭证，职责不同**：

| 凭证 | 形态 | 怎么来的 | 能做什么 |
|---|---|---|---|
| 会话 token | `sess-...`（短时） | `POST /auth/login` 用**账号 + 密码**换取 | 前端全部功能：调模型 + 看自己的指标/日志 + 管自己的 Key；admin 另可在用户管理页看别人的调用日志与生成资产、管账号 |
| API Key | `wjd-...`（长期） | 各账号在「API Key」页自建（默认一把都没有）；普通账号最多 3 把、admin 不限 | **只能调模型/媒体/指标**；不能改密码、不能铸删 Key、不能管账号 |

- 初始管理员 `admin` / `admin`（首次启动自动创建，**登录后请立即改密**）；`admin` 是唯一管理员。
- admin 可创建账号：账号名仅字母数字（2~32 位），不填密码时**默认密码 = 账号名**。普通账号看不到别人
  的指标与日志，也不能建号（接口 403）。
- **停用 / 启用账号（admin，第二十四轮）**：用户管理页每行一颗「停用 / 启用」。停用后该账号
  ①**不能登录**（密码正确也回 403「账号已被停用，请联系管理员」；密码错的人仍旧只看到 401，
  不泄露账号是否存在/已被停用）；②名下 **API Key 立刻失效**（回 403 说明原因，且不计入鉴权失败
  限流 —— 拿得出有效 Key 的人不是暴力破解者）；③**已登录会话当场踢掉**。Key 记录**不删除** ——
  停用是「暂时关掉」，重新启用后原密码、原 Key 立刻恢复；真要作废凭证就点「吊销全部 Key」。
  `admin` 账号永不可停用（唯一管理员被停用后没人能进管理页，只能停机改 `users.json` 救回，故
  接口直接拒绝，且启动加载时会把手工写入的 `disabled` 自动恢复并告警），也不能停用自己。
  停用状态落在账号记录的 `disabled` / `disabled_at` 两个字段上。
- 普通账号最多 3 把 API Key（到量后页面按钮置灰并给出提示，不再是一句裸 400）；**admin 不限数量**。显式设置/修改的密码至少 4 位。
- **Key 明文回显**：创建后页面展示明文，之后随时可在列表里再复制。代价是 `data/users.json` 里额外存一份
  明文 Key（该文件权限固定 600、已在 `.gitignore`）。因此 `GET /auth/apikeys` 也只认会话凭证——
  泄漏一把 Key 不该能顺走同账号其它 Key 的明文。此改动之前发的旧 Key 无法反推明文（只回显哈希前缀），
  页面会提示「删除后重建」。

**吊销语义（第八轮审计补，之前"删号/改密"根本止不了损）**：

| 动作 | 会话 | API Key |
|---|---|---|
| 删除账号（admin） | 该账号全部会话立即 401 | 随账号记录一起消失 |
| **停用账号（admin，第二十四轮）** | 该账号全部会话立即 401，且停用期间**无法重新登录** | 全部 Key 立即 403（Key 记录**保留**，重新启用即恢复） |
| 自己改密码 / admin 重置密码 | 该账号全部会话立即 401（含操作者自己，需重新登录） | **不受影响**（Key 与密码无关） |
| 「API Key」页删除某把 Key（仅会话可操作） | 不受影响 | 该 Key 立即 401 |
| admin 点「吊销全部 Key」（`DELETE /auth/users/{name}/apikeys`） | 不受影响 | 该账号全部 Key 立即 401 |
| 正常登出（`POST /auth/logout`） | 仅当前这一个会话失效 | 不受影响 |

> **安全基线（2026-09-11 依安全报告加固 + 第八轮补强）**：
> - `/healthz`：纯探活，永远只回 `{"status":"ok"}`，不泄露任何信息。
> - `/health`：**未鉴权只回 `{"status":"ok"}`**；带有效凭证才给完整看板。
> - `/health/{provider}`：会用网关 Key 探上游，**一律要求鉴权**（无凭证 → 401）。
> - `/docs`、`/redoc`、`/openapi.json`：默认关闭（`server.docs_enabled=false`），本地开发想开再设 true。
> - 安全响应头：`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`、`Content-Security-Policy`（网关层已带）。
> - 防暴力枚举：同一来源 IP 每分钟鉴权失败超过 `server.auth_fail_limit_per_min`（默认 20）→ 429；
>   登录成功一次即清零该来源计数（避免同出口 NAT 下被前人失败连坐）。
> - 防用户名枚举：账号不存在时也会走一次等代价的 PBKDF2，登录响应耗时不随账号存在与否变化。
> - 全局限流按**账号**归并成桶：同一账号多开几个会话/多建几把 Key 不再成倍放大配额。

### 历史（图像 / 视频 / 对话）落盘

这三类"生成过的东西"以前只存在浏览器里（图像/视频在 `localStorage`、对话在 `sessionStorage`），
于是换设备看不见、关标签页就丢；更糟的是 `setItem` 写失败（配额满）被 `catch {}` 静默吞掉，
表现就是"我生成过的东西怎么没了"。现在统一存服务端：

```
data/history.json          # {账号: {类别: [条目…]}}，权限 600，已进 .gitignore
GATEWAY_HISTORY=<路径>     # 可选，测试/多实例时指定
```

- **按账号隔离**：普通账号只看得到自己的历史；admin 也只看自己的（历史是个人数据，不是运维数据）。
- **按 id upsert**：带已存在 id → 原地更新（视频"生成中→完成"走这条）；新 id → 插到最前。
  所以两台设备各自生成不会互相覆盖；也因此**没有**"整份 PUT"那种一提交就把别人抹掉的坑。
- **容量**：对话 50 条（单条可到 512KB，正文不截）、图像/视频各 200 条；单条超限会截断其中的
  长字段（视频的 `rawtext` 就是典型）；整份文件软上限 4MB，超了按账号削最旧的尾部。
- 前端仍然写一份本地缓存作为离线兜底，但**服务端写不上会明确弹提示**，不再静默丢数据。
- 首次打开某页时，如果服务端为空而浏览器里有旧数据，会自动把旧数据搬上服务端（一次性迁移）。

### 运维开关（server）

```yaml
server:
  host: "127.0.0.1"
  port: 8808
  timeout: 120
  retry: 1
  health_auth: false          # /health/* 是否也要鉴权
  hot_reload: true           # 监听 yaml mtime 自动重载（改配置免重启）
  hot_reload_interval: 15
  metrics_enabled: true      # 记录调用指标
  rate_limit_rps: null       # 全局单 user QPS 上限（null=不限）
  max_body_size: 2000000     # 请求体字节上限（防 DDoS，约 2MB）
  cors_origins: ["*"]        # CORS 允许来源
  graceful_shutdown: 30      # 优雅关闭等待秒
  # ── 防滥用 / 安全（v2.8；v2.9 起这些键全部做类型/范围校验） ──
  batch_max_concurrency: 16  # /batch 并发上限（客户端 max_concurrency 会被钳到此值）
  batch_max_items: 64        # /batch 单次最大条目数（超出返回 400）
  webhook_allow_private: false  # 视频 callback_url 是否允许指向内网/回环（SSRF 防护）
  docs_enabled: false           # FastAPI 文档（/docs、/redoc、/openapi.json）是否开放（安全默认关）
  auth_fail_limit_per_min: 20   # 同一来源 IP 每分钟鉴权失败上限，超过 → 429（防暴力枚举）
```

### 可观测性 + 定价

```yaml
observability:
  log_level: "INFO"
  log_json: false            # true = 结构化 JSON 行日志（接 ELK/CloudWatch）
  access_log_maxlen: 500

pricing:                     # 人民币/百万 token，用于估算成本
  per_model:
    agnes-3.0-flash:  { input: 0.35, output: 1.00 }
    ark-code-latest:  { input: 0.50, output: 2.00 }
    qwen3.8-flash:    { input: 0.30, output: 0.60 }
    # 未配置的模型不返回 estimated_cost_cny
```

## 启动

```bash
cd api-gateway
pip install -r requirements.txt
python run.py            # 默认读 data/gateway.yaml，端口 8808
```

或用 Makefile：`make install`、`make run`、`make test`。

### 用 .env 提供 token

不想写死在 yaml 也不想像前缀 export 每个 token？在项目根或 `data/` 下放一个 `.env`：

```bash
cp .env.example .env
# 编辑 .env 填入 AGNES_API_KEY / B_AI_API_KEY / VOLC_API_KEY
python run.py           # 启动时自动读取 .env
```

优先级：真实环境变量 > `.env`（密钥所在，经各供应商 `token_env` 引用）> yaml 其它字段。


## API 端点（含 v2.1 新增）

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| `GET` | `/` | 否 | 端点导航 |
| `GET` | `/health` | 否（可开） | 运维看板：uptime/失败率/耗时/熔断状态 |
| `GET` | `/health/{provider}` | 否（可开） | 供应商探活 |
| `GET` | `/models` / `/v1/models` | 是 | 全部模型 + 路由别名 + 供应商 |
| `POST` | `/v1/chat/completions` | 是 | 主接口（fallback + 流式 + 熔断 + 三级限流 + 成本） |
| `POST` | `/v1/chat/completions/batch` | 是 | 并发多 prompt |
| `POST` | `/v1/images/generations` | 是 | 图像生成（支持路由别名） |
| `POST` | `/v1/videos` | 是 | 视频创建（`wait`/`callback_url`，支持路由别名） |
| `GET` | `/v1/videos/{id}` | 是 | 视频轮询（`?wait=true`） |
| `GET` | `/metrics` | 是 | 用量统计（JSON，含熔断状态） |
| `GET` | `/metrics/prometheus` | 是 | Prometheus 文本格式 |
| `GET` | `/logs?n=100` | 是 | 最近访问日志（普通账号只看到自己的调用） |
| `POST` | `/auth/login` | 否 | 账号密码换短时会话 token |
| `POST` | `/auth/logout` | 是 | 吊销当前会话 |
| `POST` | `/auth/password` | 是（仅会话） | 改自己的密码，改后全账号会话需重登 |
| `GET` / `POST` / `DELETE` | `/auth/apikeys[/{id}]` | 是（仅会话，含只读） | 本账号 API Key：查看（回显明文）/ 创建 / 删除 |
| `GET` / `POST` / `DELETE` | `/auth/users[/{name}]` | 是（admin） | 账号管理：列表 / 建号 / 删号（连带吊销会话与 Key） |
| `GET` / `POST` / `DELETE` | `/history/{kind}` | 是（写仅会话） | 前端历史（`kind` = chat / images / videos），按账号隔离、按 id upsert |
| `DELETE` | `/history/{kind}/{item_id}` | 是（仅会话） | 删除单条历史 |
| `POST` | `/auth/users/{name}/password` | 是（admin，仅会话） | 重置某人密码（默认=账号名）并踢掉其会话 |
| `PUT` | `/auth/users/{name}/disabled` | 是（admin，仅会话） | 停用 / 启用账号：`{"disabled": true\|false}`（停用=不能登录 + 名下 Key 全失效 + 会话当场踢；Key 记录保留，admin 不可停用） |
| `DELETE` | `/auth/users/{name}/apikeys` | 是（admin，仅会话） | 一键吊销某人全部 API Key（长期泄漏时的止损开关） |
| `POST` | `/feedback` | 是（仅会话，**管理员不可用**） | 提交意见/建议：正文 ≤2000 字 + 最多 3 张图（data URI 直传，服务端按解码后魔数验类型）；admin 提交一律 403 |
| `GET` | `/feedback/mine` | 是 | 「我提交过的」：含处理进度 `status` / `note` / `updated` |
| `GET` | `/feedback` | 是（admin） | 「用户反馈」收件箱：全部账号，按时间新→旧 |
| `PATCH` | `/feedback/{id}` | 是（admin，仅会话） | 更新处理进度：`status` = `pending` / `doing` / `done`，可选处理说明 `note`（≤500 字，提交人可见） |
| `DELETE` | `/feedback/{id}` | 是（仅会话，**仅本人**） | 撤回自己提交的反馈（连图片文件一起删）：**只有 `pending` 待处理的能撤**；处理中/已处理 → 400，撤别人的 → 403 |
| `GET` | `/feedback/media/{id}/{idx}` | 是 | 反馈原图：仅**提交人本人或 admin**；`id` 必须先在库里存在（防路径穿越） |

> 「仅会话」= 只接受账号密码登录换来的 `sess-` token，用 API Key 调用返回 403（见上文吊销语义）。

> 媒体端点的 `model` 可以填**路由别名**（如 `wjd`）：网关会自动解析到该路由链里第一个 `type==image/video` 的模型，找不到才 404。

## 关键行为说明（v2.4）

- **三级限流**：路由级 `routes.<name>.rate_limit_rps` > 供应商级 `providers.<key>.rate_limit_rps` > 全局 `server.rate_limit_rps`。路由/供应商级按独立桶分桶，打满则 429；chat 接口下打满会切下一个模型而不是直接 429。
- **媒体端点也限流**（v2.3 / v2.6 修正）：`/v1/images`、`/v1/videos` 按 `providers.<key>.rate_limit_rps` 的**供应商桶**限流，与 chat 共用同一个 `prov:<pkey>` 桶（v2.6 起媒体不再另起 `media:prov:<key>` 独立桶 —— 那样实际放行量会翻倍、与文档不符）。高成本资源不再裸奔。
- **route.timeout 全链生效**（v2.3）：配了 `routes.<name>.timeout` 后，**整条链（含链首最高优先级模型）统一用这个超时**；之前只对兜底模型生效、链首仍用 `server.timeout`。
- **熔断按次数窗口**（v2.3）：`circuit.window` 现在真正按"最近 N 次调用"滑动（之前写死 60 秒、配置无效）。判定 = 最近 window 次里失败 ≥ failure_threshold。
- **失败也算耗时**：`/metrics` 的 P50/P95 包含失败请求的等待时间。
- **in_flight 精确统计**（v2.4）：按响应**实际类型**（`StreamingResponse`）判断流式，非流式/413/502 在中间件减计数，SSE 流式在生成器收尾减计数。修复了"3MB 的流式 POST 被 413 后 in_flight 永久 +1"的泄漏。
- **适配器 client 并发安全**（v2.4）：`get_client()` 加 `asyncio.Lock`，并发下只建一个 client（之前竞态建多个泄漏 fd）；`close()` 幂等可重入（之前并发 double-close 抛 `Event loop is closed`）。
- **图像 size 缺省 1K**（v2.4）：`/v1/images/generations` 不传 size 时默认最低档 `1K`，避免 Agnes 因缺 size 返回 400；`n=1` 时省略该字段（Agnes 一次一张）。
- **热重载真正生效**：改 `circuit` / `observability` / `metrics` / `auth` / `routes` / `pricing` 即时重建运行时对象；日志级别/JSON 格式热改也真能生效；`uptime_seconds` 因热重载不会清零。
- **优雅关闭**：`server.graceful_shutdown` 传给 uvicorn `timeout_graceful_shutdown`，收到停止信号时先排空在途请求再退出。**（v2.8 重要修复）**：lifespan 里曾用 `loop.add_signal_handler` 注册自己的 SIGTERM/SIGINT 处理器，把 **uvicorn 自己的处理器覆盖掉了**，导致 `SIGTERM`/`SIGINT` 完全无法停服（实测发信号后 10s+ 仍存活，只能 `kill -9`）。现已移除该注册，交给 uvicorn 处理；实测 SIGTERM 后 0.5s 内完成 `Shutting down → 应用收尾 → Finished`。
- **OpenAI 全兼容**（v2.2）：响应带 `created` / `system_fingerprint`，流式末尾带 `usage` 块，错误为 `{"error":{message,type,code}}`，`/v1/models` 标准端点——OpenAI 官方 SDK 零适配直连。

### 调用示例

```bash
# 授权：所有调用带 Bearer（data/gateway.yaml 的 auth.users 里 key）
AUTH="Authorization: Bearer <你的 key>"

# 文本（自动路由）+ 响应带 estimated_cost_cny
curl -X POST http://localhost:8808/v1/chat/completions \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"model":"agnes-3.0-flash","messages":[{"role":"user","content":"你好"}],"max_tokens":256}'

# 路由别名 wjd（运行时挑可用 + 最快文本模型优先，其余按速度顺序 fallback + 熔断）
curl -X POST http://localhost:8808/v1/chat/completions \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"model":"wjd","messages":[{"role":"user","content":"hi"}]}'

# 并发多 prompt（batch）
curl -X POST http://localhost:8808/v1/chat/completions/batch \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"model":"agnes-3.0-flash","max_concurrency":4,
       "items":[{"messages":[{"role":"user","content":"say 1"}],"label":"a"},
                 {"messages":[{"role":"user","content":"say 2"}],"label":"b"}]}'

# 图像
curl -X POST http://localhost:8808/v1/images/generations \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"model":"agnes-image-2.5-flash","prompt":"A red cube on white background","size":"1K","ratio":"1:1"}'

# 视频（创建 + 轮询）
curl -X POST http://localhost:8808/v1/videos \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"model":"agnes-video-2.5-flash","prompt":"A cat walking","seconds":"4","mode":"text","size":"720P","aspect_ratio":"16:9"}'
# → 返回 video_id，然后：
curl "http://localhost:8808/v1/videos/<video_id>?model=agnes-video-2.5-flash&wait=true&wait_timeout=180"
```

响应带 `provider` / `provider_name` / `served_model` / `estimated_cost_cny` / `x_request_id` 字段。

## 前端 Web 控制台

- 前端由网关托管：`frontend/dist` 由网关挂在 `/web`（同源、公开无密钥），浏览入口
  `https://api.wangjinding.com/web/`（哈希路由，默认 `/web/#/dashboard`，未登录跳 `/web/#/login`）。
- 登录：POST `/auth/login` 用原始 key 换会话（`sess-` 前缀、TTL 滑动过期 86400s），
  前端不持原始 key；登出 `/auth/logout`。
- 根路径 `/`：浏览器（`Accept: text/html`）访问自动 307 跳 `/web/`；API 调用（Accept 无
  text/html）仍返回 JSON 端点导航。
- CSP（网关 security_headers）：`default-src 'self'; script-src 'self'` —— 放行同源前端
  脚本、仍禁外部 CDN 脚本与框架嵌入。若引入外部字体/脚本，需对应放宽 `font-src`/`script-src`。
- 里程碑：**M1** 登录 + 网关托管 + 去掉批量页（构建栈 Vue 3 + Vite + TS + Element Plus +
  ECharts + Pinia + Vue Router + marked）。JS 主包约 1.2MB，分包/按需引入留给 M5。
- 里程碑：**M2** 对话 Playground（`/web/#/chat`）：流式/非流式对话（fetch 直连
  `/v1/chat/completions` 解析 SSE）、模型/路由选择（路由显示完整降级链）、参数面板
  （temperature / top_p / max_tokens / 流式开关 / 系统提示词）、Markdown+代码高亮渲染
  （marked + highlight.js + DOMPurify 消毒）、推理折叠（`reasoning_content` 单独折叠）、
  降级可视化（meta 帧的 served/provider/fallback_used 徽标）、token/成本展示（流式 usage
  帧现也带 `estimated_cost_cny`）、会话历史 sessionStorage 持久、Enter 发送/Shift+Enter 换行。
- 里程碑：**M3** 图像页面（`/web/#/images`）+ 视频页面（`/web/#/videos`）：
  图像生成网格（`/v1/images/generations`，url/base64 均可显示、el-image 大图预览、耗时/供应商标注、
  尺寸/张数可选）、视频（异步提交 `/v1/videos` 立即返回 + 前端每 5s 后台轮询 `GET /v1/videos/{id}`，
  任务卡片展示状态/耗时/原始返回、完成后内嵌播放器播放真实 mp4；也可选同步 `wait=true` 直出）。
  图像/视频的 `src` 走外部 CDN，故 CSP 放宽 `img-src`/`media-src` 允许 `https:`。
- **M3 补充（第十三轮）素材上传**：对话/图像/视频三页都能**直接上传素材**（对话附图、图像参考图、
  视频首帧/尾帧/参考图/参考音频）。图片在浏览器内压缩成 data URI（`src/lib/upload.ts`，分级档位 +
  逐级降档 + 缩略图），提交前做整包体积预算检查；视频「张数」改为逐张串行（上游 `n` 必须为 1）。
- 里程碑：**M4** 仪表盘图表打磨 + 指标页 + 日志页（引入 ECharts，`src/components/EChart.vue`
  封装 + `src/lib/echarts.ts` tree-shake 按需注册）：
  仪表盘新增供应商份额饼图 + 模型延迟 P50/P95 柱图；指标页（`/web/#/metrics`）含指标卡
  （已运行/在途/降级/双 token/调用比）+ 各模型 ok/fail 堆叠柱图 + 供应商份额 + 路由 vs 供应商
  堆叠 + 延迟 P50/P95 + 熔断器状态表（自动刷新 8s）；日志页（`/web/#/logs`）展示最近 200 条
  `/logs` 访问日志（方法/路径/供应商/实际模型/OK-FAIL/SSE/调用方/rid/错误），支持方法、结果、
  关键字筛选与自动刷新 5s。EChart 共享分包约 594kB。
- 里程碑：**M5** 设置页（`/web/#/settings`）+ 分包优化：
  - 设置页：连接信息（网关地址/版本/用户/会话长度）、供应商状态表、账号（退出登录）、
    对话默认参数（model/temperature/top_p/max_tokens/stream/system，存 localStorage，
    Chat 新会话用作默认值）、主题切换（浅色/深色，`<html class="dark">` + Element Plus
    dark css-vars + 本项目配色 CSS 变量，localStorage 持久化、mount 前应用避免闪烁）、关于。
  - 分包：主入口由约 1.2MB 降到 ~6.7kB；`vite.manualChunks` 拆 `vendor-vue` /
    `vendor-element` / `vendor-echarts` / `vendor-markdown` 四个缓存友好 chunk；
    highlight.js 由全量改为 core + 15 种常用语言按需注册（Chat chunk 1MB → ~77kB）。
- 里程碑：**M6 前端重设计 v2（2026-09-13）**：一套结构 + 三套主题 token
  （`html[data-theme="A|B|C"]`：**A 深空蓝**（深色）/ **B 素白蓝紫**（默认）/ **C 暖砂墨绿**），
  切换入口＝顶栏调色盘按钮 + 手机抽屉主题卡 + 设置页「外观与语言」，另有「跟随系统深色模式」
  开关（系统深色自动用 A；`html.dark` 只给 Element Plus 弹层兜底）。
  - 新增 `src/lib/theme.ts`（`initTheme()` / `getThemeId()` / `useTheme()`，mount 前上色避免闪屏）、
    `src/lib/nav.ts`、`src/components/Icon.vue`（内联线性图标，替掉 emoji）、
    `src/layouts/{SideNav,TabBar,MainLayout}.vue`；`style.css` 重写为三套 token + Element Plus
    变量桥接；图表配色改由 `chartTheme()` 按主题取（ECharts 不认 CSS 变量）。页面改造规范见
    `frontend/REDESIGN.md`。
  - 手机端导航改「左上汉堡抽屉 + 底部 5 Tab（仪表盘 / 对话 / 图像 / 视频 / 更多）」；
    图像/视频下载按钮统一为卡片下方的普通按钮（去掉缩略图上的绝对定位浮层，修手机端变形）。
  - `lib/echarts.ts` 补注册 `GraphicComponent`：少注册它时 ECharts 会**静默丢掉** empty 图表的
    「暂无」文案，空数据显示成一块空白画布（按需引入的坑）。
  - i18n 收口：主题名/说明（`theme.nameA|B|C` / `theme.descA|B|C`，`THEME_META` 只留色值 + key）、
    对话参数占位符、token 合计、已上传图片占位、导出文件的「推理」标签、密钥页示例代码里的
    注释与示例文案，全部走 `t()`；中英 key 对称（488/488）、插值占位符一致、无未定义 key。
  - 用户管理页展开账号后的「图像资产 / 视频资产」补**下载**按钮（管理员代下载该账号产出的图/视频）：
    与图像页/视频页同一条路（`lib/download.ts` → 同源 `/v1/media/download` 代拉 + `Authorization`），
    按钮样式同为卡片下方的普通 `el-button--small`（全宽，手机端不挤变形）。
  - 去掉「配置在 data/gateway.yaml，密钥在 .env；改完重启生效」提示（仪表盘底部 `gwLine` 与设置页
    网关信息卡的 `gwReadonly` 一处不留，相关 i18n key 一并删除，避免死文案）。
  - 登录页去掉副标题「统一接入三家上游大模型，对外只暴露一个 OpenAI 兼容入口。」（`login.lead`，
    中英 key 同步删除）：左栏改为「品牌 → 标题 → 四条特性」，原留给副标题的间距由 `h1` 下边距
    （26px）撑开，桌面三主题/手机/英文复核留白正常、无溢出。
  - **手机端登录页回归修复**（去掉副标题带出来的问题）：手机端色块原来是「内容高度 + padding」，
    副标题一去掉就变矮，而 `.pane-wrap` 的 `-46px` 上浮（沿用设计稿的卡片压接缝效果）是固定值，
    于是开始吃卡片自己的内容 —— 「登录」标题整行被色块盖住、下面一行被接缝横切。修法三条：
    ① 手机端色块改给死留白高度（`min-height: 232px` + `padding-bottom: 72px`），让接缝只落在空白区；
    ② 卡片补 `position: relative; z-index: 1`（`.login-art` 是 `position: relative`，会盖住未定位的卡片）；
    ③ 卡片 `flex: 1` 撑满剩余高度，消掉页面底部那一大片死白。
    390×844 / 360×640 / 414×896 × 中英 × 三主题复核：标题与说明行均在卡片之上、无横竖溢出、控制台无报错。
  - 手机端登录页按老板要求**去掉卡片底部所有提示**：四条特性（手机端不出）与
    「密码只用于换取短时会话」胶囊都不显示，**中英切换挪到色块右上角**（`.lang-top`，半透明白胶囊 +
    `--primary-ink`）。窄屏 390/360 × 三主题复核：切换器位于 `right: 18px; top: 22px`、
    点开可切中英（切完按钮文案变 `EN`/`Sign in`）、无溢出无报错。
  - **桌面端登录页第二次调整**（老板后续要求，手机端验收通过后）：① 顶部加通栏背景图；
    ② 登录卡整体往下挪；③ 卡片底部那行提示（`密码只用于换取短时会话` + 切换器）桌面端也**整行去掉**，
    中英切换统一挪到页面右上角（`.lang-top` 从 `.login-art` 里提出来、挂到 `.login-wrap` 下，
    桌面＝深色毛玻璃胶囊 + 白字，窄屏＝半透明白胶囊 + `--primary-ink`，同一个组件两套配色）。
    背景图是一张**自绘 SVG**（`src/assets/login-banner.svg`，约 2.8KB，含注释；Vite 构建时内联成 data URI，
    不引外链、不动 CSP，`img-src 'self' data:` 已覆盖）：图形层只画白色低透明度形状
    （点阵 + 同心圆弧 + 「三家上游汇到一个网关」的路由母题），底色用主题 token 铺渐变
    （`--primary` → `--bg-2` 垂直渐变），所以 A/B/C 三套主题自动对色；另叠了左上柔光与整体压深两层，
    压深的目的是让卡片的主色面板（`--primary`）在图上有边界 —— 不加这层，A 主题这种亮主色会和图糊成一块。
    尺寸取 `clamp(270px, 40vh, 380px)`（矮屏图要矮一点，否则渐变的过渡段正好落在卡片上沿）。
    登录区下移靠 `.login-wrap` 上下内边距不对称（`156px / 44px`，等效下移约 56px），不是写死 margin。
  - **顺带修掉一个绘制顺序 bug**（几何位置对、探针量不出来，肉眼看出来的）：
    `.login-bg` 是绝对定位元素，会画在**未定位**的 `.login-frame` 之上，矮屏（720p）下它把卡片上沿、
    「登录」标题和两个输入框盖住；给 `.login-frame` 补 `position: relative; z-index: 1` 后正常
    （与手机端当年修 `.login-art` 盖住卡片是同一类问题）。切换器 `.lang-top` 的 `z-index: 3`
    保持在其上，`pointer-events: none` 只加在背景图上。
  - 桌面端复核（本地临时实例真机走查 + 尺寸探针）：1024×768 / 1280×700 / 1280×720 / 1440×900 /
    1440×950 / 1920×1080 全部 `noOverflow=true`、卡片均在图下方（y=218/228/318/343/408）、
    三主题（含 A 深色）边界清晰、无控制台报错；真实页面真机交互：输入框可聚焦可输入、
    右上角切换器点开可切中英（`Sign in` / `EN`）、`.tail` 与 `footShort` 文案确认消失。
    窄屏 390×844 / 360×640 复核为**未变**（背景图 `display: none`、色块 232px、卡片 y=186、
    特性与底部提示不显示）；i18n 486/486 对称（`login.footShort` 中英一并删除）、`typecheck` 0 error。
    线上为纯静态资源切换，后端零改动、未重启，`Login-Ddk7Znt9.css` 与本地构建 md5 一致。
  - **登录页第三次调整**（老板要求：提示内容全去 + 换科技抽象背景图 + 手机端上面也用这张图 + 登录区再往下挪）：
    ① **提示文案清零**：卡片里的提示行（`login.subtitle`）与品牌区四条特性（`login.feat1-4`）连同
    早已无人引用的 `login.connectedTip` / `login.foot` 一起删掉（中英 key 同步删），登录页现在只剩
    「品牌标题 + 登录标题 + 用户名/密码 + 按钮」（`.hint` 样式与 `.feats` 样式一并清理，
    `h2` 下边距 6 → 20px 补上原来那行提示占的间距）；i18n 从 486/486 降到 **479/479**（仍完全对称）。
    ② **背景图重画成科技抽象**（同一路径 `src/assets/login-banner.svg`）：方格网 + 交错点阵 + 星座网络
    （节点/连线/发光节点）+ 中心「核心」母题（同心环 + 六边形 + 六向辐射）+ 两道斜向光带 +
    右上角轨道环 + 左下角弧线与十字标记；配色口径不变（只画白色形状，底色仍走主题 token 渐变），
    另加两团**取主题图表色**（`--chart-a` / `--chart-b`）的侧翼光晕，三主题各自换色更明显。
    构图按两种裁切取交集排布（桌面端中间被卡片盖住、窄屏 cover 后只剩中间一列），
    主视觉统一落在图里 y 40~300 的中间带。底色渐变的保持段 44% → **58%**，让白色形状在卡片上沿附近
    仍有对比度。
    ③ **窄屏顶部色块改用同一张图**：把整套图层收进 `.login-wrap` 上的变量 `--hero-img`，
    桌面通栏 `.login-bg` 与窄屏 `.login-art` 共用（不会再出现两边不一致）；窄屏隐藏
    `.login-art .orb`（原来那两个装饰圆与图上的轨道环打架）。
    ④ **登录区继续下移**：桌面 `.login-wrap` 内边距 `156/44` → **`190/44`**（等效下移约 73px，
    1280×720 下卡片上沿 y=257，1440×900 y=347，1024×768 y=281）；窄屏色块 `min-height`
    232 → **268px**（卡片随之再下移 36px，y=222）。
    ⑤ **顺手补一个构建自检**：`frontend/scripts/check-svg.mjs`（`npm run check:svg`，已挂进 `build` 前置）
    逐个校验 `src/assets/*.svg`。起因是这轮真踩了坑 —— SVG 注释里写了 CSS 变量名（连续两个连字符），
    XML 不允许，整个文件解析失败、图层**静默不画**：页面不报错、探针也量不出，只表现为「改完没变化」。
    guard 会拦住这类注释与标签不配平，构建期直接失败。
  - 本轮复核：本地临时静态服务 + 真机走查（1280×720 / 1024×768 / 1440×900 / 1920×1080 × 三主题 ×
    中英，以及 390×844 / 360×640 × 三主题）：全部 `ok ok`（无横竖溢出）、卡片均落在图上且边界清晰、
    `hint`/`feats` 节点确认为 0、窄屏 `.login-art` 已带 `url(login-banner-*.svg)` 且 `.orb` 为 `display:none`、
    真实交互（输入框聚焦输入、右上角切换器切到 English 后文案变 `Sign in`）通过；
    `typecheck` 0 error、`npm run build` 成功（新产物 `index-DG7g6NaW.js` / `Login--E4bzU7N.css` /
    `login-banner-7rrSIoiP.svg`）。线上 `https://api.wangjinding.com/web/#/login` 已服务新产物
    （公网取 `login-banner-7rrSIoiP.svg` 200 / 7139B），后端零改动、未重启。
    另修一处小 a11y 问题：`i18n.ts` 只在 `setLocale()` 里写 `<html lang>`，初始化路径漏了，
    导致 localStorage 存 `en` 的用户 `lang` 一直停在 `zh-CN`；改为模块初始化时也写一次。
  - **登录页第四次调整**（老板：① 去掉品牌区底部版本行 ② 整页重做）：
    ① 品牌区底部那行「v2.4.0 + 网关地址」（`.art-foot`）删掉：模板、桌面样式与窄屏那条
    `.login-art .art-foot { display:none }` 一并清理；随之失效的 `version` / `base` 状态、
    `onMounted` 里那次「只为取版本号」的 `/healthz` 请求与 `api` import 一起删除 —— 登录页现在
    不再额外发任何请求。i18n 无改动（那行是直读 `/healthz` 与 `location.origin`，没走 `t()`），
    无 key 残留。
    ② 整页重做为**三套设计稿**，落在 `design/login-v3/`（该目录 2026-09-13 按老板指示清理归档，未 `rm`，恢复见
      `/root/.trash/2026-09-13-api-gateway-cleanup/MANIFEST.md`）：`d1.html` 极简白卡（浅底单列居中卡）、
    `d2.html` 深色玻璃卡（深色科技底 + 毛玻璃）、`d3.html` 通栏一体（左侧主题色品牌面 + 右侧白色
    表单面，整屏无卡片框）；`h.html` 是「iframe 定尺预览机」（每条 `文件:宽x高x缩放`，按指定视口
    真实渲染再 scale 进浏览器窗口），出稿对照图都用它。三套各含桌面端与手机端形态、token 口径与
    产品 `style.css` 的 B 主题一致（A/C 主题落地时自动跟随），**等老板选定方向后再改产品代码**
    （`views/Login.vue`）。
  - **登录页方案一落地**（老板从三套设计稿里选定 `d1`「极简白卡」）：
    `views/Login.vue` 整页重写。结构从「顶部通栏背景图 + 左品牌面 + 右表单面」的两栏卡片改成
    **浅底 + 单列居中白卡**：logo 方块 + 品牌标题 + 用户名/密码 + 登录按钮，卡片 440×444 居中，
    页脚 / 版本行 / 提示行一律没有。整页装饰改为纯 CSS 四层（全部 `pointer-events:none`、无外链
    资源）：左上与右下双柔光 `radial-gradient`、卡片背后一团主色光雾（`blur(14px)`）、左上角渐显
    点阵肌理（`mask-image` 化开）、右下三个同心细线环。原先内联的 `login-banner.svg`（科技抽象
    自绘）自此无任何代码引用（文件保留备查；Vite 未引用就不入产物，`dist/assets` 里已无它）。
    配色一律走 token + `color-mix(in srgb, var(--x) N%, transparent)`，不再写死
    `rgba(79,99,245,…)` 这类只对单一主题成立的常量，所以 A 深空蓝 / C 暖砂墨绿下自动换色：
    logo 方块是「主色自身上下渐变」、按钮阴影是主色半透明、柔光与同心环也跟着主色走。
    手机端（≤600px）卡片化整为零：去掉底 / 边框 / 阴影，表单直接在柔光底上居中，同心环缩一档。
    踩坑：输入框聚焦本想要「1px 主色 + 3.5px 柔光」双环，被全局 `style.css` 里
    `.el-input__wrapper.is-focus { box-shadow: … !important }` 整条盖掉（只剩 1px 细边），
    已在登录页作用域内用 `!important` 覆盖并写明原因（作用范围仍只限本页）。
    验证：`typecheck` 0 error、`build` 成功（`Login-CazImMjd.css` / `Login-tSNCQlh5.js` /
    `index-BWFYxQdn.js`）；用同源探针 `probe.html`（原 `design/login-v3/probe.html`，已随设计稿目录归档）逐格量过 **3 主题 × 7 分辨率**
    （1280×720 / 1024×768 / 1440×900 / 1920×1080 / 390×844 / 360×640 / 414×896）共 21 组：
    全部 `ovX=ovY=0` 无溢出、卡片 440×444 居中、三主题的卡片 / 按钮 / logo / 聚焦环颜色各自正确、
    旧结构节点 0；英文单独验过（`API Gateway Console` / `Username` / `Password` / `Sign in`、
    `<html lang="en">`、卡片 434 高同样无溢出）；真机交互验过语言切换（右上角胶囊 → English →
    文案与 `<html lang>` 同步）与空提交报错（卡片内红色提示、卡片自动增高不溢出）。
    线上 `https://api.wangjinding.com/web/#/login` 已服务新产物（`Login-CazImMjd.css` 200 / 4426B，
    旧 `Login-BMLIXfzt.css` 404），后端零改动、未重启。
    > 探针为什么挂在 vite dev 上：CSP 里有 `frame-ancestors 'none'`（安全项，不为此放宽），
    > `/web` 下的页面不能被 iframe 嵌（`__probe.html` 放 `/web` 里量不到东西），所以改用
    > **无 CSP 的 vite dev（`--port 5199`）**同源托管探针：同源才能读 iframe 的 DOM、才能直接写
    > `localStorage` 切主题/语言，再逐格量溢出与计算样式。探针跑完即从 `frontend/public/` 删除
    > （否则会被 build 拷进 `dist`），只留 `design/login-v3/probe.html` 这一份；该探针与三套设计稿
    > 已于 2026-09-13 一并归档到 `/root/.trash/2026-09-13-api-gateway-cleanup/`（未 `rm`，可恢复）。
  - 修 data URI 下载的 CSP 坑：产物是 `data:` 时原实现用 `fetch(dataURI)` 取字节，而 CSP
    `connect-src 'self'` 会把它拦掉（上游以 base64 直回图片时「下载」必失败，本地 vite preview
    无 CSP 时看不出来）。改为本地 base64 解码 `dataUriToBlob()`，不触网、也不动 CSP 白名单。
  - 复核（本地临时实例真机走查，未动线上数据）：10 个路由 × 桌面 1440 / 手机 390 / 手机英文，
    页面级横向溢出 0、控制台无报错（仅 seed 的假视频地址报 `ERR_NAME_NOT_RESOLVED`）；
    移动端表格横滑时表头与表体同步（Element Plus 内部 `overflow: hidden` + JS 同步，非缺陷）。
    已知残留：英文界面下上游/网关返回的**服务端消息**仍是中文（如 `size 仅支持 720P`），
    以及历史里的业务数据（提示词等）不翻译 —— 这两类按老板决定**不做双语**
    （要双语得后端按 `Accept-Language` 出英文消息，收益不抵成本）。
- **日志响应内容 + 推理回退**：`/logs` 每条新增 `response`（流式累积 delta.content、
  非流式取 message.content，截断 2KB）；正文为空时回落到 `reasoning_content`（标注
  `[推理] `，避免小预算下看起来像“空响应”）；日志页去掉常驻的「调用方/rid」列
  （收进展开行），合并路径+SSE 标、模型/供应商一列，新增「响应内容」列，搜索覆盖
  请求/响应/错误。流式全失败此前会因累加器未初始化而 NameError（SSE 变 500），已修复
  并在全链 `_get_adapter` 失败路径补回归测试。
- **图表文字挤压修复**：ECharts 6 把 legend 默认位置从 top 改为 bottom，导致图例压在
  x 轴标签上。新增 `src/lib/charts.ts` 统一 `legend(top:0)` / 类目轴截断+倾斜+hideOverlap /
  `grid.containLabel` 留白，仪表盘与指标页 4 类图全部接入。
- **模型与供应商页**（`/web/#/models`）：指标卡（模型/文本/图像/视频/路由/供应商）+ 路由别名表
  （降级链逐 tag 展示、供应商数）+ 模型清单（ID/类型/供应商/Key 已配），数据来自 `/v1/models` + `/health`。
- **多语言（M5+）**：Vue I18n + `src/locales/zh|en.ts`，全站中英可切换；语言按钮在头部与登录页、设置页
  （`settings.language`），存 `localStorage.gw_locale`、默认中文；Element Plus 语言包经
  `ElConfigProvider` 跟随（`App.vue` 引入 `element-plus/dist/locale/zh-cn|en.mjs`）。
  所有视图（导航/登录/仪表盘/指标/日志/对话/图像/视频/模型/设置）主要文案已接入 `t()`。
- **白屏根因修复（CSP）**：接入 vue-i18n 后网关 CSP 从 `script-src 'self'` 拦掉其运行时
  message 编译（`new Function`），整个前端白屏（本地仅无 CSP 的 vite preview 正常，极难察觉）。
  已把 `script-src` 补为 `'self' 'unsafe-eval'`，其余安全策略（禁外部脚本/连接/框架/base/form）不变。
- **`/web` 缓存策略（M5+）**：Vite 滚动产出时若 index.html 被缓存，会引用已被新一轮 build 删除的旧
  hash asset 而 404 白屏。网关对 `/web` 的 index.html 响应 `Cache-Control: no-cache`（每次重新校验），
  asset（带 hash）回 `public, max-age=31536000, immutable`。
  里程碑：此前 `frontend/dist` 每次 `npm run build` 后即被网关挂载（`app.mount("/web", StaticFiles(html=True))`）。
- **杂项**：版本号收敛为单一 `VERSION = "2.4.0"` 常量（FastAPI / `/` / `/auth/login` /
  `/health` 四处对齐，修复前端「网关版本 v-」）；`/health` 新增 `version` 字段；Dashboard
  熔断列大小写归一化（`/health` 返回小写，此前一直误标红）+ 新增「媒体熔断」列；
  Dashboard 轮询定时器改为 onMounted/onUnmounted 配对（原来裸 setInterval 泄漏）。
- **日志显示请求内容**：后端 `/logs` 每条现在带 `body`（请求体摘要，中间件按 rid 暂存、
  截断到约 2KB），前端日志页新增「请求内容」列（两行截断、悬停看全文、可搜索）；GET 类
  请求及校验/鉴权拒绝的请求无 body。另前端新增会话过期自动引导：任一接口 401 时清空本地
  token 并回登录页（此前会话失效会 401/429 循环、静默空数据）。

> **第九轮（2026-09-13）历史落盘 + 视频卡片修复**
> - 图像/视频/对话历史改为服务端持久化（`data/history.json`，按账号隔离、按 id upsert），
>   前端本地只留缓存，**写失败改为明确提示**（以前 `catch {}` 静默丢数据）。
> - **真 Bug**：视频页卡片 `v-for="t in tasks"` 的别名 `t` 把 i18n 的 `t()` 遮蔽了 ——
>   卡内 `t('videos.status')` 变成"对任务对象调用函数"，卡片整块渲染不出来，
>   表现就是"任务在列表里计数、却看不到任何视频"。循环变量改名 `tk` 后实测恢复
>   （1 张卡片、`<video>` 正常、状态/任务号/服务/耗时齐全）。
> - 图像卡与视频卡下方新增**提示词**一行（视频任务对象以前根本没存 prompt，现已随任务一起记录并 upsert 到服务端；改动前生成的旧条目无提示词可考，显示 `—`）。长提示词按 3 行截断、悬停看全文。
> - 仪表盘「已运行」改为本地每秒推进（/health 5s 一拉，旧版每 5 秒跳一格）。
> - 新增 `/history/{kind}` 端点与 2 组回归测试，单测 116 passed / 0 failed。

> **第十六轮（2026-09-13）Key 数量上限的提示收敛为「已达上限」**
>
> - 老板要求：到量时的长句（「已达上限：普通账号最多同时保存 3 把，删掉不用的再创建」）改掉，
>   只留「已达上限」四个字。三处一起收敛：接口 400 文案（`app/users.py`）、页面顶部告警、
>   创建按钮 tooltip 与点按钮时的 toast。
> - 「最多几把、要删哪把」这类说明留在页面上（按钮到量置灰 + 数字仍在 `/auth/apikeys` 响应里），
>   不再塞进每次点击的 toast 里。

> **全面体检后的「全部修复」轮（2026-09-13，紧跟全站体检）**：一个 P0 撞名 + 六个 P1 + 三处 P2
> 来源：老板要求全站体检（前端 8 分辨率 × 3 主题 × 中英 × 11 页 + 后端 130 单测），
> 体检发现的问题**全部核实属实**后按 P0 → P1 → P2 顺序修完。
> （注：README 的轮次序列主线是后端修复轮（第九~十六轮），媒体产物下载与前端整体重设计两轮未单独编号；
> 本轮即「体检发现项修复」轮，内部代号 `fix18`，备份在 `/root/.trash/2026-09-13-fix18-backup/`。）
>
> - **P0 全局类名撞车（真 Bug，肉眼可见）**：`style.css` 里有个全局进度条样式
>   `.bar { height: 5px; overflow: hidden }`，而**对话页输入区那一行按钮的容器 class 也叫 `bar`** ——
>   于是「上传图片 / 上传文件」按钮、模型下拉、token 计数、发送按钮全被压进 5px 高、还被裁掉。
>   进度条改名 **`.meter`**（`Dashboard.vue` 同步），`.composer .bar` 恢复 34px（手机 86px）。
>   教训：全局样式里**别用 `bar`/`cell`/`t` 这类极短通用词**做类名，组件内同名会被静默命中。
> - **P1-1 设置页标签被挤成竖排**：`.set-row > .lb` 没有 `flex-shrink: 0`，窄卡片下「供应商状态」被压到 13px 宽、
>   一个字一行。改 `flex: 0 0 auto`（`.lb.flex-1` 保留可伸缩），`.rt` 改 `flex: 0 1 auto` + `min-width: 0` + `flex-wrap: wrap`。
>   实测 390 宽下所有标签单行、右侧不再溢出卡片。
> - **P1-2 图像页「张数」数字看不见**：`el-input-number` 的 `controls-position="right"` 占了宽度，
>   窄列 92px → **108px**、`min-width: 30px`。实测输入区 8px → **51px**（桌面与手机一致）。
> - **P1-3 非对象 JSON 从 500 变 400**：`POST` 时若 body 是合法 JSON 但不是对象（`[1,2]` / `"x"` / `1`），
>   pydantic 会抛 `ValidationError` → 500。新增 `_json_object()` 统一入口，`/auth/login`、`/auth/password`、
>   `/auth/users`、`/auth/users/{u}/password`、`/auth/apikeys`、`/history/{kind}` 一律回 400
>   并说清「请求体必须是 JSON 对象」。
> - **P1-4 `X-Forwarded-For` 取段错误（限流可被绕过 / 也可被冒用）**：原来取 XFF 的**最左段**，
>   而左侧段是客户端可以随便编的 → 攻击者每次换一个假 IP 就能绕过登录失败限流，反过来也能伪造别人的 IP
>   把别人刷进黑名单。改为取**最右段**（真正由我们信任的反代写进去的那一跳），并且**只在本机反代直连时**才信 XFF，
>   另截断到 64 字符防超长头部。
> - **P1-5 视频轮询偷跑熔断试探令牌**：`videos_poll` 里 `wait=true` 分支会走两次 `allow()`；
>   且 `n != 1` 的归还挪到了限流/熔断检查**之后** —— 纯参数错误会白吃掉一个 HALF_OPEN 试探令牌。
>   改为 `probe_held` 标记（一次请求只取一次令牌），并把 `n != 1` 的入站校验放在所有限流/熔断检查之前
>   （图像侧同样处理，`/v1/images/generations` 的 `n != 1` 现在也先于熔断检查返回 400）。
> - **P1-6 `history.upsert` 索引重建**：没有 id 的条目插入后索引不更新，后续同 id upsert 会追不到。
>   改为**恒重建索引**，并补回归测试。
> - **P2-1 配置里的明文 Key 死配置**：`data/gateway.yaml` 里残留的 `key:` 明文配置（早已废除、不会被读取，
>   但会误导人以为「密钥写在 yaml 里」）删除，密钥统一走 `.env`。
> - **P2-2 vite 代理漏了 `/history`**：`frontend/vite.config.ts` 的 dev proxy 没配 `/history`，
>   本地开发时历史读写会 404（构建产物走同源，线上不受影响）。
> - **P2-3 手机端手感**：`.el-button--small` 手机端 `min-height: 34px`（原 24px，低于触控推荐下限）、
>   底部 tab 与场景卡副标题字号 11.5px（原 10 / 10px）、视频场景卡手机端两列（原来一格一行、
>   且「首尾帧（图生视频）」会折成两行）、用户管理页手机端折列 + 操作按钮改图标。
> - **顺带**：PBKDF2 哈希挪到线程池（`_offload`），避免登录/改密时阻塞事件循环。
> - 回归测试新增 7 条（非对象 JSON 400 / XFF 最右段 / 图像 n≠1 不吃试探令牌 / 视频轮询只取一次令牌 /
>   索引重建 / 线程池哈希），单测 **137 passed / 0 failed**、pyflakes 0、`vue-tsc` typecheck 0 error、
>   构建通过（`index-Du-kU_xh.css` / `vendor-element-Bryi61XR.js`）。
> - **场景卡由三列改两列（老板 2026-09-13 拍板「改」）**：桌面参数列只有 ~289px 宽，三列时每格 91px，
>   中文「首尾帧（图生视频）」标签（实测 108px）在格内必然折两行、英文 `Keyframe (img2video)`（117px）更挤。
>   改为 `repeat(2, minmax(0, 1fr))`（手机端原来的两列覆盖也随之删掉，规则收敛到 `style.css` 一处），
>   每格 141px（可用 127px）：中英标签全部单行、三格等高（zh 49px / en 46px）、右边界距卡片 15px。
>   实测 1280/1440/1024 桌面 + 390 手机 × 中英 7 组全部 `折行=no`、横向溢出 0。
> - 上线与复核（2026-09-13 17:13 老板批准重启）：`supervisorctl restart api-gateway:api-gateway_00`；
>   线上 `/healthz` 200、非对象 JSON 400、错误密码 401、无凭证 401；线上 CSS 内含 `.meter{height:5px}`、
>   `.set-row>.lb{...flex:0 0 auto}`、手机端 `min-height:34px`，旧的 `.bar{height:5px` 已消失；
>   专项探针 24 组合（B/A/C 主题 × 1280/390 × 6 页）横向溢出全 0，输入区 34/86px、张数输入 51px、
>   手机触控目标 34px、字号 11.5px，`.acc-sub` 仅手机显示、操作按钮 3 个全在屏内。
>   收尾（同日傍晚，老板「收掉、清掉、改」）：临时体检环境（后端 8899 + vite dev 5199 + `gwcheck.vite.config.mts`）
>   已停进程并归档到 `/root/.trash/2026-09-13-fix18-cleanup/`（含 MANIFEST.md 与恢复命令）；
>   `frontend/dist.bak-0913-0127` 与 4 处 `__pycache__` 同样归档；场景卡改两列后构建并上线
>   （`index-Gg1BF2WV.js` / `index-DdRZNrpf.css`，纯静态切换、后端零改动未重启）；
>   顺手补 `.gitignore` 里漏掉的 `.test_history.json`。
>   保留未动：`live_check.py` / `smoke_test.py`（README 写明的正式运维脚本）、现役 `frontend/dist/`。

> **第十五轮（2026-09-13）改密不再要求「当前密码」**
>
> - 老板要求：已经用网页登录进来了，改密就别再让人输一遍当前密码 —— 会话本身就是身份证明。
>   设置页现在只留「新密码」输入框（+一句说明），`POST /auth/password` 的 `current_password` 变为可选。
> - **安全边界不退化**（单测覆盖）：仍然必须持**会话**（API Key 调这里照样 403）；
>   改完立刻吊销该账号全部会话强制重新登录；新密码与原密码相同时照样拒绝（不填当前密码也能判断 ——
>   拿新密码去校验已有哈希即可）；**老客户端若仍传 `current_password`，传错必须报错**，不能默默放行。
> - 单测新增 1 组共 5 个断言（免密改密成功/旧会话失效/旧密码失效/同密码拒绝/错误当前密码拒绝/Key 改密 403），
>   总 **125 passed / 0 failed**。

> **第十四轮（2026-09-13）页面细节：运行时间实时走 + 真删要二次确认 + Key 页自带调用文档**
>
> - **指标页运行时间实时跳动**：`/metrics` 是 8s 一拉，直接把返回值渲染就是「8 秒跳一格」。
>   改成以「拿到值的那一刻」为基准、本地每秒累加（`uptimeAt` + `nowTick` + 1s 心跳，`onUnmounted` 清理），
>   与仪表盘同一套做法 —— 秒针连续走，且不影响后台 8s 的真实采样。
> - **清空/清除类操作补二次确认**：视频页「清除已完成」、图像页「清空历史」都是**服务端真删且不可逆**
>   （不是只清本地视图），原先点一下就没了。现在弹确认框并写明条数与后果（另：视频页没东西可清时按钮置灰）。
> - **设置 → 关于去掉文档外链**（不再往外部站点跳），同时删掉对应 i18n 键。
> - **API 密钥页新增「API 调用说明」**：接口地址（复制按钮）· 鉴权头 · 模型选择（`wjd` 路由别名 vs 真实模型 id）·
>   请求格式 · 常用端点表（与 `app/main.py` 的真实路由一一对应）· 四段可直接复制的示例
>   （对话 / 图像 / 视频提交+轮询 / Python OpenAI SDK）· 七条注意事项（n=1、seconds 4–12、
>   轮询别快于 5s、素材可用 data URI 但请求体 <2MB、Key 调管理接口 403、Key 泄漏处置、审计归属）。
>   示例里 Key 一律用 `$GW_KEY` 占位，不把真实密钥渲染进文档。
> - 前端 `npm run typecheck` 0 error、`npm run build` 通过（`index-Dp4R0e28.js`）；真机（临时实例）验过：
>   运行时间 21s→24s→28s 连续走、确认框「取消保留 / 删除真删（服务端历史同步清空）」、
>   Key 页文档中英文渲染、按文档中的 curl 原样调用对话/图像成功、Key 调管理接口回 403。
>   后端本轮无改动。

> **第十三轮（2026-09-13）按 Agnes 三个文档把「对话 / 图像 / 视频」做全场景覆盖（含素材上传）**
> 依据：`agnes-30-flash` / `agnes-image-25-flash` / `agnes-video-25-flash` 三份官方文档 + 真 key 实测。
>
> - **素材上传（三个页面都能"给图"了）**：新增 `frontend/src/lib/upload.ts` —— 浏览器内把选中的
>   图片用 Canvas 压缩到分级档位（对话/参考图 1024px、视频首尾帧 1280px），逐级降档直到落进单件预算，
>   再转成 `data:` URI 直传。**实测上游三条路径都接受 data URI**（对话视觉、图生图、视频首尾帧/参考音频），
>   所以不必引入上传端点与临时文件：素材不落盘、不占公网带宽。音频无法在浏览器重编码，只做体积与格式校验。
> - **整包体积预算**：网关 `max_body_size` = 2MB、Nginx 也是 `client_max_body_size 2m`，而 base64 会再放大 4/3 ——
>   提交前统一 `assertBodyBudget()` 检查（素材 + 提示词），超了给一句人话，而不是让用户撞 413。
> - **对话页**：新增「上传图片」（可多张、可删、可混 URL）；模型下拉给视觉模型标 👁；带图但所选模型
>   不支持视觉时**提前告警 + 一键切到视觉模型**（避免"发了图但模型根本没看"）。历史落盘只存**缩略图**
>   （服务端单条上限 512KB，原图 base64 会被截断成坏图）。
> - **图像页**：参考图可**上传**（缩略图预览，≤5 张；也可粘 URL）；**张数改成逐张串行生成** ——
>   实测上游 `n=2` 直接 400「n 必须为 1」，网关入站挡下并 400 说明正确用法，页面则每张一次 `n=1`、
>   出图即上屏、中途失败保留已生成的说清第几张断的。
> - **视频页**：首帧/尾帧可**上传或粘 URL**（二者可混用）；reference 模式的参考图（≤5）与参考音频（≤3）
>   同样支持上传；新增 `seed`；卡片显示素材摘要（只存文件名，不把 data URI 塞进 16KB 的历史条目）。
> - **后端入站校验补齐**（按文档"Flash 专属错误/生成模式规则"）：`size` 只认 720P、`aspect_ratio` 白名单
>   （21:9/16:9/4:3/1:1/3:4/9:16）、`n` 必须 1、**`videos` 明确拒绝**（此前字段未声明会被 pydantic 静默丢弃，
>   用户以为传了参考视频其实被吞）；非法组合一律干净 400、不发上游、不进熔断。
> - **上游错误分流对齐**：图像路径此前所有上游错误一律 502 且都记媒体熔断 —— 选错一个尺寸戳三次就能把
>   图像页+视频页一起打成 503；现在 4xx → 干净 400（带上游原话）**不记熔断**，5xx 才 502 + 记熔断（与第十轮视频对齐）。
> - **轮询限流不再误伤**：实测 `/agnesapi` 每 3 秒一问就零星回 429「查询过于频繁」；原逻辑把 429 记成熔断失败
>   并给前端 502 —— 连撞三次就把整个媒体服务熔断 30 秒。现在回 `200 + throttled + retry_after`，
>   熔断桶与调用指标都不动，前端按退避继续轮（页面轮询间隔保持 5s，不按文档建议的 1~2s，正是为了不撞限流）。
> - **视觉能力位**：`/v1/models` 新增 `vision`（`agnes-3.0-flash` = true，即文档里的"输入模态：文本、图像 URL"）；
>   **带图请求走路由别名 `wjd` 时把视觉模型排到链首**（否则链首的纯文本模型要么忽略图片、要么报参数错误，
>   两种都极难自查）；直接指定真实模型时不改用户选择。
> - 回归测试新增 6 组（vision 暴露 / 视觉优先重排 / 带图块识别 / 图像 n 拦截 / 图像 4xx·5xx 分流 /
>   视频 videos·size·aspect·n 校验 / 轮询 429），单测 **124 passed / 0 failed**、pyflakes 0；
>   前端 `npm run typecheck`（vue-tsc）**0 error** —— 顺手补了 element-plus 语言包的模块声明、
>   删掉 `Dashboard.vue` 里残留的 `:class="c.cls || ''"`（该字段早已不存在）。

> **第十一轮（2026-09-13）日志：图像/视频响应数据 + 视频轮询合成一条**
> - `/logs` 里**图像 / 视频生成**现在有「响应数据」了：图像行补 `response`（上游摘要：
>   served 模型 / 供应商 / 张数 / 耗时 / 首张 url，base64 太长只留头部），视频创建行补
>   `response`（video_id / 模型 / 供应商 / status / async）—— 展开行即可看到，不用再翻上游。
> - **视频轮询不再每条各占一行**（原来每 5s 一个 GET `/v1/videos/{id}` 就多一行，整个
>   生成过程刷屏几十行）。改为按 `video_id` 在内存聚合，`/logs` 只返回「创建」那一条，
>   展开后在「轮询记录」时间线里一次看全每次轮询的状态 / 进度 / 上游原文（前端
>   `el-timeline` 渲染，时间线每行附原始 JSON，上限 500 次、最多追踪 200 个任务）。
> - 新增 3 组回归测试（图像带响应 / 视频轮询合成一条 & 轮询全部数据 / 4xx 分流），
>   单测 **113 passed / 0 failed**、pyflakes 0。

> **第十二轮（2026-09-13）设置精简 + 按用户审计 + 速度路由**
> - **#1 设置页去掉「对话默认参数」**卡片（对话页内的参数照常可临时调，只是不再有全局默认值编辑器）。
> - **#2 日志/资产按用户隔离 + admin 集中审计**：`/logs` 现在对**所有人（含 admin）只显示自己**的调用；
>   新增管理员专属只读端点 `GET /auth/users/{name}/logs` 与 `GET /auth/users/{name}/history/{kind}`，
>   **集成进「用户管理」页的行展开**：展开任一账号即看 TA 的调用日志（含视频轮询合成）、图像资产、视频资产（三个 tab、懒加载、缩略图/预览）。非 admin 403、账号不存在 404。
> - **#4 `wjd` 路由改为「先判可用 + 速度最快优先」**：不再按 yaml 里的数组优先级，而是 ——
>   ① 当前不可用的模型（供应商没配凭证 / 熔断 OPEN）排到最后；② 可用模型按**最近平均延迟**升序（最快先调）；
>   ③ 没测到延迟的可用模型给「探测位」排在已测慢模型之后，以便学到它的速度；④ **冷启动全链无样本时原样退回配置顺序**。
>   直接填真实模型 id（非路由别名）不受影响，保持确定性。新增 `Metrics.recent_avg_latency()`。
> - 新增回归测试（按用户日志/资产端点 + 越权/404/非法 kind + 速度&可用性排序 + 冷启动回退），
>   单测 **115 passed / 0 failed**、pyflakes 0。
> - **#3 全场景调用**（依 Agnes 官方文档解锁）：
>   - **视频页**新增「场景」选择器 —— 文生视频 / 首尾帧（图生视频）/ 参考图·音频，字段严格对齐 Agnes
>     `mode ∈ {text, keyframe, reference}`：keyframe 传 `first_frame`/`last_frame`（至少一个），reference 传 `images`（≤5）/`audios`（≤3，提示词可用 `<Picture N>`/`<Audio N>`）；`seconds` 放开到 **4–12**；`aspect_ratio` 全量 21:9/16:9/4:3/1:1/3:4/9:16；`size` 固定 720P。
>   - **图像页**新增「比例」(1:1/3:4/4:3/16:9/9:16/2:3/3:2/21:9)、尺寸 1K–4K，以及**图生图 / 多图合成**（每行一个图片 URL 或 data:）。
>   - **对话页**新增「附图（视觉）」：带图的用户消息按 OpenAI 视觉格式 `content=[{type:text},{type:image_url}]` 发送（需所选文本模型本身支持多模态）。
>   - 后端 `_validate_video_request` 在网关按上述契约**入站校验**非法 mode/媒体/时长组合 → 干净 400、**不发上游、不进熔断**。
>   - 实测：真实调用 Agnes（当前免费）跑通 文生图→图生视频(keyframe, completed 带 mp4)、参考图(reference, completed)、6 图被网关 400 拦下。

> **第十一轮补充（2026-09-13）鉴权可见性 / 日志展开 / 速度路由**
> - **日志展开不被自动刷新收起**：日志页每 5s 自刷新会整体替换表格数据、把展开的视频
>   轮询详情重新折叠；改为记录已展开行的 `row-key`（`rid` 兜底 `method:path:ts`），刷新后
>   `toggleRowExpansion` 显式恢复；并对**已展开、仍生成中**的视频行由日志页每 5s 主动轮询
>   （先看旧数据里展开的未完成视频 → 打一次 GET → 再取 /logs），让详情时间线实时往下走，
>   完成后自动停轮询。视频页本身也已是 5s 轮询。
> - **#1 设置页去掉「对话默认参数」**卡片（对话页内联参数照常保留）。
> - **#2 日志可见性收口**：`/logs` 对**所有人（含 admin）只显示自己**；新增 admin 专属
>   `GET /auth/users/{name}/logs` 与 `GET /auth/users/{name}/history/{kind}`，把「看别人的
>   调用日志 + 生成的图像/视频资产」集成进**用户管理页的行展开**（调用日志 / 图像资产 /
>   视频资产 三个 tab，懒加载、只读、非 admin 403、账号不存在 404）。
> - **#4 `wjd` 路由改「可用 + 速度」排序**：路由别名不再走配置里的静态优先级，而是先剔除
>   当前不可用的文本模型（供应商没凭证 / 熔断 OPEN），可用者按**最近平均延迟升序**（最快先调），
>   没测到延迟的可用模型给「探测位」兜到已测慢模型之后，整条链都没样本时**原样退回配置顺序**。
>   直接填真实模型 id（非路由）不受影响。新增 `Metrics.recent_avg_latency()`。
> - 单测 **115 passed / 0 failed**、pyflakes 0。

> **第二十轮（2026-09-13）对话页「整页要一直往下拉」——grid 行高没钉住**
> 来源：老板 ——「对话里面被挤压，页面要一直下拉，对话框要固定高度、滚动条」。
>
> 先说清真因（临时实例实测复现，不是感觉问题）：`.chat-layout` 是 `display:grid`，
> 但**没有声明 `grid-template-rows`** → 默认 `auto`，这一行的高度按**内容**撑开
> （24 条消息时行高 6156px、手机 7982px）。行一撑高，`.chat-main` / `.chat-card` 的
> `height:100%` / `flex:1` 又反过来跟着这个行高走，于是：
> `.msgs`（`overflow-y:auto`）永远等于内容高度、自己**永远不滚**，
> 整页（`.gw-body`）被撑成 6172px / 7994px 的可滚区域，
> **输入框被推到视口下方 5354px / 7202px** —— 想发下一条得先一路滚到底。
> 佐证：旧构建下 `.gw-body` 可滚 5354px、`.msgs` scrollHeight == clientHeight、
> `输入区 ❌ 被切 5354px`；其余页面（图像 / 视频 / 日志）量下来都没有这个问题 —— 只有对话页的两栏骨架有这个 bug。
>
> 修复三处（前端）：
> 1. `style.css .chat-layout` 补 **`grid-template-rows: minmax(0, 1fr)`**：行高吃容器给的
>    确定高度（`height:100%`），超出的部分交给内部滚动。`minmax(0, …)` 而不是 `1fr` 是必须的 ——
>    `1fr` 的下限仍是 `auto`，照样会被内容顶开。
> 2. `Chat.vue .chat-main` 补 **`min-height: 0`**：flex/grid 子项默认 `min-height:auto`
>    （= 内容高度），不写这一条，里面的 `.msgs` 还是撑得开、滚不动。
> 3. `Chat.vue .conv-list` 补 `min-height: 0`（左栏会话列表同理，长列表自己在栏内滚）。
> 4. **顺带修掉一个连带 bug**：`scrollToBottom()` 原来在 `watch(messages, …)`（默认
>    `flush:'pre'`）里同步读 `scrollHeight` —— 读到的还是**旧的**高度，加上 `.msgs` 之前根本
>    不滚，等于一直是空操作。现在改成 `nextTick()` 里滚，**打开长会话直接停在最新一条**。
>
> 实测（临时实例，24 条混合消息：长 markdown + 代码块 + 推理块 + 降级 meta）：
> | 项 | 桌面 1280×880 | 手机 390×844 |
> |---|---|---|
> | 页面自身滚动 | ✅ 不滚（`.gw-body` 可滚 0px） | ✅ 不滚（0px） |
> | 消息区自己滚 | ✅ 可滚 5370px | ✅ 可滚 7269px |
> | 打开位置 | ✅ 最新一条（scrollTop = 最大） | ✅ 最新一条 |
> | 输入框 | ✅ 完整可见（距底 16px） | ✅ 完整可见（距底 67px，底部 Tab 之上） |
> | 真越界元素 | ✅ 0 个 | ✅ 0 个 |
>
> 另测：点「新建对话」清空后空态占位居中、输入框仍贴底；同批体检脚本依次量了
> 仪表盘 / 图像 / 视频 / 日志，均无「真越界」（仪表盘与视频页在窄屏按设计让整页滚，属预期）。
> 质量门：`vue-tsc` 0 错、`vite build` 通过、`check:svg` 通过（本轮未动后端，故不涉后端单测）。
> 滚动条本身沿用全局 `::-webkit-scrollbar` 细条样式（8px）；是否常显取决于各系统
> 是否启用 overlay 滚动条，与站内其他滚动区域口径一致。
> - **上线（2026-09-13 17:56，老板批准）**：`npm run build` → 线上现役
>   `index-C_MmjTC1.js` / `index-BuS4X-hx.css` / `Chat-DUjZh8wz.js` / `Chat-C5OGUx8e.css`
>   （`Videos-CYhg2onR.js` 等也跟着换名 —— 它们 import 了换名后的 index chunk，内容里引用文件名变了）。
>   旧 `index-Yy0kGuVA.js` / `index-DKyqf84q.css` / `Chat-BMV2mwlU.js` / `Chat-CJxLcgM9.css` 均已 404。
>   **本轮只动前端，没有重启后端**：进程 pid 1999293（`127.0.0.1:8808`）未动，**网页会话不会被踢、不用重新登录**。
>   复核：公网 `/web/` 200 且引用新 hash、新 asset 200 / 旧 asset 404、`/healthz` 200、CSP 未放宽；
>   拉线上产物核对 —— index CSS 含 `grid-template-rows:minmax(0,1fr)`、Chat CSS 含 `min-height:0`、
>   Chat JS 里滚到底已包在 `nextTick` 回调里；公网登录页实测正常渲染（无白屏）。
>   小坑记一笔：查 `Cache-Control` 要用 **GET**（`curl -s -D - -o /dev/null`），
>   `curl -I` 发的是 HEAD，而 `web_cache_policy` 中间件只处理 `GET` → 会误判成「没设缓存头」。

> **第二十四轮（2026-09-13）管理员停用 / 启用账号（停用=登不进、Key 也失效）**
> 来源：老板一句「admin 管理员支持停用和启用用户，停用的用户不能登录，也不能调用 apikey」。
>
> - **改了什么**
>   | 位置 | 内容 |
>   | --- | --- |
>   | `app/users.py` | 账号记录新增 `disabled` / `disabled_at`；`is_disabled()`、`set_disabled(name, disabled, by)`（幂等；`admin` 永不可停用、不能停用自己，两条规则只在 store 里判一次）；`list_users()` 回状态与停用时间；`resolve_key()` **跳过已停用账号**（Key 记录不删除）；`disabled_key_owner()` 供鉴权层区分「Key 有效但账号停用」；启动加载时 `_keep_admin_usable()` 把手工写入的 admin `disabled` 自动恢复并告警 |
>   | `app/main.py` | 登录：口令校验**通过之后**再判停用 → 403「账号已被停用，请联系管理员」（不知道密码的人依旧只看到 401，探不出账号状态）；`_identity_from_credentials()` 会话分支加兜底（停用即吊销该账号会话）；`require_auth()` 对「已停用账号的 Key」回 403 而非 401，且**不计入**鉴权失败限流；新增 `PUT /auth/users/{name}/disabled`（admin + 仅会话；停用时吊销其全部会话）；新增 `_body_bool()` —— `{"disabled":"false"}` 绝不能被 `bool("false") is True` 当成真，坏值直接 400 |
>   | 用户管理页 | 每行「停用 / 启用」（停用带二次确认）、账号名旁「已停用」胶囊（悬停显示停用时间）、停用行压暗；admin 那行不给这颗按钮；操作列手机端从 4 颗图标变 5 颗 → `.ops` 加 `flex-wrap`（宁可换行也不撑宽表格） |
>   | i18n | 新增 7 条中英文案（`userDisabled` / `userDisabledAt` / `disableUser` / `enableUser` / `confirmDisableUser` / `userDisabledOk` / `userEnabledOk`），中英 **674 / 674** 对称 |
>   | **顺带修的真 bug** | `frontend/src/api.ts` 一直只读 `body.detail`，而网关自己的报错信封是 `{"error":{"message":…}}` → 全站错误提示退化成「HTTP 400」。现在统一认两种信封并导出 `errText()`；登录页 403 也改为显示服务端原文（此前是写死的「账号被禁用或角色异常」） |
>
> - **验证**（隔离实例 8899，项目副本 + 临时 `users24.json`，**生产 `data/` 未碰**）
>   - 单测 155 passed / 0 failed（原 151 + 新增 4：三闸门 + 启用恢复、权限与入参矩阵、全接口闸门与手工改盘兜底、admin 自动恢复）。环境无 pytest，用 `python tests/test_gateway.py` 跑。
>   - API 逐条实测：停用前 Key 调 `/models` 200 → 停用后登录 403、Key 403、旧会话 401、密码错仍 401 → 重新启用后登录 200 且**原 Key 恢复 200**（`api_key_count` 仍为 1，证明 Key 没被删）；负例：停用 admin 400、不存在的账号 404、`"maybe"`/`[]` 400、字符串 `"false"` **不停用**、拿 Key 调停用接口 403。
>   - 真浏览器：确认框文案、toast、已停用胶囊、按钮切换、启用恢复、英文界面（`Disable` / `Disabled`）全部实测；被停用账号在页面上登录，报的就是「账号已被停用，请联系管理员」；顺带验证错误信封修复（非法账号名 toast 从 `HTTP 400` 变成「账号名只能由字母和数字组成」）。
>   - 未实测项（如实记）：**390px 手机断点没测到** —— 本会话浏览器没有视口控制（试过 Ctrl+缩放压低 CSS 视口无效），故用 `flex-wrap` 兜住「5 颗图标超宽」的最坏情况，建议真机上瞄一眼。
>
> - **上线（2026-09-13 20:0x，老板批准）**：`npm run build` → 线上现役 `index-BYW9huII.js` / `index-BCEpjaoj.css` / `Users-Ym7Y7v4h.js`（旧 `index-BV89HnWe.js` 已 404）；
>   `supervisorctl restart api-gateway:api-gateway_00`（旧 pid 2102891 → 新 pid 2157737，仍监听 `127.0.0.1:8808`）。
>   **本轮有后端改动，重启会清空内存态网页会话 —— 所有人（含 admin）需要重新登录。**
>   复核：公网 `/healthz` 200、`/web/` 200 且引用新 hash、新 asset 200（`immutable` 长缓存）与旧 asset 404、
>   CSP 未放宽、`index.html` 仍 `no-cache`；新端点在线判据 = 无凭证调 `PUT /auth/users/x/disabled` 得 **401**（旧代码是 404）。

> **第二十三轮（2026-09-13）「网关配置」页 —— 配置文件可视化（admin，保存即生效）**
> 来源：老板三点 ——「设置里的『关于』整块去掉」「配置文件里的配置项都做成 admin 能配、保存后实时生效」
> 「改完直接重启上线」。收尾时老板一句「收掉」= 把这一轮彻底闭合（含排查卡点、上线、清理）。
>
> - **改了什么**
>   | 项 | 内容 |
>   | --- | --- |
>   | 设置页 | 「关于」整块删除（设置页不再露出版本/仓库信息） |
>   | 新页面 | `/gateway-config`（侧栏「网关配置」，仅 admin）：`data/gateway.yaml` 的每一项都成了可视控件 |
>   | 保存行为 | 校验 → 原子写盘（`os.replace` + 滚动备份 `.bak` + 权限 600）→ 复用同一条 `_apply_config` 立刻切运行态 |
> - **后端**
>   - 新增 `app/settings.py`：配置文件 ↔ 面板 JSON 的完整映射、**带注释的 YAML 渲染**（保存后文件还是人读的样子）、备份与原子写。
>   - `app/config.py` 抽出 `parse_config()`：先内存解析校验、不通过就**不写盘**；新增 `_opt_bool()`
>     （顺手修掉 `bool("false") == True` 这个真 bug）；`server.port` 走 `_opt_int` + 1–65535 校验。
>   - `GET /config`（管理员）：面板值 + 文件原文 + 告警 + 需重启键 + 模型/路由清单；
>     **密钥只回报「配没配」，绝不回显**。
>   - `PUT /config`：管理员 + **网页会话**（拿 API Key 调一律 403）；热重载循环改为共用这条应用路径，
>     `server.hot_reload` 关掉后真正停手。
> - **前端**
>   - 5 张分区卡片（服务与安全 / 供应商与模型 / 路由与熔断 / 日志与定价 / 下载代理）
>     + 顶部「跳到」胶囊 + 每张卡右上与页面底部各一组保存按钮；改了「需重启」的键会亮 `需重启` 药丸。
>   - 保存成功顶部出「已保存并生效」；失败出错误框且**盘上文件不动**。
> - **这一轮真正的坑（记一笔，别再踩）**
>   - 现象：`/gateway-config` **第一屏整块不渲染**，其余屏正常；`app.config.errorHandler` 也不报错。
>   - 真因：**文案里写了 `{"status":"ok"}`** —— vue-i18n 把 `{...}` 当占位符，这条编译不过
>     （`Invalid token in placeholder`），而文案是**渲染时才编译**的，于是"用到这条文案的整块界面直接空白"。
>     修法：字面量花括号写成 `{'{'}` / `{'}'}`（这条文案因此改用双引号 TS 字符串）。
>   - 顺着补了 `frontend/scripts/check-i18n.mjs`（`npm run check:i18n`，已挂进 `build` 前置）：
>     扫占位符语法 + 中英键对称 + 重复键 + 空文案，四类问题都在构建阶段拦下（自测能抓回上面那条）。
>   - 另一个小坑：页面标题取 `t('nav.menu' + route.name)`，路由 `name` 必须和 `nav.ts` 的 `key` 一致，
>     否则标题直接显示原始键名（本轮把 `gatewayconfig` 改回 `gatewaycfg`）。
>   - 教训：界面"整块空白 + 控制台只有一条 Vue 报错"时，**先查文案编译**，别先去怀疑组件库的渲染机制。
> - **质量门 / 上线（2026-09-13 19:1x，老板批准）**
>   - 后端 151 passed / 0 failed（原 146 + 新增 5：视图↔yaml 往返零漂移、密钥不回显、自定义段/明文 token 不丢、
>     假值/坏值报字段名、权限矩阵 + 保存即生效）。
>   - 前端 `vue-tsc` 0 错、`check:svg` / `check:i18n` 通过、`vite build` 通过。
>   - 隔离实例（8899，配置走 `/tmp` 副本，**生产 `data/` 全程只读**）真浏览器实测：5 屏全渲染、
>     「跳到」滚动生效（目标卡片 y 4221 → 62）、会话时长改成 45 保存 → 盘上文件立刻更新且生成 `.bak`（权限 600）、
>     页面出「已保存并生效」。
>   - 上线：`npm run build` → 线上现役 `index-T9rUvEqS.js` / `GatewayConfig-Cujt5GjO.js`
>     （旧 `index-BMd-U-9_.js` 已 404）；`supervisorctl restart api-gateway:api-gateway_00` →
>     新 pid **2102891**（`127.0.0.1:8808`）—— **本轮有后端改动，重启踢掉所有网页会话，需重新登录**。
>     随后小修一处文案（`{'{'}"status":"ok"{'}'}` 里花括号写法之间多打了空格，渲染成 `{ "status":"ok" }`，
>     与 /health 真实报文不符）→ 再 `npm run build` → 现役 **`index-CladK7BT.js` /
>     `GatewayConfig-V1lLUX_x.js`**（旧 `index-T9rUvEqS.js`、`GatewayConfig-Cujt5GjO.js` 已 404）；
>     **只动前端，没有重启后端**（pid 2102891 未动、网页会话不掉）。
>     用 vue-i18n 自己的运行时核过两份文案的渲染结果都是 `{"status":"ok"}`（无空格）。
>   - 复核（全部实测）：`/healthz` 200、`/web/` 200 且 `Cache-Control: no-cache`、新 asset 200 / 旧 asset 404、
>     CSP 未放宽、`GET|PUT /config` 未登录 **401**（证明新端点已随重启上线）。
>     线上产物含 `sec-server` / `sec-download` 分区。
>     线上逐屏复核没做（当前管理员密码非默认值，小银不掌握、也不去试），面板的浏览器实测以隔离实例为准。
>   - 收尾：隔离实例 8899 已 kill（线上 8808 未受影响），`/tmp` 残留与临时配置副本归档到
>     `/root/.trash/2026-09-13-gw23-cleanup/`（含 `MANIFEST.md`），`frontend/public/` 与 `dist/` 无探针残留。
>
> - **追加一（同日 19:2x，老板「模型定价去掉」「左侧菜单上下间距缩短一点，让滚动条不要出现」）**
>   - **侧栏间距收紧**（`layouts/SideNav.vue`）：菜单项内边距 `9px→7px`（行高 **36→32px**）、项间距 `2px→1px`、
>     分隔线上下 `10px→7px`、`.rail-inner` `16px→14px`、`.brand` 下边距 `16px→12px`。
>     实测（视口 720px）：菜单区 **500px → 427px**，出滚动条的临界视口高 **~605px → ~538px**。
>   - 当轮还按字面把「网关配置」页里的**模型定价块删了**（`prices` / `PriceRow` / `PUT` 的 `pricing.per_model` 一起撤），
>     i18n 中英各删 5 条。**此改动已按追加二回退，见下**（`settings.py` 的"只在视图带 pricing 时才覆盖"行为
>     当时验证过：面板保存不会清空 `data/gateway.yaml` 里的价格段）。
>
> - **追加二（同日 19:3x，老板「网关配置功能先不要删掉先隐藏起来，这块先不支持页面配置」）**
>   - **定价块原样恢复**：模板整块、`prices` 状态、`interface PriceRow`、`GET /config` 填充、`PUT /config` 的
>     `pricing: { per_model: perModel }` 全部退回；i18n 中英各恢复 5 条（`pricing` / `h_pricing` / `priceIn` /
>     `priceOut` / `addPrice`）+ `tabLog` / `tabLogSub` / `gwConfigSub` 复原 → `check:i18n` **中 667 / 英 667**。
>     zh 五条取自归档 `zh.ts.good` **逐字原文**；en 的 `addPrice` / `priceIn` / `priceOut` 三条因旧构建产物已删、
>     无留底，按中文对应重写（`Add pricing` / `Input price` / `Output price`），恢复页面时如有原稿可覆盖。
>   - **整页隐藏（不删）**：新增开关 `src/lib/features.ts` 的 `GATEWAY_CONFIG_ENABLED = false`，一处控三处 ——
>     ① `lib/nav.ts` 的侧栏/手机抽屉入口 ② `views/Settings.vue` 那颗跳转按钮 ③ `router.ts` 守卫：
>     手敲 `#/gateway-config` 会被送回 `/dashboard`。页面组件、路由定义、i18n、`GET|PUT /config` **全部保留**
>     （懒加载 chunk 仍照常打包，恢复 = 把开关改 `true`）。
>   - 质量门：`vue-tsc` 0 错、`check:i18n` 中 667 / 英 667、`npm run build` 通过。
>   - 上线：**纯前端**，`npm run build` 覆盖 `frontend/dist`（dist 即线上目录），**未重启后端**（pid 2102891 未动、
>     网页会话不掉）。线上现役 **`index-71M4gDBC.js`** / `GatewayConfig-DBW2jA7a.js` /
>     `MainLayout-D8ZEF-hU.css`（旧 `index-CHZfJ5j7.js` = 19:2x 那版已 404）。公网复核：主页现在指向
>     `index-71M4gDBC.js`、`index.html` 仍 `no-cache`、CSP 未放宽。
>   - 隔离实例（8899；`GATEWAY_CONFIG` / `GATEWAY_USERS` / `GATEWAY_HISTORY` / `GATEWAY_FEEDBACK` 全指 `/tmp`，
>     生产 `data/` 只读）真浏览器实测：**侧栏 11 项、无「网关配置」**；设置页**无**那颗跳转按钮；
>     手敲 `#/gateway-config` **落回 `/dashboard`**。构建产物静态核对：定价块文案仍在（`addPrice` / `h_pricing` /
>     `输入价` / `输出价` / `list-edit` 均能在产物里找到）→ 功能只是藏起来、没被删。
>   - 收尾：隔离实例已 kill，两轮残留分别归档 `/root/.trash/2026-09-13-gw25-tweaks-cleanup/` 与
>     `/root/.trash/2026-09-13-gw26-hide-gwconfig-cleanup/`（各含 `MANIFEST.md`）。
>   - 备注：生产 `data/users.json` 在 19:29 有一次写入，**不是本次验证造成**（隔离实例的账号/配置/历史全走 `/tmp`），
>     看字段像线上侧的登录/建 Key 动作。
>
> - **追加三（同日 19:4x，老板「左侧菜单上下间距可以大一点」）**
>   - 上一版把行距压到 `7px`（行高 32px）是为了不出滚动条，老板觉得偏紧 → 改成**随窗口高度自适应**：
>     菜单项 `padding: clamp(7px, 1.4vh, 11px) 10px`、分隔线 `margin: clamp(7px, 1.2vh, 13px) 8px`
>     （下限 7px/32px 与上一版一致、上限 11px/40px 比最初的 9px/36px 更松）。
>   - 实测（隔离实例，视口 720px）：**行高 38.2px**、菜单内容 468px / 可用 609px → **无滚动条**；
>     推算"开始出滚动条"的窗口高从最初的 ~605px、上一版的 ~538px 降到 **~508px**。
>   - 上线：纯前端 `npm run build` 覆盖 `frontend/dist`（未重启后端，会话没掉）。线上现役
>     **`index-BV89HnWe.js`** / `MainLayout-BOSEGVwr.css`（`index-BeKWQQGn.js`、`MainLayout-8X3ca3Qa.css` 已 404）。
>   - 收尾：残留归档 `/root/.trash/2026-09-13-gw27-rail-spacing-cleanup/`（含 `MANIFEST.md`）。

> **第二十二轮（2026-09-13）意见反馈 / 用户反馈 + 侧栏底部清理 + 处理进度**
> 来源：老板三点 ——「侧栏菜单左下角那行去掉」「普通用户新增意见反馈，支持上传图片」
> 「管理员账号新增用户反馈菜单，内容是全部用户的反馈意见」；第二轮追加定稿 ——
> 「**管理员只能看没法提**，用户提完后可以查看处理进度，管理员处理反馈可以更新状态」。
>
> - **侧栏底部两行删除**：`SideNav.vue` 的 `.rail-foot`（B · 素白蓝紫 / v2.4.0 · 控制台）
>   整块移除，连带删掉只服务于它的 `version` prop 与 `MainLayout.vue` 里的 `version` ref
>   （`/health` 探针与顶栏连接小灯**保留**）。实测侧栏菜单到「设置」为止。
> - **两条菜单互斥，权限双向封闭**：
>   | 账号 | 菜单 | 说明 |
>   | --- | --- | --- |
>   | 普通账号 | 「意见反馈」（提 + 看自己的进度） | 无「用户管理」「用户反馈」 |
>   | 管理员 | 「用户反馈」（看全部 + 改进度） | **无「意见反馈」**，提交接口也 403 |
>   双向都**不只是 UI 约定**：`nav.ts` 不显示 + 路由守卫兜住手敲地址
>   （member 进 `/feedback-inbox` → 仪表盘；admin 进 `/feedback` → 收件箱），
>   服务端 `POST /feedback` 对 admin 直接 **403「管理员账号只处理反馈，不提交反馈」**。
> - **处理进度（三档状态机）**：`pending` 待处理（提交即此态）→ `doing` 处理中 → `done` 已处理，
>   外加一条**处理说明**（≤500 字，可选）。管理员在收件箱每条右侧点「处理」→ 选状态 + 写说明 → 保存；
>   提交人刷新「我提交过的」就能看到状态徽标（`.badge.o/p/g` 橙/蓝/绿）、说明（左侧主题色竖线块）
>   和「更新于 …」（追加项后这块由「处理记录」时间线取代，见下方）。
>   `status`/`note`/`updated` 落 `data/feedback.json`，
>   **重启不丢**；老数据缺 `status` 字段按 `pending` 归一化（不会因未知状态白屏）。
> - **撤回（只能趁「待处理」）**：提交人在「我提交过的」里，仅 `pending` 的单子右侧出现「撤回」，
>   走二次确认（撤回=真删：条目 + `data/feedback_media/` 下的图片文件一起删）。
>   **管理员一动它（处理中/已处理）就不能撤** —— 否则管理员白干，且已处理的单子从提交人列表里
>   凭空消失也不合理。权限三层都把着：前端只对 `pending` 渲染按钮、`DELETE /feedback/{id}`
>   校验「本人 + 待处理」、管理员也不能替谁撤（收件箱本就没有删除入口）。
>   拒绝口径：非本人 403「只能撤回自己提交的反馈」、已进流程 400「管理员已开始处理，这条不能撤回了」、
>   已撤过/不存在 404。前端遇到 400 会把服务端原话弹出来并**重拉列表**（可能刚好被管理员接管了）。
> - **存储**：正文 + 内嵌缩略图进 `data/feedback.json`，原图落 `data/feedback_media/<fid>-<i>.<ext>`
>   （列表页零额外请求预览，点开才取原图）。上限：3 张/条、单张解码后 ≤700KB、正文 2000 字、
>   **保留 500 条**（超限丢最旧并连带删其图片文件）、坏 JSON 自动备份重建、无主图片启动时清理。
>   图片类型按**解码后魔数**判定，不信任客户端 MIME。
> - **只管会话**：`POST /feedback` 只认网页会话，API Key 调用 403（与 `/history` 写接口同一口径）。
> - 前端新增 `lib/feedback.ts`、`Feedback.vue`（普通账号）、`FeedbackInbox.vue`（管理员，账号下拉
>   + 进度下拉 + 关键词 + 10 条/页 + 缩略图弹大图）、`FeedbackImages.vue`，nav/router/图标
>   （`bulb` / `inbox`）/i18n（中英 **527/527** 键集对称）/vite 代理 `/feedback`。
>   （追加项再新增 `FeedbackTimeline.vue` 与 `fbLog.*` 四条文案，中英仍对称。）
> - **处理记录（本轮追加：每个环节都留痕）**：老板「处理的每个环节都要记录和展示」——
>   每条反馈多一个 `history` 数组，**只增不改**，一条记录 = 谁（`by`）、什么时候（`ts`）、
>   从哪一档到哪一档（`from` → `to`）、留了什么说明（`note`）。
>   - `POST /feedback` 落第 1 条（`from` 为空串 = 「提交」，`by` = 提交人自己）；
>     `PATCH /feedback/{id}` 每成功一次追加 1 条（`by` = 操作的管理员账号）；
>     **被拒的请求（400/403/404）不留下记录**，记录只跟着成功变更走。
>   - 同一档位重复改（如「处理中 → 处理中」只补说明）也记一条，`from === to` 时前端不画「← 来源」箭头。
>   - 每条最多留 **50** 条（超出丢最旧，只影响展示）；`history` 随 `status`/`note`/`updated` 一起落
>     `data/feedback.json`，重启不丢。
>   - **老数据自动补记录**：本轮之前提交、没有 `history` 的条目，加载时按现有字段补
>     「提交 → 待处理」；若已推进过，再补一条「待处理 → 现状态」（当时是谁点的已不可考，`by` 留空）。
>   - 展示：新组件 `FeedbackTimeline.vue`，**提交人与管理员看的是同一串记录**（服务端原样下发），
>     竖线 + 状态色圆点 + 账号标签 + 时间，说明块沿用左侧主题色竖线；超过 **4** 条默认折叠成
>     最后 4 条（最新的处理动作最重要），带「展开全部 N 条 / 收起」。
>     「我提交过的」里原「更新于 …」的单行说明块被时间线取代（信息不再重复两处）。
> - 验证：隔离实例（8899 + vite dev 5199）**真浏览器跑通闭环** —— member 带图提交 →
>   admin 收件箱可见并弹大图 → 管理员改「处理中」+ 写说明 → member 端立刻看到状态与说明；
>   curl 另验：admin 提交 403、member 改状态 403、非法状态 400、不存在 id 404、未登录 401。
>   窄屏 390×844 实测（同源 iframe 探针）两页均**无横向溢出**，进度徽标与「处理」按钮不挤爆。
>   质量门：`vue-tsc` 0 错、`build` 通过（`check:svg` 前置）、后端单测 **146 passed / 0 failed**
>   （新增处理进度用例：状态非法 400 / 越权 403 / 落盘重载仍在 / 缺字段归一化；
>   处理记录用例：提交即有第 1 条且 `by` 是提交人 / 两次 PATCH 后 `to` 依次 pending→doing→done
>   且 `by` 是操作管理员 / 被拒请求不追加 / 老数据自动补记录 / 重载后记录仍在；
>   新增撤回用例：本人撤待处理 200 且图片文件被删 / 再撤 404 / 撤别人的 403 / 管理员替撤 403 /
>   处理中·已处理 400 / API Key 撤不动 403）。撤回部分在隔离实例复核过接口与真浏览器交互。
> - 上线（2026-09-13 18:4x，老板批准「补上说明后直接上线」）：`npm run build` → 线上现役
>   `index-b0LZHsJm.js` / `index-BCEpjaoj.css`（旧 `index-CDcUA_16.js` 已 404）；
>   `supervisorctl restart api-gateway:api-gateway_00` → pid **2055315**（127.0.0.1:8808）——
>   **本轮有后端改动，重启会踢掉所有网页会话，需重新登录**。（该次 pid 已被下方追加项的上线覆盖。）
>   追加项（老板 18:39「待处理的单子用户可以自己撤回」）代码与隔离实测完成后，老板 18:5x 批准上线：
>   再次 `npm run build` + `supervisorctl restart` → 现役 `index-9Whl_Fn4.js`（旧 `index-b0LZHsJm.js` 已 404）、
>   pid **2063248**（127.0.0.1:8808）。复核：`/web/` 引新 hash、`index-9Whl_Fn4.js`/`index-BCEpjaoj.css` 200、
>   线上产物含「撤回这条反馈？/ 撤回后这条反馈「…」会被删除 / 已撤回 / 管理员已开始处理的单子撤不了」、
>   `DELETE /feedback/{id}` 未登录 401（路由在）、6 条反馈路由全在、`/healthz` 200、CSP 与缓存头未变、
>   登录页正常渲染。**重启同样踢掉了所有网页会话，需重新登录。**
>   复核（全部实测）：公网 `/web/` 200 且引用新 hash、新 asset（含 `Feedback-*` / `FeedbackInbox-*`）全 200、
>   旧 index asset 404、`/healthz` 200、CSP 未放宽、`index.html` → `no-cache`、带 hash asset → `immutable`；
>   线上主 chunk 内含 `memberOnly` 守卫与 `feedback-inbox` 路由；线上产物含
>   「待处理 / 处理中 / 已处理 / 处理说明（提交人可见）/ 进度已更新 / 提交后在这里看处理进度」；
>   未登录打 `/feedback` 系列 5 个路由全部 401（**PATCH 也在**，证明新接口已随重启生效）。
> - 待老板刷新（`Ctrl/Cmd+Shift+R`）确认：侧栏底部两行没了、普通账号有「意见反馈」且能提图、
>   提完能看到进度、管理员只有「用户反馈」并能改进度。
>   追加项（老板「处理的每个环节都要记录和展示，改好后直接重启上线」）**已上线**：
>   `npm run build`（先确认 `frontend/public/` 无探针残留）→ 现役 `index-BTllhoyi.js`
>   （旧 `index-9Whl_Fn4.js` 已 404；`index-BCEpjaoj.css` 哈希未变，本轮无样式改动）；
>   `supervisorctl restart api-gateway:api-gateway_00` → pid **2073622**（`127.0.0.1:8808`）——
>   **有后端改动，重启踢掉了所有网页会话，需重新登录**。
>   复核（全部实测）：公网 `/web/` 200 且引用新 hash、`index-BTllhoyi.js` 与
>   `FeedbackTimeline-CfkAgUmj.js` / `Feedback-DsePxU4t.js` / `FeedbackInbox-D1R_k-8C.js` 全 200、
>   旧 `index-9Whl_Fn4.js` 404、`/healthz` 200、CSP 未放宽、`index.html` → `no-cache`；
>   线上产物含「处理记录 / 展开全部」；`PATCH /feedback/{id}` 与 `GET /feedback/mine` 未登录均 401
>   （路由已随重启生效）。**生产 `data/*` 全程只读**（并未往线上写测试反馈）。
>   收尾：隔离实例（8899 / vite dev 5199）已 kill，探针 `gwmv23.html` + 临时 vite 配置
>   `gwcheck23.vite.config.mts` 与 `/tmp/gw23` 已归档到
>   `/root/.trash/2026-09-13-fix22b-cleanup/`（含 `MANIFEST.md`），`frontend/public/` 已清空、
>   `dist` 无探针残留。

> **第二十一轮（2026-09-13）顶栏「铃铛」删除 + 主题下拉点不开（tooltip 套在 dropdown 触发器里）**
> 来源：老板 ——「右上角的通知图标是有功能的吗？…如果有用就保留，没用就去掉，然后主题点击没反应」。
>
> 两个都是真问题，先说结论：
> 1. **铃铛是死图标**：顶栏那个 `<button class="icon-btn"><Icon name="bell"/></button>`
>    **没有 `@click`**，唯一的行为是一句 tooltip「暂无新通知」——没有任何通知数据源。
>    按老板要求**删掉**（连同 `top.noNotify` 文案，中英各删 1 条，键集仍对称 482/482）。
>    顶栏控件从 4 个变 3 个：**主题 / 刷新 / 账号**。
>    `bell` 图标**保留**在 `Icon.vue` —— `ApiKeys.vue` 还在用它（密钥页提示）。
> 2. **主题点了没反应＝dropdown 触发器被 tooltip 包住了**。原写法：
>    ```html
>    <el-dropdown trigger="click" @command="onTheme">
>      <el-tooltip :content="t('theme.choose')"><button class="icon-btn">…</button></el-tooltip>
>      <template #dropdown>…</template>
>    </el-dropdown>
>    ```
>    Element Plus 的 `el-dropdown` 用 `ElOnlyChild` 给**默认槽的第一个节点**挂点击事件；
>    第一个节点是 `el-tooltip`（它自己又渲染一层包装），于是事件挂在包装层上，
>    真正被点的 `<button>` 收不到 → **菜单永远不开**。
>    实测证据（临时实例 + 真浏览器）：DOM 里 `.el-dropdown__popper` 存在且文案完整
>    （`A · 深空蓝 / B · 素白蓝紫 / C · 暖砂墨绿 / 跟随系统深色模式`），
>    但 `bounding_box()` 为 `None`（不可见）——点了等于没点。
>    **修法：tooltip 挪到 dropdown 外面**：
>    ```html
>    <el-tooltip :content="t('theme.choose')">
>      <el-dropdown trigger="click" @command="onTheme"> <button class="icon-btn">…</button> … </el-dropdown>
>    </el-tooltip>
>    ```
>    修完实测：点调色盘 → 菜单真的弹出（160×153 @ 顶栏下方）、
>    「A · 深空蓝」→ 全站变深色且侧栏标签同步为 `A · 深空蓝`，再点回 `B · 素白蓝紫` 正常；
>    tooltip「主题外观」悬停照常显示。
>    顺带**全库扫了一遍同类写法**（每个 `el-dropdown` 触发器的第一个元素）：只有这一处，无第二个。
>
> 窄屏回归（390×844 iframe 实测）：手机顶栏 2 个图标（菜单 / 主题），点主题仍在
> **B → C → A** 循环切换（本次实拍 B → C 生效）；抽屉底部 4 张主题卡布局正常
> （`x=12…240`、抽屉宽 252，`.dt-card` 各 51px，无溢出），当前主题卡高亮正确。
> 质量门：`vue-tsc` 0 错、`build` 通过、`check:svg` 通过（本轮**未动后端**，不涉后端单测）。
> 生产 `dist` 在本轮验证期间**未被覆盖**（构建先出到临时目录 `/tmp`，探针 html 也随构建产物一并归档）。
> - **上线（2026-09-13 18:2x，老板批准）**：`npm run build` → 线上现役 `index-CDcUA_16.js`
>   （旧 `index-C_MmjTC1.js` 已 404）。`index-BuS4X-hx.css` **哈希没变**（本轮没动样式，正常）；
>   `Chat-*` / `Videos-*` 的 JS 换了名但 CSS 没换 —— 它们 import 的 index chunk 文件名变了，
>   只是引用串跟着变（内容无关）。
>   **本轮只动前端，没有重启后端**：pid 1999293（`127.0.0.1:8808`）未动，网页会话不会被踢。
>   复核（全部实测）：公网 `/web/` 200 且引用新 hash、新 asset 全 200、旧 index/Chat asset 404、
>   `/healthz` 200、CSP 未放宽（`default-src 'self'` + `frame-ancestors 'none'` 照旧）、
>   `index.html` → `no-cache`、带 hash 的 asset → `immutable` 长缓存；
>   拉线上 `index-CDcUA_16.js` 核对：**「主题外观」在、「暂无新通知」为 0 次** —— 铃铛确实下线了。
>   待老板刷新（`Ctrl/Cmd+Shift+R`）确认：顶栏只剩 主题 / 刷新 / 账号，点调色盘能弹出主题菜单。

> **第十九轮（2026-09-13）日志筛选「不生效」真因 + 视频任务卡版式重写**
> 来源：老板两点 ——「视频的列表显示不协调，优化下」「日志筛选文字图像和视频不生效」。
>
> - **日志筛选「不生效」是两个独立真因，已用上一版构建（`frontend/dist` = 线上 `index-Gg1BF2WV.js`）
>   在临时实例上逐个复现**：
>   1. **类型胶囊与「失败」胶囊不是互斥的**（只 toggle 自己那个 state）。点过「失败」之后
>      `result=fail` 一直亮着，再点「图像/视频」就叠加成「类型=视频 + 结果=失败」→ 必然 0 条；
>      胶囊上没有计数、空列表也没有任何提示，表现就是「点了没反应，筛选坏了」。
>      实测（旧构建）：点[视频] 1 行 → 再点[失败] 0 行 → 失败亮着时点[对话] 仍是 0 行。
>      修复：**互斥**（点类型清「失败」，点「失败」清类型；要叠加仍可用「结果」下拉）
>      + 每个胶囊带**条数**（全部/对话/图像/视频/失败各几条第 0.5 秒就知道）
>      + 空结果提示写明「已加载 {n} 条」并给「清空筛选」按钮。
>   2. **关键词搜索的 `appliedKw` 是普通 `let`，不是 `ref`** —— 不参与响应式追踪，
>      `filtered` 这个 computed 不会重算：点「搜索」后列表纹丝不动，要等下一次自动刷新
>      （5s 一轮）才顺带把关键词应用上，一会儿又变，看着就是「文字筛选不生效」。
>      修复：改 `ref` + **边打边筛**（`watch(kw)`；200 条以内开销可忽略），搜索按钮/回车保留。
> - **类型归类改由服务端给权威 `kind`**：`_access.add` 全部 14 处调用点带 `kind`
>   （`chat` / `image` / `video` / `media`），前端不再靠 `path` 子串猜（端点一改名就静默失效）；
>   旧条目没有 `kind` 时仍回落路径判断。日志行 Type 徽标同理。
> - **视频任务卡版式重写**（原版「不协调」的来源）：旧卡是「小缩略图 + 5 行 key/value +
>   折叠栏 + 独占一行的按钮」= 228px 高，缩略图下方空出 140px；状态还在胶囊（上游英文
>   `completed`）和正文（「已生成」）里说了两遍。现在：**左 16:9 缩略图 + 右三行紧凑信息** ——
>   ① 标题（2 行截断）+ 状态胶囊（只留 `已生成` / `生成失败` / `生成中…` 短词）
>   ② 摘要行（服务 · 耗时/等待 · 场景；失败时整行变红显示失败原因，原来是藏在折叠里的）
>   ③ 底部任务号 + `下载视频` / `原始返回` 按钮（原始返回由 `el-collapse` 改为内联展开）。
>   进度条只在「生成中」出现（完成后那条满格绿条纯粹是根分隔线），卡片 228px → **112~140px**，
>   缩略图 148×84 与视频比例一致，垂直居中不再留空。
> - **窄屏（≤768px）**：缩略图改为**撑满卡片高度**（108 宽）+ `object-fit: cover`，
>   与图像页缩略图口径一致；不再是一张 61px 高的小图、上下各空 45px。
> - 验证：日志筛选在临时实例逐项实测（全部 6 / 对话 3 / 图像 2 / 视频 1 / 失败 1，点空关键词出
>   提示并能一键清空）；视频卡桌面与 390 宽实测（7 张卡：常规 112/119px、长标题 140px、
>   窄屏 151~175px，无横向溢出）；`vue-tsc` 0 错、`vite build` 通过、后端单测 **137 passed / 0 failed**。
> - 上线（2026-09-13 17:44，老板批准）：`npm run build` → 线上现役
>   `index-Yy0kGuVA.js` / `index-DKyqf84q.css` / `Logs-N__ABTKT.js` / `Videos-Bzh1nuOL.js` + `Videos-BVOcHXwL.css`；
>   `supervisorctl restart api-gateway:api-gateway_00` → pid **1999293**（127.0.0.1:8808）。
>   复核：公网 `/web/` 200 且引用新 hash、新旧 asset 分别 200 / 404、`/healthz` 200、CSP 头未放宽；
>   拉线上产物实测含胶囊计数与空结果提示（`Logs`）、新版卡片结构（`Videos`）与窄屏缩略拉伸规则（`Videos` CSS）。
>   注意：**重启会踢掉所有网页会话，需重新登录**。

## 测试

```bash
# 单元测试（mock 供应商，不依赖真实网络，覆盖配置/路由/熔断/适配器/端点）
python tests/test_gateway.py
# 或
pytest tests/ -q
```

全量网关冒烟测试见 `smoke_test.py`（需网关已启动 + 真实 key）。

```bash
# 冒烟：健康 / 模型列表 / chat / batch / 错误格式（网关未起或无 key 时优雅 SKIP）
python smoke_test.py --url http://127.0.0.1:8808

# 活体检查：真实上游（Agnes / 火山方舟）跑 14 项端到端行为校验，含路由 fallback、
# 流式 SSE、413、CORS 预检、图像生成等
python live_check.py --url http://127.0.0.1:8808
```

> 当前状态：单元测试 **137 passed / 0 failed**（`python tests/test_gateway.py`，不需要 pytest）；冒烟 5/5 PASS；活体检查 14 PASS / 0 FAIL / 0 WARN。

## 备注

- 三家均为 OpenAI 兼容协议，统一 `OpenAICompatAdapter`，通过 `base_url` + `media` 区分
- Agnes 图像/视频模型 ID 用**点号**（`2.5`），不是 `25`
- 配置文件路径：`data/gateway.yaml`（唯一配置；`GATEWAY_CONFIG` 可覆盖）
- `hot_reload: true` 时改 yaml 自动重载（默认开）
- 限流为进程内令牌桶（per-key/per-route/per-provider QPS），高并发生产建议前置 Nginx 或加 Redis
- 指标/日志为进程内内存态，重启清零；生产建议接 Prometheus（`/metrics/prometheus`）+ 文件持久化
- Windows 无 make 时可手动执行 Makefile 里的对应命令
