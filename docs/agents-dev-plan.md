# 智能体（Agent）开发计划 · v1.6

> 路线 A：**界面在网关，引擎是 QwenPaw**
> 状态：**计划中，未开工**（等本计划确认）
> 版本：v1.6（2026-09-29）｜上一版 v1.5

---

## 0. 已确认的决策（老板 2026-09-29 拍板）

| # | 决策 | 结论 |
|---|---|---|
| ① | 引擎用哪个 profile | **新建一个专用 profile** `ai-gateway`（不复用 `default`，也不复用现有 3 个） |
| ② | 智能体的系统提示词归谁管 | **归 QwenPaw 管**。网关不存提示词，配置页只读展示 + 说明去哪儿改 |
| ③ | 会话存哪边 | **网关存**（`agent_sessions.json`），QwenPaw 只用 `session_id` 续上下文 |
| ④ | 建几个 profile | **只建 `ai-gateway` 一个**（不为每个智能体建 profile） |
| ⑤ | 智能体 ↔ profile 关系 | **一对多**：多个智能体共用 `ai-gateway` 这一个 profile |
| ⑥ | 别名链自指校验 + 递归深度上限 | **加**（无需再确认） |
| ⑦ | member 裁剪规则 | 按建议：member 只见 `display_name/description/status`（**`model` 已于 ⑲ 移除**） |
| ⑧ | `/auth/me` 上线顺序 | 按建议：**后端先行**，前端兼容 404 |
| ⑨ | 智能体共用一套人格/记忆 | **接受**（"同一大脑、不同模型与参数"，非不同角色） |
| ⑩ | `ai-gateway` 骨架是否继承私人内容 | **清掉**，改写为干净的"网关智能体"版 |
| ⑪ | 一期工具开关 | **全开**（27 个，保证全能）；页面加显眼提示 |
| ⑫ | 智能体数量上限 | **软上限 5 个** |
| ⑬ | 能力栏数据来源 | **不跨进程读 QwenPaw 配置**；静态说明 + 跳转链接 |
| ⑭ | 引擎调模型的 Key 与计量归属 | 新建专用 Key **`agent-engine`**；记引擎桶但打 `source=qwenpaw` + `agent_slug` + 用户账号 |
| ⑮ | 引擎断线表现 | 红条 + 对话区禁用 + 保留已收内容；**不自动续接**（二期 `reconnect`） |
| ⑯ | `agents.yaml` 是否入版本库 | **入库**（无秘密；但以后描述别写敏感内容） |
| ⑰ | 归档智能体的历史数据 | **保留**；列表可筛「已归档」并支持恢复 |
| ⑱ | 配置页「试跑」按钮 | **加**（Step 3 +0.3h） |
| ⑲ | **模型归属（实测修正）** | **甲案**：所有智能体**共用同一个模型**（因模型是 profile 级 + 共用一个 profile）；配置页模型栏改**只读 + 指路 QwenPaw** |
| ⑳ | ~~`params` 白名单~~ | ~~只 temperature + max_tokens~~ → **已被 ㉗ 取代**（扩为 4 键） |
| ㉑ | 会话列表实现 | **复用 `history/{kind}`**（`kind = agent_<slug>`），不新开端点 |
| ㉒ | `features` 与前端常量关系 | `features` 是**唯一权威**；前端常量只作"后端无该字段时"的兜底（默认 `false`） |
| ㉓ | Step -1 技术验证 | **加**（0.5h，动手前做） |
| ㉔ | §4 标题 | 改为「决策详情与取舍说明」 |
| ㉕ | 接口字段表 | 补：列表项 / 详情项分别返回什么 |
| ㉖ | **智能体个性方案** | **A + C**：接受"智能体=参数+历史预设"（A）；**并用 `system_suffix` 补个性**（C） |
| ㉗ | `params` 白名单 | 扩为 **4 键**：`temperature` / `max_tokens` / `top_p` / **`system_suffix`** |

### ⑤ 带来的直接后果（老板已接受，见 §4 已定-A）

只建一个 profile + 决策 ②（提示词归 QwenPaw）⇒ **所有智能体共用同一份 `SOUL.md` / `AGENTS.md`**
⇒ **智能体之间"人设"无法从提示词层区分**，只能靠：模型 + 参数 + 各自独立的会话历史。

