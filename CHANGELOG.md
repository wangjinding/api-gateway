# CHANGELOG

按轮次记录 api-gateway 的修复与优化（README 侧重能力/配置说明，这里侧重每轮的改动、排查、验证与上线）。

---

## 2026-09-16 · 新增智谱 GLM（open.bigmodel.cn）供应商

**一句话**：接入智谱开放平台作为新的 OpenAI 兼容供应商，收录 6 个 Flash 模型（3 文本 + 3 视觉），密钥走 `data/.env` 的 `ZHIPU_API_KEY`，真图实测确认 3 个 V 型号支持图像输入。

### 供应商接入
- `data/gateway.yaml` 新增 `providers.bigmodel`：
  - `base_url: https://open.bigmodel.cn/api/paas/v4`（适配器自动拼 `/chat/completions`）
  - `auth.token_env: ZHIPU_API_KEY`（明文 key 不进 yaml，统一放 `data/.env`，chmod 600）
  - 媒体端点 `media: {}`（智谱无图像/视频生成端点）
  - 模型 6 个：`GLM-4.7-Flash`、`GLM-4-Flash-250414`、`GLM-Z1-Flash`、`GLM-4.6V-Flash`、`GLM-4V-Flash`、`GLM-4.1V-Thinking-Flash`，其中 3 个 V 型号标 `vision: true`
- `data/.env` 追加 `ZHIPU_API_KEY=<智谱 Key>`（49 位，未入版本库）。
- 前端 `Settings.vue` 的「对话默认模型」静态清单 `TEXT_MODELS` 追加这 6 个模型；Models 页 / `/v1/models` 本身是动态的，重启后自动出现。

### 排查与实测（重点）
- 首把 Key `cdfb6b5e...` 直接裸调 6 模型一度全 **401「令牌已过期」** → 误判 Key 无效。后用官方示例格式 + `glm-5.3` 重打是 **429「余额不足」**（说明 Key 认证是通的）→ 回到 6 模型官方格式再打：5 个 200，`GLM-4.7-Flash` 偶发 **429「访问量过大」(1305，瞬时过载，模型存在)**。**结论：Key 有效，之前 401 是上游瞬时过载造成的误报。**
- 视觉探针（1×1 红图）被智谱拒 **400 code 1210「图片输入格式/解析错误」** —— 是**图太小**的问题，非格式问题。换 200×200 标准红图后 data-URI 与外链 URL 均识别：`GLM-4V-Flash`、`GLM-4.1V-Thinking-Flash` 认「红色」✅；`GLM-4.6V-Flash` 全程遇上游过载(1305)未取到视觉结果，型号带 V 故仍标 `vision: true`（如后续实测不一致可再改）。
- 端到端：用生产同一适配器 `OpenAICompatAdapter`（真实 `base_url` + `Bearer` 解析 token）直连智谱——文本 `GLM-4-Flash-250414` ✅、推理 `GLM-Z1-Flash` ✅（返回含 thinking）、视觉 `GLM-4V-Flash`（data-URI 图）✅ 全部 HTTP 200。

### 上线
- 配置层 `hot_reload` 会自动热更 gateway.yaml；但 **ZHIPU_API_KEY 是新增环境变量，需重启进程才加载** → 待老板批准后重启，`/v1/models` 由 6 → 12 个文本模型、Models 页自动多出智谱 6 个。
- 单测：修正 `test_load_config_providers`（供应商集合加入 `bigmodel` + 智谱 6 模型/V 标记断言），**154 passed / 1 failed**（仅剩 feedback 无关遗留）。

---

## 2026-09-15 · 新增 OpenCode Zen（免费档）供应商

**一句话**：把 OpenCode Zen 作为第 4 个供应商接入，只收录文档定价页列出的**免费模型**。打通后发现免费档比较刁钻：既被 Cloudflare 拦默认 UA，又强制要求每个请求带独立会话头——两条都已在适配器层解决，实测全部 200。