这不是缺陷，是"省资源"的代价。一期可以接受（你自己用，主要想试不同模型/参数/工作流）；
**若哪天需要"每个智能体真的是不同人设"，就得回到"一智能体一 profile"** —— 届时要重估内存。

### ⑲ 的展开：模型共用的实测依据（★ 方案级修正，2026-09-29 实测）

**原设计错误**：设计稿假设"每个智能体选自己的模型"。**实测证明做不到**：

| 实测 | 结果 |
|---|---|
| 打 `/api/console/chat` 传 `model: "agnes-3.0-flash"` | ❌ **被忽略** —— SSE 末尾 `turn_usage` 明文回 `provider_id=opencode, model_name=deepseek-v4.1-flash` |
| 源码 `schemas.py:247` `AgentRequest` | 只有 `input / session_id / user_id / stream / metadata` —— **没有 model 字段** |
| 模型怎么设？ | `PUT /api/providers/active`（body 含 `scope:"agent"` + `agent_id`）→ **profile 级**设置 |

**推论**：模型是 profile 级的，而所有智能体**共用 `ai-gateway` 一个 profile**（决策④⑤）⇒ **所有智能体必然用同一个模型**。

**甲案落地（老板已选）**：

| 项 | 做法 |
|---|---|
| 配置页「模型」栏 | **只读展示**当前 `ai-gateway` 用的模型 + 「去 QwenPaw 修改」链接 |
| 数据源 | `GET /api/providers/active?scope=agent&agent_id=ai-gateway`（读） |
| `agents.yaml` 的 `model` 字段 | **移除**（不存在"每个智能体的模型"这回事） |
| 智能体之间靠什么区分 | 名称 / 描述 / **参数（4 键，含 `system_suffix` 角色后缀）** / 会话历史 / 访问范围 |

**升级路径**：哪天真要"不同智能体不同模型" → 给那个智能体**单独建 profile**（届时评估内存）。
**明确不做**：网关侧切 profile 模型（原方案乙）—— 有并发覆盖风险，一期不做。

### ㉖ 的展开：A + C —— 智能体靠什么有个性（2026-09-29 定）

甲案（⑲）把模型也统一了 ⇒ 智能体之间只剩"参数 + 会话历史"。为让"智能体"这个概念真正有用，
采用 **A + C**：

- **A（接受现状）**：智能体就是"同一套引擎 + 不同参数 + 各自独立的会话历史"的**预设**。
- **C（补个性）**：新增 `params.system_suffix` —— **网关在转发给引擎时，把它追加到会话的指令里**。

#### `system_suffix` 怎么落地（关键设计）

因为 `AgentRequest` **没有 system 字段**（只有 `input/session_id/...`），所以注入方式是：

| 方案 | 做法 | 评价 |
|---|---|---|
| ① 作为首条 user 消息前缀 | 把后缀拼进第一条消息文本 | 简单，但会污染对话记录、用户看得到 |
| ② 放进 `metadata` | `metadata: {system_suffix: "..."}` | 依赖引擎认不认，未知 |
| ③ **节点：AgentRequest 加 system 字段** | 给 QwenPaw 的 schema 加字段 | **改别人的代码，不推荐** |
| ④ ★ **网关侧每轮注入一条轻量指令** | 在 `input` 里插入一条极短的 `system` 角色消息 | 需实测 QwenPaw 是否接受 system 角色 |

→ **Step -1 必须验证这一条**（见下）：**QwenPaw 的 AgentRequest 接不接受 `role: "system"` 的消息**。
  - 接受 → 用方案 ④（最干净）
  - 不接受 → 退到方案 ①（前缀拼进首条消息，UI 上明确标注"这是智能体的角色设定"）

**`system_suffix` 与决策②的关系（重要）**：
- 决策② 说的是"**智能体的主提示词**归 QwenPaw 管"（`SOUL.md` 那种，一大段）
- `system_suffix` 是**一小条补充说明**（如"回答只给结论，不要解释过程"），属于**智能体级微调**
- **不冲突**：主提示词在 QwenPaw，微调在网关。但 UI 要写清楚区别，避免两边打架。

#### 一期不做（明确）

- ❌ 完整的 per-agent system prompt（那就是建 profile 了）
- ❌ 智能体级工具开关（profile 级）
- ❌ 智能体级记忆

### ② 的展开：网关侧怎么表达"提示词在 QwenPaw"

- `agents.yaml` **不存** `system_prompt` 字段（避免两套真相）。
- 配置页「提示词」一栏改成**只读展示 + 指引**：
  - 显示 QwenPaw 侧该 profile 当前生效的提示词文件路径（`SOUL.md` / `AGENTS.md`）
  - 提供一个「打开 QwenPaw 设置」链接（`http://127.0.0.1:8088`）
  - 文案说明：**要改提示词请去 QwenPaw**，网关这边不接管
- 多智能体共用一份提示词时，页面要**显式提示**这一点，别让用户以为改了 A 不影响 B。

### ③ 的展开：会话归属

- 网关 `data/agent_sessions.json` 是**唯一会话列表**（列表 / 搜索 / 导出 / 重命名都在网关）。
- 每次对话把网关的会话 id 映射成 QwenPaw 的 `session_id` 传过去，让它保持上下文。
- **不在网关保存完整历史正文的副本**（QwenPaw 侧自己也有）；网关只存**索引级**信息（id / 标题 / 时间 / 轮次 / tokens / 归属账号 / engine session）→ 省空间、避免两份正文不一致。

---

## 1. 目标与范围

**一句话**：在网关加一个「智能体」菜单（仅管理员），能创建/管理若干智能体（**共用** `ai-gateway` 这一个引擎 profile，也共用同一个模型），在页面里直接对话，工具执行过程可视化。

### 一期做什么（P0）

- ✅ 新建 QwenPaw profile `ai-gateway`
- ✅ `data/agents.yaml`：智能体定义（名称 / slug / 描述 / **参数** / 引擎 profile / 访问范围）
- ✅ 后端 7 个端点（含新增 `GET /auth/me` 灰度通道）
- ✅ 前端「智能体」页（列表 + 对话 + 配置 + 运行记录）
- ✅ 工具执行轨迹可视化（设计稿的"执行了 3 步"）
- ✅ 计量：agent 维度 + `source=qwenpaw` 打标 + **引擎模型防自指**（校验 `ai-gateway` 的 active_model 不指向 `agent/*`）
- ✅ 灰度：`features.agents` 后端下发，一期只对 admin 开

### 一期不做（明确排除）

- ❌ 图片/附件（Agent API 的图片支持要实测，二期）
- ❌ 工具逐个勾选开关（一期继承 profile 的全局设置，页面只显示**实际开关状态**）
- ❌ 给普通用户下发（结构预留，功能二期）
- ❌ 断线自动重连（`reconnect=true`，二期）
- ❌ 在网关里编辑提示词（决策 ② 已定：归 QwenPaw）
- ❌ 多引擎（就是 QwenPaw 一套）

---

## 2. 开发步骤

### Step -1 · 技术验证 ✅ **已完成**（2026-09-29）

| # | 验证项 | 结论 |
|---|---|---|
| 1 | 请求级指定模型是否可行 | ❌ **不可行** → 方案已按甲案修正（见 ⑲） |
| 2 | `PUT/GET /api/providers/active` 的 scope | ✅ 支持 `scope=agent` + `agent_id` |
| 3 | **SSE 事件谱 + 夹具** | ✅ **已录制并落盘** → 见下 |
| 4 | `X-Agent-Id` 选 profile | ✅ 生效（实测带 `X-Agent-Id: default` 走 default 的会话/记忆） |
| 4b | **`input` 接不接受 `role:"system"`** | ✅ **接受且真的生效** → `system_suffix` 用方案 ④（注入 system 消息） |
| 5 | `ai-gateway` 的 active_model 指向 | ✅ 已确认为 `wangjinding` / `wjd`（引擎调用过网关，计量闭环成立） |

**产出物**：
- `tests/fixtures/sse-tool-flow.sse` —— 真实含工具调用的 SSE（14,900 字节 / 57 事件）
- `docs/qwenpaw-agent-api-sse.md` —— **完整事件谱 + 适配器映射表**（Step 2 实现依据）

**事件谱要点（实测）**：`text`(36，正文增量，`delta:true` 累积 / `delta:false` 是快照要忽略) ·
`plugin_call`(工具调用，名在 `content[0].data.name`、参数是 JSON 字符串) ·
`plugin_call_output`(结果，在 `data.output`，用 `call_id` 配对) · `data`(分片，**忽略**) ·
`response`(终态，`status: completed/failed/cancelled`) · `turn_usage`(**记 token/成本 的直接来源**，
带 `model_name` 与缓存字段)。
**顺序**：文本 → 工具调用 → 工具结果 → 更多文本 → … → `response` → `turn_usage`（同一响应内穿插）。