### 背景 / 需求
老板要求查看 [OpenCode Zen 文档](https://opencode.ai/docs/zh-cn/zen#pricing)，接入该供应商，**模型只要文档里免费的模型**，并提供一把 API Key。

### 上游真相（重点！）
文档定价页的"免费模型"共有 6 个。**直接以"Bearer key + 标准请求"裸调全部失败**，逐一实测定位（带浏览器 UA 后才能看到模型层错误）：

| 免费模型 id | 裸调结果 | 根因 | 处理 |
|---|---|---|---|
| `big-pickle` | 401 `Model is disabled` | 隐身模型，未对本工作区/Key 开放 | ❌ 排除 |
| `mimo-v2.5-free` | 400 `MissingSessionID` | 免费档要求每个请求带会话 id | ✅ 收录 |
| `ling-3.0-flash-fin-free` | 400 `MissingSessionID` | 同上 | ✅ 收录 |
| `nemotron-3-ultra-free` | 400 `MissingSessionID` | 同上 | ✅ 收录 |
| `nemotron-3.5-lightning-free` | 400 `MissingSessionID` | 同上 | ✅ 收录 |
| `muse-spark-1.3-contributor-free` | 400 `MissingSessionID` | 且走 `/responses` 端点（本适配器是 `chat/completions` 调不了）+ contributor 档会用你的提示词/补全训练 Meta 模型 | ❌ 排除 |

**根因一（Cloudflare）**：裸调用默认 `urllib` UA 报 `403 error code: 1010`（基于客户端签名的访问被禁）。改成浏览器 UA 后请求才抵达模型层。网关默认 `python-httpx` UA 大概率同样被拦 → 必须在请求头带浏览器 `User-Agent`。

**根因二（会话标识）**：通过 [DeepSeek Harness 生产集成讨论帖](https://github.com/deepseek-ai/deepseek-harness/discussions/5495) 确认——所有打到 `opencode.ai` 的请求都要带 **`x-opencode-session`** 请求头（会话 id，UUID 即可；同一次多轮保持稳定会走路由亲和性）。不带 → `400 MissingSessionID`。带上后 4 个免费模型全部可用。

**Key 与计费**：该 Key 有效（能通过鉴权、能识别到 workspace），但账号**未绑定支付方式** → 付费模型返回 `CreditsError: No payment method`。因此当前这套 Key 下能用的只有免费档（且经会话头 + 浏览器 UA 后确实能用）。

### 改动明细
- **`app/adapters/openai_compat.py`**（后端代码改动，需重启生效）
  - 新增常量 `OPENCODE_SESSION_SENTINEL = "@per-request-uuid@"`。
  - 新增 `_call_headers()`：若 `extra_headers` 里 `x-opencode-session` 为哨兵值，则**每次 `chat` / `chat_stream` 调用现生成一个新 UUID** 再发请求；否则原样返回静态头（其它供应商零行为变化）。
  - 目的：所有用户/会话共用一个固定 session 会互相踩 OpenCode 的限流与路由亲和，必须按请求/会话隔离。
  - `chat()` 与 `chat_stream()` 的请求头由 `self._headers` 改为 `self._call_headers()`。
- **`data/gateway.yaml`**
  - 新增 `opencode` 供应商：`name: OpenCode Zen`、`base_url: https://opencode.ai/zen/v1`、`token_env: OPENCODE_API_KEY`。
  - `extra_headers`：`User-Agent`（浏览器 UA，过 Cloudflare）+ `x-opencode-session: "@per-request-uuid@"`（哨兵）。
  - `media: {}`（仅文本，无图像/视频）。
  - `models`：4 个免费模型（`mimo-v2.5-free` / `ling-3.0-flash-fin-free` / `nemotron-3-ultra-free` / `nemotron-3.5-lightning-free`）。
  - `pricing.per_model`：新增这 4 个，`input: 0, output: 0`（免费）。
  - 顶部注释供应商清单补 `OpenCode Zen（免费档）`。
- **`data/.env`**：新增 `OPENCODE_API_KEY=<sk-...>`，权限 0600（保留原行、仅追加）。key 明文不进 yaml、不入版本库。
- **`tests/test_gateway.py`**：`test_load_config_providers` 断言更新为 4 家，并新增守卫断言（opencode.base_url / 浏览器 UA / 哨兵头 / 4 个模型集），防以后误改。
- **`README.md`**：顶部供应商清单、供应商表格、`openai_compat.py` 架构注释、OpenCode 两条硬性要求说明。

### 验证（非编造，实录）
- 上游直打 `https://opencode.ai/zen/v1/chat/completions`（**浏览器 UA + 每请求新 UUID 会话头**）：
  - 4 个免费模型**非流式全部 200**，`nemotron-3.5-lightning-free` 出真实中文回复 + `usage.reasoning_tokens`（推理模型先吐推理再吐内容）。
  - **流式全部 200**：`content-type: text/event-stream`，`nemotron-3.5-lightning-free` 收了 123 个 chunk 并正常收到 `[DONE]`。
  - `big-pickle` 反复 401 `Model is disabled` → 确认本 Key 未开放，排除。
- 适配器哨兵逻辑单测：哨兵值 → 每次新 UUID（36 位）；普通静态值 → 原样保留。
- 全量单测 `python tests/test_gateway.py`：**153 passed, 2 failed**。
  - 涉及的 provider 相关用例通过；余下 2 个失败为历史遗留、与本次无关：
    - `test_load_config_routes`：断言 `wjd.models == 6`，但 GLM-5.3-Flash 移除后 `wjd` 链是 5 个（旧断言未同步）。
    - `test_feedback_withdraw_only_when_pending`：feedback 撤回逻辑，非本次改动范围。

### 部署
- `supervisorctl restart api-gateway:api-gateway_00`。
- 重启前验证文件已就位：yaml `safe_load` 通过、`providers = [agnes, b_ai, volc_engine, opencode]`、opencode 4 个模型。
- 上线后校验：新 pid 61457、healthz `{"status":"ok"}`、`/v1/models` 200 且含 opencode 4 个免费模型、无凭证/错 key 仍 401、真实 chat 调用 `provider=opencode` 返回 200。

### 待办 / 已知
- `big-pickle` 与付费模型都因账号条件（Model disabled / 未绑支付方式）不可用，等以后条件变化再按需补录。
- 免费档是否偶尔有速率/额度上限：OpenCode 注明"目前免费提供，团队在收集反馈"，可能随时变化；若某模型开始报错，在 Models 页一键测速即可发现。
- 本次未把 OpenCode 加进 `wjd` 兜底路由，先在 **Models 页 + 直连模型 id** 可用；要进 `wjd` 需老板另行确认。

### 视觉支持实测（2026-09-15，真图判定）
发一张「青色背景 + 品红五角星」的答案唯一测试图，走 OpenAI 多模态 `image_url` 格式（带浏览器 UA + `x-opencode-session`）实测：
- `ling-3.0-flash-fin-free` → **❌ 无视觉**：上游 `400 ... [404] No endpoints found that support image input`。
- `nemotron-3-ultra-free` → **❌ 无视觉**：同上。
- `nemotron-3.5-lightning-free` → **❌ 无视觉**：同上。
- `mimo-v2.5-free` → **⚠️ 未判定**：连纯文本都持续 `429 FreeUsageLimitError`（免费额度是小时/天级窗口，等 7 分钟未回血），够不到读图那步。强先验：MiMo-V2.5 家族在 b.ai 早前实测即 `Vision is disabled`，故基本可断定无视觉。
- 结论：4 个 OpenCode 免费模型**均无视觉**（3 个实测坐实、1 个受限于限流未坐实但先验一致）。gateway.yaml 里这 4 个都只标 `type: text`、**未加 `vision:true`**，与实测一致，无需改动；Models 页也不会给它们显示 👁 图标。

---

## 2026-09-15 · 一键测速报错展示优化（Models 页）

**问题**：Models 页「一键测速」某个模型失败时，错误内容被塞进硬编码的窄 `pill`（`.st-lat`）里，长错误（无空格 JSON / 上游错误串）不换行、到卡片边缘就被切掉 → 报错显示不全。

**改动**（`frontend/src/views/Models.vue`，纯前端，不重启后端）：
- 模板：每个测速行 `st-row` 外包一层 `st-item`；当某模型测速失败时，在行下方新增一整行 `st-err`（`ban` 图标 + 完整错误文本）全宽展示。
- 逻辑：新增 `latErr(id)` 返回完整错误串（用于 st-err 行）；`latText(id)` 失败时只返回短的「失败」标记（长错误不再塞窄 pill）。
- 样式：`.st-err` 用 `--err` 文字 + `--err-soft` 底、圆角块；`overflow-wrap: anywhere` + `word-break: break-word` 让长 JSON/无空格错误串自动换行不溢出；`.st-item + .st-item` 虚线分隔。

**验证**：`npm run check:svg && npm run check:i18n && vite build` 通过；`vue-tsc --noEmit` 无错；新产物 `index-C_e1f2ok.js` / `Models-C-QAxhv1.js` 线上 200、旧 hash 404。后端挂载 `frontend/dist`，build 即可见，pid 未动、会话不掉。

### 附带修复：后端空错误兜底
真实实测中发现 nemotron-3-ultra-free / mimo-v2.5-free 偶尔 429 限流，部分时候上游只返回空 body 导致 `error = "{}"`（`str({})`），既丑又无信息。`app/main.py` 的 `_upstream_detail` 修复：当 `j` 无 `message/error/detail` 且 `str(j) in ("{}","","null","None")` 时返回空串，前端 `row.error` 兜底显示「调用失败」而不是 `{}`。重启后生效（`pid 121772`）。

### 截图留档
- `docs/st-err-speedtest-error-display.png`：真实浏览器实拍，两个 OpenCode 免费模型同时报错，`st-err` 红块完整展示 429 限流错误信息（含上游文档链接）——验证"显示不全"已修复。

---

## 2026-09-15 · 一键测速改为「逐个测、测一个显示一个」

**需求**：一键测速原来是并发/一次性返回全部结果、最后一起显示；改成**串行逐个测，每测完一个立刻显示那一行，再测下一个**。

**改动**
- **`app/main.py` `/v1/models/speedtest`**：新增可选 `body.model`。传了就只测该模型、返回单条（不排序）；乱传/非文本模型返回一条兜底错误行「模型不存在或非文本模型」。不传仍是一次性全测（保留兼容）。
- **`frontend/src/views/Models.vue`**：`runSpeedtest()` 改为按列表顺序 `for` 循环，逐个 `POST {model:id}`，每测完一个 `latByModel.value = {...latByModel.value, [id]: row}` 响应式即时刷新该行；**单个模型异常不中断整轮**（catch 后标失败继续下一个）。新增 `latDone/latTotal`，按钮测速中显示「测速中 n/total」进度。`latText()` 改为：已测行显示 ms/失败、测速中未轮到的显示 `…`、空闲显示 `—`。
  - 注：`textModelRows` 本就按供应商分组顺序、不随延迟重排，所以逐个测时行不会跳动，无需额外锁序。
- **i18n**：`models.testingProgress` 中英各加一条（`测速中 {n}/{total}` / `Testing {n}/{total}`），保持对称。

**为什么串行更好**：并发一次性测会把 OpenCode 免费档瞬间打满触发 429；串行逐个测天然错峰。

**验证**：后端单测 153 passed（2 个历史遗留不变）；前端 build + `vue-tsc` 通过；新产物 `index-D81T1xmr.js` / `Models-DpOHvPHF.js` 线上 200、旧 hash 404；`supervisorctl restart` → pid 187860、healthz ok。接口实测 `{model:"hy3"}` 只返回 1 条、乱传 model 有兜底错误行。浏览器实拍：点测速后按钮「测速中 1/9」、第一行已出 `748 ms`、其余 8 行 `…` 排队，33s 全部完成——逐个填充生效。
- 截图留档：`docs/speedtest-sequential-progress.png`

### 追加：按钮去数字 + 当前行加载图标
老板反馈测速按钮不要出现 `n/total` 数字，改成「轮到哪个模型、就在那一行显示加载图标」。
- 按钮：`runSpeedtest` 期间文案改 `t('models.testingShort')`（「测速中…」），去掉 `latDone/latTotal` 数字；`el-button :loading` 自带转圈。
- 当前行：新增 `latCurrentId`，循环里每测一个置为该 id、整轮结束清空；模板延迟格里 `v-if="m.id===latCurrentId"` 显示 CSS spinner（`.st-spin` 圆环旋转，`prefers-reduced-motion` 降速），否则显 `latText`。`latText` 未轮到/空闲统一 `—`（去掉原 `…`）。
- i18n：删 `testingProgress`、加 `testingShort`（中英）。
- 纯前端，build 覆盖 dist 即生效、未重启后端。新产物 `index-CZxVI3oL.js` / `Models-C-o3SZgZ.js`，线上 200、旧 hash 404。
- 浏览器实拍：按钮「测速中…」无数字、`agnes` 已出 `929 ms`、`qwen3.8-flash` 行内转圈、其余 `—`。截图 `docs/speedtest-current-row-spinner.png`。

### 追加：失败行「重新测速」按钮
老板要失败的模型旁边可点击单独重测。
- `Models.vue` 新增 `retestOne(id)`：只测这一个模型（`POST {model:id}`），复用当前行加载图标；`latTesting || latCurrentId` 时禁用/忽略，避免与整轮或另一单测打架；单模型异常也不抛、写回失败行。
- 模板：`st-err` 错误块右侧加 `.st-retry` 胶囊按钮（`refresh` 图标 + 「重新测速」），`v-if="latErr(m.id)"` 只在失败行出现；错误文本用 `.st-err-txt{flex:1}` 占满、按钮靠右。
- i18n：`models.retry`（`重新测速` / `Retry`）中英。
- 纯前端，build 覆盖 dist 即生效、未重启后端。新产物 `index-nCRwIFRg.js` / `Models-CGpanPte.js`，线上 200、旧 hash 404。
- 浏览器实拍：整轮后 4 个失败行各带「重新测速」；点其中一个 → 仅该行转圈（`.st-spin`=1）、按钮未变「测速中…」（未误触发整轮）、15s 后该行结果刷新。截图 `docs/speedtest-per-row-retry.png`。

### 追加：每行常驻「单独测速」按钮（取代仅失败行重试）
老板要常态下（没跑整轮时）每个模型行右侧都有测速按钮，可单独测任意一个模型。
- 把上一版只在 `st-err` 里的「重新测速」按钮改为**每个 `st-row` 右侧常驻一个 ⚡ 图标按钮** `.st-one`（`bolt` 图标，`title/aria-label=单独测速`），点击调 `retestOne(id)` 只测该模型；`latTesting || latCurrentId` 时禁用（一次只允许一个测速动作，防与整轮/另一单测打架）。
- `st-err` 移除旧 `.st-retry` 按钮，只留错误文本；删除 `.st-retry` 样式，新增 `.st-one` 图标按钮样式（hover 用 `--primary`/`--primary-soft`）。
- i18n：删 `models.retry`、加 `models.testOne`（`单独测速` / `Test this model`）中英。
- 纯前端，未重启后端。新产物 `index-BYzyfwi3.js` / `Models-CJ5mchi4.js`，线上 200、旧 hash 404。
- 浏览器实拍：9 个文本模型行 = 9 个 `.st-one` 按钮、无 `.st-retry` 残留；点第一行 `agnes-3.0-flash` 只它出 `754 ms`、其余仍 `—`、未误触发整轮。截图 `docs/speedtest-per-row-test-button.png`。

---

## 2026-09-15 · 移除 OpenCode 的 mimo-v2.5-free 模型

**需求**：老板要求把 `mimo-v2.5-free` 从 OpenCode 供应商去掉，相关配置与文档一并清理。

**改动**
- **`data/gateway.yaml`**：删 `providers.opencode.models` 里的 `mimo-v2.5-free` 条目、删 `pricing.per_model` 的 `mimo-v2.5-free`；opencode 注释「这 4 个」改「这 3 个」，并在排除/已移除清单加一行说明（此前实测可 200，但免费档常 429 限流，按要求移除）。opencode 现 3 个免费模型：`ling-3.0-flash-fin-free` / `nemotron-3-ultra-free` / `nemotron-3.5-lightning-free`。
- **`README.md`**：供应商表格 OpenCode 行的模型清单去掉 `mimo-v2.5-free`。
- **`tests/test_gateway.py`**：`test_load_config_providers` 里 opencode 模型集断言去掉 `mimo-v2.5-free`。
- 注意：只删 OpenCode 的 `mimo-v2.5-free`，**b.ai 的 `mimo-v2.5` 保留不动**。

**验证**：`yaml.safe_load` 后 opencode models=3、pricing 无该项；全量单测 153 passed（2 个历史遗留不变）。**纯配置改动，hot_reload 自动生效、未重启**——线上 `/v1/models` 由 12 → 11、`mimo-v2.5-free` 已消失、`mimo-v2.5`(b.ai) 仍在。CHANGELOG 保留此前收录/实测该模型的历史条目（真实记录），此处仅追加移除说明。

---

## 2026-09-15 · 排查「opencode 模型大概率失败」+ 测速探针加 429/5xx 重试

**排查结论（非网关 bug）**：翻网关日志 `opencode.ai/zen/v1/chat/completions` 上游响应 = 30×200 / 14×429 / 3×503，且有明显「首个 429 → 隔 1~2s → 200」模式。**根因是 OpenCode 免费档对共享 key 的激进限流(429) + 偶发 503**。真实 chat 直连有 `retry:3` 兜底（退避 0.3/0.6s）多能扛过；但**一键测速是单次探针、不重试**，正好撞高频首个 429 → Models 页显示一片"失败"。反复点测速又消耗同一把免费 key 的额度 → 越点越限流。

**修复（老板选定的方案 1）**：让测速探针也重试。
- `app/main.py` 新增 `_speedtest_retryable(e)`：仅上游 **429 / 5xx** 判为可重试；4xx（模型不存在/鉴权/参数）与超时**不重试**（前者无意义、后者拖慢整轮）。
- `/v1/models/speedtest` 探针由单次改为 `for attempt in range(1,4)` 最多 3 次、退避 `0.3*attempt`（与真实链路一致），成功即 break；`latency_ms` 记整个重试过程总耗时（用户真实等待）。

**验证**：`_speedtest_retryable` 判定单测全对（429/503/500→重试、400/401/404/超时→不重试）；全量单测 153 passed；`supervisorctl restart` → pid 249194、healthz ok。日志实测到重试生效：`ling-3.0-flash-fin-free` 一次 `503→503→200`（attempt 1/3、2/3）被救回。

**遗留发现（待老板定）**：`nemotron-3-ultra-free` 仍测速失败，但**已不是 429**——直连实测它响应要 **88.4 秒**（"Ultra" 大推理模型 + 免费档，极慢），远超探针 15s 超时；`nemotron-3.5-lightning`(2.4s)、`ling`(1.1s) 正常。可选：①调大探针超时（但 88s 会让整轮测速极慢、意义不大）；②像 mimo-v2.5-free 一样把 nemotron-3-ultra-free 移除（88s 不实用）；③保留（测速如实显示它太慢，真实 chat 靠 120s server timeout 仍可用但要等 ~88s）。

---

## 2026-09-15 · 整体移除 OpenCode Zen 供应商

**需求**：老板决定把 OpenCode Zen 供应商整个去掉，相关配置、代码、文档一并清理。

**移除（opencode 专属）**
- **`data/gateway.yaml`**：删 `providers.opencode` 整块（base_url / `token_env: OPENCODE_API_KEY` / UA+哨兵 `extra_headers` / 3 个免费模型）+ `pricing.per_model` 里 3 条 opencode 免费模型 + 顶部注释供应商清单去 OpenCode。
- **`app/adapters/openai_compat.py`**：删 `OPENCODE_SESSION_SENTINEL` 常量、`_call_headers()` 方法、`import uuid`；`chat()`/`chat_stream()` 请求头由 `self._call_headers()` 回滚为 `self._headers`（适配器回到"纯静态头"）。
- **`data/.env`**：删 `OPENCODE_API_KEY` 行（保留 AGNES/B_AI/VOLC 三把，权限 0600）。
- **`README.md`**：顶部"四个供应商"→"三个"、架构注释"四家通用+opencode 会话头"→"三家通用+媒体端点"、供应商表格 OpenCode 行删除、OpenCode 两条硬性要求 blockquote 删除。
- **`tests/test_gateway.py`**：`test_load_config_providers` 回退到 3 家、删 opencode 相关断言。

**保留（通用改进，非 opencode 专属）**：测速探针 429/5xx 重试（`_speedtest_retryable` + 重试循环）与 `_upstream_detail` 空 body→「调用失败」兜底 —— 对 b.ai/agnes 等偶发限流同样受益，仅把注释里的 "OpenCode" 字样改通用。前端 Models 页动态读 `/v1/models`，opencode 行自动消失，无需改前端。

**验证**：`yaml.safe_load` 后 providers=[agnes,b_ai,volc_engine]、文本模型回到 5 个；`openai_compat` 已无 `_call_headers`/`OPENCODE_SESSION_SENTINEL`；`import app.main` OK；源码/配置/README/测试 grep `opencode` 清零（CHANGELOG 历史条目保留）；全量单测 153 passed。代码有改动 → `supervisorctl restart` → pid 254282、healthz ok、线上 `/v1/models` = 8（无 opencode）。CHANGELOG 此前收录/排查 opencode 的历史条目全部保留（真实记录）。

---

## 2026-09-15 · 移除 b.ai 的 mimo-v2.5 模型

**需求**：老板要求把 b.ai 供应商下的 `mimo-v2.5` 也去掉，相关配置、代码、文档一并清理。（注：与上一轮已删的 OpenCode `mimo-v2.5-free` 是两个不同模型。）

**改动**
- **`data/gateway.yaml`**：删 `providers.b_ai.models` 里的 `mimo-v2.5` 条目、删 `pricing.per_model` 的 `mimo-v2.5`、并从 **`routes.wjd` 兜底链**移除第 4 项 `mimo-v2.5`（`hy3` 由「5)」改「4)」重编号）。wjd 链现 `[ark-code-latest, agnes-3.0-flash, qwen3.8-flash, hy3]`。
- **`app/main.py`**：测速探针注释里 `b.ai(qwen3.8-flash/mimo-v2.5/hy3)` 去掉 `mimo-v2.5`（`max_tokens>2` 约束对 qwen3.8-flash/hy3 仍成立，逻辑不变）。
- **`README.md`**：供应商表格 b.ai 行模型清单去掉 `mimo-v2.5`；路由链示例同步去掉并重编号。
- **`tests/test_gateway.py`**：`test_load_config_routes` 断言 `wjd.models` 数量由历史遗留的 `== 6` 修正为实际 `== 4`（该用例此前一直 FAIL，本次顺带修好）。