**另一条实测**：`GET /healthz` **只回 `{"status":"ok"}` 不含版本** —— 验版本要读 `GET /` 的 `version`。

### Step 0 · 建 QwenPaw profile `ai-gateway` ✅ **已完成**（2026-09-29）

```
0. 备份：cp /root/.qwenpaw/config.json → config.json.bak-<日期>（记回滚方式）
1. 复制骨架：default 的 workspaces → workspaces/ai-gateway
2. 清私人内容：该 profile 的 SOUL.md / AGENTS.md 改写为"网关智能体"版本
   （不继承"小银"身份、老板的私人上下文、项目记忆）—— 见 §4 已定-B
3. config.json 的 agents.profiles 加一项：
     {id: "ai-gateway", workspace_dir: "/root/.qwenpaw/workspaces/ai-gateway", enabled: true}
4. 工具开关：一期全开（与 default 一致，保证"全能"）；二期下发前收窄
5. 权限：只监听 127.0.0.1（现状已是）；确认 allow_no_auth_hosts 含 127.0.0.1/::1
6. 验证：curl 带 X-Agent-Id: ai-gateway 打 /api/console/chat 能收到流式
7. 回归：确认 default 与我当前会话**不受影响**（老板口径：新建不影响 default）
8. ★ 建引擎专用 API Key `agent-engine`（网页里建，供 ai-gateway profile 调模型用）
   · 写进 QwenPaw 的 provider 配置或环境；同时打 source=qwenpaw 计量标
   · Key 明文只落在需要它的地方（QwenPaw 侧 secret 目录 / 网关 data/.env），不入库
```

**验收**：`X-Agent-Id: ai-gateway` 跑通一轮对话；`default` 行为与之前一致。

**实际完成情况（实测）**：

| 项 | 结果 |
|---|---|
| 备份 | `config.json.bak-20260929-agents` |
| 工作区 | `/root/.qwenpaw/workspaces/ai-gateway/`（**全新空白，未复制 default 的任何私人文件**） |
| 提示词 | 新写 `SOUL.md` + `AGENTS.md`（网关智能体版）；`system_prompt_files: ["AGENTS.md","SOUL.md"]`（**不含 PROFILE.md**） |
| 注册 | `config.json` 的 `agents.profiles` + `agent_order` 均已加 `ai-gateway` |
| 工具 | ✅ **全开 27 个**（决策⑪）；`approval_level: OFF` |
| 模型 | `active_model = {provider_id: "wangjinding", model: "wjd"}` |
| 引擎 Key | 新建 `agent-engine`（label），明文暂存 `/tmp/agent_engine_key`(600)，待写入 QwenPaw 配置 |
| **profile 可用性** | ✅ 实测 `X-Agent-Id: ai-gateway` 跑通，"网关就绪"正常返回 |
| **模型指向** | ✅ `turn_usage` 明证 `provider_id: wangjinding / model_name: wjd` → **引擎过网关，计量闭环** |
| **隔离验证** | ✅ 问"你是谁/认识老板吗"，答"没有个人身份、没有个人记忆…没有任何关于「老板」的信息"，并实际检查了工作区记忆文件 |
| `default` 是否受影响 | ✅ 无影响（新建 profile 不动已有 profile） |
**注意**：改 `config.json` 会触发配置重载。实测口径是"新增 profile 不影响已有 profile"，
但**仍要先备份**；若发现 `default` 会话中断，立即回滚。

**新增一个语义确认点**：`ai-gateway` 这个 profile 是**网关的公共引擎**（所有智能体共用），
不是"某一个智能体"。命名上要避免以后误以为它对应某个具体智能体。

### Step 1 · 数据层（约 2h）

```
新增 app/agents.py
  ├─ AgentStore：data/agents.yaml 读写（600 / 原子写 / mtime 热载 ≤15s / 备份）
  ├─ 校验（抛 ConfigError，不静默）：
  │    · slug：小写连字符、唯一、不撞模型 id、不撞别名、不撞 profile id
  │    · engine.type 必须是 qwenpaw；engine.agent_id 必须存在
  │    · params 键必须在白名单内（temperature / max_tokens / top_p / system_suffix）否则报错（㉗）
  │    · system_suffix 长度上限（如 2000 字符）—— 防止塞一整段提示词绕过决策②
  │    · access.roles ⊆ {admin, member}
  │    · ★ 数量上限：未归档的智能体 ≤ max_agents（默认 5）→ 超了抛 ConfigError
  └─ 默认值：access.roles=["admin"] / status="published"
新增 data/agents.yaml（初始 1 个示例）
```