**验证**：`yaml.safe_load` 后 b_ai models=[qwen3.8-flash,hy3]、wjd 链 4 项、pricing 无 mimo-v2.5；源码/配置/README/测试 grep `mimo-v2.5` 清零（CHANGELOG 历史保留）；全量单测 **154 passed / 1 failed**（routes 用例转绿，仅剩 feedback 无关遗留）。**纯配置改动，hot_reload 自动生效、未重启**——线上 `/v1/models` 由 8 → 7、`mimo-v2.5` 已消失。

---

## 2026-09-15 · 初始化 Git 仓库并推送到 CNB（cnb.cool）

- 项目此前**不是 git 仓库**：`git init` 建库、设提交身份、首次提交（**91 个源码文件**）。
- **`.gitignore` 补充**忽略 `node_modules/`、`frontend/node_modules/`、`frontend/dist/`、`dist/`、`*.egg-info/`、`.DS_Store` —— 否则 `git add -A` 会把 **15342 个 node_modules 文件**塞进仓库。
- **推送前安全审计**：确认 `.env`、`data/users.json`、`data/history.json`、`data/feedback.json`、`data/feedback_media/` 全部被忽略、无任何密钥/隐私入库（仅无密钥的 `.env.example` 模板随库）。
- **远程**：CNB `https://cnb.cool/Jidekin/api-gateway`，分支 `master`，首推成功（远端 `HEAD=6cc65ff` 与本地一致）。`origin` 配为不含 token 的干净地址，**令牌不写入 `.git/config`**；后续推送用 `git push https://cnb:<token>@cnb.cool/Jidekin/api-gateway.git master`。