`agents.yaml` 字段表：

| 字段 | 类型 | 一期可改 | 说明 |
|---|---|---|---|
| `slug` | str | 新建时可填 | URL/接口用；唯一 |
| `display_name` | str | ✅ | 页面显示名 |
| `description` | str | ✅ | 一句话说明 |
| ~~`model`~~ | — | — | **已移除**（见 ⑲：模型共用一个，不在智能体上配） |
| `engine.type` | str | ❌ | 固定 `qwenpaw` |
| `engine.base_url` | str | ❌ | `http://127.0.0.1:8088` |
| `engine.agent_id` | str | ❌ | QwenPaw profile id（如 `ai-gateway`） |
| `access.roles` | list | ❌（二期开） | 一期固定 `["admin"]` |
| `access.users` | list | ❌（二期开） | 灰度点名，一期空 |
| `status` | enum | ✅ | `draft` / `published` / `retired` |
| `params` | obj | ✅ | **白名单 4 键**：`temperature` / `max_tokens` / `top_p` / `system_suffix`；其他键报 `ConfigError`（㉗） |
| `created_at` / `updated_at` | str | 自动 | 审计 |

顶层配置（不属于单个智能体）：

| 键 | 默认 | 说明 |
|---|---|---|
| `max_agents` | `5` | 未归档智能体数量上限（已定-D） |

### Step 2 · 后端端点 + 引擎适配器（约 4h）

**`GET /agents` 返回字段（㉕）**：

| 层级 | 字段 |
|---|---|
| 列表项 | `slug` / `display_name` / `description` / `status` / `engine_type` / `access_scope`（`admin` / `users`）/ `session_count` / `last_used_at` |
| 详情项 | 列表项全部 + `params` / `engine`（`base_url` / `agent_id`）/ `access`（`roles` / `users`）/ `created_at` / `updated_at` |
| 引擎实况（详情额外） | `engine_model`（读 `GET /api/providers/active?scope=agent&agent_id=...` 拿当前模型）+ `engine_ready`（探活结果） |
| member 裁剪 | 只有 `slug` / `display_name` / `description` / `status`（⑦；无 model —— 已 ⑲ 移除） |

```
GET    /agents                   列表（admin 全量；member 只回分派的角色）+ 裁剪字段：
                                 member 只见 display_name/description/status；
                                 不见 engine.base_url、access.users、运行记录（⑦ 已定）
POST   /agents                   新建        （_require_admin；超 max_agents 返 400）
PUT    /agents/{slug}            改（名称/描述/模型/params/status 可改）
DELETE /agents/{slug}            归档 retired（不物理删）
POST   /agents/{slug}/chat       对话 → 转发 /api/console/chat（SSE）
POST   /agents/{slug}/stop       停止 → /api/console/chat/stop
GET    /agents/{slug}/runs       运行记录
★ 会话列表：**复用现有 `GET|POST|DELETE /history/{kind}`**（kind = `agent_<slug>`）
   不新开端点 —— 列表/新建/重命名/删除全用现成的那套（㉑）
GET    /auth/me                  ★ 回 features{agents:{enabled, canManage, visible}}
```

**引擎适配器**（`app/agents.py` 或 `app/adapters/qwenpaw_engine.py`）四件事：

| # | 事项 | 说明 |
|---|---|---|
| 1 | 请求形状 | OpenAI messages → `AgentRequest{input:[Message], session_id, stream}`；带 `X-Agent-Id` |
| 2 | **SSE 事件映射** | `MESSAGE`→文本增量 / `PLUGIN_CALL`+`OUTPUT`→轨迹步骤 / `REASONING`→思考 / `PROGRESS`→进度 / `RESULT`→结束 / `RunStatus.failed`→错误 |
| 3 | session_id 映射 | 网关会话 ↔ QwenPaw `session_id` |
| 4 | 错误兜底 | 引擎不可达 → 502 + 明确文案（不静默） |
| 5 | **`system_suffix` 注入** | 按 Step -1 第 4b 项的结论，把智能体的角色后缀注入请求（㉖） |

**引擎断线表现（已定 ⑮）**：

| 场景 | 表现 |
|---|---|
| 引擎不可达（QwenPaw 没跑） | 对话区禁用 + 顶部红条「引擎不可达（QwenPaw 未运行）」+ 重试按钮 |
| 流式中引擎中断 | 末尾追加「⚠ 引擎连接中断，本轮未完成」+ **保留已收到的内容** |
| 引擎重启后 | 用户重新发消息即可（**不自动续接**，二期做 `reconnect=true`） |

> ⚠️ **实施顺序**：**先做只映射文本的最小闭环**（跑通端到端），**再加轨迹**。SSE 事件是最不确定的部分，别一上来就啃它。

### Step 3 · 前端（约 5h）

```
views/Agents.vue            列表 + 详情三 Tab（对话/配置/运行记录）
components/AgentTrace.vue   ★ 执行轨迹（"执行了 3 步"）
lib/agents.ts               接口封装 + SSE 解析
router.ts                   /agents  meta.admin
lib/nav.ts                  核心组第 5 位 + icon: bot
components/Icon.vue         加 bot 图标
locales/zh.ts + en.ts       agents.* 键，zh/en 对称
```

**实现细节（沿用设计稿）**：
- **模型栏（⑲ 甲案）**：**只读展示** `ai-gateway` 当前模型 + 「去 QwenPaw 修改」链接；
  **不做模型下拉**（原设计稿的下拉删掉）
- 配置页：名称/描述/模型/params 可改；**引擎/工具/访问范围只读**并给原因；**提示词只读 + 指路 QwenPaw**（决策 ②）
- 对话页：左会话列表 + 右消息区 + 底部输入（流式中"停止"替换"发送"）
- 手机端：列表全宽 + 滑入详情
- **数量上限**：达 5 个时「新建」禁用 + 提示（已定-D）
- **共用说明**：新建弹窗与配置页显式说明"提示词与记忆共用"（已定-A）
- **参数表单（㉗）**：temperature / max_tokens / top_p 三个数值 + **system_suffix 多行输入**
  （标注"这段是智能体的角色补充，与 QwenPaw 的主提示词叠加生效"）
- **风险提示**：对话页顶部常驻"可在服务器上执行操作"（已定-C）
- **features 权威（㉒）**：`features.agents` 后端下发说了算；前端常量仅"后端没这字段"时兜底（默认 false）
- **能力栏**：不列 27 项明细，改静态说明 + 「去 QwenPaw 查看」链接（已定 ⑬）
- **试跑按钮**：配置页改完模型/params 后点一下，发 1-token 请求验证连通，就地显示结果（已定 ⑱）
- **归档管理**：列表可筛「显示已归档」，归档项可「恢复」（已定 ⑰）

### Step 4 · 计量与审计（约 1.5h）

```
metrics.py       agent 维度（按智能体统计）
usage 打标        source=qwenpaw（区分"个人使用"与"网关 agent 使用"）
                  引擎调模型统一用 `agent-engine` Key → 记引擎桶，
                  但每条带 agent_slug + 调用者账号（明细靠网关自身记录，不依赖引擎）
data/agent_runs.json   每轮一条：时间/账号/智能体/轮次/工具数/tokens/耗时/状态
防自指（⑲ 后风险已转移，见下方说明）：
                 ★ 现在没有 per-agent model 了 → 真正的风险变成：
                   **`ai-gateway` profile 的 active_model 不能指向 `agent/*`**
                   （否则引擎调模型 → 打到智能体 → 又调引擎 → 死循环）
                 ★ 校验点：Step -1 第 5 项确认 active_model 指向正常模型（如 wjd）
                 ★ 递归深度上限：同一请求内引擎调用 ≤ 3 层（超限 400 并写明原因）
                 ★ 保留别名链解析：若 active_model 是别名，解析目标后校验不是 agent/*
```

### Step 5 · 测试与上线（约 2.5h）

```
tests/test_gateway.py   +12~15 条
   · agents.yaml 解析与校验（含 slug 查重、params 白名单、数量上限、非法枚举）
   · 引擎模型防自指（active_model 指向 agent/* 时应被拦）
   · 权限矩阵（admin 全量 / member 只读分派 / 无凭证 401）
   · SSE 事件映射（用录制的真实事件做夹具）
   · features 下发（一期 member 不可见）
   · 归档后不出现在列表
scripts/ci_check.sh     跑绿
回归 209 → 约 222 条全过
★ 上线顺序（⑧ 已定）：**后端先行** —— 先发后端（含 GET /auth/me 与 features 下发），
   再 build 前端。前端 store/session.ts 要兼容"后端没有 /auth/me"（404 → 按 enabled:false）
上线：restart → 真机验证 → commit → 推 Gitea（项目硬性规矩）
文档：README + CHANGELOG 各记一轮
```

---

## 3. 总量

| 项 | 估量 |
|---|---|
| Step -1（技术验证） | 0.5h（4 项已做 2 项） |
| Step 0（profile） | 1h |
| Step 1（数据层） | 2h |
| Step 2（端点+适配器） | 4.3h（含 system_suffix 注入） |
| Step 3（前端） | 5h（含试跑 0.3h；模型栏改只读 -0.5h，净 5h） |
| Step 4（计量） | 1.5h |
| Step 5（测试上线） | 2.5h |
| **合计** | **约 17.5h ≈ 2.5 个工作日** |

（v1.0 是 11~14h；v1.2 定"只建 1 个 profile + 一对多"，估量维持 16h —— 省下的是 profile 数量，
但多了骨架清理、Step -1 技术验证、接口字段表、member 裁剪四处细节。）

---

## 4. 决策详情与取舍说明

### 已定-A ✅ 所有智能体共用一套"人格"（老板已接受）：明确这是"同一大脑、不同入口"，非不同角色

决策 ④⑤ 已定"只建 `ai-gateway` 一个 profile"，因此：

| 智能体之间能区分的 | 不能区分的 |
|---|---|
| ✅ 绑定的**模型**（各选各的） | ❌ 系统提示词（共用 `SOUL.md`） |
| ✅ **参数**（temperature / max_tokens / top_p / `system_suffix`） | ❌ 记忆文件（共用一份 `MEMORY.md`） |
| ✅ **会话历史**（各自独立） | ❌ 工具开关（共用 profile 级设置） |
| ✅ 名称 / 描述 / 访问范围 | ❌ 人格 / 语气 / 专长 |

**建议的一期用法**：把智能体理解为"**同一双手、不同工具配置的入口**"——
比如「代码审查」= 同一个引擎 + `qwen3.8-flash` + 低温参数；「周报助手」= 同一个引擎 + `agnes-3.0-flash`。
**别指望它们像两个人**。

**升级路径（记着，别忘）**：真要独立人格时 → 给那个智能体单独建 profile（`agents.yaml` 的 `engine.agent_id` 换一个新的），
届时评估内存（当前 2C/3.6G，QwenPaw 已占 852MB）。

**老板已接受。** 由此要求 UI **必须显式说明**（否则以后会困惑"为什么两个智能体像一个人"）：

| 位置 | 提示文案（示意） |
|---|---|
| 新建智能体弹窗顶部 | ⓘ 智能体共用同一个引擎（`ai-gateway`），**模型与参数各自独立，但提示词与记忆共用** |
| 配置页「引擎」区 | 当前引擎 `ai-gateway`（所有智能体共用）· 提示词与记忆请到 QwenPaw 修改 |
| 智能体数量达上限时 | 已达上限 5 个（软上限）；需要更多时先归档不用的 |

### 已定-B ✅ 清掉私人内容（老板已定）：Step 0 第 2 步执行

**已定：清掉私人内容。** `ai-gateway` 的 `SOUL.md` / `AGENTS.md` 要**新写**一份"网关智能体"版本，**不继承**：

| 必须清掉 | 说明 |
|---|---|
| "小银"身份 / 老板称呼 / 私人作息 | 它将来要给普通用户用，不能暴露你的个人信息 |
| 项目记忆 / `MEMORY.md` 里的私人上下文 | 同上 |
| `AGENTS.md` 里的"工具调用自律"等会话规则 | 那是为**我**（default）写的，与网关智能体无关 |

**保留**：通用的"如何工作"（不乱删文件、破坏性操作先问、`trash` > `rm` 等安全约定）—— 这些对任何 agent 都适用。

**验收**：向 `ai-gateway` 问"你是谁" / "老板是谁"，**不应**答出我的身份或你的私人信息。

### 已定-C ✅ 一期工具全开（老板已定）：页面必须给风险提示

**已定：一期全开**（27 个工具 + 审批 `OFF` + 沙箱关）→ 引擎能读写文件、跑命令、开浏览器。
这是"全能"的来源，也是最大的风险面。**落地要求：**

| 要求 | 位置 |
|---|---|
| 对话页顶部常驻风险提示 | 「该智能体可在服务器上读写文件、执行命令」 |
| 配置页「能力」区显示实际开关与状态 | 27 项工具的启用情况（只读） |
| README / MEMORY 记录此风险 | 避免以后忘记 |
| **二期下发普通用户前必须收窄** | 已列进风险登记（硬前置） |

### 已定-D ✅ 智能体数量软上限 **5 个**（老板已定）

- 达到 5 个时，「新建」按钮禁用 + 提示「已达上限，请先归档不用的智能体」
- 已归档（`retired`）**不计入**上限
- 上限值写进配置（`agents.yaml` 顶层 `max_agents: 5`），以后改一行即可

### 已定（无需再确认）

| 项 | 结论 |
|---|---|
| 会话是否存正文副本 | **只存索引**（正文在 QwenPaw 侧） |
| 智能体可选模型范围 | **只允许文本模型**（图片/视频模型不在 agent 里用） |
| 智能体数量上限 | 软上限 **5**（`agents.yaml` 的 `max_agents: 5`，归档不计入） |
| Step 0 何时做 | 老板答复：新建 `ai-gateway` 不影响他在用的 `default` → **可直接做**（但改 `config.json` 仍需备份+记回滚） |
| 删除语义 | 归档 `retired`，不物理删 |
| 版本号 | `2.4.0` → `2.5.0` |
| 能力栏数据来源 | 静态说明 + 跳转 QwenPaw（不跨进程读配置） |
| 引擎专用 Key | `agent-engine`（新建，落 `data/.env`，不入库） |
| `agents.yaml` | **入版本库**（配置无秘密） |
| 归档智能体历史 | 保留 + 可恢复 |
| 「试跑」按钮 | 一期加 |

---

## 5. 风险登记

| 风险 | 影响 | 对策 |
|---|---|---|
| SSE 事件映射复杂 | Step 2 可能超时 | 先做文本最小闭环，轨迹后加 |
| QwenPaw 重启断在途对话 | 用户体验 | 一期提示"引擎重连中"；二期 `reconnect=true` |
| 网关 ↔ 引擎循环调用 | 死循环烧钱 | 校验 `ai-gateway` 的 `active_model` 不指向 `agent/*`（Step -1 第 5 项确认）+ 递归上限 3 层 |
| 内存（2C/3.6G） | 多 profile 撑爆 | 一期 1 个 profile；二期评估升配 |
| **改 QwenPaw `config.json`** | 影响你正在用的会话 | 改前备份；记回滚方式；实测口径=新增 profile 不影响 default |
| 引擎权限暴露 | 安全 | 仅 127.0.0.1；一期只 admin；下发前必须收窄工具+提审批等级 |
| `agents.yaml` 入库后描述含敏感内容 | 泄密 | 提交前扫一遍描述；敏感内容写进 `data/.env` 而非 yaml |
| 「试跑」被滥点消耗额度 | 费用 | 试跑限制频率（如 5s 冷却）+ 只发 1-token |
| `system_suffix` 被当成"绕过决策②的提示词通道" | 架构漂移 | 限长 2000 字符 + UI 明确它只是"微调"；要长提示词请去建 profile |

---

## 6. 涉及文件清单

**后端**
- 新增 `app/agents.py`（存储 + 适配器）
- 改 `app/main.py`（7 个端点 + `_require_admin` 复用）
- 改 `app/config.py`（agents 相关新配置项，如 params 白名单、max_agents）
- 改 `app/metrics.py`（agent 维度）
- 新增 `data/agents.yaml`（**入库**，需确认 `.gitignore` 没拦它）、`data/agent_sessions.json`、`data/agent_runs.json`（后两个**不入库**，需加进 `.gitignore`）
- 改 `data/.env`（新增 `AGENT_ENGINE_KEY`，不入库）

**前端**
- 新增 `views/Agents.vue`、`components/AgentTrace.vue`、`lib/agents.ts`
- 改 `router.ts`、`lib/nav.ts`、`components/Icon.vue`、`locales/zh.ts`、`locales/en.ts`

**QwenPaw 侧**
- 改 `/root/.qwenpaw/config.json`（profiles 加一项）
- 新增 `/root/.qwenpaw/workspaces/ai-gateway/`（骨架 + SOUL.md / AGENTS.md）

**文档**
- 改 `README.md`、`CHANGELOG.md`
- 改 `MEMORY.md`（本项目记忆）
